"""Tests for `bridge.health` — out-of-band auth probe via `claude -p`.

Background: 2026-05-22 incident — monitor pane sat in 401 state since 2026-05-20
because nothing checked auth. Bridge kept injecting into a logged-out pane;
`claude auth status` lied (`loggedIn: true`) so pane-grep was the only signal,
and that's brittle. `claude -p ping --output-format json` returns a structured
`{is_error, api_error_status, ...}` blob — definitive auth signal at ~2s cost,
zero tokens (401 rejects before billing).
"""
import json
import subprocess
from unittest.mock import patch

from bridge.health import MonitorHealth


class _FakeResult:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def test_probe_healthy_when_claude_responds_without_error():
    """A successful `claude -p` returns is_error=False → state=healthy."""
    health = MonitorHealth()
    stdout = json.dumps({"is_error": False, "result": "pong", "duration_ms": 1800})
    with patch("subprocess.run", return_value=_FakeResult(stdout=stdout)):
        health.probe(now=1000.0)
    assert health.state == "healthy"
    assert health.last_probe == 1000.0


def test_probe_auth_failed_on_401():
    """401 in api_error_status → state=auth_failed (the today's symptom)."""
    health = MonitorHealth()
    stdout = json.dumps({
        "is_error": True,
        "api_error_status": 401,
        "result": "Failed to authenticate. API Error: 401 Invalid authentication credentials",
    })
    with patch("subprocess.run", return_value=_FakeResult(stdout=stdout)):
        health.probe(now=1000.0)
    assert health.state == "auth_failed"


def test_probe_auth_failed_on_403():
    """403 (perms revoked) is also an auth-class failure."""
    health = MonitorHealth()
    stdout = json.dumps({"is_error": True, "api_error_status": 403, "result": "forbidden"})
    with patch("subprocess.run", return_value=_FakeResult(stdout=stdout)):
        health.probe(now=1000.0)
    assert health.state == "auth_failed"


def test_probe_api_error_on_5xx():
    """5xx is API-side, NOT user-actionable — distinct from auth_failed."""
    health = MonitorHealth()
    stdout = json.dumps({"is_error": True, "api_error_status": 529, "result": "overloaded"})
    with patch("subprocess.run", return_value=_FakeResult(stdout=stdout)):
        health.probe(now=1000.0)
    assert health.state == "api_error"


def test_probe_unknown_on_timeout():
    """Probe timing out → state=unknown (network blip; don't disrupt injection)."""
    health = MonitorHealth()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=20)):
        health.probe(now=1000.0)
    assert health.state == "unknown"


def test_probe_unknown_on_malformed_json():
    """Bad JSON from claude (CLI bug or interactive prompt) → state=unknown."""
    health = MonitorHealth()
    with patch("subprocess.run", return_value=_FakeResult(stdout="not json at all")):
        health.probe(now=1000.0)
    assert health.state == "unknown"


def test_probe_unknown_on_filenotfound():
    """If `claude` binary is missing (mid-upgrade), don't crash — degrade to unknown."""
    health = MonitorHealth()
    with patch("subprocess.run", side_effect=FileNotFoundError("claude")):
        health.probe(now=1000.0)
    assert health.state == "unknown"


def test_maybe_probe_throttles_within_interval():
    """maybe_probe is a no-op within PROBE_INTERVAL seconds of last probe."""
    health = MonitorHealth()
    stdout = json.dumps({"is_error": False, "result": "pong"})
    with patch("subprocess.run", return_value=_FakeResult(stdout=stdout)) as mock_run:
        health.maybe_probe(now=1000.0)
        health.maybe_probe(now=1000.0 + 60)   # 1 min later
        health.maybe_probe(now=1000.0 + 250)  # well within 5 min
    assert mock_run.call_count == 1


def test_maybe_probe_fires_after_interval():
    """After PROBE_INTERVAL seconds, the next maybe_probe actually probes."""
    health = MonitorHealth()
    stdout = json.dumps({"is_error": False, "result": "pong"})
    with patch("subprocess.run", return_value=_FakeResult(stdout=stdout)) as mock_run:
        health.maybe_probe(now=1000.0)
        health.maybe_probe(now=1000.0 + MonitorHealth.PROBE_INTERVAL + 1)
    assert mock_run.call_count == 2


def test_can_inject_true_when_healthy_or_unknown():
    """Injection allowed on healthy or unknown — only block on confirmed failure."""
    health = MonitorHealth()
    health.state = "healthy"
    assert health.can_inject is True
    health.state = "unknown"
    assert health.can_inject is True


def test_can_inject_false_when_auth_failed():
    """Auth failure → bridge MUST stop fire-and-forget injecting."""
    health = MonitorHealth()
    health.state = "auth_failed"
    assert health.can_inject is False


def test_can_inject_false_when_api_error():
    """5xx API errors also block inject — message would be eaten by a broken pane."""
    health = MonitorHealth()
    health.state = "api_error"
    assert health.can_inject is False


def test_alert_text_returns_targeted_message_for_auth_failed():
    """First detection of auth_failed produces an actionable Telegram alert."""
    health = MonitorHealth()
    health.state = "auth_failed"
    text = health.alert_text(queued_count=3)
    assert text is not None
    assert "/login" in text or "auth" in text.lower()
    assert "3" in text  # queue count surfaced


def test_alert_text_returns_none_when_healthy():
    """No alert fires while monitor is healthy."""
    health = MonitorHealth()
    health.state = "healthy"
    assert health.alert_text(queued_count=0) is None


def test_just_recovered_true_on_unhealthy_to_healthy_edge():
    """A probe that flips from auth_failed to healthy sets recovered_in_last_probe=True."""
    failed_stdout = json.dumps({"is_error": True, "api_error_status": 401, "result": "401"})
    healthy_stdout = json.dumps({"is_error": False, "result": "pong"})
    health = MonitorHealth()
    with patch("subprocess.run", return_value=_FakeResult(stdout=failed_stdout)):
        health.probe(now=1000.0)
    assert health.recovered_in_last_probe is False  # first wedge, not a recovery

    with patch("subprocess.run", return_value=_FakeResult(stdout=healthy_stdout)):
        health.probe(now=2000.0)
    assert health.recovered_in_last_probe is True


def test_just_recovered_false_on_healthy_to_healthy():
    """Two healthy probes in a row — no recovery edge, no replay needed."""
    healthy_stdout = json.dumps({"is_error": False, "result": "pong"})
    health = MonitorHealth()
    with patch("subprocess.run", return_value=_FakeResult(stdout=healthy_stdout)):
        health.probe(now=1000.0)
        health.probe(now=2000.0)
    assert health.recovered_in_last_probe is False


def test_just_recovered_false_from_unknown_first_boot():
    """The initial unknown→healthy transition on bridge boot is NOT a recovery.

    On bridge restart, MonitorHealth starts at state='unknown' (or restored
    from State). Going unknown→healthy on the first probe is just learning
    the live state, not recovering from a wedge. We don't want a spurious
    "replayed 0 messages" celebration alert here.
    """
    healthy_stdout = json.dumps({"is_error": False, "result": "pong"})
    health = MonitorHealth()  # state="unknown"
    with patch("subprocess.run", return_value=_FakeResult(stdout=healthy_stdout)):
        health.probe(now=1000.0)
    assert health.recovered_in_last_probe is False


def test_alert_text_dedupes_within_episode():
    """alert_text returns text once per unhealthy episode — re-armed when a probe sees healthy.

    Episode boundaries are owned by probe() (the only path that drives state
    transitions in production). Re-arming on direct state mutation isn't
    needed because the bridge never sets state outside probe().
    """
    failed_stdout = json.dumps({"is_error": True, "api_error_status": 401, "result": "401"})
    healthy_stdout = json.dumps({"is_error": False, "result": "pong"})

    health = MonitorHealth()
    # Episode 1: enter auth_failed via probe
    with patch("subprocess.run", return_value=_FakeResult(stdout=failed_stdout)):
        health.probe(now=1000.0)
    assert health.state == "auth_failed"

    first = health.alert_text(queued_count=1)
    assert first is not None
    second = health.alert_text(queued_count=1)
    assert second is None  # already alerted this episode

    # Recovery via probe re-arms the alert
    with patch("subprocess.run", return_value=_FakeResult(stdout=healthy_stdout)):
        health.probe(now=2000.0)
    assert health.state == "healthy"
    assert health.alert_text(queued_count=0) is None  # no alert while healthy

    # Episode 2: fail again via probe → re-armed
    with patch("subprocess.run", return_value=_FakeResult(stdout=failed_stdout)):
        health.probe(now=3000.0)
    third = health.alert_text(queued_count=1)
    assert third is not None
