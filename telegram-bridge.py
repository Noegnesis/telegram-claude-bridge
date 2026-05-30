#!/usr/bin/env python3
"""Telegram bridge entry point.

Reads token + paired user from ~/.claude/channels/telegram/.env (or .env.test
when BRIDGE_ENV=test). Runs forever; caller (bridge-loop.sh) handles restarts.

Env vars:
    BRIDGE_ENV     "prod" (default) | "test"  - selects .env vs .env.test
    BRIDGE_PANE    target tmux pane (default: "agents:monitor")
"""
import logging
import os
import sys
from pathlib import Path

from bridge.main import main


def _read_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


if __name__ == "__main__":
    log_dir = Path.home() / "agents" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "bridge-errors.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    env_name = os.environ.get("BRIDGE_ENV", "prod")
    env_file = ".env" if env_name == "prod" else ".env.test"
    env_path = Path.home() / ".claude" / "channels" / "telegram" / env_file
    env = _read_env(env_path)

    pane = os.environ.get("BRIDGE_PANE", "agents:monitor")
    state_path = Path.home() / ".agents" / "bridge" / f"state-{env_name}.json"

    main(
        token=env["TELEGRAM_BOT_TOKEN"],
        paired_user_id=int(env["PAIRED_USER_ID"]),
        pane=pane,
        state_path=state_path,
    )
