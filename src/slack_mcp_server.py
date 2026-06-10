"""First-party Slack MCP server (stdio).

A thin wrapper over Slack's Web API so agent-sentry can relay a briefing to a Slack channel.
No third-party MCP package: just FastMCP (transitive via claude-agent-sdk) and the shared
`slack_client` (auth + backoff over httpx, a declared project dep).

The bot token is read from SLACK_BOT_TOKEN by `slack_client` (the launcher loads it from the
repo .env) and is never logged. Scopes: `chat:write` to post; `users:read.email` + `im:write`
to DM a user by email; `reactions:read` + `im:history` are used by the feedback poller, not here.

Tools exposed (prefix `mcp__slack__` once registered):
  - send_message(channel, text, briefing_path=None, skill=None) -> chat.postMessage. When
    briefing_path is given, the returned message `ts` is recorded to briefings/.slack-sent.jsonl
    so the feedback poller can later correlate reactions/replies back to this briefing + skill.

Run: `python src/slack_mcp_server.py` (stdio transport). See slack-launcher.sh.
"""

import os
import re
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

# This file's dir (src/) is sys.path[0] when run as a script, but add it explicitly so the
# sibling import works regardless of how we're launched.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from slack_client import call, resolve_channel  # noqa: E402

# Where send_message records the outbound message -> briefing correlation. The poller reads this.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SENT_MAP_PATH = PROJECT_ROOT / "briefings" / ".slack-sent.jsonl"

mcp = FastMCP("slack")


# --- markdown -> Slack mrkdwn -------------------------------------------------------------
#
# Briefings are written in GitHub-flavored markdown, but Slack's chat.postMessage renders its
# own "mrkdwn": `[label](url)` shows literally (no hyperlink), `**bold**` and `#` headings
# don't render. We translate the common constructs once here so EVERY skill's relay looks
# right in Slack without each skill having to emit Slack-specific text. Idempotent on text
# that is already mrkdwn (single `*`, `<url|label>`, `•` bullets are left untouched).
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")   # [label](url) -> <url|label>
_MD_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$", re.MULTILINE)
_MD_HR = re.compile(r"^[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.MULTILINE)
_MD_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")                    # **bold** -> *bold*
_MD_BOLD_ALT = re.compile(r"__([^_\n]+)__")                   # __bold__ -> *bold*
_MD_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)  # "- item" -> "• item"


def to_slack_mrkdwn(text: str) -> str:
    """Best-effort GitHub-markdown → Slack-mrkdwn conversion for a relayed briefing."""
    if not text:
        return text
    text = _MD_LINK.sub(r"<\2|\1>", text)   # links FIRST (before bold touches '*')
    text = _MD_HEADING.sub(r"*\1*", text)   # "## Title" -> "*Title*"
    text = _MD_HR.sub("", text)             # "---" rule -> drop (Slack has no <hr>)
    text = _MD_BOLD.sub(r"*\1*", text)      # "**x**" -> "*x*"
    text = _MD_BOLD_ALT.sub(r"*\1*", text)  # "__x__" -> "*x*"
    text = _MD_BULLET.sub(r"\1• ", text)    # nicer bullets (also kills stray "*" list markers)
    return text


def _record_sent(ts: str, channel: str, briefing_path: str, skill: str | None) -> None:
    """Append one correlation record to briefings/.slack-sent.jsonl. Best-effort.

    The record `{ts, channel, briefing_path, skill, sent_at}` is the join key the feedback
    poller uses: `ts` ties a later reaction/reply back to this exact message, `skill` routes the
    distilled feedback to the right skill's preferences. A failed append must NEVER fail the
    send (the briefing already reached the user), so swallow errors after a non-secret warning.
    """
    try:
        SENT_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": ts,
            "channel": channel,
            "briefing_path": briefing_path,
            "skill": skill,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        with SENT_MAP_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001 - never let bookkeeping break the relay
        print(f"slack send_message: could not record sent-map entry: {exc}", file=sys.stderr)


@mcp.tool()
def send_message(
    channel: str,
    text: str,
    briefing_path: str | None = None,
    skill: str | None = None,
) -> dict:
    """Post a message to a Slack channel or DM a user by email.

    Args:
        channel: One of — a channel name (`#exec-briefings`), a channel/DM id (`C0123ABCD`),
            or a user **email** (`someone@atlan.com`). An email is resolved to that user and the
            message is sent as a direct message from the bot. For a channel the bot must be a member.
        text: The message body. Slack mrkdwn is supported; long briefings are fine.
        briefing_path: Optional repo-relative path of the briefing this message carries (e.g.
            `briefings/meeting-prep-20260610T080000Z.md`). When set, the returned `ts` is recorded
            to briefings/.slack-sent.jsonl so the hourly feedback poller can correlate a later
            reaction/reply back to this briefing.
        skill: Optional name of the skill that produced the briefing. Recorded alongside
            briefing_path so feedback routes to that skill's durable preferences.

    Returns a dict: {ok: bool, channel: str, ts: str} where `ts` is the message timestamp
    (its id within the channel/DM).
    """
    target = resolve_channel(channel)
    # Translate GitHub markdown → Slack mrkdwn so links hyperlink and bold/headings render.
    body = call(
        "chat.postMessage",
        json_body={"channel": target, "text": to_slack_mrkdwn(text), "mrkdwn": True},
    )
    ok, ch, ts = body.get("ok"), body.get("channel"), body.get("ts")
    if ok and ts and briefing_path:
        _record_sent(ts=ts, channel=ch or target, briefing_path=briefing_path, skill=skill)
    return {"ok": ok, "channel": ch, "ts": ts}


if __name__ == "__main__":
    mcp.run(transport="stdio")
