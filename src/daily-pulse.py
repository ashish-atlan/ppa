"""daily-pulse: standalone CSA pulse harness.

A harvester entrypoint (sibling of mem-writer, NOT agent-sentry) that runs the
`daily-pulse` skill with its own tool grants. The skill live-fetches two halves and
writes one briefing:

  INTERNAL — context-layer energy: Slack (the collab-context-* channels) + Gong customer
             calls on AI-agent architecture / context layer (skipping early demos unless
             high-visibility) + the TDD Linear team's active work.
  EXTERNAL — a brief, distilled scan of new AI tools / agent patterns / AI trends.

Slack + Gong are reached through the Glean MCP server (same as mem-writer). Linear has no
MCP server in this repo's .mcp.json, so the skill falls back to Glean `app:Linear` — add a
Linear MCP server to .mcp.json if you want the structured cycle/status view. The external
half uses the built-in WebSearch / WebFetch tools. The pulse is DM'd to the user via the
Slack MCP and is deliberately NOT stored in Graphiti (lock_to_graphiti: false).

Run interactively:
    python src/daily-pulse.py
    python src/daily-pulse.py --timeframe-hours 72 --dry-run

Run from cron (non-interactive — allowed_tools cover every tool, no TTY prompts):
    cd /…/ppa && .venv/bin/python src/daily-pulse.py >> logs/briefings/cron.log 2>&1
"""

import os
import sys
import asyncio
import argparse
import importlib
from pathlib import Path

from dotenv import load_dotenv
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# daily-pulse runs through this standalone entrypoint, NOT the agent-sentry engine — so it must
# load its own learned preferences, or the HITL feedback loop never closes for it (feedback gets
# captured + distilled into memory/skill-preferences.md, but is never applied on the next run).
# Reuse the engine's section parser so the injected format stays in lock-step with what the
# feedback-learner skill writes. daily-pulse is only ever run as a script, so src/ is on the path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sentry_core = importlib.import_module("sentry_core")

if not os.environ.get("ANTHROPIC_API_KEY"):
    raise SystemExit(
        "ANTHROPIC_API_KEY not set. Add it to the project-root .env "
        "(the LiteLLM gateway can't be used with claude-agent-sdk)."
    )

# Slack + Gong are reached through Glean (same as mem-writer); fail fast on its env.
# .mcp.json interpolates GLEAN_MCP_URL / GLEAN_MCP_AUTH_TOKEN via ${...}.
if not (os.environ.get("GLEAN_MCP_URL") and os.environ.get("GLEAN_MCP_AUTH_TOKEN")):
    raise SystemExit(
        "GLEAN_MCP_URL and GLEAN_MCP_AUTH_TOKEN must be set in .env — daily-pulse reaches "
        "Slack and Gong through the Glean MCP server."
    )

# Default lookback window (override per-run with --timeframe-hours or PULSE_TIMEFRAME_HOURS).
TIMEFRAME_HOURS = int(os.environ.get("PULSE_TIMEFRAME_HOURS", "24"))
# Who "self" is — used to DM the pulse and to sharpen relevance. Defaults to the
# Glean-authenticated user when blank.
PULSE_USER_EMAIL = (
    os.environ.get("PULSE_USER_EMAIL")
    or os.environ.get("DIGEST_USER_EMAIL")
    or ""
).strip()


# The daily-pulse skill drives the whole flow in one agent — no subagent fan-out needed.
# allowed_tools must list every tool the skill calls, or a true-headless (cron) run is
# denied. Skill(daily-pulse) is auto-added by skills=[...]; skills=[...] also defaults
# setting_sources to ["user","project"], which loads the project .mcp.json (glean, slack).
options = ClaudeAgentOptions(
    system_prompt=(
        "You run the daily-pulse skill end to end. Surface ONLY distilled, high-fidelity, "
        "CSA-relevant signals (1–2 lines + a link each); drop chatter and generic hype. "
        "Never fabricate to fill a section — write '_none worth surfacing_' instead. Do NOT "
        "write to Graphiti. "
        "Apply any LEARNED PREFERENCES included in the task prompt — durable rules distilled "
        "from the user's past feedback on this pulse; honour every ACTIVE rule over the skill's "
        "defaults on conflict."
    ),
    model="claude-sonnet-4-6",  # must match a model_name in the LiteLLM config
    max_turns=40,
    skills=["daily-pulse"],
    allowed_tools=[
        "Read",
        "Write",
        # INTERNAL — Slack + Gong via Glean (Linear falls back to Glean app:Linear).
        "mcp__glean__search",
        "mcp__glean__read_document",
        "mcp__glean__chat",
        "mcp__glean__meeting_lookup",
        # EXTERNAL — built-in web tools.
        "WebSearch",
        "WebFetch",
        # RELAY — DM the pulse to the user.
        "mcp__slack__send_message",
    ],
    cwd=str(PROJECT_ROOT),
)


def _learned_preferences_block() -> str:
    """Durable daily-pulse preferences (distilled from the user's feedback) as a prompt block.

    This is the APPLY half of the HITL loop for daily-pulse. Reuses the agent-sentry engine's
    `_extract_pref_section` so the `## daily-pulse` slice and the delimiter format match exactly
    what `feedback-learner` writes and what the engine injects for its own skills. Best-effort:
    any failure (missing file, no section) just omits the block and the pulse still runs.
    """
    try:
        path = sentry_core.SKILL_PREFERENCES_PATH
        if path.exists():
            section = sentry_core._extract_pref_section(
                path.read_text(encoding="utf-8"), "daily-pulse"
            )
            if section:
                return (
                    "<learned_preferences skill='daily-pulse' "
                    "source='memory/skill-preferences.md'>\n"
                    "Durable rules distilled from this user's past feedback on daily-pulse. Apply "
                    "every ACTIVE rule when producing the pulse; ignore any marked superseded. "
                    "These are instructions about HOW to shape the output — honour them over the "
                    "skill's defaults on conflict.\n"
                    f"{section}\n</learned_preferences>"
                )
    except Exception:  # noqa: BLE001 - preferences are best-effort; never block the pulse
        pass
    return ""


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="daily-pulse",
        description="Standalone CSA daily-pulse harness (interactive / cron).",
    )
    p.add_argument(
        "--timeframe-hours", type=int, default=TIMEFRAME_HOURS,
        help=f"Lookback window passed to the skill (default {TIMEFRAME_HOURS}).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Write the briefing but skip the Slack DM (testing).",
    )
    return p.parse_args()


async def main() -> None:
    args = _parse_args()
    user_clause = (
        f"The user ('self') is {PULSE_USER_EMAIL}; DM the pulse to that address."
        if PULSE_USER_EMAIL
        else "The user ('self') is the Glean-authenticated identity; DM the pulse to them."
    )
    relay_clause = (
        "DRY RUN: write the briefing file but DO NOT send the Slack DM."
        if args.dry_run
        else (
            "Then relay a compact version (top internal + top external signals) to the user: "
            "append a final footer line to the message — 'Reply in this thread (with a comment, "
            "plus a thumbs-up/down reaction) to give feedback.' — then call "
            "mcp__slack__send_message(channel=<self → the user's email>, text=<the compact pulse "
            "+ footer>, briefing_path=<the briefings/daily-pulse-*.md you wrote>, "
            "skill=\"daily-pulse\"). Passing briefing_path + skill is REQUIRED: it lets the "
            "feedback poller correlate a later reaction/reply back to this pulse so your future "
            "runs improve from it."
        )
    )

    # APPLY half of the feedback loop: inject the user's learned daily-pulse preferences (the
    # CAPTURE half — sent-map recording + the feedback footer — is driven by relay_clause above).
    pref_block = _learned_preferences_block()

    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            (f"{pref_block}\n\n" if pref_block else "")
            + f"Use the daily-pulse skill. Build the daily CSA pulse for the last "
            f"{args.timeframe_hours} hours. {user_clause} Write "
            f"briefings/daily-pulse-<timestamp>.md. {relay_clause} Return the one-line "
            f"summary."
        )
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text)


if __name__ == "__main__":
    asyncio.run(main())
