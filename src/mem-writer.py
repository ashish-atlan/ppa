"""mem-writer: personal daily digest orchestrator.

A main orchestrator agent fans out to six per-source sub-agents IN PARALLEL — one
per source system (slack, gmail, gong, granola, linear, google calendar). Every
source is reached ONLY through the Glean MCP server; each sub-agent scopes its
Glean queries to its one source (`app:<Source>`) and to the user's relevant
artifacts in a timeframe (default last 24h), then writes a digest to
`memory/<source>-<timestamp>.md` via the `source-digest` skill.

Once the six files exist the orchestrator ingests each into Graphiti under a single
group_id `ppa`, then delegates to a `profile-curator` sub-agent that distils durable
user-profile facts (via the reusable `user-profile` skill) into the same `ppa` group
(episode `profile: <user>`) and `memory/user-profile.md`.

Reference shapes: ../../WorkArea/agent_practice (sub_agents.py / multi_agents.py).
"""

import os
import asyncio
from pathlib import Path

from dotenv import load_dotenv
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AgentDefinition,
    AssistantMessage,
    TextBlock,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

if not os.environ.get("ANTHROPIC_API_KEY"):
    raise SystemExit(
        "ANTHROPIC_API_KEY not set. Add it to the project-root .env "
        "(the LiteLLM gateway can't be used with claude-agent-sdk)."
    )


# Graphiti MCP server: stdio launcher living outside ~/Desktop (dodges the macOS
# TCC exec gate) that self-loads grafiti/.env and runs the server. The dict key
# `graphiti` sets the tool prefix -> `mcp__graphiti__<tool>`.
GRAPHITI_LAUNCHER = "/Users/ashish.desai/.config/claude-mcp/graphiti-launcher.sh"

mcp_servers = {
    "graphiti": {"type": "stdio", "command": GRAPHITI_LAUNCHER, "args": []},
}

# Glean MCP server (streamable-HTTP transport). Bearer token + host come from the
# project .env loaded above. GLEAN_MCP_URL holds only the host, so append the MCP
# endpoint path. Required for this pipeline — every source is reached via Glean.
glean_url = os.environ.get("GLEAN_MCP_URL")
glean_token = os.environ.get("GLEAN_MCP_AUTH_TOKEN")
if glean_url and glean_token:
    glean_endpoint = glean_url.rstrip("/") + "/mcp/default"
    mcp_servers["glean"] = {
        "type": "http",
        "url": glean_endpoint,
        "headers": {"Authorization": f"Bearer {glean_token}"},
    }
else:
    raise SystemExit(
        "GLEAN_MCP_URL and GLEAN_MCP_AUTH_TOKEN must be set in .env — the digest "
        "pipeline reaches every source only through the Glean MCP server."
    )

# Graphiti's add_memory only *queues* an episode; a background worker then does LLM
# entity-extraction and writes to Neo4j. The MCP server runs as a stdio subprocess
# torn down when the client's `async with` exits — so the client that writes must be
# held open afterward to let the queue drain, else the episode is lost.
GRAPHITI_DRAIN_SECONDS = 45

# Default lookback window for the daily digest.
TIMEFRAME_HOURS = int(os.environ.get("DIGEST_TIMEFRAME_HOURS", "24"))
# Optional identity to sharpen Glean to:/from:/cc: filters (defaults to the
# Glean-authenticated user when blank).
DIGEST_USER_EMAIL = os.environ.get("DIGEST_USER_EMAIL", "").strip()


# --- Per-source sub-agents (one AgentDefinition each, fanned out in parallel) ---
#
# A subagent is a constrained worker the orchestrator delegates to via the built-in
# `Task` tool. Registering all six in `agents={...}` lets the orchestrator fire six
# Task calls in a single turn (parallel fan-out). Each runs in its own context window,
# inherits mcp_servers, but only sees the tools granted in `tools=[...]`. They use the
# shared `source-digest` skill; the per-source rule lives in the skill, the prompt just
# names this agent's one source. Sub-agents write files; they do NOT touch Graphiti.

# (slug, label, Glean app: filter, user-relevance rule)
SOURCES = [
    ("slack", "Slack", "app:Slack",
     "Slack threads where the user is tagged directly or indirectly, or participated."),
    ("gmail", "Gmail", "app:Gmail",
     "Gmail messages where the user is in the to, cc, or bcc list."),
    ("gong", "Gong", "app:Gong",
     "Gong calls the user was invited to / attended."),
    ("granola", "Granola", "app:Granola",
     "Granola meetings the user was invited to / attended."),
    ("linear", "Linear", "app:Linear",
     "Linear issues assigned to, created by, mentioning, or subscribed-to by the user."),
    ("google-calendar", "Google Calendar", 'app:"Google Calendar"',
     "Google Calendar events where the user is an attendee / invitee."),
]

GLEAN_TOOLS = [
    "mcp__glean__search",
    "mcp__glean__read_document",
    "mcp__glean__chat",
    "mcp__glean__meeting_lookup",
]


def _source_subagent(slug: str, label: str, app_filter: str, rule: str) -> AgentDefinition:
    user_hint = f" The user is {DIGEST_USER_EMAIL}." if DIGEST_USER_EMAIL else ""
    return AgentDefinition(
        description=(
            f"Harvest the user's last-{TIMEFRAME_HOURS}h {label} artifacts via Glean "
            f"and write a digest file. Use for the {label} source only."
        ),
        prompt=(
            f"You harvest ONLY the {label} source, reached ONLY through the Glean MCP "
            f"server. Use the `source-digest` skill. Scope EVERY Glean query with the "
            f"`{app_filter}` filter and make it explicit the request is for {label}. "
            f"Keep only artifacts matching this user-relevance rule: {rule}{user_hint} "
            f"Timeframe: last {TIMEFRAME_HOURS} hours. Write the digest to "
            f"memory/{slug}-<timestamp>.md and return only the file path."
        ),
        tools=[*GLEAN_TOOLS, "Write"],
        skills=["source-digest"],
        model="claude-sonnet-4-6",
    )


source_agents = {
    f"{slug}-digest": _source_subagent(slug, label, app_filter, rule)
    for slug, label, app_filter, rule in SOURCES
}


# --- profile-curator sub-agent (durable user-profile facts) ---
#
# Runs after the digests land. Reads the six digest files + existing profile, dedups,
# and persists durable facts to Graphiti (group ppa, episode `profile: <user>`) and
# memory/user-profile.md via the reusable `user-profile` skill.
profile_curator = AgentDefinition(
    description=(
        "Curate durable user-profile facts from given input; dedup + persist to "
        "Graphiti (group ppa, episode 'profile: <user>') and memory/user-profile.md."
    ),
    prompt=(
        "You curate the durable user profile. Use the `user-profile` skill on the input "
        "files named in the task. Extract only slow-changing facts (role, collaborators, "
        "projects, recurring meetings, topics, preferences), dedup against the existing "
        "profile in Graphiti group `ppa` and memory/user-profile.md, then persist to BOTH. "
        "Return a short summary of what changed."
    ),
    tools=[
        "Read",
        "Write",
        "mcp__graphiti__search_nodes",
        "mcp__graphiti__get_episodes",
        "mcp__graphiti__add_memory",
    ],
    skills=["user-profile"],
    model="claude-sonnet-4-6",
)


# --- Orchestrator ---
#
# Thin: it does not gather sources itself. It delegates one Task per source IN PARALLEL,
# ingests the resulting files into Graphiti (group ppa), then delegates to the
# profile-curator. `skills=[...]` defaults setting_sources to ["user", "project"], which
# discovers skills at <cwd>/.claude/skills/<name>/SKILL.md — so cwd is pinned to the repo
# root. allowed_tools auto-approves the listed tools (no prompt).
options = ClaudeAgentOptions(
    system_prompt=(
        "You are a digest orchestrator. Do NOT gather any source data yourself. "
        "Step 1: delegate ONE Task per source IN PARALLEL (issue all six Task calls in a "
        "single turn) to the *-digest subagents; each writes a memory/<source>-<ts>.md file "
        "and returns its path. "
        "Step 2: once all six files exist, Read each and call mcp__graphiti__add_memory ONCE "
        "per file with group_id='ppa', name='<source> digest <ts>', source='text', "
        "source_description='ppa source digest', episode_body=the file contents. "
        "Step 3: delegate one Task to the profile-curator subagent, passing the six digest "
        "file paths, to update the durable user profile. "
        "Report a concise summary at the end."
    ),
    model="claude-sonnet-4-6",  # must match a model_name in the LiteLLM config
    max_turns=40,
    skills=["source-digest", "user-profile"],
    mcp_servers=mcp_servers,
    agents={**source_agents, "profile-curator": profile_curator},
    allowed_tools=[
        "Task",  # auto-approve delegation to subagents
        "Read",
        "Glob",
        "mcp__graphiti__add_memory",  # orchestrator ingests the digest files
    ],
    cwd=str(PROJECT_ROOT),
)


async def main() -> None:
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            f"Build the daily digest for the last {TIMEFRAME_HOURS} hours across all six "
            f"sources (slack, gmail, gong, granola, linear, google calendar): delegate one "
            f"Task per source IN PARALLEL. Then persist each resulting memory/ file to "
            f"Graphiti under group_id 'ppa'. Finally delegate to the profile-curator "
            f"subagent with the six digest file paths to update the durable user profile."
        )
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text)

        # Keep the MCP server alive so queued episodes reach Neo4j.
        print(f"\n[waiting {GRAPHITI_DRAIN_SECONDS}s for Graphiti to persist episodes...]")
        await asyncio.sleep(GRAPHITI_DRAIN_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
