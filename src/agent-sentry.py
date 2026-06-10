"""agent-sentry CLI / cron entrypoint.

A thin wrapper over `sentry_core.run_briefing`. The generic briefing agent: loads context
from Graphiti group `ppa`, auto-selects a skill from the prompt (every skill in
`.claude/skills/` is loaded — no routing code), runs it, writes a briefing under
`briefings/`, relays it per the skill's frontmatter, and optionally locks it into Graphiti.

Run interactively:
    python src/agent-sentry.py --prompt "give me today's exec briefing"

Run from cron (deterministic — name the skill, machine output, non-interactive):
    python src/agent-sentry.py --skill exec-daily-briefing --json

The HTTP API for the UI lives in `sentry_api.py`; both share `sentry_core`.
"""

import os
import sys
import json
import asyncio
import argparse
import importlib

# Make the sibling module importable regardless of the cwd cron runs us from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# This file's name has a hyphen, so it is only ever run as a script, never imported. Import
# the shared engine by its (underscore) module name.
sentry_core = importlib.import_module("sentry_core")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="agent-sentry",
        description="Generic skill-driven briefing agent (CLI / cron).",
    )
    p.add_argument("--prompt", help="Free-form request; the skill is auto-selected from it.")
    p.add_argument(
        "--skill",
        help="Optional skill name. Prepended as a hint ('Use the <skill> skill.'); "
        "preferred for cron because it is deterministic.",
    )
    p.add_argument(
        "--timeframe-hours", type=int, default=None,
        help="Optional lookback window passed to the skill.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit the result envelope as JSON on stdout (for programmatic callers).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Write the briefing but skip relay + Graphiti (testing).",
    )
    return p.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    result = await sentry_core.run_briefing(
        prompt=args.prompt,
        skill=args.skill,
        timeframe_hours=args.timeframe_hours,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps({
            "skill": result.skill,
            "briefing_path": result.briefing_path,
            "content": result.content,
            "relayed": result.relayed,
            "graphiti_locked": result.graphiti_locked,
            "violations": result.violations,
        }, indent=2))
    else:
        print(result.transcript)
        print(
            f"\n[skill={result.skill} path={result.briefing_path} "
            f"relayed={','.join(result.relayed) or 'none'} "
            f"graphiti_locked={result.graphiti_locked}]"
        )
        if result.violations:
            print(f"[guardrails: {'; '.join(result.violations)}]")
    # No briefing path usually means the run did not complete a skill.
    return 0 if result.briefing_path else 1


def main() -> None:
    args = _parse_args(sys.argv[1:])
    if not args.prompt and not args.skill:
        print("error: provide --prompt and/or --skill", file=sys.stderr)
        raise SystemExit(2)
    try:
        raise SystemExit(asyncio.run(_amain(args)))
    except sentry_core.SentryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
