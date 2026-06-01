---
name: daily-priority-brief
description: Build the user's daily priority brief and rank everything on an Eisenhower (urgent x important) matrix. Reads the user's context from the ppa Graphiti graph (durable profile + recent slack/gmail/gong/linear/calendar/granola episodes), extracts today's actionable commitments, deadlines, meetings and open asks, then sorts each into Do-First / Schedule / Delegate / Eliminate with a Top-3 focus. Trigger on "daily priority brief", "what should I focus on today", "prioritize my day", "today's priorities", "eisenhower".
briefing:
  name: daily-priority-brief
  relay: [ui, slack]
  slack_channel: "self"   # sentinel → resolved to the configured user's email → DM'd to them
  lock_to_graphiti: true
  graphiti_group: ppa
---

# daily-priority-brief

Turn the user's recent context into a focused daily plan, with every item placed on an
**Eisenhower matrix** (importance x urgency). The **ppa Graphiti graph is the source of
truth** — do not fetch live sources. Ground every item in the profile or a graph episode;
**never invent** work. If little signal exists, produce a short brief and say so.

## Inputs (from the task prompt / agent context)

- `user` — the profile owner (defaults to the authenticated identity).
- `timeframe` — lookback for "recent" signal; default **last 24h** (the agent may pass more).
- Background context the agent already loaded (profile + relevant ppa facts). Supplement it
  with the queries in step 1.

## Procedure

1. **Gather context from `ppa` (source of truth).**
   - `Read` `memory/user-profile.md` — this defines what is **important** to the user (active
     projects/accounts, key collaborators, recurring meetings, focus topics, preferences).
   - Query Graphiti group `ppa` for recent **signal**:
     - `mcp__graphiti__get_episodes(group_ids=["ppa"], max_episodes=20)` for the latest source
       digests + any prior `daily-priority-brief` episodes (continuity).
     - `mcp__graphiti__search_memory_facts(group_ids=["ppa"], ...)` and
       `mcp__graphiti__search_nodes(group_ids=["ppa"], ...)` for: deadlines, items where someone
       is **waiting on / blocked by** the user, escalations, renewals/ARR, and meetings today.

2. **Extract candidate priority items.** Keep only items that actually involve the user (owned
   by, owed by, waiting on, tagged in, attending). For each, capture: a short **title**, the
   **source link**, the **next action / ask**, **who is waiting**, any **date/deadline**, and the
   **related project/account**. Dedup items that appear across sources (e.g. a Slack thread + a
   calendar invite for the same call).

3. **Classify each item on the Eisenhower matrix.** Apply these heuristics, grounded in the
   profile + episode facts (state the reason per item):

   | Axis | Mark it when... |
   |---|---|
   | **Important** | tied to an active project/account in the profile; revenue / renewal / ARR; a commitment the user personally owns; customer- or exec-facing; blocks a teammate or customer |
   | **Urgent** | due today or overdue; someone is explicitly waiting / blocked **now**; an escalation; a meeting within the day; a hard external deadline this week |

   Quadrants:
   - **Q1 Do First** = Important + Urgent
   - **Q2 Schedule** = Important + Not Urgent (give a suggested when)
   - **Q3 Delegate** = Not Important + Urgent (suggest a delegate from the profile's collaborators)
   - **Q4 Eliminate** = Not Important + Not Urgent (decline / drop — use the user's preferences,
     e.g. "optional on non-essential calls", to push low-value meetings here)

4. **Pick the Top 3 focus** — the three highest-leverage items, Q1 first, then Q2.

5. **Write the brief** with the `Write` tool to the path the agent gave you
   (`briefings/daily-priority-brief-<UTC-timestamp>.md`). Use BOTH a summary table and detailed
   sections:

   ```markdown
   ---
   brief: daily-priority-brief
   generated_at: <UTC ISO timestamp>
   user: <email/name>
   source: ppa graph (profile + recent episodes)
   ---

   # Daily Priority Brief — <YYYY-MM-DD>

   ## Top 3 focus today
   1. <item> — <one-line why it's #1>
   2. <item> — <why>
   3. <item> — <why>

   ## Eisenhower matrix
   |                   | Urgent                     | Not Urgent                  |
   |-------------------|----------------------------|-----------------------------|
   | **Important**     | Q1 Do First: <titles>      | Q2 Schedule: <titles>       |
   | **Not Important** | Q3 Delegate: <titles>      | Q4 Eliminate: <titles>      |

   ## 🔴 Do First (Urgent + Important)
   - **<title>** — <link>
     why: <importance + urgency reason> · deadline: <date/none> · waiting: <who/none>
     next: <concrete next action>
   ## 🟡 Schedule (Important, Not Urgent)
   - **<title>** — <link>
     why: … · suggested when: <day/slot>
     next: …
   ## 🔵 Delegate (Urgent, Not Important)
   - **<title>** — <link>
     why: … · delegate to: <collaborator from profile>
   ## ⚪ Eliminate (Not Urgent, Not Important)
   - **<title>** — <link>
     why: <why it can be dropped/declined>

   <!-- if a quadrant is empty: -->
   _none._
   <!-- if no signal at all: -->
   _Quiet day — no actionable priorities found in the last <timeframe> of ppa context._
   ```

6. **Return** ONE line: the brief path + a per-quadrant count, e.g.
   `wrote briefings/daily-priority-brief-20260602T120000Z.md (Q1:3 Q2:4 Q3:2 Q4:1)`.
