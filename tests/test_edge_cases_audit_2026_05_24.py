"""Edge case audit 2026-05-24 — see project_telegram_bridge memory.

Probes documented-but-untested edge cases across inbox handling, auth probe,
and replay races. Each test states the hypothesis it's checking and what the
expected/observed behavior tells us about system robustness.

Findings get written to memory; non-trivial issues become open-thread tickets.
"""
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bridge import main as main_mod
from bridge.health import MonitorHealth
from bridge.state import State


# ---------- C1: Non-integer file stem in inbox ----------

def test_replay_queued_skips_non_integer_stems_safely(tmp_path, monkeypatch):
    """C1 fix: a stray non-int-stem file is filtered out, not fatal.

    Old behavior: `int(p.stem)` in sort key raised uncaught ValueError →
    entire replay loop died → no messages got through. Confirmed by
    removing fix temporarily.

    New behavior: `_valid_inbox_files` filters them and logs a WARNING.
    The legit message file still replays.
    """
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "514106214.txt").write_text(
        "received_at: ...\nupdate_id: 514106214\n\nhello\n"
    )
    # Stray file with non-integer stem
    (inbox / "notes.txt").write_text("manual debugging notes, not from bridge")

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "wait_for_pane", lambda p, **kw: True)

    injected = []
    count = main_mod.replay_queued(
        state, pane="agents:monitor",
        inject_fn=lambda t, pane: injected.append(t), replay_spacing=0,
    )
    assert count == 1
    assert injected == ["hello"]
    # Stray file stays put — only valid bridge files get drained.
    assert (inbox / "notes.txt").exists()
    assert (inbox / "processed" / "514106214.txt").exists()


# ---------- C4: Empty-body file ----------

def test_replay_queued_skips_empty_body_and_drains(tmp_path, monkeypatch):
    """C4 fix: empty-body file is drained without inject.

    Old behavior: tmux send-keys -l '' + Enter sent a blank line to claude.
    Worse than useless — pollutes pane, leaves bogus prompt.

    New behavior: empty body → WARNING + drain to processed/ without
    bumping inject_count. count returned 0 since nothing was injected.
    """
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "100.txt").write_text(
        "received_at: 2026-05-24T00:00:00+00:00\nupdate_id: 100\n\n\n"
    )

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "wait_for_pane", lambda p, **kw: True)

    injected = []
    count = main_mod.replay_queued(
        state, pane="agents:monitor",
        inject_fn=lambda t, pane: injected.append(t), replay_spacing=0,
    )
    assert count == 0
    assert injected == []
    # Drained without injection
    assert not (inbox / "100.txt").exists()
    assert (inbox / "processed" / "100.txt").exists()
    # Tracking entry may exist from setdefault but inject_count stays 0
    # (we didn't actually inject — empty body case is drain-only).
    assert state.inbox_tracking.get("100", {}).get("inject_count", 0) == 0


# ---------- C7: Many files in inbox ----------

def test_replay_queued_caps_at_max_replay_batch_per_call(tmp_path, monkeypatch):
    """With 50 files queued, one replay call processes at most MAX_REPLAY_BATCH.

    Fix for C7 (audit 2026-05-24): without the cap, prod 8s spacing meant
    50 files × 8s = 400s where the main loop never yielded back to
    getUpdates polling or health probing. Cap of 3 (= 24s blocked, fits
    inside one 25s long-poll window) keeps the loop responsive.
    """
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for i in range(50):
        (inbox / f"{i:08d}.txt").write_text(
            f"received_at: ...\nupdate_id: {i}\n\nmsg-{i}\n"
        )

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "wait_for_pane", lambda p, **kw: True)

    injected = []
    count = main_mod.replay_queued(
        state, pane="agents:monitor",
        inject_fn=lambda t, pane: injected.append(t), replay_spacing=0,
    )
    # Capped at MAX_REPLAY_BATCH
    assert count == main_mod.MAX_REPLAY_BATCH
    assert injected == [f"msg-{i}" for i in range(main_mod.MAX_REPLAY_BATCH)]
    # First batch drained, rest still in inbox awaiting next call
    drained = {p.name for p in (inbox / "processed").iterdir()}
    remaining = {p.name for p in inbox.iterdir() if p.is_file() and p.suffix == ".txt"}
    assert len(drained) == main_mod.MAX_REPLAY_BATCH
    assert len(remaining) == 50 - main_mod.MAX_REPLAY_BATCH


def test_replay_queued_drains_full_queue_across_multiple_calls(tmp_path, monkeypatch):
    """Repeatedly calling replay_queued eventually drains all files in batches.

    This is what the main loop does via state.replay_pending — first call
    on the recovery edge, subsequent calls every tick until queue empty.
    """
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    total = 11
    for i in range(total):
        (inbox / f"{i:08d}.txt").write_text(
            f"received_at: ...\nupdate_id: {i}\n\nmsg-{i}\n"
        )

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "wait_for_pane", lambda p, **kw: True)

    injected = []
    calls = 0
    while True:
        n = main_mod.replay_queued(
            state, pane="agents:monitor",
            inject_fn=lambda t, pane: injected.append(t), replay_spacing=0,
        )
        calls += 1
        if n < main_mod.MAX_REPLAY_BATCH:
            break
        if calls > 20:
            pytest.fail("replay didn't terminate")

    assert len(injected) == total
    # Order preserved across batches
    assert injected == [f"msg-{i}" for i in range(total)]
    # Expected number of calls: ceil(11/3) = 4
    assert calls == 4


# ---------- B1: claude binary not found on PATH ----------

def test_health_probe_binary_not_found_yields_unknown_state():
    """FileNotFoundError during probe → state='unknown' → can_inject=True.

    This is intentional per the code comment ('false-negative on a real
    failure is worse than holding messages during a transient probe
    failure') — but it means a permanently broken probe (e.g., claude
    binary uninstalled, or PATH issue in Task Scheduler env) silently
    permits injection into a potentially wedged pane.
    """
    health = MonitorHealth()
    with patch("bridge.health.subprocess.run", side_effect=FileNotFoundError):
        health.probe(now=time.time())
    assert health.state == "unknown"
    assert health.can_inject is True


# ---------- B5: Mid-session token expiry between probes ----------

def test_can_inject_during_probe_window_after_last_healthy():
    """If state went healthy 4 min ago but token expires NOW, bridge happily injects.

    Probe cadence is 300s. Between probes, can_inject reflects the last
    snapshot. Mid-window in-memory token expiry (Claude Max session
    refresh failure) is invisible to bridge until next probe.

    NOT a code bug — it's a documented design tradeoff. Test exists
    to flag the window for runbook awareness.
    """
    health = MonitorHealth()
    health.state = "healthy"
    health.last_probe = time.time() - 240  # 4 min ago
    # Bridge would inject into the pane — could black-hole into 401.
    assert health.can_inject is True
    # Workaround: lower PROBE_INTERVAL when faster detection matters,
    # or add a per-inject lightweight check (cost: 2s per message).


# ---------- B8: Probe returns non-JSON (e.g., MOTD or update prompt) ----------

def test_health_probe_non_json_output_yields_unknown():
    """A claude invocation that prints non-JSON before/instead of JSON.

    Real example: 'Update available — run `claude update`' or 'Logging
    in...' lines from a half-broken state. JSONDecodeError → 'unknown'
    → can_inject True (same caveat as B1).
    """
    health = MonitorHealth()
    fake = MagicMock()
    fake.stdout = "Update available — run `claude update` for v3.0.0\n"
    fake.returncode = 0
    with patch("bridge.health.subprocess.run", return_value=fake):
        health.probe(now=time.time())
    assert health.state == "unknown"
    assert health.can_inject is True


def test_health_probe_json_with_leading_garbage_yields_unknown():
    """A common variant: probe returns 'Press [Enter] to continue\\n{json...}'.

    json.loads on the full stdout fails — current code doesn't strip or
    extract JSON from mixed output.
    """
    health = MonitorHealth()
    fake = MagicMock()
    fake.stdout = 'noise line\n{"is_error": false}\n'
    fake.returncode = 0
    with patch("bridge.health.subprocess.run", return_value=fake):
        health.probe(now=time.time())
    assert health.state == "unknown"


# ---------- E3: Replay race with monitor's startup-drain ----------

def test_replay_continues_when_file_vanishes_mid_loop(tmp_path, monkeypatch):
    """Monitor's startup-drain races with bridge's replay drain on the same file.

    Both call `path.replace(... / "processed" / path.name)`. The losing
    side raises FileNotFoundError. Bridge's new code (2026-05-24) wraps
    the move in an inner try/except OSError, so the loss is logged and
    the loop continues — verified.
    """
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "100.txt").write_text(
        "received_at: ...\nupdate_id: 100\n\nhello\n"
    )
    (inbox / "200.txt").write_text(
        "received_at: ...\nupdate_id: 200\n\nworld\n"
    )

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "wait_for_pane", lambda p, **kw: True)

    def inject_then_external_drain(text, pane):
        """After inject succeeds, simulate monitor concurrently draining the file."""
        if text == "hello":
            (inbox / "100.txt").unlink()  # monitor moved it (or unlinked it)

    count = main_mod.replay_queued(
        state, pane="agents:monitor",
        inject_fn=inject_then_external_drain, replay_spacing=0,
    )
    # Both messages got injected. Drain race on file 100 was absorbed.
    assert count == 2


# ---------- Bonus: drain failure for a stuck file ----------

def test_replay_drain_failure_does_not_block_subsequent_files(tmp_path, monkeypatch):
    """If path.replace on file 1 raises OSError (e.g., readonly fs),
    file 2 still gets injected. Inject already succeeded; drain is
    best-effort cleanup."""
    state = State(path=tmp_path / "s.json", offset=0, paired_user_id=42)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "100.txt").write_text("received_at: ...\nupdate_id: 100\n\nfirst\n")
    (inbox / "200.txt").write_text("received_at: ...\nupdate_id: 200\n\nsecond\n")

    monkeypatch.setattr(main_mod, "INBOX_DIR", inbox)
    monkeypatch.setattr(main_mod, "wait_for_pane", lambda p, **kw: True)

    injected = []
    original_replace = Path.replace

    def replace_with_failure(self, target):
        if self.name == "100.txt":
            raise PermissionError("read-only filesystem")
        return original_replace(self, target)

    with patch.object(Path, "replace", replace_with_failure):
        count = main_mod.replay_queued(
            state, pane="agents:monitor",
            inject_fn=lambda t, pane: injected.append(t),
            replay_spacing=0,
        )

    assert count == 2
    assert injected == ["first", "second"]
    # 100.txt should still be in inbox (drain failed), 200.txt moved.
    assert (inbox / "100.txt").exists()
    assert (inbox / "processed" / "200.txt").exists()
