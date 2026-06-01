---
name: user-profile
description: Curate DURABLE user-profile facts from any user-related input (digest files, a chat response, an email thread, etc.) — extract slow-changing facts, dedup/merge against what is already stored, then persist to BOTH Graphiti (group_id ppa, episode "profile: <user>") AND memory/user-profile.md. Generic and reusable by any agent that processes user responses, not just mem-writer. Trigger when asked to update/curate the user profile or extract durable facts about the user.
---

# user-profile

Maintain a durable profile of the user. This skill is **generic** — it curates the profile
from **whatever input the caller names** (one or more digest files, a chat transcript, an
email thread, raw text). It does not gather data itself; the caller supplies the input.

## Inputs (from the task prompt)

- `inputs` — file paths and/or text to read for profile-worthy facts.
- `user` — the user's name/email (used as the episode subject); default to the
  authenticated identity if unspecified.

## Procedure

1. **Read the inputs** named in the task (`Read` each file / consume the provided text).

2. **Extract only DURABLE, slow-changing facts** about the user:
   - role / title, team / org
   - recurring collaborators (people they repeatedly work with) + relationship
   - active projects / initiatives
   - recurring meetings / cadences
   - topics / areas of focus / interests
   - stable working preferences
   **Skip ephemeral day-to-day items** — those belong in the daily `ppa` digests, not the profile.

3. **Dedup / merge against what is already stored.** Before writing:
   - Query Graphiti: `mcp__graphiti__search_nodes` and/or `mcp__graphiti__get_episodes`
     with `group_ids=["ppa"]`, looking for the existing `profile: <user>` episode and
     related person/topic nodes.
   - Read `memory/user-profile.md` if present.
   - Merge: **update** changed facts, **add** new ones, **drop** nothing silently, and
     **never blind-append duplicates**. No fabrication — only facts grounded in the inputs
     or already stored.

4. **Persist to BOTH stores:**
   - **Graphiti** — `mcp__graphiti__add_memory` once:
     - `name`: `"profile: <user>"`
     - `source`: `"json"`
     - `source_description`: `"user-profile skill"`
     - `group_id`: `"ppa"`  (shared group so profile entities link to digest people)
     - `episode_body`: the merged fact set as a JSON **string**, shaped:
       ```json
       {
         "user": "<name/email>",
         "role": "<title|unknown>",
         "team": "<team|unknown>",
         "collaborators": [{"name": "<name>", "relationship": "<e.g. manager, teammate>"}],
         "projects": ["<...>"],
         "recurring_meetings": ["<...>"],
         "topics": ["<...>"],
         "preferences": ["<...>"]
       }
       ```
   - **`memory/user-profile.md`** — rewrite the canonical snapshot (grouped sections:
     Role, Team, Collaborators, Projects, Recurring meetings, Topics, Preferences;
     plus a `_last updated: <UTC timestamp>_` line). This is the clean standalone read.

5. **Return** a short summary of what changed: counts of facts **added / updated / unchanged**.
