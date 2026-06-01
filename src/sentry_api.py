"""agent-sentry HTTP API (FastAPI).

Exposes the same engine as the CLI for the (later-phase) tabbed UI:
  - GET  /skills  -> one entry per skill; the UI renders one tab per skill.
  - POST /run     -> run the tab's skill; the response `content` is the briefing markdown
                     the UI renders in the right-hand pane.

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
import time
import importlib

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

sentry_core = importlib.import_module("sentry_core")

# --- config (from .env, loaded by sentry_core) ---------------------------------------
API_KEY = os.environ.get("SENTRY_API_KEY", "").strip()
MAX_PROMPT_CHARS = 4000
MAX_TIMEFRAME_HOURS = 24 * 30  # 30 days
RATE_LIMIT_MAX = int(os.environ.get("SENTRY_RATE_LIMIT_PER_MIN", "10"))
RATE_LIMIT_WINDOW_S = 60


class RunRequest(BaseModel):
    skill: str = Field(..., min_length=1, max_length=200)
    prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    timeframe_hours: int | None = Field(default=None, ge=1, le=MAX_TIMEFRAME_HOURS)


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


def create_app() -> FastAPI:
    """App factory so tests can build the app without binding a port."""
    app = FastAPI(title="agent-sentry", version="0.1.0")

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

        try:
            result = await sentry_core.run_briefing(
                prompt=body.prompt,
                skill=body.skill,
                timeframe_hours=body.timeframe_hours,
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
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("SENTRY_API_PORT", "8787")))
