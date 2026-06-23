#!/usr/bin/env bash
# Stop hook (monitor context): backstop for the "processed a Telegram message
# but never shelled out to tg-send.sh" black-hole.
#
# The PreToolUse ban (block-pane-only-tools.sh) only covers AskUserQuestion /
# plan-mode. A plain pane-only prose reply is NOT blocked — only discouraged by
# CLAUDE.md. This hook is the deterministic backstop: at turn end, if the most
# recent inbound has NO outbound after it AND it arrived recently, auto-send a
# fallback so the remote user is never silently dropped.
#
# Claude Code Stop hook contract:
#   stdin  = JSON session info (ignored here)
#   exit 0 = allow the stop (we NEVER block — no decision body emitted)
# Self-limiting: the fallback we send is itself an outbound, so the next Stop
# sees it after the inbound and won't re-fire.
set -euo pipefail

LOG="$HOME/agents/logs/bridge-messages.jsonl"
[ -f "$LOG" ] || exit 0

RECENT_WINDOW=600   # only rescue an unanswered inbound from the last 10 minutes

python3 - "$LOG" "$RECENT_WINDOW" <<'PY' || true
import json, os, re, subprocess, sys, time
from datetime import datetime

log, window = sys.argv[1], float(sys.argv[2])
ACK_MAX_LEN = 2  # outbound this short = a non-answer ack (👀), not a reply

def epoch(ts):
    if not ts:
        return None
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts)  # tg-send.sh logs ns; fromisoformat wants <=6
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None

last_in = last_out = None
for ln in open(log, errors="replace"):
    ln = ln.strip()
    if not ln:
        continue
    try:
        d = json.loads(ln)
    except json.JSONDecodeError:
        continue
    dirn, ts = d.get("direction"), d.get("ts")
    if dirn == "inbound":
        last_in = ts
    elif dirn == "outbound":
        # Trivial acks (👀, text_len=1) are outbound but don't answer the user;
        # mirrors main.py ACK_MAX_LEN so one emoji can't suppress the fallback.
        if d.get("text_len", ACK_MAX_LEN + 1) > ACK_MAX_LEN:
            last_out = ts

ein, eout = epoch(last_in), epoch(last_out)
if ein is None:
    sys.exit(0)                       # no inbound ever — nothing to rescue
if eout is not None and eout >= ein:
    sys.exit(0)                       # outbound after the inbound — loop closed
if time.time() - ein > window:
    sys.exit(0)                       # stale; don't fire during unrelated work

msg = ("⚠️ (auto) I finished a turn after your last message but didn't "
       "send a reply here — I may have answered only in the local pane. "
       "Re-send if you still need it.")
tg = os.path.expanduser("~/agents/tools/tg-send.sh")
try:
    subprocess.run([tg, msg], timeout=10, check=False)
except Exception:
    pass
PY
exit 0
