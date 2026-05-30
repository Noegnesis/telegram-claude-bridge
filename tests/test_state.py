import json
import os
from pathlib import Path

import pytest

from bridge.state import State


def test_load_creates_default_if_missing(tmp_path):
    p = tmp_path / "state.json"
    s = State.load(p)
    assert s.offset == 0
    assert s.paired_user_id is None
    assert s.policy == "allowlist"


def test_load_reads_existing(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "offset": 42, "paired_user_id": 8873212188,
        "policy": "allowlist", "last_message_at": "2026-05-18T10:00:00Z",
    }))
    s = State.load(p)
    assert s.offset == 42
    assert s.paired_user_id == 8873212188
    assert s.policy == "allowlist"
    assert s.last_message_at == "2026-05-18T10:00:00Z"


def test_load_returns_defaults_on_corrupt_json(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{ this is not valid json")
    s = State.load(p)
    assert s.offset == 0
    assert s.paired_user_id is None
    assert s.policy == "allowlist"


def test_save_is_atomic(tmp_path):
    p = tmp_path / "state.json"
    s = State(path=p, offset=100, paired_user_id=8873212188,
              policy="allowlist", last_message_at=None)
    s.save()
    # File exists, contents are correct JSON
    data = json.loads(p.read_text())
    assert data["offset"] == 100
    # No stale tmp file left behind
    assert not (tmp_path / "state.json.tmp").exists()


def test_save_mode_0600(tmp_path):
    p = tmp_path / "state.json"
    s = State(path=p, offset=0, paired_user_id=None,
              policy="allowlist", last_message_at=None)
    s.save()
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_load_back_compat_without_monitor_fields(tmp_path):
    """Existing state-prod.json files (pre-2026-05-22) have no monitor health fields.

    Loading them must default monitor_status="unknown", monitor_last_probe_at=None,
    monitor_alerted_for_episode=False, inbox_tracking={} — never raise.
    """
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "offset": 514106218,
        "paired_user_id": 8873212188,
        "policy": "allowlist",
        "last_message_at": "2026-05-22T18:47:25.105838+00:00",
    }))
    s = State.load(p)
    assert s.offset == 514106218
    assert s.monitor_status == "unknown"
    assert s.monitor_last_probe_at is None
    assert s.monitor_alerted_for_episode is False
    assert s.inbox_tracking == {}


def test_save_load_roundtrip_monitor_fields(tmp_path):
    """New monitor health fields persist across save+load."""
    p = tmp_path / "state.json"
    s = State(
        path=p, offset=10, paired_user_id=42, policy="allowlist",
        last_message_at=None,
        monitor_status="auth_failed",
        monitor_last_probe_at="2026-05-22T19:00:00+00:00",
        monitor_alerted_for_episode=True,
        inbox_tracking={"514106214": {"alerted_at": "2026-05-20T22:00:00Z"}},
    )
    s.save()
    reloaded = State.load(p)
    assert reloaded.monitor_status == "auth_failed"
    assert reloaded.monitor_last_probe_at == "2026-05-22T19:00:00+00:00"
    assert reloaded.monitor_alerted_for_episode is True
    assert reloaded.inbox_tracking == {"514106214": {"alerted_at": "2026-05-20T22:00:00Z"}}
