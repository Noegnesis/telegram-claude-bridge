#!/bin/bash
# Single idempotent "ensure the agents stack is up" entrypoint.
# Guarantees tmux:agents session + `monitor` window + `bridge` window.
# Called by BOTH scheduled tasks (every 2 min) and manual revival.
#
# Mutual exclusion uses an atomic mkdir lock (NOT flock): an flock fd is
# inherited by the tmux server when start-monitor first spawns it, pinning the
# lock for the server's whole life and breaking all future recovery. A lock
# DIRECTORY has no fd to inherit; it is removed by the EXIT trap, with a
# PID-liveness check to steal a lock left stale by a SIGKILL.
set -uo pipefail

LOGFILE="$HOME/agents/logs/start-bridge.log"
LOCKDIR=/tmp/agents-ensure.lock.d
mkdir -p "$(dirname "$LOGFILE")"

if [ -f "$LOGFILE" ] && [ "$(stat -c %s "$LOGFILE" 2>/dev/null || echo 0)" -gt 524288 ]; then
  tail -c 131072 "$LOGFILE" > "$LOGFILE.tmp" 2>/dev/null && mv "$LOGFILE.tmp" "$LOGFILE"
fi
log() { echo "[$(date -Iseconds)] $*" >> "$LOGFILE"; }

if ! mkdir "$LOCKDIR" 2>/dev/null; then
  opid=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")
  if [ -n "$opid" ] && kill -0 "$opid" 2>/dev/null; then
    log "ensure already in progress (holder=$opid) - skip"
    exit 0
  fi
  log "stale lock (holder=${opid:-none} not alive) - stealing"
  rm -rf "$LOCKDIR"
  mkdir "$LOCKDIR" 2>/dev/null || { log "lost steal race - skip"; exit 0; }
fi
echo "$$" > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR" 2>/dev/null' EXIT

created=0
if ! tmux has-session -t agents 2>/dev/null; then
  if "$HOME/agents/scripts/start-monitor.sh"; then
    log "created agents session + monitor window"; created=1
  else
    log "ERROR: start-monitor.sh failed (rc=$?)"
  fi
fi

if ! tmux list-windows -t agents -F '#{window_name}' 2>/dev/null | grep -q '^bridge$'; then
  if tmux new-window -t agents -n bridge -c "$HOME/agents/bridge" \
        "$HOME/agents/bridge/bridge-loop.sh"; then
    tmux set-window-option -t agents:bridge remain-on-exit on
    log "created bridge window"; created=1
  else
    log "ERROR: failed to create bridge window (rc=$?)"
  fi
fi

[ "$created" -eq 1 ] && log "ensure complete (recovered missing components)"
exit 0
