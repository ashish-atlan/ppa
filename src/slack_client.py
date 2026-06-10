"""Shared Slack Web API client (auth + backoff).

A thin wrapper over Slack's Web API (https://slack.com/api) used by BOTH the first-party Slack
MCP server (`src/slack_mcp_server.py`, outbound `chat.postMessage`) and the feedback poller
(`src/slack_feedback_poller.py`, inbound `reactions.get` / `conversations.replies`). Keeping the
token loading, the 429/5xx backoff, and the channel/email->DM resolution in one place means the
two callers can't drift.

The bot token is read from `SLACK_BOT_TOKEN` (format `xoxb-...`) and is NEVER logged. Scopes the
two callers need between them: `chat:write`, `users:read.email`, `im:write` (outbound) plus
`reactions:read`, `reactions:write`, `im:history` (inbound feedback + the processed-✓ ack).

Single fixed host, no user-controlled URL (outbound-HTTP allowlist invariant): every call targets
`https://slack.com/api/<method>`.
"""

import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load the repo .env so direct callers (the poller) get the token even when not launched via the
# MCP launcher (which loads it for the MCP server). Idempotent — harmless if already loaded.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_BASE = "https://slack.com/api"

# Fail fast: without a token every call would fail auth. Surface it at import time.
BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit(
        "SLACK_BOT_TOKEN not set. Add it to the repo .env (a bot token with the "
        "chat:write, users:read.email, im:write, reactions:read, reactions:write, "
        "im:history scopes). Format: xoxb-..."
    )

# The token lives only in this header; never log the client or its headers.
_client = httpx.Client(
    base_url=API_BASE,
    headers={"Authorization": f"Bearer {BOT_TOKEN}"},
    timeout=30.0,
)

# Slack rate-limits (~1 msg/s/channel) -> HTTP 429. Back off and retry transient failures
# (429 + 5xx) a few times with exponential delay.
_MAX_RETRIES = 4
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def call(method: str, *, json_body: dict | None = None, params: dict | None = None) -> dict:
    """Call a Slack Web API method (GET if `params`, else POST JSON) with backoff.

    Slack returns HTTP 200 with `{"ok": false, "error": "..."}` on logical failures, so check
    the body's `ok` field too, not just the HTTP status. Raises RuntimeError on a logical error
    (surfacing only the error code, never the token or full request) or after exhausting retries.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            if params is not None:
                resp = _client.get(f"/{method}", params=params)
            else:
                resp = _client.post(f"/{method}", json=json_body or {})
            if resp.status_code in _RETRY_STATUSES:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2.0 ** attempt
                time.sleep(min(delay, 30.0))
                continue
            resp.raise_for_status()
            body = resp.json()
            if not body.get("ok", False):
                # Logical error (bad channel, missing scope, ...). Surface the code only.
                raise RuntimeError(f"Slack {method} failed: {body.get('error')}")
            return body
        except httpx.HTTPError as exc:
            last_exc = exc
            time.sleep(2.0 ** attempt)
    raise RuntimeError(f"Slack API {method} failed after {_MAX_RETRIES} attempts: {last_exc}")


def resolve_channel(channel: str) -> str:
    """Turn an email into the bot<->user DM channel id; pass channels/IDs through unchanged.

    If `channel` looks like an email (e.g. a personal briefing addressed to the user themself),
    look the user up and open a DM, returning that DM channel id. Otherwise return `channel`
    as-is (a `#name` or `C…`/`G…`/`D…` id). Requires `users:read.email` and `im:write` in
    addition to `chat:write`.
    """
    if "@" not in channel or channel.startswith("#"):
        return channel
    user = call("users.lookupByEmail", params={"email": channel})
    user_id = user["user"]["id"]
    dm = call("conversations.open", json_body={"users": user_id})
    return dm["channel"]["id"]
