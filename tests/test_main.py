import os
import time

import httpx
import respx
from unittest.mock import MagicMock, patch

from bridge.main import process_update
from bridge.state import State


def test_process_update_authorized_text_injects_and_advances_offset(tmp_path):
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42,
                  policy="allowlist")
    update = {"update_id": 100,
              "message": {"from": {"id": 42}, "text": "hello"}}
    client = MagicMock()
    inject_fn = MagicMock()
    transcribe = MagicMock()

    process_update(update, state, client, inject_fn=inject_fn,
                   transcribe_fn=transcribe, pane="agents:monitor")

    inject_fn.assert_called_once_with("hello", pane="agents:monitor")
    assert state.offset == 101


def test_process_update_unauthorized_does_not_inject(tmp_path):
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42,
                  policy="allowlist")
    update = {"update_id": 100,
              "message": {"from": {"id": 99}, "text": "intruder"}}
    inject_fn = MagicMock()

    process_update(update, state, MagicMock(), inject_fn=inject_fn,
                   transcribe_fn=MagicMock(), pane="agents:monitor")

    inject_fn.assert_not_called()
    assert state.offset == 101


def test_process_update_unsupported_type_advances_offset_without_inject(tmp_path):
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42,
                  policy="allowlist")
    update = {"update_id": 100,
              "message": {"from": {"id": 42}, "sticker": {}}}
    inject_fn = MagicMock()

    process_update(update, state, MagicMock(), inject_fn=inject_fn,
                   transcribe_fn=MagicMock(), pane="agents:monitor")

    inject_fn.assert_not_called()
    assert state.offset == 101


def test_process_update_inject_failure_advances_offset_and_logs(tmp_path, monkeypatch):
    import bridge.main as main_mod
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42,
                  policy="allowlist")
    update = {"update_id": 200,
              "message": {"from": {"id": 42}, "text": "tmux gone"}}

    def failing_inject(text, pane):
        raise RuntimeError("pane vanished")

    messages_log = tmp_path / "bridge-messages.jsonl"
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", messages_log)

    # Should NOT raise — inject failure is caught internally
    process_update(update, state, MagicMock(),
                   inject_fn=failing_inject, transcribe_fn=MagicMock(),
                   pane="agents:monitor")

    # Offset advanced despite inject failure
    assert state.offset == 201
    # Log entry recorded the failure
    content = messages_log.read_text()
    assert '"direction": "inject_failed"' in content
    assert '"error": "pane vanished"' in content


def test_process_update_extract_error_advances_offset_and_logs(tmp_path, monkeypatch):
    import bridge.main as main_mod
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42, policy="allowlist")
    update = {"update_id": 400, "message": {"from": {"id": 42}, "voice": {"file_id": "BAD"}}}

    messages_log = tmp_path / "bridge-messages.jsonl"
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", messages_log)

    # Mock client that raises on get_file_url
    failing_client = MagicMock()
    failing_client.get_file_url.side_effect = RuntimeError("download failed")

    # Should NOT raise
    process_update(update, state, failing_client, inject_fn=MagicMock(),
                   transcribe_fn=MagicMock(), pane="agents:monitor")

    assert state.offset == 401
    content = messages_log.read_text()
    assert '"direction": "error"' in content
    assert '"error": "download failed"' in content


def test_process_update_logs_inbound_after_save(tmp_path, monkeypatch):
    import bridge.main as main_mod
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42,
                  policy="allowlist")
    update = {"update_id": 300,
              "message": {"from": {"id": 42}, "text": "ok"}}

    messages_log = tmp_path / "bridge-messages.jsonl"
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", messages_log)

    process_update(update, state, MagicMock(),
                   inject_fn=MagicMock(), transcribe_fn=MagicMock(),
                   pane="agents:monitor")

    content = messages_log.read_text()
    assert '"direction": "inbound"' in content
    assert state.offset == 301


def test_wait_for_pane_requires_idle_by_default(monkeypatch):
    """When require_idle=True (default), wait_for_pane polls pane_is_safe_to_inject until True.

    Updated 2026-05-22: wait_for_pane now uses the stricter `pane_is_safe_to_inject`
    (which rejects permission menus + Y/n prompts) rather than `pane_is_claude_idle`.
    """
    import bridge.main as main_mod

    monkeypatch.setattr(main_mod, "has_session", lambda s: True)
    monkeypatch.setattr(main_mod, "pane_accepts_input", lambda p: True)

    calls = {"n": 0}
    def fake_safe(pane):
        calls["n"] += 1
        return calls["n"] >= 3
    monkeypatch.setattr(main_mod, "pane_is_safe_to_inject", fake_safe)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    result = main_mod.wait_for_pane("agents:monitor", timeout=10)
    assert result is True
    assert calls["n"] == 3


def test_wait_for_pane_can_skip_idle_check(monkeypatch):
    """require_idle=False reverts to v1.0 behavior (no safety check)."""
    import bridge.main as main_mod

    monkeypatch.setattr(main_mod, "has_session", lambda s: True)
    monkeypatch.setattr(main_mod, "pane_accepts_input", lambda p: True)
    monkeypatch.setattr(main_mod, "pane_is_safe_to_inject", lambda p: False)

    result = main_mod.wait_for_pane("agents:monitor", timeout=10, require_idle=False)
    assert result is True


def test_process_update_unsupported_sends_ack(tmp_path, monkeypatch):
    """Sticker → tg-send.sh called with 'sticker not supported' message."""
    import bridge.main as main_mod
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42, policy="allowlist")
    update = {"update_id": 500, "message": {"from": {"id": 42}, "sticker": {}}}

    messages_log = tmp_path / "bridge-messages.jsonl"
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", messages_log)

    captured = []
    def fake_subprocess_run(cmd, **kwargs):
        captured.append(cmd)
        class FakeResult:
            returncode = 0
            stderr = ""
        return FakeResult()
    monkeypatch.setattr(main_mod.subprocess, "run", fake_subprocess_run)

    process_update(update, state, MagicMock(),
                   inject_fn=MagicMock(), transcribe_fn=MagicMock(),
                   pane="agents:monitor")

    # Typing fires first ("I see you"), then tg-send.sh for the unsupported-type ack.
    send_cmds = [c for c in captured if "tg-send.sh" in c[0]]
    typing_cmds = [c for c in captured if "tg-typing.sh" in c[0]]
    assert len(typing_cmds) == 1
    assert len(send_cmds) == 1
    cmd = send_cmds[0]
    assert "sticker" in cmd[1]
    assert "not supported" in cmd[1]

    content = messages_log.read_text()
    assert '"ack_sent": true' in content
    assert '"type": "sticker"' in content

    assert state.offset == 501


def test_process_update_persists_inbox_before_inject(tmp_path, monkeypatch):
    """Bridge writes inbox/<update_id>.txt with body BEFORE calling inject."""
    import bridge.main as main_mod
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42,
                  policy="allowlist")
    update = {"update_id": 600,
              "message": {"from": {"id": 42}, "text": "important memo"}}

    inbox_dir = tmp_path / "inbox"
    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", tmp_path / "msgs.jsonl")

    # Capture call order: inject must see the inbox file already on disk.
    seen_inbox_exists = {"value": None}
    def inject_fn(text, pane):
        seen_inbox_exists["value"] = (inbox_dir / "600.txt").exists()

    process_update(update, state, MagicMock(),
                   inject_fn=inject_fn, transcribe_fn=MagicMock(),
                   pane="agents:monitor")

    inbox_file = inbox_dir / "600.txt"
    assert inbox_file.exists()
    body = inbox_file.read_text()
    assert "important memo" in body
    assert "update_id: 600" in body
    assert "received_at:" in body
    # Persisted BEFORE inject ran
    assert seen_inbox_exists["value"] is True
    # Inbound log records the inbox path
    log_content = (tmp_path / "msgs.jsonl").read_text()
    assert '"direction": "inbound"' in log_content
    assert str(inbox_file) in log_content


def test_process_update_persist_failure_still_injects(tmp_path, monkeypatch):
    """If inbox persist raises, inject still runs (best-effort delivery)."""
    import bridge.main as main_mod
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42,
                  policy="allowlist")
    update = {"update_id": 700,
              "message": {"from": {"id": 42}, "text": "delivers anyway"}}

    monkeypatch.setattr(main_mod, "MESSAGES_PATH", tmp_path / "msgs.jsonl")
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(main_mod, "_persist_inbox", boom)

    inject_fn = MagicMock()
    process_update(update, state, MagicMock(),
                   inject_fn=inject_fn, transcribe_fn=MagicMock(),
                   pane="agents:monitor")

    inject_fn.assert_called_once_with("delivers anyway", pane="agents:monitor")
    assert state.offset == 701


def test_process_update_fires_typing_before_extract(tmp_path, monkeypatch):
    """Typing indicator fires on authorized inbound, before extract_text."""
    import bridge.main as main_mod
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42,
                  policy="allowlist")
    update = {"update_id": 800,
              "message": {"from": {"id": 42}, "text": "hello"}}
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", tmp_path / "msgs.jsonl")

    typing_called = {"count": 0}
    def fake_typing():
        typing_called["count"] += 1
    monkeypatch.setattr(main_mod, "_fire_typing", fake_typing)

    process_update(update, state, MagicMock(),
                   inject_fn=MagicMock(), transcribe_fn=MagicMock(),
                   pane="agents:monitor")

    assert typing_called["count"] == 1


def test_process_update_no_typing_for_unauthorized(tmp_path, monkeypatch):
    """Unauthorized senders should NOT trigger typing (no info leak)."""
    import bridge.main as main_mod
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42,
                  policy="allowlist")
    update = {"update_id": 801,
              "message": {"from": {"id": 99}, "text": "intruder"}}
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", tmp_path / "msgs.jsonl")

    typing_called = {"count": 0}
    monkeypatch.setattr(main_mod, "_fire_typing",
                        lambda: typing_called.update(count=typing_called["count"] + 1))

    process_update(update, state, MagicMock(),
                   inject_fn=MagicMock(), transcribe_fn=MagicMock(),
                   pane="agents:monitor")

    assert typing_called["count"] == 0


def test_check_stale_inbox_alerts_on_old_file(tmp_path, monkeypatch):
    """A file older than STALE_INBOX_SECONDS fires tg-send.sh exactly once."""
    import bridge.main as main_mod
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    stale = inbox / "9001.txt"
    stale.write_text("old message")
    # Force mtime to 120s ago
    old_mtime = time.time() - 120
    os.utime(stale, (old_mtime, old_mtime))

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "pane_is_claude_idle", lambda p: True)
    monkeypatch.setattr(main_mod, "pane_accepts_input", lambda p: True)
    sent = []
    monkeypatch.setattr(main_mod, "_send_alert", lambda text: sent.append(text) or True)

    alerted: set[str] = set()
    main_mod.check_stale_inbox(alerted, pane="agents:monitor")
    assert sent == [main_mod.STALE_ALERT_TEXT]
    assert "9001" in alerted

    # Second call must NOT re-alert
    main_mod.check_stale_inbox(alerted, pane="agents:monitor")
    assert len(sent) == 1


def test_check_stale_inbox_skips_fresh_files(tmp_path, monkeypatch):
    """A file freshly written should NOT trigger an alert."""
    import bridge.main as main_mod
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "9002.txt").write_text("just arrived")

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "pane_is_claude_idle", lambda p: True)
    monkeypatch.setattr(main_mod, "pane_accepts_input", lambda p: True)
    sent = []
    monkeypatch.setattr(main_mod, "_send_alert", lambda text: sent.append(text) or True)

    main_mod.check_stale_inbox(set(), pane="agents:monitor")
    assert sent == []


def test_check_stale_inbox_ignores_processed_subdir(tmp_path, monkeypatch):
    """Files under inbox/processed/ are off-limits to the staleness scanner."""
    import bridge.main as main_mod
    inbox = tmp_path / "inbox"
    processed = inbox / "processed"
    processed.mkdir(parents=True)
    moved = processed / "9003.txt"
    moved.write_text("already handled")
    old_mtime = time.time() - 120
    os.utime(moved, (old_mtime, old_mtime))

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "pane_is_claude_idle", lambda p: True)
    monkeypatch.setattr(main_mod, "pane_accepts_input", lambda p: True)
    sent = []
    monkeypatch.setattr(main_mod, "_send_alert", lambda text: sent.append(text) or True)

    main_mod.check_stale_inbox(set(), pane="agents:monitor")
    assert sent == []


def test_check_stale_inbox_suppresses_when_monitor_busy(tmp_path, monkeypatch):
    """No false-alarm alerts while monitor is mid-response on a long task.

    Reproduces the 2026-05-19 09:25-09:28 false-positive: 3 alerts fired while
    a research specialist was actively processing the user's queued messages.
    """
    import bridge.main as main_mod
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    # Three queued messages, all stale by file-age alone
    for i, content in enumerate(["long task", "follow-up", "another"]):
        f = inbox / f"900{i}.txt"
        f.write_text(content)
        old_mtime = time.time() - 120
        os.utime(f, (old_mtime, old_mtime))

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "pane_is_claude_idle", lambda p: False)  # busy
    sent = []
    monkeypatch.setattr(main_mod, "_send_alert", lambda text: sent.append(text) or True)

    alerted: set[str] = set()
    main_mod.check_stale_inbox(alerted, pane="agents:monitor")
    # Suppressed entirely while busy — alerted set also stays empty so we'll
    # re-evaluate on a future idle window.
    assert sent == []
    assert alerted == set()


def test_check_stale_inbox_suppresses_when_pane_in_copy_mode(tmp_path, monkeypatch):
    """No alerts when the user has scrolled the pane into copy-mode.

    If `pane_accepts_input` is False, the pane is in tmux copy-mode (user is
    actively scrolling/reviewing). Firing alerts here is noise — the user is
    looking at the pane and can scroll back to see queued messages themselves.
    More importantly, treating a copy-mode pane as "idle and stuck" was a real
    inconsistency: `wait_for_pane` gates on `pane_accepts_input` but
    `check_stale_inbox` did not.
    """
    import bridge.main as main_mod
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "9100.txt"
    f.write_text("queued during scroll")
    old_mtime = time.time() - 120
    os.utime(f, (old_mtime, old_mtime))

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "pane_is_claude_idle", lambda p: True)
    monkeypatch.setattr(main_mod, "pane_accepts_input", lambda p: False)  # copy-mode
    sent = []
    monkeypatch.setattr(main_mod, "_send_alert", lambda text: sent.append(text) or True)

    alerted: set[str] = set()
    main_mod.check_stale_inbox(alerted, pane="agents:monitor")
    assert sent == []
    assert alerted == set()


def test_wait_for_pane_uses_safe_to_inject_not_claude_idle(monkeypatch):
    """wait_for_pane must use the stricter `pane_is_safe_to_inject` for the inject path.

    Regression for the 2026-05-22 audit: a permission menu shows ❯ at the
    bottom and pane_is_claude_idle returns True, but injecting would auto-pick
    option 1. wait_for_pane must require the stricter safety check.
    """
    import bridge.main as main_mod

    monkeypatch.setattr(main_mod, "has_session", lambda s: True)
    monkeypatch.setattr(main_mod, "pane_accepts_input", lambda p: True)
    # claude_idle says True (would currently allow inject) but safe_to_inject says False
    monkeypatch.setattr(main_mod, "pane_is_claude_idle", lambda p: True)
    monkeypatch.setattr(main_mod, "pane_is_safe_to_inject", lambda p: False)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    result = main_mod.wait_for_pane("agents:monitor", timeout=2)
    assert result is False  # never safe to inject → timeout


def test_process_update_persists_but_skips_inject_when_should_inject_false(tmp_path, monkeypatch):
    """When the monitor health is auth_failed/api_error, persist to inbox but
    do NOT fire-and-forget inject into a wedged pane. Don't fire typing either
    (it would mislead the user that claude is working).
    """
    import bridge.main as main_mod
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42, policy="allowlist")
    update = {"update_id": 1000,
              "message": {"from": {"id": 42}, "text": "queued for replay"}}

    inbox_dir = tmp_path / "inbox"
    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", tmp_path / "msgs.jsonl")

    inject_fn = MagicMock()
    typing_called = {"count": 0}
    monkeypatch.setattr(main_mod, "_fire_typing",
                        lambda: typing_called.update(count=typing_called["count"] + 1))

    main_mod.process_update(update, state, MagicMock(),
                            inject_fn=inject_fn, transcribe_fn=MagicMock(),
                            pane="agents:monitor", should_inject=False)

    # Persisted for later replay (the bridge inbox is the durable copy).
    assert (inbox_dir / "1000.txt").exists()
    body = (inbox_dir / "1000.txt").read_text()
    assert "queued for replay" in body
    # No inject — would hit a wedged pane.
    inject_fn.assert_not_called()
    # No typing — would mislead user that claude is working.
    assert typing_called["count"] == 0
    # Offset still advances — Telegram updates aren't re-fetched once acknowledged.
    assert state.offset == 1001
    # Log entry uses distinct direction so we can grep for queued-but-skipped.
    content = (tmp_path / "msgs.jsonl").read_text()
    assert '"direction": "queued_unhealthy"' in content


def test_replay_queued_re_injects_in_update_id_order(tmp_path, monkeypatch):
    """After recovery, replay_queued re-injects inbox files in monotonic update_id order.

    Telegram update_ids are monotonic — that's the chronological order from the
    user's perspective. If we replay in filename-sort order we'd accidentally
    sort "514106214" after "514106215" lexicographically only because they
    happen to share a prefix; ordering by int(stem) makes this robust to wider
    id ranges.
    """
    import bridge.main as main_mod
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42, policy="allowlist")

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for uid, body in [("514106215", "second"), ("514106214", "first"),
                      ("514106216", "third")]:
        (inbox / f"{uid}.txt").write_text(
            f"received_at: 2026-05-20T00:00:00+00:00\nupdate_id: {uid}\n\n{body}\n"
        )

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "wait_for_pane", lambda p, **kw: True)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    injected = []
    def fake_inject(text, pane):
        injected.append(text)

    count = main_mod.replay_queued(state, pane="agents:monitor",
                                   inject_fn=fake_inject, replay_spacing=0)
    assert count == 3
    assert injected == ["first", "second", "third"]
    # Tracking persisted in state
    assert state.inbox_tracking["514106214"]["inject_count"] == 1
    assert state.inbox_tracking["514106215"]["inject_count"] == 1
    # Drain: successfully replayed files moved out of inbox into processed/
    # so stale-alert and re-replay paths see them as drained.
    assert not (inbox / "514106214.txt").exists()
    assert not (inbox / "514106215.txt").exists()
    assert not (inbox / "514106216.txt").exists()
    assert (inbox / "processed" / "514106214.txt").exists()
    assert (inbox / "processed" / "514106215.txt").exists()
    assert (inbox / "processed" / "514106216.txt").exists()


def test_replay_queued_caps_at_max_reinjects_per_file(tmp_path, monkeypatch):
    """A file already injected MAX_REINJECTS times is skipped on the next replay.

    Prevents infinite re-inject loops when claude can't actually process a
    specific message (e.g., a malformed voice transcript that crashes the
    routing logic). Cap is 3 in production; tests use the same default.
    """
    import bridge.main as main_mod
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42, policy="allowlist")
    state.inbox_tracking = {"100": {"inject_count": 3}}

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "100.txt").write_text(
        "received_at: 2026-05-20T00:00:00+00:00\nupdate_id: 100\n\nrepeatedly tried\n"
    )

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "wait_for_pane", lambda p, **kw: True)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    injected = []
    count = main_mod.replay_queued(state, pane="agents:monitor",
                                   inject_fn=lambda t, pane: injected.append(t),
                                   replay_spacing=0)
    assert count == 0
    assert injected == []
    # inject_count not bumped
    assert state.inbox_tracking["100"]["inject_count"] == 3


def test_defer_ancient_moves_files_older_than_threshold(tmp_path, monkeypatch):
    """Files older than the defer threshold get archived to inbox/.deferred/.

    Stops the multi-day re-fire pattern observed 2026-05-22: "Yes" from
    2026-05-20 was still firing stale alerts 42 hours later.
    """
    import bridge.main as main_mod
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    old = inbox / "100.txt"
    old.write_text("ancient")
    young = inbox / "200.txt"
    young.write_text("fresh")
    # Mtime: old = 30h ago, young = 1h ago
    old_mtime = time.time() - 30 * 3600
    young_mtime = time.time() - 3600
    os.utime(old, (old_mtime, old_mtime))
    os.utime(young, (young_mtime, young_mtime))

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    moved = main_mod.defer_ancient(threshold_seconds=24 * 3600)
    assert len(moved) == 1
    assert (inbox / ".deferred" / "100.txt").exists()
    assert not old.exists()
    # Young file untouched
    assert young.exists()
    assert not (inbox / ".deferred" / "200.txt").exists()


def test_defer_ancient_no_op_when_inbox_missing(tmp_path, monkeypatch):
    """defer_ancient gracefully returns [] if INBOX_DIR doesn't exist."""
    import bridge.main as main_mod
    monkeypatch.setattr(main_mod, "INBOX_DIR", tmp_path / "does_not_exist")
    assert main_mod.defer_ancient(threshold_seconds=24 * 3600) == []


def test_check_stale_inbox_suppressed_when_monitor_unhealthy(tmp_path, monkeypatch):
    """When health gate says monitor is unhealthy, per-file stale alerts are silenced.

    Two-channel alert noise was the 2026-05-22 pattern: the health probe fires
    its own targeted "/login needed" alert, AND the per-file stale check fires
    on the same files. Suppress the per-file noise when health-class alert is
    handling it.
    """
    import bridge.main as main_mod
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "9200.txt"
    f.write_text("stuck")
    old_mtime = time.time() - 120
    os.utime(f, (old_mtime, old_mtime))

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "pane_is_claude_idle", lambda p: True)
    monkeypatch.setattr(main_mod, "pane_accepts_input", lambda p: True)
    sent = []
    monkeypatch.setattr(main_mod, "_send_alert", lambda text: sent.append(text) or True)

    alerted: set[str] = set()
    main_mod.check_stale_inbox(alerted, pane="agents:monitor", health_state="auth_failed")
    assert sent == []
    assert alerted == set()


def test_process_update_should_inject_true_is_default(tmp_path, monkeypatch):
    """Existing callers passing no should_inject get the healthy-path behavior."""
    import bridge.main as main_mod
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42, policy="allowlist")
    update = {"update_id": 1001,
              "message": {"from": {"id": 42}, "text": "normal flow"}}

    monkeypatch.setattr(main_mod, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(main_mod, "MESSAGES_PATH", tmp_path / "msgs.jsonl")
    inject_fn = MagicMock()

    main_mod.process_update(update, state, MagicMock(),
                            inject_fn=inject_fn, transcribe_fn=MagicMock(),
                            pane="agents:monitor")

    inject_fn.assert_called_once_with("normal flow", pane="agents:monitor")
