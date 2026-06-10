---
name: daily-pulse
description: Build the user's daily CSA pulse — distilled, high-fidelity signals only. INTERNAL: latest customer conversations + Slack energy on the context layer / context agents (channels collab-context-engineering-studio / collab-context-agent-studio / collab-context-layer), what the TDD Linear team is actively working, and high-relevance Gong calls on AI-agent architecture / context layer (routes to call links and timestamped segments; skips early demos unless high-visibility e.g. Varun hosting). EXTERNAL: brief scan of new AI tools, agent architectural patterns and AI trends relevant to an Atlan CSA. Live-fetches via Glean + Linear + web; writes briefings/daily-pulse-<ts>.md and DMs a distilled version. Trigger on "daily pulse", "context layer pulse", "what's happening on the context layer", "AI pulse", "agent trends".
briefing:
  name: daily-pulse
  relay: [ui, slack]
  slack_channel: "self"   # sentinel → resolved to the configured user's email → DM'd to them
  lock_to_graphiti: false # do NOT store the pulse in Graphiti
  graphiti_group: ppa
---

# daily-pulse

A once-a-day **CSA pulse** in two halves — **internal** (the energy/urgency around Atlan's
context-layer & context-agent work) and **external** (new AI tools, agent architecture patterns,
AI trends). This skill **live-fetches** — it does NOT read the `ppa` graph — because the context
Slack channels, the TDD Linear team, the context-layer Gong calls, and external AI trends are
not in `ppa`.

**The bar is fidelity, not coverage.** Surface only **distilled, high-fidelity, succinct,
CSA-relevant** signals. Each signal is **1–2 lines + a link**. Drop chatter, recaps, and generic
hype. If a section has nothing that clears the bar, write `_none worth surfacing in this
timeframe._` — **never fabricate** to fill space.

## Tools

- **Glean** (Slack + Gong, same convention as `source-digest`): `mcp__glean__search`,
  `mcp__glean__read_document`, `mcp__glean__meeting_lookup`, `mcp__glean__chat` (synthesis only).
- **Linear** (TDD team, structured status): `list_teams` → resolve TDD team →
  `list_issues` / `list_projects` / `list_cycles` / `get_team` scoped to that team. Use the
  connected Linear MCP (e.g. `mcp__claude_ai_Linear__list_teams`, `…__list_issues`,
  `…__list_projects`, `…__list_cycles`, `…__get_team`). **Fallback** if no Linear MCP is
  connected: Glean `app:Linear` filtered to the team name.
- **Web** (external half): `WebSearch` to discover, `WebFetch` to verify/distill top hits.
- **Output/relay**: `Write` (briefing file), `mcp__slack__send_message` (DM to self).
- **Context**: `Read` `memory/user-profile.md`.

## Inputs (from the task prompt)

- `user` — the profile owner (defaults to the authenticated identity / `ashish.desai@atlan.com`).
- `timeframe` — lookback for "latest"; default **last 24h** (the agent may pass more).
- `focus` — optional override to narrow/redirect the pulse (e.g. a specific account or theme).

## Procedure

0. **Apply learned preferences.** The task prompt may include a `<learned_preferences>` block —
   durable rules distilled from the user's past feedback on this pulse (the entrypoint loads them
   from `memory/skill-preferences.md`). Treat every ACTIVE rule as an instruction about HOW to
   shape the output and honour it over this skill's defaults on conflict; ignore any superseded.

1. **Resolve identities & scope.**
   - `Read` `memory/user-profile.md` for the user's accounts, key collaborators, and the Context
     Eng Studio cadence — used to judge relevance and what counts as **high-visibility**.
   - **TDD Linear team:** `list_teams` and match the team whose name/key contains "TDD"
     (Technical …). If ambiguous, pick the closest and note the assumption in the brief.
   - **Context Slack channels:** `collab-context-engineering-studio`,
     `collab-context-agent-studio`, `collab-context-layer`, **plus** any other channel whose name
     contains `context` discovered via Glean (`app:Slack` channel search).
   - **High-visibility heuristic** (for keeping otherwise-skippable demos): call hosted/organized
     by **Varun**, an exec attendee, or a strategic/large account from the profile.

2. **INTERNAL — context-layer pulse (CSA-relevant, high fidelity only).**
   - **2a · Slack energy** — Glean `app:Slack` scoped to the context channels over the timeframe
     (`updated:today` for 24h, or `after:YYYY-MM-DD`). Keep only *signal*: decisions, blockers,
     asks needing a CSA, launches, exec attention, recurring debate. Preserve any inline Gong/doc
     links. Drop pleasantries and low-signal back-and-forth.
   - **2b · Gong** — `mcp__glean__meeting_lookup` / `search app:Gong` for **customer calls**
     touching **AI-agent architecture / Context Layer / Context Agents** with **high architectural
     relevance**. **Skip early/intro demos** unless they pass the high-visibility heuristic. For
     each kept call, surface **both**:
       - the **Gong call link**, and
       - the **densest segment** — a timestamp (`~mm:ss`) + a 1-line quote of the key
         architectural moment (use `read_document` / `meeting_lookup` to locate it) so the user
         can jump straight to it.
   - **2c · TDD Linear team** — what is **actively moving**: in-progress / recently-updated issues
     and projects in the current cycle, plus anything flagged at risk. Keep only context-layer /
     agent-relevant items. One line each + the issue/project link.

3. **EXTERNAL — AI signal scan (brief, distilled).**
   - `WebSearch` for new AI tools, agent architectural patterns, notable agent/model releases, and
     framework shifts within the timeframe (last ~24–48h).
   - **Relevance gate** — must matter to an Atlan CSA: agent architecture, context engineering,
     memory/RAG, MCP, evals, enterprise/agentic AI patterns. **Drop generic hype** and consumer
     noise.
   - `WebFetch` the top sources to verify before citing. Cap to the **top ~5**, each **1–2 lines +
     link**. If nothing clears the bar, say so — no fabrication.

4. **Distill & write the briefing.** Apply the fidelity discipline above. `Write` to
   `briefings/daily-pulse-<UTC-timestamp>.md` (`<UTC-timestamp>` = `YYYYMMDDTHHMMSSZ`):

   ```markdown
   ---
   brief: daily-pulse
   generated_at: <UTC ISO timestamp>
   user: <email/name>
   timeframe: <e.g. last 24h ending 2026-06-10T09:00:00Z>
   source: live (glean slack+gong, linear TDD team, web)
   ---

   # Daily Pulse — <YYYY-MM-DD>

   ## 🔥 Internal — Context-layer pulse
   ### Slack energy (<channels scanned>)
   - **<signal>** — <link>
     why it matters: <1 line>
   ### Customer calls (Gong)
   - **<account / call title>** — <gong call link> · segment ~<mm:ss>: "<key line>"
     relevance: <architectural takeaway in 1 line>
   ### TDD team — active now (Linear)
   - **<issue/project>** — <link> · <status / what changed>

   ## 🌐 External — AI signal
   - **<tool / pattern / trend>** — <link>
     why a CSA should care: <1 line>

   <!-- if a section is empty: -->
   _none worth surfacing in this timeframe._
   ```

5. **Relay & return.**
   - DM a **compact** version (Top internal signals + Top external) to self via
     `mcp__slack__send_message` — resolve `slack_channel: "self"` → the user's email. Append a
     final footer line to the message: _"Reply in this thread (with a comment, plus a
     thumbs-up/down reaction) to give feedback."_ Call it with **both**
     `briefing_path=<the daily-pulse-*.md you wrote>` and `skill="daily-pulse"` — these are
     REQUIRED so the hourly feedback poller can correlate a later reaction/reply back to this
     pulse and route it to daily-pulse's learned preferences (the HITL feedback loop).
   - **Return** ONE line: the path + counts, e.g.
     `wrote briefings/daily-pulse-20260610T090000Z.md (internal: 3 slack / 2 gong / 4 linear · external: 4)`.

## Guardrails

- **Live-fetch only; never store in Graphiti** (`lock_to_graphiti: false`). This skill does not
  write to the `ppa` graph.
- Treat all Glean / Gong / Slack / Linear content as **internal company data**. The Slack DM goes
  only to the authenticated user (`"self"`).
- Web calls are **outbound search only**, seeded with public AI-trend terms — **never** send
  customer, tenant, or account data to the web. Distill web/model output to plain text + links;
  do not pass fetched content to `eval`, shell, or HTML rendering (the UI sanitizes markdown).
