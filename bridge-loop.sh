#!/bin/bash
# Self-healing bridge loop. Restarts telegram-bridge.py on any exit with 5s backoff.
# Started by start-bridge.sh inside tmux:agents:bridge.
#
# Watchdog (added after the silent-hang incident): if the bridge's heartbeat
# log file ages past WATCHDOG_THRESHOLD seconds, SIGKILL the python process so
# the outer loop restarts it. Without this, an httpx long-poll that hangs
# without raising will pin the wrapper in `kill -0` forever.

set -u

LOGFILE="$HOME/agents/logs/bridge-restart.log"
HEARTBEAT_FILE="$HOME/agents/logs/bridge-heartbeat.log"
WATCHDOG_THRESHOLD=120   # heartbeat fires every 30s in main loop's finally
WATCHDOG_GRACE=120       # no watchdog during cold-start window
WATCHDOG_POLL=30         # gap between heartbeat-age checks
mkdir -p "$(dirname "$LOGFILE")"
cd "$HOME/agents/bridge" || exit 1

if [[ ! -f .venv/bin/activate ]]; then
  echo "[$(date -Iseconds)] FATAL: .venv/bin/activate missing in $(pwd); aborting" >> "$LOGFILE"
  exit 1
fi
source .venv/bin/activate

while true; do
  echo "[$(date -Iseconds)] starting telegram-bridge.py (BRIDGE_ENV=${BRIDGE_ENV:-prod})" >> "$LOGFILE"
  python3 telegram-bridge.py &
  PYTHON_PID=$!
  START_TIME=$(date +%s)
  while kill -0 "$PYTHON_PID" 2>/dev/null; do
    sleep "$WATCHDOG_POLL"
    NOW=$(date +%s)
    if [ $((NOW - START_TIME)) -lt "$WATCHDOG_GRACE" ]; then
      continue
    fi
    if [ ! -f "$HEARTBEAT_FILE" ]; then
      echo "[$(date -Iseconds)] WATCHDOG: heartbeat file missing past grace; SIGKILL python pid=$PYTHON_PID" >> "$LOGFILE"
      kill -9 "$PYTHON_PID"
      break
    fi
    AGE=$((NOW - $(stat -c %Y "$HEARTBEAT_FILE")))
    if [ "$AGE" -gt "$WATCHDOG_THRESHOLD" ]; then
      echo "[$(date -Iseconds)] WATCHDOG: heartbeat stale (${AGE}s > ${WATCHDOG_THRESHOLD}s); SIGKILL python pid=$PYTHON_PID" >> "$LOGFILE"
      kill -9 "$PYTHON_PID"
      break
    fi
  done
  wait "$PYTHON_PID" 2>/dev/null
  rc=$?
  echo "[$(date -Iseconds)] telegram-bridge.py exited rc=$rc; restarting in 5s" >> "$LOGFILE"
  sleep 5
done
