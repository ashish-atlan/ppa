#!/usr/bin/env bash
# Stop the servers started by start-ui.sh (reads PIDs from .ui.pids).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$ROOT/.ui.pids"

if [[ ! -f "$PIDFILE" ]]; then
  echo "Nothing to stop (no $PIDFILE)." >&2
  exit 0
fi

while IFS= read -r pid; do
  [[ -z "$pid" ]] && continue
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    echo "Stopped pid $pid."
  else
    echo "pid $pid already gone."
  fi
done <"$PIDFILE"

rm -f "$PIDFILE"
# Remove the generated config so the embedded SENTRY_API_KEY does not linger on disk.
rm -f "$ROOT/frontend/config.js"
echo "Done."
