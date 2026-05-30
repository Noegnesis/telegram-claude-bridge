"""Loop-closure-aware stale-alert tests (2026-05-29).

Root cause fixed here: `check_stale_inbox` was a pure age heuristic. The
steady-state inject path persists `<update_id>.txt` but never drains it
(only startup-drain and replay_queued do). So every answered message left
an orphan that aged past STALE_INBOX_SECONDS and fired a false
"⚠️ monitor unresponsive" alert — even though the monitor had replied.

Reproduced live 2026-05-29 12:44:53: stale alert for update_id 514106239
fired at the SAME second the monitor sent 3 outbound replies; the file was
still sitting in ~/.agents/inbox/.

Fix: a file is "answered" iff an outbound entry exists with ts >= the file's
arrival (mtime). Answered + stale files are drained to processed/ and never
alert. Genuinely-unanswered stale files still alert (true-wedge case).
"""
import json
import os
import time
from datetime import datetime

import bridge.main as main_mod


def _write_msgs(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_latest_outbound_epoch_returns_last_outbound(tmp_path, monkeypatch):
    """Returns the epoch of the most recent outbound entry, tolerating the
    nanosecond-precision ts that tg-send.sh writes via `date -Ins`."""
    msgs = tmp_path / "msgs.jsonl"
    _write_msgs(msgs, [
        {"ts": "2026-05-29T19:42:13.620064+00:00", "direction": "inbound", "text_len": 5},
        {"ts": "2026-05-29T19:43:27.731074616+00:00", "direction": "outbound", "text_len": 1},
        {"ts": "2026-05-29T19:44:53.371598114+00:00", "direction": "outbound", "text_len": 102},
        {"ts": "2026-05-29T19:45:00.000000+00:00", "direction": "inbound", "text_len": 9},
    ])
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", msgs)
    expected = datetime.fromisoformat("2026-05-29T19:44:53.371598+00:00").timestamp()
    assert main_mod._latest_outbound_epoch() == expected


def test_latest_outbound_epoch_none_when_no_outbound(tmp_path, monkeypatch):
    msgs = tmp_path / "msgs.jsonl"
    _write_msgs(msgs, [
        {"ts": "2026-05-29T19:42:13.620064+00:00", "direction": "inbound", "text_len": 5},
    ])
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", msgs)
    assert main_mod._latest_outbound_epoch() is None


def test_latest_outbound_epoch_none_when_log_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", tmp_path / "nope.jsonl")
    assert main_mod._latest_outbound_epoch() is None


def test_stale_inbox_drains_answered_file_without_alert(tmp_path, monkeypatch):
    """Outbound after arrival ⇒ monitor answered ⇒ drain, no false alert."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "514106239.txt"
    f.write_text("received_at: ...\nupdate_id: 514106239\n\nvoice memo body")
    now = time.time()
    arrived = now - 151
    os.utime(f, (arrived, arrived))

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "pane_is_claude_idle", lambda p: True)
    monkeypatch.setattr(main_mod, "pane_accepts_input", lambda p: True)
    sent = []
    monkeypatch.setattr(main_mod, "_send_alert", lambda text: sent.append(text) or True)

    alerted: set[str] = set()
    main_mod.check_stale_inbox(alerted, pane="agents:monitor", now=now,
                               latest_outbound=now - 90)  # reply 90s ago, after arrival

    assert sent == []
    assert not f.exists()
    assert (inbox / "processed" / "514106239.txt").exists()
    assert "514106239" not in alerted


def test_stale_inbox_drains_answered_file_even_if_already_alerted(tmp_path, monkeypatch):
    """If an alert already fired and THEN a reply went out, the file must still
    drain so a bridge restart (which clears the in-memory alerted set) does not
    re-fire on it — this is symptom ③ (unprompted 'monitor unresponsive')."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "777.txt"
    f.write_text("received_at: ...\nupdate_id: 777\n\nbody")
    now = time.time()
    arrived = now - 300
    os.utime(f, (arrived, arrived))

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "pane_is_claude_idle", lambda p: True)
    monkeypatch.setattr(main_mod, "pane_accepts_input", lambda p: True)
    sent = []
    monkeypatch.setattr(main_mod, "_send_alert", lambda text: sent.append(text) or True)

    alerted = {"777"}  # already alerted earlier
    main_mod.check_stale_inbox(alerted, pane="agents:monitor", now=now,
                               latest_outbound=now - 100)

    assert sent == []
    assert not f.exists()
    assert (inbox / "processed" / "777.txt").exists()


def test_stale_inbox_alerts_when_no_outbound_since_arrival(tmp_path, monkeypatch):
    """Last outbound predates this message's arrival ⇒ genuinely unanswered ⇒
    fire the real wedge alert and leave the file in place."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "9009.txt"
    f.write_text("received_at: ...\nupdate_id: 9009\n\nunanswered")
    now = time.time()
    arrived = now - 120
    os.utime(f, (arrived, arrived))

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "pane_is_claude_idle", lambda p: True)
    monkeypatch.setattr(main_mod, "pane_accepts_input", lambda p: True)
    sent = []
    monkeypatch.setattr(main_mod, "_send_alert", lambda text: sent.append(text) or True)

    alerted: set[str] = set()
    main_mod.check_stale_inbox(alerted, pane="agents:monitor", now=now,
                               latest_outbound=now - 200)  # last reply predates arrival

    assert sent == [main_mod.STALE_ALERT_TEXT]
    assert f.exists()
    assert "9009" in alerted


def test_stale_inbox_alerts_when_no_outbound_at_all(tmp_path, monkeypatch):
    """latest_outbound=None (no outbound ever) ⇒ unanswered ⇒ alert + keep."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "9010.txt"
    f.write_text("body")
    now = time.time()
    arrived = now - 120
    os.utime(f, (arrived, arrived))

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "pane_is_claude_idle", lambda p: True)
    monkeypatch.setattr(main_mod, "pane_accepts_input", lambda p: True)
    sent = []
    monkeypatch.setattr(main_mod, "_send_alert", lambda text: sent.append(text) or True)

    main_mod.check_stale_inbox(set(), pane="agents:monitor", now=now,
                               latest_outbound=None)

    assert sent == [main_mod.STALE_ALERT_TEXT]
    assert f.exists()
