---
name: source-digest
description: Harvest the current user's relevant artifacts from ONE source system (slack, gmail, gong, granola, linear, or google calendar) over a timeframe (default last 24h), reached ONLY via the Glean MCP server, and write a markdown digest to memory/<source>-<timestamp>.md. Trigger when asked to build a per-source digest, harvest a source, or summarize a user's recent activity in a single named source.
---

# source-digest

Harvest the **current user's relevant** artifacts from **exactly one** source system over a
timeframe and write them to a file. Sources are reached **only through the Glean MCP
server** — never a direct source API. Every Glean call must make the source explicit.

Operate on the **one** source named in the task. Do not mix sources. If no source is given,
ask for one — do not guess.

## Inputs (from the task prompt)

- `source` — one of: `slack`, `gmail`, `gong`, `granola`, `linear`, `google calendar`.
- `timeframe` — default **last 24 hours** if unspecified.
- `user` — optional email/name (`DIGEST_USER_EMAIL`) to sharpen `to:`/`from:`/`cc:` filters.
  Glean results are already permission-filtered to the authenticated user, so "the user" =
  the Glean token's identity by default.

## Procedure

1. **Translate the timeframe to a Glean date filter.** For last 24h use `updated:today`
   (or compute `after:YYYY-MM-DD` from the cutoff and add it to the query string).

2. **Query Glean, scoped to the one source.** ALWAYS include that source's `app:` filter
   AND state in the query text that you want results for that source only:

   | source           | Glean `app:` filter      | user-relevance rule (what counts)                                   | best tool |
   |------------------|--------------------------|---------------------------------------------------------------------|-----------|
   | slack            | `app:Slack`              | threads where the user is tagged directly/indirectly or participated| `search` → `read_document` |
   | gmail            | `app:Gmail`              | user is in **to / cc / bcc** (use `to:`/`cc:` with the user email)  | `search` → `read_document` |
   | gong             | `app:Gong`               | user was **invited to** the call / meeting                          | `meeting_lookup`, `search` |
   | granola          | `app:Granola`            | user was **invited to** the meeting                                 | `meeting_lookup`, `search` |
   | linear           | `app:Linear`             | issues assigned to / created by / mentioning / subscribed-to user   | `search` → `read_document` |
   | google calendar  | `app:"Google Calendar"`  | events where the user is an **attendee / invitee**                  | `meeting_lookup`, `list` |

   Tools: `mcp__glean__search` for discovery, `mcp__glean__read_document` for full content,
   `mcp__glean__meeting_lookup` for gong/granola/calendar, `mcp__glean__chat` only to
   synthesize across several results. Refine with extra filters rather than broadening.

3. **Apply the user-relevance rule** for the source (table above). Keep only artifacts that
   actually involve the user. **No fabrication** — if nothing matches, record `none found`.

4. **Write the digest file** with the `Write` tool to:
   `memory/<source-slug>-<UTC-timestamp>.md`
   - `<source-slug>`: lowercase, spaces→`-` (e.g. `google-calendar`).
   - `<UTC-timestamp>`: `YYYYMMDDTHHMMSSZ`.
   - File shape:
     ```markdown
     ---
     source: <source>
     timeframe: <e.g. last 24h ending 2026-06-01T18:00:00Z>
     generated_at: <UTC ISO timestamp>
     user: <email/name or "authenticated Glean user">
     ---

     # <source> digest

     - **<title>** — <link>
       who/why relevant: <e.g. tagged in thread / cc'd / invited to meeting>
       <1–2 line snippet>
     - ...

     <!-- if empty: -->
     _none found in this timeframe._
     ```

5. **Return** ONE line only: the path of the file written (the orchestrator needs it),
   e.g. `wrote memory/slack-20260601T180000Z.md (4 artifacts)`.
