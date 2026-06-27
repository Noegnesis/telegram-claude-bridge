#!/usr/bin/env bash
# Stop hook (monitor context): backstop for the "processed a Telegram message
# but never shelled out to tg-send.sh" black-hole.
#
# v2 (2026-06-27): instead of a generic "re-send it" fallback, RELAY the
# monitor's actual last in-pane reply (extracted from the Stop hook's
# transcript_path) so the real answer reaches the phone. The generic text is now
# only the last-resort fallback when no assistant text can be extracted.
#
# Claude Code Stop hook contract:
#   stdin = JSON session info; we read transcript_path from it.
#   exit 0 = allow the stop (we NEVER block — no decision body emitted).
# Self-limiting: whatever we send is itself a substantive outbound (text_len>2),
# so the next Stop sees an outbound after the inbound and won't re-fire.
set -euo pipefail

LOG="$HOME/agents/logs/bridge-messages.jsonl"
[ -f "$LOG" ] || exit 0

RECENT_WINDOW=600            # only rescue an unanswered inbound from the last 10 minutes
STOP_JSON="$(cat || true)"   # Claude Code pipes the Stop event JSON on stdin

STOP_JSON="$STOP_JSON" python3 - "$LOG" "$RECENT_WINDOW" <<'PY' || true
import json, os, re, glob, subprocess, sys, time
from datetime import datetime

log, window = sys.argv[1], float(sys.argv[2])
ACK_MAX_LEN = 2     # outbound this short = a non-answer ack (eyes), not a reply
MAX_RELAY = 3000    # cap a relayed pane-reply to one tg-send chunk

def epoch(ts):
    if not ts:
        return None
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts)   # tg-send logs ns; fromisoformat wants <=6
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
        # Trivial acks (eyes emoji, text_len=1) are outbound but don't answer the
        # user; mirrors main.py ACK_MAX_LEN so one emoji can't suppress the relay.
        if d.get("text_len", ACK_MAX_LEN + 1) > ACK_MAX_LEN:
            last_out = ts

ein, eout = epoch(last_in), epoch(last_out)
if ein is None:
    sys.exit(0)                       # no inbound ever — nothing to rescue
if eout is not None and eout >= ein:
    sys.exit(0)                       # substantive outbound after inbound — loop closed
if time.time() - ein > window:
    sys.exit(0)                       # stale; don't fire during unrelated work

def last_assistant_text():
    """The monitor's most recent in-pane assistant text, from the transcript."""
    tpath = None
    try:
        tpath = json.loads(os.environ.get("STOP_JSON", "")).get("transcript_path")
    except Exception:
        tpath = None
    if not tpath or not os.path.exists(tpath):
        cands = sorted(
            glob.glob(os.path.expanduser("~/.claude/projects/*monitor*/*.jsonl")),
            key=os.path.getmtime, reverse=True)
        tpath = cands[0] if cands else None
    if not tpath or not os.path.exists(tpath):
        return None
    text = None
    for ln in open(tpath, errors="replace"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        m = d.get("message")
        if d.get("type") == "assistant" and isinstance(m, dict):
            c = m.get("content")
            if isinstance(c, list):
                parts = [b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
                if parts:
                    text = "\n".join(parts).strip()   # keep overwriting → last assistant wins
    return text or None

GENERIC = ("⚠️ (auto) I finished a turn after your last message but didn't "
           "send a reply here — I may have answered only in the local pane. "
           "Re-send if you still need it.")

body = last_assistant_text()
if body:
    if len(body) > MAX_RELAY:
        body = body[:MAX_RELAY].rstrip() + "\n\n… (truncated; full reply in my pane)"
    msg, mode = body, "relay"
else:
    msg, mode = GENERIC, "generic"

tg = os.path.expanduser("~/agents/tools/tg-send.sh")
try:
    subprocess.run([tg, msg], timeout=15, check=False)
except Exception:
    pass

# Forensic trail: records when the monitor forgot tg-send and the backstop fired,
# and whether we relayed the real reply or fell back to generic.
try:
    with open(os.path.expanduser("~/agents/logs/ensure-outbound.log"), "a") as f:
        f.write(f"{datetime.now().isoformat()} fired mode={mode} len={len(msg)}\n")
except Exception:
    pass
PY
exit 0
