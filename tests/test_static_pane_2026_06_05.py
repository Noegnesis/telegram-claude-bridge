"""Regression tests for the 2026-06-05 permanent false-busy incident.

What happened: the monitor's last turn ended with a truncated tool-call
display line `[monitor]…)` (Claude Code truncates wide commands with U+2026)
in the visible tail. `pane_is_claude_idle` rule #1 treats ANY `…` in the
tail-12 as busy; the turn was over so the pane was static and the line never
scrolled out → `wait_for_pane` failed every retry for 10.5h. The inject-stuck
alert was suppressed by the same `…` via `pane_is_busy` → total silence.

Fix under test:
  1. `pane_is_static` — byte-identical captures across samples ⇒ not working
     (Claude Code's busy UI always animates: spinner glyph cycles sub-second,
     timers/token counts tick).
  2. `pane_has_clean_prompt` — last `❯` line is an empty input box, no
     interactive prompt patterns, no `esc to interrupt`.
  3. `wait_for_pane` escape hatch — static + clean prompt ⇒ injectable even
     when the `…` heuristic says busy.
  4. `_pane_busy_for_detector` — a static pane is never "busy" for
     inject-stuck alert suppression (so a draft-blocked pane alerts instead
     of black-holing).
"""

import subprocess
import time
import uuid

import pytest

from bridge.inject import (
    _prompt_line_has_real_text,
    pane_is_claude_idle,
    pane_has_clean_prompt,
    pane_is_static,
)
from bridge.main import wait_for_pane, _pane_busy_for_detector


@pytest.fixture
def bash_session():
    """Ephemeral tmux session running bash, for echoing pane fixtures."""
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    yield f"{name}:main"
    subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def _paint(pane: str, script: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", script], check=True)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
    time.sleep(0.4)


# The 2026-06-05 pane, distilled: truncated tool-call `…` in the transcript
# tail, post-hoc spinner summary, then a CLEAN empty prompt.
INCIDENT_CLEAN_PROMPT = (
    "clear; "
    "echo '● Bash(~/agents/tools/tg-send.sh heredoc)'; "
    "echo '      [monitor]…)'; "
    "echo '  ⎿  (No output)'; "
    "echo '● Loop closed — summary landed on Telegram.'; "
    "echo '✻ Worked for 52s'; "
    "echo '────────'; "
    "echo '❯ '; "
    "echo '────────'"
)

# Same, but with an unsent human draft sitting in the input box.
INCIDENT_WITH_DRAFT = INCIDENT_CLEAN_PROMPT.replace(
    "echo '❯ '", "echo '❯ compare against my 00-06 structure'"
)


# ---------------------------------------------------------------- primitives


def test_pane_is_static_true_for_static_pane(bash_session):
    _paint(bash_session, "clear; echo '❯ '")
    assert pane_is_static(bash_session, samples=2, interval=0.3) is True


def test_pane_is_static_false_for_changing_pane(bash_session):
    _paint(bash_session,
           "while true; do echo tick $((i=i+1)); sleep 0.1; done")
    assert pane_is_static(bash_session, samples=2, interval=0.5) is False


def test_pane_is_static_false_for_dead_pane():
    assert pane_is_static("no-such-session:main", samples=2, interval=0.1) is False


def test_pane_has_clean_prompt_empty(bash_session):
    _paint(bash_session, INCIDENT_CLEAN_PROMPT)
    assert pane_has_clean_prompt(bash_session) is True


def test_pane_has_clean_prompt_rejects_draft(bash_session):
    _paint(bash_session, INCIDENT_WITH_DRAFT)
    assert pane_has_clean_prompt(bash_session) is False


# Ghost-text suggestions (discovered mid-incident 2026-06-05): Claude Code
# renders an auto-suggested reply in the EMPTY input box as SGR-dim text,
# with the terminal cursor as a reverse-video block on its first char:
#     \e[39m❯\xa0\e[7mc\e[0;2mompare against my 00-06 structure\e[0m
# It is not typed text — Esc/Enter/BSpace are all no-ops — so it must be
# treated as an empty (clean) prompt, or delivery black-holes forever.


def _paint_raw(pane: str, printf_arg: str) -> None:
    """Paint a line containing raw escape sequences via printf %b."""
    subprocess.run(
        ["tmux", "send-keys", "-t", pane, "-l",
         f"clear; echo '✕ Worked for 52s'; printf %b '{printf_arg}\\n'"],
        check=True,
    )
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
    time.sleep(0.4)


def test_pane_has_clean_prompt_accepts_ghost_suggestion(bash_session):
    """Dim-rendered suggestion text after ❯ = empty input box = clean."""
    _paint_raw(
        bash_session,
        "\\033[39m❯\\302\\240\\033[7mc\\033[0;2m"
        "ompare against my 00-06 structure\\033[0m",
    )
    assert pane_has_clean_prompt(bash_session) is True


def test_prompt_line_real_text_false_for_ghost():
    line = ("\x1b[39m❯\xa0\x1b[7mc\x1b[0;2m"
            "ompare against my 00-06 structure\x1b[0m")
    assert _prompt_line_has_real_text(line) is False


def test_prompt_line_real_text_true_for_typed_draft():
    assert _prompt_line_has_real_text(
        "❯\xa0compare against my 00-06 structure") is True


def test_prompt_line_real_text_false_for_empty_prompt():
    assert _prompt_line_has_real_text("❯\xa0") is False


def test_prompt_line_real_text_true_for_typed_text_after_ghost_reset():
    # Normal-intensity char anywhere after ❯ wins over surrounding dim runs.
    assert _prompt_line_has_real_text(
        "❯\xa0\x1b[2mghost\x1b[0m real\x1b[2m more ghost\x1b[0m") is True


def test_pane_has_clean_prompt_rejects_permission_menu(bash_session):
    _paint(bash_session,
           "clear; echo 'Do you want to proceed?'; "
           "echo '❯ 1. Yes'; echo '  2. No, and tell Claude what to do differently'")
    assert pane_has_clean_prompt(bash_session) is False


def test_pane_has_clean_prompt_rejects_active_work_marker(bash_session):
    _paint(bash_session,
           "clear; echo '✶ Determining… (esc to interrupt)'; echo '❯ '")
    assert pane_has_clean_prompt(bash_session) is False


def test_pane_has_clean_prompt_rejects_no_prompt(bash_session):
    _paint(bash_session, "clear; echo 'just some text'")
    assert pane_has_clean_prompt(bash_session) is False


# ------------------------------------------------------------ the regression


def test_incident_pane_false_busies_the_idle_heuristic(bash_session):
    """Characterization: rule #1 reads the truncated `…` line as busy.

    This is the heuristic limitation the escape hatch exists for. If this
    ever starts passing as idle, the escape hatch is no longer load-bearing
    for this fixture — revisit whether it still has coverage.
    """
    _paint(bash_session, INCIDENT_CLEAN_PROMPT)
    assert pane_is_claude_idle(bash_session) is False


def test_wait_for_pane_escape_hatch_delivers_on_static_clean_prompt(bash_session):
    """THE 2026-06-05 fix: static pane + clean prompt ⇒ ready, despite `…`."""
    _paint(bash_session, INCIDENT_CLEAN_PROMPT)
    assert wait_for_pane(bash_session, timeout=15) is True


def test_wait_for_pane_still_blocks_on_draft(bash_session):
    """A draft in the input box must keep blocking (inject would garble it
    by appending). The inject-stuck alert covers this branch instead."""
    _paint(bash_session, INCIDENT_WITH_DRAFT)
    assert wait_for_pane(bash_session, timeout=6) is False


# ------------------------------------------------------- detector suppression


def test_detector_busy_false_for_static_pane_with_ellipsis(bash_session):
    """The same `…` that blocked injection also suppressed the inject-stuck
    alert (pane_is_busy → True). A static pane cannot be working, so the
    detector must see busy=False and let the alert fire."""
    _paint(bash_session, INCIDENT_WITH_DRAFT)
    assert _pane_busy_for_detector(bash_session) is False


def test_detector_busy_true_for_animating_pane_with_ellipsis(bash_session):
    """Genuine long task: ellipsis present AND pane animating ⇒ still busy
    (alert correctly suppressed)."""
    _paint(bash_session,
           "while true; do echo '✶ Determining… ('$((i=i+1))'s)'; sleep 0.1; done")
    assert _pane_busy_for_detector(bash_session) is True
