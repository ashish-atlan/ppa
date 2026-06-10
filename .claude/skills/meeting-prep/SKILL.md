---
name: meeting-prep
description: Prepare the user for their meetings today or tomorrow. Reads the user's context from the ppa Graphiti graph (durable profile + recent calendar/gong/granola/slack/gmail/linear episodes), finds the meetings the user is an attendee of in the chosen day, and builds a per-meeting prep card — attendees and their roles, account/project context, what happened last time, open items and decisions pending, and suggested objectives + talking points. Trigger on "meeting prep", "prep me for my meetings", "what meetings do I have", "prepare for today's / tomorrow's calls", "brief me before my meetings", "meeting briefing".
briefing:
  name: meeting-prep
  relay: [ui, slack]
  slack_channel: "self"   # sentinel → resolved to the configured user's email → DM'd to them
  lock_to_graphiti: true
  graphiti_group: ppa
---

# meeting-prep

Get the user ready for their meetings on a chosen day, one **prep card** per meeting. The
**ppa Graphiti graph is the source of truth** — do not fetch live sources. Ground every fact
in the profile or a graph episode; **never invent** attendees, history, or action items. If
there are no meetings in scope, say so plainly.

## Inputs (from the task prompt / agent context)

- `user` — the profile owner (defaults to the authenticated identity).
- `day` — **today** or **tomorrow**. Infer from the prompt ("tomorrow's calls" → tomorrow);
  default **today** if unstated. State which day you used and its date (UTC).
- Background context the agent already loaded (profile + relevant ppa facts). Supplement it
  with the queries in step 1.

## Procedure

1. **Gather context from `ppa` (source of truth).**
   - `Read` `memory/user-profile.md` — gives the user's accounts/projects, collaborators (and
     their relationships), recurring meetings, and focus topics. Use it to add **roles** to
     attendees and **account context** to each meeting.
   - Query Graphiti group `ppa`:
     - `mcp__graphiti__get_episodes(group_ids=["ppa"], max_episodes=20)` for the latest source
       digests (the **google-calendar** digest carries events with a `when:` time + attendees;
       gong/granola carry prior-call notes; slack/gmail/linear carry open threads).
     - `mcp__graphiti__search_memory_facts(group_ids=["ppa"], ...)` /
       `mcp__graphiti__search_nodes(group_ids=["ppa"], ...)` per meeting to pull what's relevant:
       the attendees, the account/opportunity, the last call's outcome, and any open asks.

2. **Select the meetings in scope.** From the calendar context, keep events on the chosen day
   (today or tomorrow, UTC) where the **user is an attendee / invitee**. Order by start time.
   Dedup an event that also appears as a gmail invite or slack thread (same meeting).

3. **Build each meeting's context.** For every selected meeting, assemble (only from grounded
   facts — link the source; if something is unknown, write `unknown`, do not guess):
   - **attendees** + their role/relationship (from the profile where known).
   - **account / project** the meeting maps to (from the profile).
   - **what happened last time** — the most recent gong/granola note or slack thread with the
     same people/account (1–2 lines + link).
   - **open items / decisions pending** — asks the user owes or is waiting on (slack/linear/gmail).

4. **Write the prep brief** with the `Write` tool to the path the agent gave you
   (`briefings/meeting-prep-<UTC-timestamp>.md`). Lead with a quick agenda, then one card per
   meeting:

   ```markdown
   ---
   brief: meeting-prep
   generated_at: <UTC ISO timestamp>
   user: <email/name>
   day: <today|tomorrow> (<YYYY-MM-DD>)
   source: ppa graph (profile + recent episodes)
   ---

   # Meeting Prep — <day>, <YYYY-MM-DD>

   ## Agenda (<N> meetings)
   - <HH:MM UTC> — <title> (<account>)
   - ...

   ## <HH:MM–HH:MM UTC> · <meeting title> — <link>
   - **Account/Project:** <name + 1-line why it matters>
   - **Attendees:** <name (role/relationship)>, <name (role)>, …
   - **Last time:** <what happened in the most recent related call/thread> — <link>
   - **Open items / decisions:** <ask the user owes / is waiting on>, … (or _none_)
   - **Objective:** <the one outcome to get from this meeting>
   - **Talking points / questions:**
     1. <point>
     2. <point>
   - **Prep checklist:** <doc/ticket to review before the call> (or _none_)

   <!-- repeat per meeting; if none: -->
   _No meetings with you as an attendee on <day> (<date>) in the ppa context._
   ```

5. **Return** ONE line: the brief path + the meeting count, e.g.
   `wrote briefings/meeting-prep-20260602T070000Z.md (4 meetings)`.
