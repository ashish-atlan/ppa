"""First-party Slack MCP server (stdio).

A thin wrapper over Slack's Web API (https://slack.com/api/chat.postMessage) so
agent-sentry can relay a briefing to a Slack channel. No third-party MCP package: just
FastMCP (transitive via claude-agent-sdk) and httpx (a declared project dep).

The bot token is read from SLACK_BOT_TOKEN in the environment (the launcher loads it from
the repo .env) and is never logged. Scopes: `chat:write` to post; plus `users:read.email`
and `im:write` so a message can be DM'd to a user given their email.

Tools exposed (prefix `mcp__slack__` once registered):
  - send_message(channel, text)  -> chat.postMessage (channel can be a name, id, or email→DM)

Run: `python src/slack_mcp_server.py` (stdio transport). See slack-launcher.sh.
"""

import os
import time

import httpx
from mcp.server.fastmcp import FastMCP

API_BASE = "https://slack.com/api"

# Fail fast: without a token every call would fail auth. Surface it at startup.
BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit(
        "SLACK_BOT_TOKEN not set. Add it to the repo .env "
        "(a bot token with the chat:write scope). Format: xoxb-..."
    )

# Single fixed host, no user-controlled URL (outbound-HTTP allowlist invariant).
# The token lives only in this header; never log the client or its headers.
_client = httpx.Client(
    base_url=API_BASE,
    headers={"Authorization": f"Bearer {BOT_TOKEN}"},
    timeout=30.0,
)

# Slack rate-limits chat.postMessage (~1 msg/s/channel) -> HTTP 429. Back off and retry
# transient failures (429 + 5xx) a few times with exponential delay.
_MAX_RETRIES = 4
_RETRY_STATUSES = {429, 500, 502, 503, 504}

mcp = FastMCP("slack")


def _call(method: str, *, json_body: dict | None = None, params: dict | None = None) -> dict:
    """Call a Slack Web API method (GET if `params`, else POST JSON) with backoff.

    Slack returns HTTP 200 with `{"ok": false, "error": "..."}` on logical failures, so
    check the body's `ok` field too, not just the HTTP status.
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
                # Logical error (bad channel, missing scope, ...). Surface the code only,
                # not the token or full request.
                raise RuntimeError(f"Slack {method} failed: {body.get('error')}")
            return body
        except httpx.HTTPError as exc:
            last_exc = exc
            time.sleep(2.0 ** attempt)
    raise RuntimeError(
        f"Slack API {method} failed after {_MAX_RETRIES} attempts: {last_exc}"
    )


def _resolve_channel(channel: str) -> str:
    """Turn an email into the bot↔user DM channel id; pass channels/IDs through unchanged.

    If `channel` looks like an email (e.g. a personal briefing addressed to the user
    themself), look the user up and open a DM, returning that DM channel id. Otherwise
    return `channel` as-is (a `#name` or `C…`/`G…` id). Requires the `users:read.email` and
    `im:write` scopes in addition to `chat:write`.
    """
    if "@" not in channel or channel.startswith("#"):
        return channel
    user = _call("users.lookupByEmail", params={"email": channel})
    user_id = user["user"]["id"]
    dm = _call("conversations.open", json_body={"users": user_id})
    return dm["channel"]["id"]


@mcp.tool()
def send_message(channel: str, text: str) -> dict:
    """Post a message to a Slack channel or DM a user by email.

    Args:
        channel: One of — a channel name (`#exec-briefings`), a channel/DM id
            (`C0123ABCD`), or a user **email** (`someone@atlan.com`). An email is resolved
            to that user and the message is sent as a direct message from the bot. For a
            channel the bot must be a member.
        text: The message body. Slack mrkdwn is supported; long briefings are fine.

    Returns a dict: {ok: bool, channel: str, ts: str} where `ts` is the message
    timestamp (its id within the channel/DM).
    """
    target = _resolve_channel(channel)
    body = _call("chat.postMessage", json_body={"channel": target, "text": text})
    return {"ok": body.get("ok"), "channel": body.get("channel"), "ts": body.get("ts")}


if __name__ == "__main__":
    mcp.run(transport="stdio")
