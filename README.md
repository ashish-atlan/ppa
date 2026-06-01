# ppa — personal digest writer

`src/mem-writer.py` is a multi-agent **personal daily digest writer** built on the
[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python). A main
**orchestrator** agent fans out to six per-source sub-agents — one per source system —
to harvest the user's recent, relevant artifacts, writes each to a markdown file, then
ingests everything into a local **Graphiti** knowledge graph and distils a durable
**user profile**.

Every source is reached **only through the Glean MCP server** — never a direct source API.

## Pipeline

![mem-writer pipeline](docs/mem-writer-pipeline.svg)

> Editable diagram: [open in Excalidraw](https://excalidraw.com/#json=EIdr7KpmElv0Tmw92-qq5,75dJe3A7OWW6WF4F8gVGiQ)
> · source: [`docs/mem-writer-pipeline.excalidraw.json`](docs/mem-writer-pipeline.excalidraw.json)

<details>
<summary>Text fallback (mermaid)</summary>

```mermaid
flowchart TD
  main["main()"] -->|query| orch["Orchestrator agent"]
  orch -->|"Task x6 — parallel"| subs["slack · gmail · gong · granola · linear · gcal<br>(*-digest subagents)"]
  subs -->|"via Glean only<br>app:&lt;source&gt;, last 24h, user-relevant"| glean["Glean MCP"]
  glean -->|"subagent Write"| mem["memory/&lt;source&gt;-&lt;timestamp&gt;.md  (6 files)"]
  mem -->|"Read + add_memory"| graph["Graphiti (Neo4j, local)<br>group_id: ppa"]
  mem -->|"Task: digest files"| pc["profile-curator<br>(user-profile skill)"]
  pc -->|"add_memory (profile)"| graph
  pc -->|Write| up["memory/user-profile.md"]
```
</details>

## How it works

1. **Orchestrator** (`ClaudeAgentOptions` in `src/mem-writer.py`) does no gathering itself.
   It delegates **one `Task` per source, in parallel**, to six `AgentDefinition`
   sub-agents: `slack-digest`, `gmail-digest`, `gong-digest`, `granola-digest`,
   `linear-digest`, `gcal-digest`.
2. **Each source sub-agent** uses the shared **`source-digest`** skill. It scopes *every*
   Glean query to its one source (`app:<Source>`), applies that source's user-relevance
   rule, and writes `memory/<source>-<timestamp>.md`. It has only Glean tools + `Write`
   — no Graphiti access.

   | source | `app:` filter | relevant when… |
   |---|---|---|
   | slack | `app:Slack` | user tagged directly/indirectly or participated in the thread |
   | gmail | `app:Gmail` | user is in **to / cc / bcc** |
   | gong | `app:Gong` | user was **invited to** the call |
   | granola | `app:Granola` | user was **invited to** the meeting |
   | linear | `app:Linear` | issue assigned to / created by / mentioning / subscribed-to user |
   | google calendar | `app:"Google Calendar"` | user is an **attendee / invitee** |

3. **Graphiti ingest** — once the six files exist, the orchestrator reads each and calls
   `mcp__graphiti__add_memory` once per file under **`group_id="ppa"`**
   (`name="<source> digest <ts>"`).
4. **profile-curator** sub-agent — the orchestrator then delegates the six digest files to
   it. Using the reusable **`user-profile`** skill, it extracts durable, slow-changing
   facts (role, recurring collaborators, projects, recurring meetings, topics,
   preferences), **dedups against what is already stored**, and persists to **both**
   Graphiti (`group_id="ppa"`, episode `profile: <user>`) **and** `memory/user-profile.md`.
5. **Drain** — Graphiti's `add_memory` only *queues* an episode (a background worker
   extracts entities and writes to Neo4j). `main()` holds the client open for
   `GRAPHITI_DRAIN_SECONDS` (45s) so queued episodes land before the stdio MCP server is
   torn down.

### Why one Graphiti group (`ppa`) for everything

Digests and profile facts share a single `group_id`. Graphiti scopes entity resolution and
edges **within** a group, so a shared group lets profile entities (e.g. a recurring
collaborator) link to the same people appearing in the daily digest episodes — one
connected personal graph, not isolated partitions. Episode kinds stay distinguishable by
episode name (`profile: <user>` vs `<source> digest <ts>`). `memory/user-profile.md` is the
clean standalone read of the profile.

## Skills

| skill | used by | purpose |
|---|---|---|
| [`source-digest`](.claude/skills/source-digest/SKILL.md) | the 6 source sub-agents (+ orchestrator) | harvest ONE source via Glean, scoped + user-relevant, write the digest file |
| [`user-profile`](.claude/skills/user-profile/SKILL.md) | `profile-curator` (+ any other agent) | generic: from any input → extract durable facts → dedup → write to Graphiti + `user-profile.md` |

The `user-profile` skill is deliberately **generic / reusable** — it curates the profile
from whatever input the caller names (digest files, a chat response, an email thread), so
other agents that process user responses can reuse it, not just mem-writer.

## MCP servers

| server | transport | tools (prefix) |
|---|---|---|
| `graphiti` | stdio launcher (`~/.config/claude-mcp/graphiti-launcher.sh`), local Neo4j | `mcp__graphiti__*` |
| `glean` | HTTP, `${GLEAN_MCP_URL}/mcp/default` + bearer token | `mcp__glean__*` |

Wiring lives both **in code** (`src/mem-writer.py`, for the SDK agent) and in
[`.mcp.json`](.mcp.json) (for headless `claude -p`, see below). Keys (`graphiti`, `glean`)
match in both, so tool names resolve identically.

## Setup

```bash
cp .env.example .env   # then fill in the values
uv sync                # or: pip install -e .
```

Required in `.env`:

| var | purpose |
|---|---|
| `ANTHROPIC_API_KEY` | claude-agent-sdk (the LiteLLM gateway can't be used with the SDK) |
| `GLEAN_MCP_URL`, `GLEAN_MCP_AUTH_TOKEN` | Glean MCP — every source is reached via Glean |
| `DIGEST_USER_EMAIL` *(optional)* | sharpens Glean `to:`/`from:`/`cc:` filters; defaults to the Glean-authenticated user |
| `DIGEST_TIMEFRAME_HOURS` *(optional)* | lookback window, default `24` |

Graphiti must be running locally (the launcher self-loads its own `.env` and starts the
MCP server against local Neo4j).

## Run

**Full pipeline (SDK agent):**
```bash
.venv/bin/python src/mem-writer.py
```
Produces `memory/<source>-<timestamp>.md` ×6 + `memory/user-profile.md`, and lands the
matching episodes in Graphiti under `group_id="ppa"`.

**Headless, single skill (`claude -p`):** a skill reproduces *its own* step — not the
fan-out. `.mcp.json` gives the headless session the same Glean/Graphiti tools:
```bash
claude -p "Use the source-digest skill for source=slack, timeframe=last 24h." \
  --allowedTools "mcp__glean__search,mcp__glean__read_document,mcp__glean__chat,mcp__glean__meeting_lookup,Write"
```
Caveats vs the SDK agent: (1) **no fan-out/sequencing** — one `-p` call = one source (or
one profile curation); script the six calls + the profile step yourself. (2) **Graphiti
drain** — `claude -p` exits on completion and tears down the stdio graphiti server
immediately, so a just-queued episode can be lost; re-query/verify and re-run if it didn't
land (the SDK agent's 45s drain handles this automatically).

## Layout

```
src/mem-writer.py                       orchestrator + 7 sub-agents
.claude/skills/source-digest/SKILL.md   per-source harvest skill (shared)
.claude/skills/user-profile/SKILL.md    durable-profile curation skill (reusable)
.mcp.json                               glean + graphiti wiring for headless claude -p
memory/                                 digest outputs + user-profile.md (runtime)
docs/mem-writer-pipeline.svg            pipeline diagram (embedded above)
docs/mem-writer-pipeline.excalidraw.json  editable diagram source
```
