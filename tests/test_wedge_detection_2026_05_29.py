"""Wedge-detection tests (2026-05-29) — fixes for two gaps found during the
loop-closure diagnosis:

  Gap 1: a message that can't be delivered because the monitor pane is wedged
         (stuck un-submitted draft, NOT actively working) was a SILENT
         black-hole — no alert. Observed live: update 514106241 blocked ~2h.
         Fix: main-loop inject-blocked detector gated on `pane_is_busy`.

  Gap 2: inject() typed text then pressed Enter, but if the Enter didn't take
         (race with claude finishing a prior turn) the text sat unsent in the
         prompt. Fix: verify-submit + corrective re-Enter via
         `_text_still_in_prompt`.
"""
import contextlib
import subprocess
import time
import uuid

import bridge.main as main_mod
from bridge.inject import _text_still_in_prompt, pane_is_busy


@contextlib.contextmanager
def _pane(render: str):
    """Ephemeral tmux pane rendered with `render` (a bash command), à la test_inject."""
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["tmux", "new", "-d", "-s", name, "-n", "main", "bash"], check=True)
    try:
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "-l", render], check=True)
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        time.sleep(0.4)
        yield f"{name}:main"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


# ---------------- pane_is_busy ----------------

def test_pane_is_busy_true_on_spinner_ellipsis():
    with _pane("clear; printf '%s\\n%s\\n' '✶ Determining… (5m · 10k tokens)' '❯ '") as p:
        assert pane_is_busy(p) is True


def test_pane_is_busy_false_on_empty_prompt():
    with _pane("clear; echo '────'; echo '❯ '; echo '────'") as p:
        assert pane_is_busy(p) is False


def test_pane_is_busy_false_on_stuck_unsent_draft():
    """The wedge case: text sitting unsent in the prompt, no in-progress `…`.
    Must be classified NOT busy so the inject-stuck detector can fire."""
    with _pane("clear; printf '%s\\n%s\\n' '✻ Crunched for 1m 4s' "
               "'❯ friend is Maya, send via Rosie'") as p:
        assert pane_is_busy(p) is False


def test_pane_is_busy_ignores_status_bar_ellipsis():
    """The persistent footer `/compact…` must not count as busy."""
    with _pane("clear; printf '%s\\n%s\\n%s\\n' '❯ ' "
               "'  O4.7 (1M context)  |  [░░░░] --%  |  /compact…' "
               "'  ⏵⏵ bypass permissions on'") as p:
        assert pane_is_busy(p) is False


# ---------------- _text_still_in_prompt ----------------

def test_text_still_in_prompt_true_when_unsent():
    with _pane("clear; printf '%s\\n' '❯ hello unsent draft here'") as p:
        assert _text_still_in_prompt(p, "hello unsent draft here") is True


def test_text_still_in_prompt_false_on_empty_prompt():
    with _pane("clear; echo '❯ '") as p:
        assert _text_still_in_prompt(p, "hello unsent draft here") is False


def test_text_still_in_prompt_false_when_no_prompt_marker():
    with _pane("clear; echo 'hello unsent draft here'") as p:  # no ❯ marker
        assert _text_still_in_prompt(p, "hello unsent draft here") is False


# ---------------- _inject_stuck_due (pure) ----------------

def test_inject_stuck_due_false_when_not_blocked():
    assert main_mod._inject_stuck_due(None, now=1000.0, busy=False,
                                      health_state="healthy") is False


def test_inject_stuck_due_false_when_busy():
    assert main_mod._inject_stuck_due(0.0, now=1_000_000.0, busy=True,
                                      health_state="healthy") is False


def test_inject_stuck_due_false_when_unhealthy():
    assert main_mod._inject_stuck_due(0.0, now=1_000_000.0, busy=False,
                                      health_state="auth_failed") is False


def test_inject_stuck_due_false_within_threshold():
    base = 1000.0
    assert main_mod._inject_stuck_due(base, now=base + 30, busy=False,
                                      health_state="healthy", threshold=120) is False


def test_inject_stuck_due_true_when_blocked_long_not_busy_healthy():
    base = 1000.0
    assert main_mod._inject_stuck_due(base, now=base + 200, busy=False,
                                      health_state="healthy", threshold=120) is True
