#!/usr/bin/env bash
# Runs the candidate browser flow against a fresh local server, the same way CI does.
# Server start, test and shutdown happen in one process tree so nothing outlives the test.
set -euo pipefail

PORT="${PORT:-8765}"
BASE="http://127.0.0.1:${PORT}"
LOG="${LOG:-server.log}"
export DJANGO_DEBUG=1 CODE_RUNNER=local

SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
on_error() {
  echo "::group::Server log (last 200 lines)"
  tail -200 "$LOG" 2>/dev/null || true
  echo "::endgroup::"
}
trap cleanup EXIT
trap on_error ERR

python manage.py migrate --noinput
python manage.py seed_demo | tee seed.log
EXAM_ID="$(grep -oP 'Demo test #\K[0-9]+' seed.log | head -1)"
if [ -z "$EXAM_ID" ]; then
  echo "Could not find the demo exam id in seed output." >&2
  exit 1
fi

python manage.py runserver "127.0.0.1:${PORT}" --noreload > "$LOG" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 30); do
  if curl -sf "$BASE/" > /dev/null; then break; fi
  sleep 1
done
curl -sf "$BASE/" > /dev/null

python e2e/smoke_test.py --base "$BASE" --exam-id "$EXAM_ID"
