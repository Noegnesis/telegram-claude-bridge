#!/bin/bash
# Idempotent monitor starter. Spawns tmux:agents:monitor only if it doesn't
# already exist. Helper — normally invoked by start-bridge.sh, not directly.
set -euo pipefail

if tmux has-session -t agents 2>/dev/null; then
  exit 0
fi

tmux new -d -s agents -n monitor "$HOME/agents/scripts/monitor-loop.sh"
tmux set-option -t agents remain-on-exit on
