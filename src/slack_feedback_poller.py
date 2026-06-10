"""Slack feedback poller — inbound half of the HITL loop (cron entrypoint).

agent-sentry DMs the user a briefing and records the message in briefings/.slack-sent.jsonl
(see slack_mcp_server.send_message). This script runs hourly, reads that sent-map, pulls the
reactions and thread replies on each recent briefing, and feeds them into the EXISTING feedback
pipeline via sentry_core.record_feedback() — which routes through the feedback-learner skill and
distills durable, per-skill preferences (Graphiti group `ppa` + memory/skill-preferences.md).

Correlation (request -> response -> skill): the Slack message `ts` is the join key. A reaction
sits on that `ts`; a thread reply's `thread_ts` equals it. The sent-map record carries the
`skill`, so the feedback lands in the right skill's preferences and changes its next run.

Rating resolution (confirmed design):
  - thumbsdown / -1 reaction          -> down
  - thumbsup / +1 reaction            -> up   (down wins if both present)
  - no reaction but a comment present -> down (treat a reply as a correction)
  - neither reaction nor comment      -> skip

Acknowledgement + dedup: once a briefing's feedback has been routed, the bot adds a ✅
(white_check_mark) reaction to the BRIEFING message. On later polls that checkmark — added by
THIS bot — is the primary "already handled, skip" signal. To submit NEW feedback on the same
briefing the user first REMOVES the ✅ and then adds the feedback; the next cycle sees no ack and
reprocesses. As a secondary safeguard the original mechanism is kept: a per-`ts` signature over
(reactions + comment) stored in briefings/.slack-processed.json, so identical feedback is not
reprocessed (e.g. if the ✅ is removed without changing the feedback). The bot's own checkmark is
stripped before rating and signature are computed, so the ack never perturbs either.

Run from cron:
    python src/slack_feedback_poller.py
Test without writing anything:
    python src/slack_feedback_poller.py --dry-run

Requires the bot token scopes `reactions:read`, `reactions:write` (to add the ack), and
`im:history` (in addition to the outbound ones).
"""

import os
import sys
import json
import hashlib
import asyncio
import argparse
import importlib
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Make sibling modules importable regardless of the cwd cron runs us from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sentry_core = importlib.import_module("sentry_core")
import slack_client  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SENT_MAP_PATH = PROJECT_ROOT / "briefings" / ".slack-sent.jsonl"
PROCESSED_PATH = PROJECT_ROOT / "briefings" / ".slack-processed.json"
# The engine returns the learner's agent transcript but the poller would otherwise discard it.
# Persisting it here makes a degraded run (e.g. the model gateway over budget) diagnosable after
# the fact instead of vanishing.
TRANSCRIPT_LOG_PATH = PROJECT_ROOT / "logs" / "briefings" / "feedback-transcripts.log"

# Only look back this far; bounds the Slack calls and still catches late reactions.
RETENTION_DAYS = int(os.environ.get("SLACK_FEEDBACK_RETENTION_DAYS", "3"))
# Attribute a non-threaded DM reply to the most-recent prior briefing. Fuzzy with several briefs
# pending, so OFF by default — threaded replies are the reliable path.
TOPLEVEL_FALLBACK = os.environ.get("SLACK_FEEDBACK_TOPLEVEL_FALLBACK", "0").strip() not in ("", "0", "false", "False")

# Reaction emoji -> rating. Slack stores names without colons (e.g. "thumbsup", "+1").
_DOWN_EMOJI = {"thumbsdown", "-1"}
_UP_EMOJI = {"thumbsup", "+1"}

# The bot stamps this on a briefing once its feedback has been routed: a visible "done" marker
# AND the primary skip signal on later polls. Needs the `reactions:write` scope to add.
_PROCESSED_EMOJI = "white_check_mark"

_BOT_USER_ID: str | None = None


def _bot_user_id() -> str | None:
    """The bot's own Slack user id (cached for the run), to tell our checkmark from anyone else's."""
    global _BOT_USER_ID
    if _BOT_USER_ID is None:
        try:
            _BOT_USER_ID = slack_client.call("auth.test").get("user_id") or ""
        except RuntimeError:
            _BOT_USER_ID = ""
    return _BOT_USER_ID or None


# --- sent-map + processed-ledger I/O -------------------------------------------------

def _load_sent_map() -> list[dict]:
    """Read .slack-sent.jsonl into a list of records, newest-last. Skips malformed lines.

    Collapses to the last record per `ts` (a resend would re-append) and keeps only entries
    within the retention window. A record with an unparseable `sent_at` is kept (better to
    process than silently drop).
    """
    if not SENT_MAP_PATH.is_file():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    by_ts: dict[str, dict] = {}
    for line in SENT_MAP_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = rec.get("ts")
        if not ts or not rec.get("channel") or not rec.get("briefing_path"):
            continue
        sent_at = rec.get("sent_at")
        if sent_at:
            try:
                if datetime.fromisoformat(sent_at) < cutoff:
                    continue
            except ValueError:
                pass
        by_ts[ts] = rec
    return list(by_ts.values())


def _load_processed() -> dict[str, str]:
    if not PROCESSED_PATH.is_file():
        return {}
    try:
        return json.loads(PROCESSED_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_processed(processed: dict[str, str]) -> None:
    PROCESSED_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_PATH.write_text(json.dumps(processed, indent=2), encoding="utf-8")


def _log_transcript(ts: str, skill: str, result) -> None:
    """Append the learner's transcript for one processed submission to a rolling log.

    Best-effort — a logging failure must never abort the poll. The transcript is the only window
    into WHY a run did or didn't learn (the engine discards it otherwise).
    """
    try:
        TRANSCRIPT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tx = (getattr(result, "transcript", "") or "").strip() or "(empty transcript)"
        header = f"\n===== ts={ts} skill={skill} briefing={getattr(result, 'briefing_path', None)} =====\n"
        with TRANSCRIPT_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(header + tx + "\n")
    except OSError:
        pass


# --- Slack reads ---------------------------------------------------------------------

def _reactions(channel: str, ts: str) -> tuple[set[str], bool]:
    """Reactions on the message at `ts` as (emoji names, already_processed).

    `already_processed` is True when OUR bot has added the processed checkmark — the primary
    "skip, already handled" signal. That checkmark is stripped from the returned names so it
    never affects rating resolution or the dedup signature (keeping the ledger a stable
    safeguard). Returns (empty set, False) if there are no reactions / the message is gone.
    """
    try:
        body = slack_client.call("reactions.get", params={"channel": channel, "timestamp": ts})
    except RuntimeError:
        return set(), False
    msg = body.get("message") or {}
    bot_id = _bot_user_id()
    names: set[str] = set()
    already_processed = False
    for r in msg.get("reactions") or []:
        name = r.get("name")
        if not name:
            continue
        if name == _PROCESSED_EMOJI and bot_id and bot_id in (r.get("users") or []):
            already_processed = True  # our own ack — don't count it as feedback
            continue
        names.add(name)
    return names, already_processed


def _mark_processed(channel: str, ts: str) -> None:
    """Stamp the briefing at `ts` with the processed checkmark. Best-effort; never aborts the poll.

    `already_reacted` is benign (we reached here again before the ledger caught it). A
    `missing_scope` error means the bot token lacks `reactions:write` — surfaced loudly so it
    gets fixed rather than silently leaving briefings unacknowledged.
    """
    try:
        slack_client.call(
            "reactions.add",
            json_body={"channel": channel, "timestamp": ts, "name": _PROCESSED_EMOJI},
        )
    except RuntimeError as exc:
        if "already_reacted" not in str(exc):
            print(f"[poll] ts={ts} -> could not add {_PROCESSED_EMOJI} ack: {exc}", file=sys.stderr)


def _thread_comment(channel: str, ts: str) -> str:
    """Join the user's thread replies under the briefing at `ts` into a single comment.

    Drops the parent (the briefing itself) and any bot-authored message, so only the human's
    replies survive. Capped at the same length the feedback path enforces elsewhere.
    """
    try:
        body = slack_client.call(
            "conversations.replies", params={"channel": channel, "ts": ts, "limit": 200}
        )
    except RuntimeError:
        return ""
    texts: list[str] = []
    for m in body.get("messages") or []:
        if m.get("ts") == ts:                                  # the briefing (thread parent)
            continue
        if m.get("bot_id") or m.get("subtype") == "bot_message":  # not a human reply
            continue
        text = (m.get("text") or "").strip()
        if text:
            texts.append(text)
    return "\n".join(texts)[: sentry_core.MAX_FEEDBACK_COMMENT_CHARS]


def _toplevel_comments(channel: str, brief_tss: list[str]) -> dict[str, str]:
    """Optional fallback: map non-threaded human DM messages to the most-recent prior briefing.

    Only used when SLACK_FEEDBACK_TOPLEVEL_FALLBACK is set. Returns {brief_ts: comment}. A
    top-level message (no thread_ts) authored by the user is attributed to the largest brief
    `ts` strictly less than the message `ts` (the brief it most plausibly responds to).
    """
    try:
        body = slack_client.call(
            "conversations.history", params={"channel": channel, "limit": 200}
        )
    except RuntimeError:
        return {}
    ordered = sorted(brief_tss, key=float)
    out: dict[str, list[str]] = {}
    for m in body.get("messages") or []:
        if m.get("bot_id") or m.get("subtype"):                # bot / system message
            continue
        thread_ts = m.get("thread_ts")
        if thread_ts and thread_ts != m.get("ts"):             # a threaded reply (handled elsewhere)
            continue
        msg_ts = m.get("ts")
        text = (m.get("text") or "").strip()
        if not msg_ts or not text:
            continue
        prior = [b for b in ordered if float(b) < float(msg_ts)]
        if not prior:
            continue
        out.setdefault(prior[-1], []).append(text)
    return {
        ts: "\n".join(parts)[: sentry_core.MAX_FEEDBACK_COMMENT_CHARS]
        for ts, parts in out.items()
    }


# --- rating / signature --------------------------------------------------------------

def _resolve_rating(reactions: set[str], comment: str) -> str | None:
    """down/up/None per the confirmed rule. None => skip (no signal)."""
    if reactions & _DOWN_EMOJI:
        return "down"
    if reactions & _UP_EMOJI:
        return "up"
    if comment:
        return "down"
    return None


def _signature(ts: str, reactions: set[str], comment: str) -> str:
    raw = f"{ts}|{','.join(sorted(reactions))}|{comment}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve_skill(record: dict, registry: dict) -> str | None:
    """target_skill from the record's `skill` (authoritative); else reverse-map the filename.

    Fallback: strip the `-<ts>.md` suffix from the briefing filename to get the briefing_name,
    then find the skill whose briefing_name matches. Returns None if neither resolves.
    """
    skill = (record.get("skill") or "").strip()
    if skill and skill in registry:
        return skill
    stem = Path(record["briefing_path"]).name
    if stem.endswith(".md"):
        stem = stem[:-3]
    briefing_name = stem.rsplit("-", 1)[0] if "-" in stem else stem
    for name, meta in registry.items():
        if meta.briefing_name == briefing_name:
            return name
    return None


# --- main ----------------------------------------------------------------------------

async def _amain(dry_run: bool) -> int:
    entries = _load_sent_map()
    if not entries:
        print("[poll] no sent-map entries within retention window; nothing to do.")
        return 0

    registry = sentry_core.discover_skills()
    processed = _load_processed()

    # Pre-compute the optional top-level fallback per channel.
    toplevel: dict[str, dict[str, str]] = {}
    if TOPLEVEL_FALLBACK:
        by_channel: dict[str, list[str]] = {}
        for rec in entries:
            by_channel.setdefault(rec["channel"], []).append(rec["ts"])
        for channel, tss in by_channel.items():
            toplevel[channel] = _toplevel_comments(channel, tss)

    submitted = 0
    for rec in entries:
        ts, channel, briefing_path = rec["ts"], rec["channel"], rec["briefing_path"]
        try:
            reactions, already_processed = _reactions(channel, ts)

            # Primary skip: this briefing already carries our ✓ (feedback handled). To submit new
            # feedback on it, the user removes the ✓ and then adds the feedback; the next cycle
            # then sees no ack and reprocesses.
            if already_processed:
                print(f"[poll] ts={ts} skill={rec.get('skill')} -> skip (already processed ✓)")
                continue

            comment = _thread_comment(channel, ts)
            if not comment and TOPLEVEL_FALLBACK:
                comment = toplevel.get(channel, {}).get(ts, "")

            rating = _resolve_rating(reactions, comment)
            if rating is None:
                print(f"[poll] ts={ts} skill={rec.get('skill')} -> skip (no reaction, no comment)")
                continue

            # Safeguard skip: dedup ledger — also stops a reprocess when the ✓ is removed but the
            # feedback is unchanged.
            signature = _signature(ts, reactions, comment)
            if processed.get(ts) == signature:
                print(f"[poll] ts={ts} skill={rec.get('skill')} -> skip (unchanged)")
                continue

            target_skill = _resolve_skill(rec, registry)
            if not target_skill:
                print(f"[poll] ts={ts} -> skip (could not resolve skill for {briefing_path})")
                continue

            print(
                f"[poll] ts={ts} skill={target_skill} rating={rating} "
                f"comment={'yes' if comment else 'no'} -> {'DRY-RUN' if dry_run else 'submit'}"
            )
            result = await sentry_core.record_feedback(
                target_skill=target_skill,
                briefing_path=briefing_path,
                rating=rating,
                comment=comment or None,
                dry_run=dry_run,
            )
            # record_feedback RAISES if the learner produced no receipt (a failed/degraded run),
            # so reaching here means the feedback was actually learned. Capture the transcript,
            # then mark processed — a run that didn't learn never gets the ✓ and is retried.
            submitted += 1
            _log_transcript(ts, target_skill, result)
            if not dry_run:
                processed[ts] = signature
                _save_processed(processed)  # persist incrementally so a crash mid-run is safe
                _mark_processed(channel, ts)  # ✓ on the briefing; remove it to re-submit feedback
        except sentry_core.SentryError as exc:
            print(f"[poll] ts={ts} -> error: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not abort the whole poll
            print(f"[poll] ts={ts} -> unexpected error: {exc}", file=sys.stderr)

    print(f"[poll] done: {submitted} feedback submission(s){' (dry-run)' if dry_run else ''}.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="slack-feedback-poller",
        description="Pull Slack reactions/replies on relayed briefings into feedback-learner.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Resolve + route feedback but skip Graphiti/file writes and the processed ledger.",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_amain(args.dry_run)))
    except sentry_core.SentryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
