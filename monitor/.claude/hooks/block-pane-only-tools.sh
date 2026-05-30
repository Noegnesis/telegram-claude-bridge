#!/usr/bin/env bash
# Block pane-only UI tools in the monitor context. Telegram users cannot
# see AskUserQuestion menus, plan-mode UI, or other terminal-rendered widgets.
# Redirect the model to tg-send.sh for any user-facing question.
#
# Claude Code PreToolUse hook contract:
#   stdin  = JSON {tool_name, tool_input, ...}
#   stdout = JSON {decision: "block"|"approve"|"ask", reason: "..."}
#   exit 0 with no body = pass-through allow
set -euo pipefail

TOOL_NAME=$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null || echo "")

case "$TOOL_NAME" in
  AskUserQuestion|EnterPlanMode|ExitPlanMode)
    REASON="Tool '${TOOL_NAME}' is BANNED in the monitor context. It renders to the local tmux pane only and is invisible to the Telegram user — the only audience that matters here. For ANY user-facing question or clarification, use the Bash tool to call: ~/agents/tools/tg-send.sh \"your one-line question with options inline, e.g. 'Deploy to prod? Y / N'\" — then yield the turn. The user's reply arrives as the next inbound Telegram message, which the bridge will inject into your pane like any other prompt. Do NOT retry this tool; it will be blocked again."
    python3 -c "import json,sys; print(json.dumps({'decision': 'block', 'reason': sys.argv[1]}))" "$REASON"
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
