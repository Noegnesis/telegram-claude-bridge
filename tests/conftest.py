import os
import subprocess as _subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _isolate_inbox(tmp_path, monkeypatch):
    """Isolate every test from the real monitor's runtime files and network.

    Three guards, all autouse so no test can leak (tests that monkeypatch any
    of these themselves still win — monkeypatch is last-write-wins):

    1. INBOX_DIR → per-test tmp_path, so fixtures never touch ~/.agents/inbox/.
    2. MESSAGES_PATH → per-test tmp_path, so `_log_message` never appends to the
       real ~/agents/logs/bridge-messages.jsonl. (2026-05-29: discovered 144
       leaked `update_id: 100` lines polluting the prod log + corrupting the
       new loop-closure reconciliation, which reads that log for outbound ts.)
    3. tg-send.sh / tg-typing.sh shell-outs → no-op. The unsupported-type ack
       path calls tg-send.sh directly (not via _send_alert), so a sticker test
       was sending a REAL Telegram message to the paired phone on every run.
       Neutralize by basename so all other subprocess calls (tmux, etc.) pass
       through untouched.
    """
    import bridge.main as main_mod
    monkeypatch.setattr(main_mod, "INBOX_DIR", tmp_path / "_default_inbox")
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", tmp_path / "_default_msgs.jsonl")

    _real_run = main_mod.subprocess.run

    def _no_telegram(cmd, *args, **kwargs):
        first = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
        if os.path.basename(str(first)) in ("tg-send.sh", "tg-typing.sh"):
            return _subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return _real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(main_mod.subprocess, "run", _no_telegram)
