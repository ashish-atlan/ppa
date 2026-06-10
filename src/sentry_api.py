"""agent-sentry HTTP API (FastAPI).

Exposes the same engine as the CLI for the (later-phase) tabbed UI:
  - GET  /skills   -> one entry per skill; the UI renders one tab per skill.
  - POST /run      -> run the tab's skill; the response `content` is the briefing markdown
                      the UI renders in the right-hand pane.
  - POST /feedback -> record the user's 👍/👎 + comment on a briefing; the feedback-learner
                      skill distills it into durable preferences so future runs improve.

Security (CLAUDE.md "New API endpoint" — auth + authz + validation + rate limit):
  - X-API-Key header checked against SENTRY_API_KEY from .env (401 on miss).
  - skill must be in the discovered registry (allowlist; 404 on unknown).
  - prompt length + timeframe range validated; errors are generic to the client.
  - per-key fixed-window rate limit (429 + Retry-After).
  - relay destinations come only from skill frontmatter, never the request body.
  - bind 127.0.0.1 by default (local UI). A public deployment needs a real auth review
    (#bu-security-and-it) before exposure.

Run: uvicorn src.sentry_api:app --host 127.0.0.1 --port 8787
"""

import os
import sys
import time
import asyncio
import importlib
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

sentry_core = importlib.import_module("sentry_core")

# --- config (from .env, loaded by sentry_core) ---------------------------------------
API_KEY = os.environ.get("SENTRY_API_KEY", "").strip()
MAX_PROMPT_CHARS = 4000
MAX_TIMEFRAME_HOURS = 24 * 30  # 30 days
RATE_LIMIT_MAX = int(os.environ.get("SENTRY_RATE_LIMIT_PER_MIN", "10"))
RATE_LIMIT_WINDOW_S = 60
# The static frontend (frontend/) calls this API cross-origin. Allowlist exactly that one
# origin — never '*' (CLAUDE.md). Auth is via the X-API-Key header, not cookies, so
# credentials stay off and there's no CSRF surface.
UI_ORIGIN = os.environ.get("SENTRY_UI_ORIGIN", "http://127.0.0.1:8080").strip()


MAX_COMMENT_CHARS = 2000

# Skills that do NOT run under agent-sentry's restricted toolset (graphiti + slack + email).
# They live-fetch (Glean / Linear / web) and ship as their own standalone harness entrypoint,
# so the API dispatches them OUT-OF-PROCESS (subprocess) rather than through
# sentry_core.run_briefing. The API process itself gains no extra tools — it only spawns the
# separate entrypoint, which carries its own allowed_tools. Map: skill name -> repo-relative
# harness script. Keeps the UI's one-tab-per-skill model intact while honouring the
# "daily-pulse is standalone" decision.
_EXTERNAL_HARNESS = {
    "daily-pulse": "src/daily-pulse.py",
}
# Live harness runs (Glean + several web fetches) are slow; cap the wait generously.
_HARNESS_TIMEOUT_S = int(os.environ.get("SENTRY_HARNESS_TIMEOUT_S", "900"))


class RunRequest(BaseModel):
    skill: str = Field(..., min_length=1, max_length=200)
    prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    timeframe_hours: int | None = Field(default=None, ge=1, le=MAX_TIMEFRAME_HOURS)
    # The UI exposes this as a "Dry run" checkbox: write the briefing but skip relay
    # (Slack/email) + Graphiti, so a skill can be tested without real side effects.
    dry_run: bool = False


class FeedbackRequest(BaseModel):
    # The skill the feedback is about (allowlisted against the registry in the handler).
    skill: str = Field(..., min_length=1, max_length=200)
    # The briefing the user reacted to. Path-traversal + existence are enforced server-side in
    # sentry_core.record_feedback (must resolve under briefings/); we just bound the length here.
    briefing_path: str = Field(..., min_length=1, max_length=300)
    # Sentiment signal. The distilled rule comes from the comment; the rating drives reinforce
    # (up) vs learn/log (down).
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)
    dry_run: bool = False


# --- simple in-memory per-key fixed-window rate limiter ------------------------------
_hits: dict[str, list[float]] = {}


def _rate_limited(key: str) -> int | None:
    """Return seconds-to-retry if over the limit, else None (and record the hit)."""
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW_S
    bucket = [t for t in _hits.get(key, []) if t > window_start]
    if len(bucket) >= RATE_LIMIT_MAX:
        retry_after = int(RATE_LIMIT_WINDOW_S - (now - bucket[0])) + 1
        _hits[key] = bucket
        return max(retry_after, 1)
    bucket.append(now)
    _hits[key] = bucket
    return None


def _require_auth(x_api_key: str | None) -> None:
    # If SENTRY_API_KEY is unset the API refuses to serve rather than run wide open.
    if not API_KEY:
        raise HTTPException(status_code=503, detail="API not configured.")
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


async def _run_external_harness(skill: str, body: RunRequest) -> dict:
    """Run a standalone live-fetch skill (e.g. daily-pulse) out-of-process and return its
    newest briefing in the same shape as the /run response.

    The skill can't run through sentry_core.run_briefing (that toolset is graphiti + slack +
    email only); its own entrypoint carries the Glean/web/slack grants. We spawn it, then read
    back the briefing it wrote under briefings/.

    Security (CLAUDE.md — no untrusted execution): argv is a FIXED server-side script path plus
    only validated scalar flags (int timeframe, bool dry-run). No user-supplied string reaches
    argv, and `create_subprocess_exec` is used (no shell), so there is no injection surface.
    The free-form `prompt` is intentionally ignored (the harness takes no prompt)."""
    root = sentry_core.PROJECT_ROOT
    script = (root / _EXTERNAL_HARNESS[skill]).resolve()
    if not script.is_file():
        raise HTTPException(status_code=500, detail="Briefing run failed.")

    argv = [sys.executable, str(script)]
    if body.timeframe_hours:
        argv += ["--timeframe-hours", str(int(body.timeframe_hours))]
    if body.dry_run:  # UI "Dry run": write the briefing but skip the Slack DM
        argv.append("--dry-run")

    briefings = sentry_core.BRIEFINGS_DIR
    before = set(briefings.glob(f"{skill}-*.md")) if briefings.is_dir() else set()

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_HARNESS_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail="Briefing run timed out.")

    if proc.returncode != 0:
        # Full harness output stays in the server log only — never returned to the client.
        print(
            f"[{skill} harness failed rc={proc.returncode}]\n"
            f"{(out or b'').decode(errors='replace')}",
            file=sys.stderr,
        )
        raise HTTPException(status_code=500, detail="Briefing run failed.")

    # Pick the briefing this run produced (newest of the files that appeared during the run;
    # fall back to the newest overall if detection misses).
    after = set(briefings.glob(f"{skill}-*.md"))
    pool = (after - before) or after
    if not pool:
        raise HTTPException(status_code=500, detail="Briefing run failed.")
    latest = max(pool, key=lambda p: p.stat().st_mtime)

    raw = latest.read_text(encoding="utf-8")
    clean, violations = sentry_core._apply_hard_guardrails(raw)  # secret-mask + size cap
    if clean != raw:
        latest.write_text(clean, encoding="utf-8")
    return {
        "skill": skill,
        "briefing_path": str(latest.relative_to(root)),
        "content": clean,
        "relayed": [] if body.dry_run else ["slack"],  # the harness DMs the user via Slack
        "graphiti_locked": False,  # daily-pulse never writes Graphiti
        "violations": violations,
    }


def create_app() -> FastAPI:
    """App factory so tests can build the app without binding a port."""
    app = FastAPI(title="agent-sentry", version="0.1.0")

    # Narrow CORS allowlist for the static frontend (no wildcard). Only the methods/headers
    # the UI actually uses; credentials off (header-key auth, not cookies).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[UI_ORIGIN],
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type"],
        allow_credentials=False,
    )

    @app.get("/skills")
    def list_skills(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
        _require_auth(x_api_key)
        try:
            registry = sentry_core.discover_skills()
        except sentry_core.SentryError:
            # Don't leak filesystem internals to the client.
            raise HTTPException(status_code=500, detail="Skill registry unavailable.")
        return [meta.public() for meta in registry.values()]

    @app.post("/run")
    async def run(
        body: RunRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ):
        _require_auth(x_api_key)

        retry_after = _rate_limited(x_api_key)
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded."},
                headers={"Retry-After": str(retry_after)},
            )

        # Authz: skill must be a known tab (allowlist). 404 hides which skills exist.
        try:
            registry = sentry_core.discover_skills()
        except sentry_core.SentryError:
            raise HTTPException(status_code=500, detail="Skill registry unavailable.")
        if body.skill not in registry:
            raise HTTPException(status_code=404, detail="Unknown skill.")

        # Live-fetch skills run as their own standalone harness, not through agent-sentry's
        # restricted toolset. Dispatch them out-of-process and return the briefing they wrote.
        if body.skill in _EXTERNAL_HARNESS:
            return await _run_external_harness(body.skill, body)

        try:
            result = await sentry_core.run_briefing(
                prompt=body.prompt,
                skill=body.skill,
                timeframe_hours=body.timeframe_hours,
                dry_run=body.dry_run,  # UI "Dry run": write briefing, skip relay + Graphiti
                force_ui=True,  # the UI renders the returned content in the right pane
            )
        except sentry_core.SentryError:
            raise HTTPException(status_code=400, detail="Run configuration error.")
        except Exception:
            # Generic to the client; full error stays in server logs.
            raise HTTPException(status_code=500, detail="Briefing run failed.")

        return {
            "skill": result.skill,
            "briefing_path": result.briefing_path,
            "content": result.content,
            "relayed": result.relayed,
            "graphiti_locked": result.graphiti_locked,
            "violations": result.violations,
        }

    @app.post("/feedback")
    async def feedback(
        body: FeedbackRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ):
        _require_auth(x_api_key)

        retry_after = _rate_limited(x_api_key)
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded."},
                headers={"Retry-After": str(retry_after)},
            )

        # Authz: feedback can only target a known skill (allowlist). 404 hides which skills exist.
        try:
            registry = sentry_core.discover_skills()
        except sentry_core.SentryError:
            raise HTTPException(status_code=500, detail="Skill registry unavailable.")
        if body.skill not in registry:
            raise HTTPException(status_code=404, detail="Unknown skill.")

        try:
            result = await sentry_core.record_feedback(
                target_skill=body.skill,
                briefing_path=body.briefing_path,
                rating=body.rating,
                comment=body.comment,
                dry_run=body.dry_run,
            )
        except sentry_core.SentryError:
            # Bad rating / unknown skill / path outside briefings/ — client-correctable, generic.
            raise HTTPException(status_code=400, detail="Invalid feedback request.")
        except Exception:
            # Generic to the client; full error stays in server logs.
            raise HTTPException(status_code=500, detail="Feedback run failed.")

        # The feedback-learner's one-line SENTRY summary is surfaced as `summary` for the UI.
        return {"ok": True, "summary": result.content or "", "briefing_path": result.briefing_path}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("SENTRY_API_PORT", "8787")))
