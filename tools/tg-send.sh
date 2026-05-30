#!/usr/bin/env bash
# Send a message to the paired Telegram user.
# Usage: tg-send.sh "text"   OR   echo "text" | tg-send.sh
# Reads token + chat from ~/.claude/channels/telegram/.env (or .env.test if BRIDGE_ENV=test).
# Chunks messages at 4000 chars (Telegram cap is 4096), retries once on 5xx.

set -euo pipefail

# Resolve python3 with httpx: prefer bridge venv (which has httpx pinned),
# fall back to whatever python3 is on PATH (runtime will fail if httpx absent).
BRIDGE_VENV="${HOME}/agents/bridge/.venv"
if [ -x "${BRIDGE_VENV}/bin/python" ]; then
  PYTHON="${BRIDGE_VENV}/bin/python"
else
  PYTHON="python3"
fi

ENV_NAME="${BRIDGE_ENV:-prod}"
if [ "$ENV_NAME" = "test" ]; then
  source ~/.claude/channels/telegram/.env.test
else
  source ~/.claude/channels/telegram/.env
fi

text="${1:-$(cat)}"

TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" "${PYTHON}" - "$text" "$PAIRED_USER_ID" <<'PY'
import os, sys, time
import httpx

text, chat_id = sys.argv[1], sys.argv[2]
token = os.environ["TELEGRAM_BOT_TOKEN"]
url = f"https://api.telegram.org/bot{token}/sendMessage"

def _redact(msg: str) -> str:
    return msg.replace(token, "<REDACTED>")

for i in range(0, len(text), 4000):
    chunk = text[i : i + 4000]
    for attempt in (1, 2):
        r = httpx.post(
            url,
            json={"chat_id": int(chat_id), "text": chunk},
            timeout=15,
        )
        if r.status_code < 500:
            break
        time.sleep(2)
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise SystemExit(f"Telegram API error: {_redact(str(e))}") from None
PY

# Loop-closure audit: log successful outbound to bridge-messages.jsonl.
# Body and token are never logged — only length, chat_id, env.
# set -euo pipefail means we only reach this line if the python send above succeeded.
LOG_FILE="${HOME}/agents/logs/bridge-messages.jsonl"
printf '{"ts": "%s", "direction": "outbound", "text_len": %d, "chat_id": %s, "env": "%s"}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%S.%6N+00:00)" \
  "${#text}" \
  "$PAIRED_USER_ID" \
  "$ENV_NAME" \
  >> "$LOG_FILE" 2>/dev/null || true
