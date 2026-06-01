"""First-party Granola MCP server (stdio).

A thin wrapper over Granola's official public API (https://public-api.granola.ai/v1)
so the mem-writer digest pipeline can reach Granola meeting notes directly — Glean does
NOT index Granola for this user, so the granola source is routed here instead of Glean.

No third-party MCP package: just FastMCP (already a transitive dep via claude-agent-sdk)
and httpx (a declared project dep). The API key is read from GRANOLA_API_KEY in the
environment (the launcher loads it from the repo .env) and is never logged.

Tools exposed (prefix `mcp__granola__` once registered):
  - list_notes(created_after, cursor)   -> GET /notes
  - get_note(note_id, include_transcript) -> GET /notes/{id}

Run: `python src/granola_mcp_server.py` (stdio transport). See granola-launcher.sh.
"""

import os
import time

import httpx
from mcp.server.fastmcp import FastMCP

API_BASE = "https://public-api.granola.ai/v1"

# Fail fast: without a key every call would 401. Surface it at startup, not per-call.
API_KEY = os.environ.get("GRANOLA_API_KEY", "").strip()
if not API_KEY:
    raise SystemExit(
        "GRANOLA_API_KEY not set. Add it to the repo .env "
        "(Granola desktop app -> Settings -> Connectors -> API keys). Format: grn_..."
    )

# Single fixed host, no user-controlled URL (outbound-HTTP allowlist invariant).
# The key lives only in this header; never log the client or its headers.
_client = httpx.Client(
    base_url=API_BASE,
    headers={"Authorization": f"Bearer {API_KEY}"},
    timeout=30.0,
)

# Granola limits: 5 req/s sustained, 25 burst -> 429. Back off and retry transient
# failures (429 + 5xx) a few times with exponential delay.
_MAX_RETRIES = 4
_RETRY_STATUSES = {429, 500, 502, 503, 504}

mcp = FastMCP("granola")


def _get(path: str, params: dict | None = None) -> dict:
    """GET with bounded exponential backoff on rate-limit / transient errors."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = _client.get(path, params=params)
            if resp.status_code in _RETRY_STATUSES:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2.0 ** attempt
                time.sleep(min(delay, 30.0))
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            last_exc = exc
            time.sleep(2.0 ** attempt)
    raise RuntimeError(
        f"Granola API GET {path} failed after {_MAX_RETRIES} attempts: {last_exc}"
    )


@mcp.tool()
def list_notes(created_after: str | None = None, cursor: str | None = None) -> dict:
    """List the user's Granola meeting notes (most recent first).

    Args:
        created_after: ISO-8601 timestamp (e.g. 2026-05-31T00:00:00Z). Only notes
            created at/after this are returned. Omit for the default recent window.
        cursor: Opaque pagination cursor from a prior response's `cursor` field.
            Pass it back (with the same filters) to fetch the next page.

    Returns a dict: {notes: [{id, title, owner{name,email}, created_at, updated_at}],
    hasMore: bool, cursor: str}. Only notes with a generated AI summary + transcript
    are returned. Use get_note(id) for full content (summary, attendees, transcript).
    """
    return _get("/notes", {"created_after": created_after, "cursor": cursor})


@mcp.tool()
def get_note(note_id: str, include_transcript: bool = False) -> dict:
    """Get one Granola note by id, with full content.

    Args:
        note_id: The note id (e.g. `not_WDwrbaiVObXAF2`) from list_notes.
        include_transcript: When true, include the transcript utterances array.

    Returns the note: {id, title, web_url, owner{name,email}, created_at, updated_at,
    summary, calendar_event{...}, attendees:[{name,email}], folder_membership,
    transcript:[{text, ...}] (only if include_transcript)}.
    """
    params = {"include": "transcript"} if include_transcript else None
    return _get(f"/notes/{note_id}", params)


if __name__ == "__main__":
    mcp.run(transport="stdio")
