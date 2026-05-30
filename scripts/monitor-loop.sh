#!/bin/bash
# Self-healing monitor loop. Restarts claude on any exit with 5s backoff.
# Started by start-monitor.sh inside tmux:agents:monitor.
LOGFILE="$HOME/agents/logs/monitor-restart.log"
mkdir -p "$(dirname "$LOGFILE")"
cd "$HOME/agents/monitor" || exit 1

while true; do
  echo "[$(date -Iseconds)] starting claude" >> "$LOGFILE"
  claude
  rc=$?
  echo "[$(date -Iseconds)] claude exited rc=$rc; restarting in 5s" >> "$LOGFILE"
  sleep 5
done
