#!/usr/bin/env bash
# Send 'typing...' indicator to the paired user. No-op on error.
# Telegram shows the indicator for ~5s, so re-run for longer operations.
set -euo pipefail

ENV_NAME="${BRIDGE_ENV:-prod}"
if [ "$ENV_NAME" = "test" ]; then
  source ~/.claude/channels/telegram/.env.test
else
  source ~/.claude/channels/telegram/.env
fi

curl -s -o /dev/null \
  -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendChatAction" \
  -d "chat_id=${PAIRED_USER_ID}" \
  -d "action=typing" || true
