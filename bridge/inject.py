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


def pane_is_static(pane: str, samples: int = 3, interval: float = 2.0) -> bool:
    """True if pane content is byte-identical across `samples` captures taken
    `interval`s apart.

    Ground truth for "not actively working" that survives UI copy drift:
    Claude Code's in-progress UI always animates (the spinner glyph cycles
    sub-second, elapsed timers and token counts tick), so a static pane
    cannot be mid-task. Exists because the `…` text heuristic in
    `pane_is_claude_idle` permanently false-busied on a truncated tool-call
    display (`[monitor]…)`) left in the transcript tail of a FINISHED turn —
    static pane, line never scrolled out, delivery blocked 10.5h
    (2026-06-05).

    Conservative on capture failure: False (unknown ⇒ assume active).
    """
    prev = None
    for i in range(samples):
        if i:
            time.sleep(interval)
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", pane, "-p"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False
        if prev is not None and result.stdout != prev:
            return False
        prev = result.stdout
    return True


_CSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def _prompt_line_has_real_text(styled_line: str) -> bool:
    """True if the input-box line contains user-typed text after `❯`.

    Claude Code renders ghost-text suggestions (an auto-suggested reply shown
    in an EMPTY input box) as SGR-dim (`2`) text, with the terminal cursor as
    a reverse-video (`7`) block on the first char:

        \\e[39m❯\\xa0\\e[7mc\\e[0;2mompare against my 00-06 structure\\e[0m

    Real typed input renders at normal intensity. A dim-only line is an empty
    input box wearing a suggestion — injectable. Treating the ghost as a
    draft would black-hole delivery forever: it cannot be cleared (Esc,
    Enter, BSpace are all no-ops on a suggestion; verified live 2026-06-05).

    Walks the styled line tracking dim/reverse state; any normal-intensity
    visible char after the `❯` marker ⇒ real draft.
    """
    seen_prompt = False
    dim = reverse = False
    i = 0
    while i < len(styled_line):
        m = _SGR_RE.match(styled_line, i)
        if m:
            params = m.group(1)
            codes = [int(c) for c in params.split(";") if c] if params else [0]
            for code in codes:
                if code == 0:
                    dim = reverse = False
                elif code == 2:
                    dim = True
                elif code == 7:
                    reverse = True
                elif code == 22:
                    dim = False
                elif code == 27:
                    reverse = False
            i = m.end()
            continue
        ch = styled_line[i]
        if ch == "❯":
            seen_prompt = True
        elif seen_prompt and ch.strip() and ch != "\xa0":
            if not dim and not reverse:
                return True
        i += 1
    return False


def pane_has_clean_prompt(pane: str) -> bool:
    """True if the input box (the LAST `❯` line in the non-footer tail) is
    empty — no unsent draft — and the tail shows no interactive prompt
    pattern and no `esc to interrupt` active-work marker.

    Used by `wait_for_pane`'s static-pane escape hatch (2026-06-05): together
    with `pane_is_static` it certifies a pane as injectable even when
    `pane_is_claude_idle` rule #1 false-busies on transcript content. A draft
    after `❯` keeps this False — injecting would append to the draft and
    submit garbage; that branch is covered by the inject-stuck alert instead.
    Ghost-text suggestions (dim-rendered, see `_prompt_line_has_real_text`)
    do NOT count as drafts — the input box under them is empty.
    """
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane, "-p", "-e"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    styled = result.stdout.splitlines()
    plain = [_CSI_RE.sub("", l) for l in styled]
    tail = [(p, s) for p, s in zip(plain, styled)
            if p.strip() and not _is_status_bar_line(p)][-12:]
    if any("esc to interrupt" in p for p, _ in tail):
        return False
    for p, _ in tail:
        for pattern in _INTERACTIVE_PROMPT_PATTERNS:
            if pattern.search(p):
                return False
    prompt_lines = [(p, s) for p, s in tail if "❯" in p]
    if not prompt_lines:
        return False
    p, s = prompt_lines[-1]
    if p.split("❯", 1)[1].strip() == "":
        return True
    return not _prompt_line_has_real_text(s)


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
