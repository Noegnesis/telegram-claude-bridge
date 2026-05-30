import subprocess
import time
import uuid

import pytest

from bridge.inject import inject, has_session, pane_accepts_input


@pytest.fixture
def tmux_session():
    """Spawn an ephemeral tmux session running `cat` (which echoes stdin)."""
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "cat"],
        check=True,
    )
    yield name
    subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def _capture(session: str) -> str:
    return subprocess.run(
        ["tmux", "capture-pane", "-t", f"{session}:main", "-p"],
        capture_output=True, text=True, check=True,
    ).stdout


def test_inject_appears_in_pane(tmux_session):
    inject("hello bridge", pane=f"{tmux_session}:main")
    time.sleep(0.3)
    assert "hello bridge" in _capture(tmux_session)


def test_inject_handles_special_chars(tmux_session):
    payload = "what's up? $PATH \"quoted\" \\backslash"
    inject(payload, pane=f"{tmux_session}:main")
    time.sleep(0.3)
    assert payload in _capture(tmux_session)


def test_inject_handles_multiline(tmux_session):
    inject("line one\nline two", pane=f"{tmux_session}:main")
    time.sleep(0.5)
    captured = _capture(tmux_session)
    assert "line one" in captured
    assert "line two" in captured


def test_has_session_true(tmux_session):
    assert has_session(tmux_session) is True


def test_has_session_false():
    assert has_session("nonexistent-session-xyz-abc-123") is False


def test_pane_accepts_input_when_not_copy_mode(tmux_session):
    assert pane_accepts_input(f"{tmux_session}:main") is True


from bridge.inject import pane_is_claude_idle


def test_pane_is_claude_idle_with_empty_prompt(tmp_path):
    """Pane showing an empty ❯ prompt with no spinner = idle."""
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; echo '────'; echo '❯ '; echo '────'"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_claude_idle(f"{name}:main") is True
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_claude_idle_with_running_indicator(tmp_path):
    """Pane showing ✻ after the last ❯ = busy."""
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; echo '❯ prior prompt'; echo '✻ Whatchamacalliting...'"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_claude_idle(f"{name}:main") is False
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_claude_idle_no_prompt_at_all_returns_false():
    """A pane with no ❯ anywhere can't be idle."""
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l", "clear; echo nothing"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.3)
        assert pane_is_claude_idle(f"{name}:main") is False
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_claude_idle_busy_on_deciphering_ellipsis():
    """Reproduces 2026-05-19 stale-alert false-positive: ✽ Deciphering… mid-voice-memo.

    Before fix, only ✻ glyph triggered busy detection. ✽ slipped through and
    fired stale-alerts while monitor was actively processing.
    """
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; printf '%s\\n%s\\n%s\\n' '────' '✽ Deciphering…' '❯ '"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_claude_idle(f"{name}:main") is False
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_claude_idle_busy_on_determining_ellipsis():
    """✢ Determining… (different glyph again) is also busy via ellipsis match."""
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; printf '%s\\n%s\\n' '✢ Determining…' '❯ '"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_claude_idle(f"{name}:main") is False
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_claude_idle_busy_when_ellipsis_has_trailing_metadata():
    """Real Claude UI shows `Determining… (5m · ↓ 10.3k tokens · thought for 45s)` — ellipsis is mid-line.

    Pinned to the actual 2026-05-19 10:30 false-positive: monitor was busy on
    `✶ Determining… (5m 54s · ↓ 10.3k tokens · thought for 45s)` but a checker
    that only matched `endswith("…")` returned True (idle) and fired an alert.
    """
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; printf '%s\\n%s\\n' "
             "'✶ Determining… (5m 4s · 10k tokens · thought for 45s)' '❯ '"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_claude_idle(f"{name}:main") is False
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_claude_idle_past_tense_summary_is_idle():
    """`✻ Sautéed for 2s` (no ellipsis) + `❯` underneath = idle (claude finished).

    This was historically a false-negative in the old heuristic — Claude UI puts
    `✻ <past-verb> for Xs` BELOW the response then `❯` for the new prompt; old
    code saw `✻` after `❯` only if they were in unusual order. New code: no
    ellipsis means we trust the prompt-position check.
    """
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; printf '%s\\n%s\\n%s\\n' 'output line' '✻ Sautéed for 2s' '❯ '"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_claude_idle(f"{name}:main") is True
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_claude_idle_ignores_status_bar_ellipsis():
    """Claude Code v2.1.139+ status bar contains `/compact…` slash-hint.

    Regression for 2026-05-20: monitor restarted, idle pane shows
        ❯ Try "refactor <filepath>"
        ────
          O4.7 (1M context)  |  [░░░░] --%  |  xhigh  |  /model  /compact…
          ⏵⏵ bypass permissions on (shift+tab to cycle)
    The `…` in `/compact…` is part of the static footer, not an in-progress
    indicator. Heuristic must filter footer lines before applying rule #1,
    otherwise the bridge can never inject into a freshly-started monitor.
    """
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; printf '%s\\n%s\\n%s\\n%s\\n' "
             "'❯ Try \"refactor <filepath>\"' "
             "'────' "
             "'  O4.7 (1M context)  |  [░░░░] --%  |  xhigh  |  /model  /compact…' "
             "'  ⏵⏵ bypass permissions on (shift+tab to cycle)'"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_claude_idle(f"{name}:main") is True
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_claude_idle_busy_even_with_status_bar_present():
    """Status-bar filtering must not mask a real busy indicator above the footer.

    Layout: spinner-with-ellipsis line ABOVE the prompt, footer BELOW.
    Filtering should remove the footer (where `/compact…` lives) but keep
    the spinner line, so the heuristic still returns False (busy).
    """
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; printf '%s\\n%s\\n%s\\n%s\\n' "
             "'✶ Determining… (5m 4s · 10k tokens)' "
             "'  O4.7 (1M context)  |  [░░░░] --%  |  xhigh  |  /model  /compact…' "
             "'  ⏵⏵ bypass permissions on (shift+tab to cycle)' "
             "'❯ '"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_claude_idle(f"{name}:main") is False
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


from bridge.inject import pane_is_safe_to_inject


def test_pane_is_safe_to_inject_allows_empty_prompt():
    """Happy path: an empty `❯` prompt is safe to inject into."""
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; echo '────'; echo '❯ '; echo '────'"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_safe_to_inject(f"{name}:main") is True
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_safe_to_inject_blocks_permission_menu():
    """Numbered Yes/No menu = bridge must NOT inject (would auto-pick option 1).

    Real Claude Code permission UI looks like:
        Do you want to proceed?
        ❯ 1. Yes
          2. No, and tell Claude what to do differently

    If the bridge sees this as "idle" and injects a Telegram message starting
    with "1" (or anything), tmux send-keys delivers "1\\n" which auto-approves
    the permission. This is a real safety bug — must return False.
    """
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; printf '%s\\n%s\\n%s\\n%s\\n' "
             "'Do you want to proceed?' "
             "'❯ 1. Yes' "
             "'  2. No, and tell Claude what to do differently' "
             "'────'"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_safe_to_inject(f"{name}:main") is False
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_safe_to_inject_blocks_resume_previous_prompt():
    """`Resume previous session? (Y/n)` is an interactive y/n prompt; must not inject.

    Claude Code's --continue flow shows this on startup. Bridge auto-injecting
    text here would either auto-resume (if message starts with Y) or auto-decline.
    """
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; printf '%s\\n%s\\n' "
             "'Resume previous session? (Y/n)' "
             "'❯ '"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_safe_to_inject(f"{name}:main") is False
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_safe_to_inject_blocks_bracketed_yn_prompt():
    """`[y/N]` style yes/no prompts are also unsafe — generic shell convention."""
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; printf '%s\\n%s\\n' "
             "'Continue installation? [y/N]' "
             "'❯ '"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_safe_to_inject(f"{name}:main") is False
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_safe_to_inject_blocks_when_pane_busy():
    """If `pane_is_claude_idle` is False (mid-task), `pane_is_safe_to_inject` must also be False.

    Establishes the invariant: safe_to_inject ⊂ claude_idle.
    """
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; printf '%s\\n%s\\n' "
             "'✶ Determining… (5m 4s · 10k tokens)' "
             "'❯ '"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_safe_to_inject(f"{name}:main") is False
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_pane_is_safe_to_inject_allows_message_quoting_yes_or_no():
    """A user message containing the literal words "Yes" or "No" must still be safe.

    Guard against an over-broad regex: we look for numbered MENU patterns
    (`1. Yes`, `2. No, ...`), not bare Yes/No anywhere in the pane.
    """
    name = f"test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new", "-d", "-s", name, "-n", "main", "bash"],
        check=True,
    )
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{name}:main", "-l",
             "clear; printf '%s\\n%s\\n%s\\n' "
             "'  Yes, that worked!' "
             "'✻ Replied for 1s' "
             "'❯ '"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", f"{name}:main", "Enter"], check=True)
        import time; time.sleep(0.4)
        assert pane_is_safe_to_inject(f"{name}:main") is True
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)
