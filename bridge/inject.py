import re
import subprocess
import time


def has_session(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    )
    return result.returncode == 0


def pane_accepts_input(pane: str) -> bool:
    """False if pane is in copy-mode or visual mode (would swallow keystrokes)."""
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_in_mode}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "0"


def _is_status_bar_line(line: str) -> bool:
    """True if line is part of Claude Code's persistent footer (v2.1.139+).

    The footer always contains two lines that must be filtered before applying
    the ellipsis heuristic (rule #1 in pane_is_claude_idle):
      - A model/context-usage row with `|` separators and a `[░...]` or
        `[█...]` progress bar. This row holds slash-command hints like
        `/compact…`, whose ellipsis would otherwise trigger false-busy.
      - A permissions indicator row marked with `⏵⏵`.
    """
    if "⏵⏵" in line:
        return True
    if ("[░" in line or "[█" in line) and "|" in line:
        return True
    return False


def pane_is_claude_idle(pane: str) -> bool:
    """True if claude appears to be at a prompt waiting for input.

    Heuristic, in order of trust:
      1. Any visible line containing `…` (U+2026 horizontal ellipsis) → BUSY.
         Claude Code's in-progress UI uses ellipsis universally regardless of
         spinner glyph: `✽ Deciphering…`, `✶ Determining… (5m · ↓ 10.3k tokens)`,
         `Running… (3m · timeout 10m)`. Note `…` may be mid-line (followed by
         metadata like `(Xs · tokens)`), not just trailing.

         The persistent footer (v2.1.139+) is stripped before this check — it
         contains `/compact…` in a slash-hint row and would otherwise
         permanently false-positive as busy.
      2. Otherwise, look for `❯ ` prompt marker more recent than the last
         spinner-summary line (any of `✻ ✽ ✢ ✦ ✶`). Spinner-summary lines like
         `✻ Sautéed for 2s` appear AFTER a response — `❯` underneath them
         means idle.
      3. No `❯` found → not idle (conservative, false-negative-on-edge-case
         beats false-positive-firing-stale-alerts mid-task).

    Trade-off: rule #1 has a false-negative risk if Claude legitimately quotes
    user text containing `…`. Acceptable: false-negatives only delay alerts;
    false-positives spam Telegram.

    Performance: one tmux capture-pane call per invocation.
    """
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane, "-p"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    lines = result.stdout.splitlines()
    # Strip trailing blank lines (tmux pads visible area with empty rows) and
    # the persistent Claude Code footer (v2.1.139+) whose `/compact…` hint
    # would otherwise trigger rule #1 below.
    nonempty = [l for l in lines if l.strip() and not _is_status_bar_line(l)]
    tail = nonempty[-12:]
    # 1. Ellipsis anywhere on a recent line = in-progress UI
    for line in tail:
        if "…" in line:
            return False
    # 2. Prompt-position vs spinner-summary
    last_prompt_idx = -1
    last_spinner_idx = -1
    spinner_glyphs = ("✻", "✽", "✢", "✦", "✶")
    for i, line in enumerate(tail):
        if "❯" in line:
            last_prompt_idx = i
        if any(g in line for g in spinner_glyphs):
            last_spinner_idx = i
    if last_prompt_idx < 0:
        return False
    return last_prompt_idx > last_spinner_idx


def pane_is_busy(pane: str) -> bool:
    """True only if claude is ACTIVELY working (in-progress UI shows `…`).

    This is the 'working' subset of `not pane_is_claude_idle`. It deliberately
    does NOT treat a pane sitting at a non-empty prompt (an un-submitted draft)
    or an interactive menu as busy — those are wedged/idle, not working. The
    inject-stuck detector uses this to tell a transient long task (suppress
    alert) apart from a genuine wedge (alert).

    Mirrors rule #1 of `pane_is_claude_idle`: any `…` in the non-status-bar
    tail. Returns False if the pane can't be captured (unknown ⇒ 'not busy', so
    a real wedge still surfaces an alert).
    """
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane, "-p"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    nonempty = [l for l in result.stdout.splitlines()
                if l.strip() and not _is_status_bar_line(l)]
    return any("…" in line for line in nonempty[-12:])


_INTERACTIVE_PROMPT_PATTERNS = (
    # Numbered menu options like `1. Yes`, `2. No, ...`, `❯ 1. Yes`. Real
    # Claude Code permission UI looks like:
    #     Do you want to proceed?
    #     ❯ 1. Yes
    #       2. No, and tell Claude what to do differently
    # Injecting any Telegram message here delivers `<msg>\n` and Enter would
    # auto-select option 1 (Yes). The pattern matches lines starting with a
    # digit-dot followed by Yes / No / Don't / Cancel / Allow / Continue —
    # specific enough to avoid colliding with prose like "1. First, do X".
    re.compile(r"^[\s❯]*\d+\.\s+(Yes|No|Don't|Cancel|Allow|Continue)\b",
               re.IGNORECASE),
    # Y/n confirmations (Claude Code 'Continue?' prompts, generic shell tools).
    re.compile(r"\([Yy]/[Nn]\)"),
    re.compile(r"\[[Yy]/[Nn]\]"),
    # `claude --continue` startup prompt — would auto-resume if we inject.
    re.compile(r"\bResume previous\b", re.IGNORECASE),
)


def pane_is_safe_to_inject(pane: str) -> bool:
    """Strict superset of `pane_is_claude_idle`: also rejects interactive prompts.

    Invariant: `pane_is_safe_to_inject(p)` implies `pane_is_claude_idle(p)`.

    Returns True only if claude appears idle AND the pane is NOT at:
      - A Claude Code permission menu (`❯ 1. Yes / 2. No, ...`)
      - A Y/n or [y/N] confirmation
      - A `Resume previous session?` prompt

    Used by the inject path (`wait_for_pane` → `process_update`). The stale-alert
    path keeps using the looser `pane_is_claude_idle` because it WANTS to fire
    alerts when claude is at an interactive prompt nobody is answering.
    """
    if not pane_is_claude_idle(pane):
        return False
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane, "-p"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # err on the side of unsafe — better to delay an inject than auto-confirm
        return False
    nonempty = [l for l in result.stdout.splitlines()
                if l.strip() and not _is_status_bar_line(l)]
    tail = nonempty[-12:]
    for line in tail:
        for pattern in _INTERACTIVE_PROMPT_PATTERNS:
            if pattern.search(line):
                return False
    return True


def _text_still_in_prompt(pane: str, text: str) -> bool:
    """True if `text` is still sitting on the `❯` prompt line (typed but not
    submitted). Lets inject() detect a dropped Enter and re-submit.

    Matches a prefix of the first line of `text` on a line containing `❯`, so a
    submitted message (which leaves the prompt empty / claude processing) reads
    as False. Conservative on capture failure (False ⇒ no corrective re-Enter).
    """
    probe = text.strip().splitlines()[0][:24] if text.strip() else ""
    if not probe:
        return False
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane, "-p"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    return any("❯" in line and probe in line for line in result.stdout.splitlines())


def inject(text: str, pane: str, verify: bool = True) -> None:
    """Inject `text` into the tmux pane as if typed, then press Enter.

    Uses `send-keys -l` (literal mode) so special chars don't need escaping.
    Caller is responsible for pre-flight checks (has_session, pane_accepts_input).

    When `verify` is True, confirms the Enter actually submitted: if the text is
    still sitting on the `❯` prompt line (the submit didn't take — a race with
    claude finishing a prior turn), sends one corrective Enter. Cheap insurance
    against the 'message stuck unsent in the prompt' wedge (found 2026-05-29).
    """
    text = text.rstrip("\n")
    subprocess.run(
        ["tmux", "send-keys", "-t", pane, "-l", text],
        check=True,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", pane, "Enter"],
        check=True,
    )
    if verify and text:
        time.sleep(0.4)
        if _text_still_in_prompt(pane, text):
            subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=False)
