import json
import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

from bridge.health import MonitorHealth
from bridge.inject import (
    has_session,
    inject,
    pane_accepts_input,
    pane_has_clean_prompt,
    pane_is_busy,
    pane_is_claude_idle,
    pane_is_safe_to_inject,
    pane_is_static,
)
from bridge.parse import detect_message_type, extract_text, is_authorized
from bridge.state import Backoff, State
from bridge.telegram import TelegramClient

LOG_DIR = Path.home() / "agents" / "logs"
HEARTBEAT_PATH = LOG_DIR / "bridge-heartbeat.log"
MESSAGES_PATH = LOG_DIR / "bridge-messages.jsonl"
INBOX_DIR = Path.home() / ".agents" / "inbox"
TOOLS_DIR = Path.home() / "agents" / "tools"
STALE_INBOX_SECONDS = 60  # Files older than this in INBOX_DIR → monitor likely wedged
STALE_ALERT_TEXT = ("⚠️ monitor unresponsive — your last message is queued at "
                    "~/.agents/inbox/. May need /login or restart.")
DEFER_THRESHOLD_SECONDS = 24 * 3600  # Files older than 24h → archive to inbox/.deferred/
DEFERRED_SUBDIR = ".deferred"
MAX_REINJECTS_PER_FILE = 3
REPLAY_SPACING_SECONDS = 8  # Pause between sequential re-injects so claude can consume each
# Cap files per replay call so main loop yields back to poll loop. With 8s
# spacing, 3 files = ~24s blocked — fits inside one 25s long-poll window.
# Remaining files drain on subsequent ticks via state.replay_pending.
MAX_REPLAY_BATCH = 3
INJECT_STUCK_THRESHOLD = 120  # seconds of inject-blocked-while-not-busy → alert
INJECT_STUCK_TEXT = ("⚠️ I can't deliver your message — the monitor's input looks "
                     "stuck (an earlier message may be sitting unsent in the "
                     "prompt). It likely needs a restart.")

logger = logging.getLogger("bridge")


def _fire_typing() -> None:
    """Best-effort typing indicator. Telegram shows for ~5s. Swallows all errors."""
    try:
        subprocess.run(
            [str(TOOLS_DIR / "tg-typing.sh")],
            capture_output=True, timeout=5, check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("tg-typing.sh failed: %s", e)


def _send_alert(text: str) -> bool:
    """Best-effort tg-send.sh alert. Returns True on success."""
    try:
        result = subprocess.run(
            [str(TOOLS_DIR / "tg-send.sh"), text],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("tg-send.sh failed: %s", e)
        return False


def _iso_to_epoch(ts: str) -> float | None:
    """Parse an ISO-8601 UTC timestamp to epoch seconds.

    Tolerates >6 fractional-second digits: tg-send.sh logs outbound ts via
    `date -Ins` (nanoseconds, 9 digits), but datetime.fromisoformat only
    accepts 3 or 6. Truncate to microseconds before parsing.
    """
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts)
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def _latest_outbound_epoch(messages_path: Path | None = None) -> float | None:
    """Epoch of the most recent outbound log entry, or None.

    `bridge-messages.jsonl` is append-only and chronological, so the LAST line
    with direction=outbound is the most recent reply. Scan from the end, stop
    at the first match. Lets the stale-alert know whether the monitor already
    answered a queued message (loop closed) before crying "unresponsive".
    """
    path = messages_path if messages_path is not None else MESSAGES_PATH
    if not path.exists():
        return None
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        if '"direction": "outbound"' not in ln:
            continue
        try:
            ts = json.loads(ln).get("ts")
        except json.JSONDecodeError:
            continue
        if ts:
            return _iso_to_epoch(ts)
    return None


def _pane_busy_for_detector(pane: str) -> bool:
    """`busy` input for `_inject_stuck_due`, staleness-corrected.

    `pane_is_busy`'s `…` heuristic suppressed the inject-stuck alert for the
    same false-busy pane that blocked injection (2026-06-05: a truncated
    tool-call display `[monitor]…)` in a finished turn's tail — blocked 10.5h,
    zero alerts). A static pane cannot be actively working, so it is never
    busy for suppression purposes; the alert fires and the user learns their
    message is stuck. Short-circuits: pane_is_static (~4s of sampling) only
    runs when the text heuristic says busy, and only while inject-blocked.
    """
    return pane_is_busy(pane) and not pane_is_static(pane)


def _inject_stuck_due(
    blocked_since: float | None,
    now: float,
    busy: bool,
    health_state: str,
    threshold: float = INJECT_STUCK_THRESHOLD,
) -> bool:
    """Whether an inject-blocked episode warrants a 'monitor input stuck' alert.

    Fires only when we've been UNABLE to deliver pending updates for longer than
    `threshold`, the pane is NOT actively working (`busy` False — so this is a
    wedge, not a long task), and the monitor auth is healthy (an unhealthy
    monitor already gets its own targeted alert). Without this, a message stuck
    unsent in the prompt is a silent black hole: it was never persisted to the
    inbox, so check_stale_inbox can't see it (verified live 2026-05-29 — a draft
    blocked delivery for ~2h with no alert).
    """
    if blocked_since is None:
        return False
    if busy or health_state != "healthy":
        return False
    return (now - blocked_since) > threshold


def _drain_to_processed(path: Path, key: str) -> None:
    """Move a fully-handled inbox file to processed/. Best-effort.

    Mirrors the drain in `replay_queued`; used by `check_stale_inbox` to clear
    answered-but-orphaned files left by the steady-state inject path.
    """
    try:
        processed_dir = INBOX_DIR / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)
        path.replace(processed_dir / path.name)
        logger.info("drained answered inbox file %s (outbound after arrival)", key)
    except OSError as e:
        logger.warning("drain of answered file %s failed: %s", key, e)


def check_stale_inbox(
    alerted: set[str],
    pane: str,
    now: float | None = None,
    health_state: str = "unknown",
    latest_outbound: float | None = None,
) -> None:
    """Scan INBOX_DIR for files older than STALE_INBOX_SECONDS not yet alerted on.

    `latest_outbound` is the epoch of the monitor's most recent outbound reply
    (None = unknown/none yet). A queued file whose arrival (mtime) is at or
    before that epoch was already answered — the monitor replied but the
    steady-state inject path never drained the file. Such files drain to
    processed/ and never alert; only files with no outbound since arrival fire
    the wedge alert. The main loop passes `_latest_outbound_epoch()`; omitting
    it gives legacy age-only behavior.

    Suppresses all alerts while:
      - The monitor pane is BUSY (mid-response).
      - The user has scrolled the pane into copy-mode (they're already looking).
      - The health probe knows the monitor is unhealthy (auth_failed / api_error).
        In that case `MonitorHealth.alert_text` is firing a targeted single alert;
        per-file stale spam on top of that is redundant noise.

    Only alert when the monitor LOOKS idle, IS accepting input, health is
    healthy-or-unknown, AND files are still sitting unconsumed — that's the
    genuine "claude is alive but failing to drain" wedge case.

    `alerted` is a mutable set of update_id strings the caller owns (in-memory,
    reset on bridge restart — re-alerting on restart is fine since stale inbox
    = real problem worth re-surfacing).
    """
    if not INBOX_DIR.exists():
        return
    # Suppress per-file noise when the health probe is already firing a
    # targeted alert for this episode. unknown is permissive (don't lose
    # alerts if the probe itself is broken).
    if health_state in ("auth_failed", "api_error"):
        return
    # Idle = at empty `❯ ` prompt, no `✻` running indicator after it. If we
    # can't determine idleness, err on the side of NOT alerting (false positives
    # are worse than missed alerts — chronic wedges get caught on next restart).
    if not pane_is_claude_idle(pane):
        return
    # Skip if the user has scrolled the pane into copy-mode — they're actively
    # reviewing it and can see queued messages themselves. Mirrors the gate in
    # `wait_for_pane`; without this, copy-mode would false-trigger alerts.
    if not pane_accepts_input(pane):
        return
    now = now if now is not None else time.time()
    for path in INBOX_DIR.iterdir():
        if not path.is_file() or path.suffix != ".txt":
            continue
        key = path.stem  # update_id
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        age = now - mtime
        if age < STALE_INBOX_SECONDS:
            continue
        # Loop-closure reconciliation: an outbound reply at or after this
        # message's arrival means the monitor answered it. The file is just an
        # un-drained orphan (steady-state inject doesn't drain; only startup +
        # replay do). Drain it and do NOT fire the false "unresponsive" alert —
        # even if we alerted earlier, so a bridge restart (which clears
        # `alerted`) can't re-fire on it. Verified live 2026-05-29 12:44:53.
        if latest_outbound is not None and latest_outbound >= mtime:
            _drain_to_processed(path, key)
            alerted.discard(key)
            continue
        if key in alerted:
            continue
        ok = _send_alert(STALE_ALERT_TEXT)
        # Mark alerted even if send failed — avoids hammering tg-send on a broken connection.
        alerted.add(key)
        logger.warning("stale inbox alert fired for update_id=%s (age=%.0fs, sent=%s)",
                       key, age, ok)


def _persist_inbox(update_id: int, text: str) -> Path:
    """Durably persist inbound text before pane injection.

    Write atomically (tmp + rename) so the monitor never reads a partial file
    on startup-drain. The monitor consumes files from INBOX_DIR and moves
    them to inbox/processed/ — see monitor CLAUDE.md.
    """
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    final = INBOX_DIR / f"{update_id}.txt"
    tmp = INBOX_DIR / f".{update_id}.txt.tmp"
    body = (
        f"received_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"update_id: {update_id}\n"
        f"\n"
        f"{text}\n"
    )
    tmp.write_text(body)
    tmp.replace(final)
    return final


def transcribe_via_shell(audio_path: str) -> str:
    """Call the existing transcribe-audio.sh helper. Returns stdout."""
    script = Path.home() / "agents" / "tools" / "transcribe-audio.sh"
    result = subprocess.run(
        [str(script), audio_path],
        capture_output=True, text=True, timeout=300,
    )
    result.check_returncode()
    return result.stdout


def _log_message(direction: str, update_id: int, payload: dict) -> None:
    MESSAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "direction": direction,
        "update_id": update_id,
        **payload,
    }
    with MESSAGES_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def process_update(
    update: dict,
    state: State,
    client: TelegramClient,
    inject_fn: Callable[..., None],
    transcribe_fn: Callable[[str], str],
    pane: str,
    should_inject: bool = True,
) -> None:
    """Persist + (optionally) inject an inbound Telegram update.

    `should_inject=False` is the wedged-monitor path: we still record the message
    durably to `INBOX_DIR` so a future healthy monitor can replay it, but we skip
    the tmux send-keys (would be eaten by a 401 prompt or wedged claude) AND the
    typing indicator (which would mislead the user that claude is working).
    """
    update_id = update["update_id"]
    log_record: dict | None = None
    try:
        try:
            if not is_authorized(update, state.paired_user_id):
                logger.warning("rejected unauthorized update %d", update_id)
                log_record = {"direction": "rejected", "payload": {"reason": "unauthorized"}}
                return
            # Immediate "I see you" signal — but only if claude can actually act.
            # When the monitor is wedged, "typing..." misrepresents the system state.
            if should_inject:
                _fire_typing()
            text = extract_text(update, client, transcribe_fn)
            if text is None:
                msg_type = detect_message_type(update.get("message", {}))
                if msg_type:
                    ack = (f"📎 {msg_type} not supported yet — only text, voice memos, "
                           f"images, documents, and audio files.")
                    ack_sent = False
                    try:
                        result = subprocess.run(
                            [str(Path.home() / "agents" / "tools" / "tg-send.sh"), ack],
                            capture_output=True, text=True, timeout=10,
                        )
                        ack_sent = result.returncode == 0
                        if not ack_sent:
                            logger.warning("ack send returned rc=%d: %s",
                                           result.returncode, result.stderr)
                    except (subprocess.SubprocessError, OSError) as e:
                        logger.warning("ack send failed: %s", e)
                    log_record = {
                        "direction": "skipped",
                        "payload": {"reason": "unsupported_type",
                                    "type": msg_type, "ack_sent": ack_sent},
                    }
                else:
                    logger.info("skipping unknown update shape %d", update_id)
                    log_record = {"direction": "skipped",
                                  "payload": {"reason": "unknown_update_shape"}}
                return
            # Persist BEFORE inject so a mid-inject pane restart can't lose the message.
            # Persist failure is logged but does NOT block inject (best-effort delivery).
            inbox_path: Path | None = None
            try:
                inbox_path = _persist_inbox(update_id, text)
            except Exception as e:
                logger.exception("inbox persist failed for update %d: %s", update_id, e)
            # Wedged-monitor path: stop here — file is durable, replay handles delivery.
            if not should_inject:
                log_record = {
                    "direction": "queued_unhealthy",
                    "payload": {"text_len": len(text), "pane": pane,
                                "inbox": str(inbox_path) if inbox_path else None},
                }
                return
            try:
                inject_fn(text, pane=pane)
            except Exception as e:
                logger.exception("inject failed for update %d: %s", update_id, e)
                log_record = {
                    "direction": "inject_failed",
                    "payload": {"text_len": len(text), "pane": pane, "error": str(e),
                                "inbox": str(inbox_path) if inbox_path else None},
                }
                return
            state.last_message_at = datetime.now(timezone.utc).isoformat()
            log_record = {"direction": "inbound",
                          "payload": {"text_len": len(text), "pane": pane,
                                      "inbox": str(inbox_path) if inbox_path else None}}
        except Exception as e:
            logger.exception("processing failed for update %d: %s", update_id, e)
            log_record = {"direction": "error", "payload": {"error": str(e)}}
            # Don't re-raise: offset must still advance to prevent infinite retry on this bad update.
    finally:
        state.offset = update_id + 1
        state.save()
        if log_record is not None:
            _log_message(log_record["direction"], update_id, log_record["payload"])


def heartbeat(state: State) -> None:
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HEARTBEAT_PATH.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} offset={state.offset}\n")


def wait_for_pane(pane: str, timeout: int = 30, require_idle: bool = True) -> bool:
    """Return True once pane is ready, False after timeout.

    Ready means: session exists, pane accepts input, and (if require_idle)
    claude is at an empty prompt that is SAFE to inject into — i.e. not at a
    permission menu, Y/n confirmation, or `Resume previous?` startup prompt.

    Uses `pane_is_safe_to_inject` (strict) rather than the looser
    `pane_is_claude_idle` because injecting a Telegram message into an open
    permission menu would auto-select option 1 (the `\\n` after our text
    confirms the highlighted choice).
    """
    deadline = time.time() + timeout
    session = pane.split(":")[0]
    while time.time() < deadline:
        if has_session(session) and pane_accepts_input(pane):
            if not require_idle or pane_is_safe_to_inject(pane):
                return True
            # Escape hatch (2026-06-05): `…` in a FINISHED turn's transcript
            # tail (e.g. a truncated tool-call display `[monitor]…)`) makes
            # pane_is_safe_to_inject false-busy forever — the pane is static,
            # so the line never scrolls out. A byte-identical pane across
            # samples cannot be working (busy UI animates), so static + clean
            # empty prompt ⇒ injectable despite the heuristic.
            if pane_has_clean_prompt(pane) and pane_is_static(pane):
                logger.info("pane %s static with clean prompt; "
                            "overriding busy heuristic", pane)
                return True
        time.sleep(2)
    return False


def _count_queued(inbox_dir: Path) -> int:
    """Count text files at the top level of inbox_dir (not in subdirs like processed/)."""
    if not inbox_dir.exists():
        return 0
    return sum(1 for p in inbox_dir.iterdir() if p.is_file() and p.suffix == ".txt")


def _extract_body(inbox_text: str) -> str:
    """Strip the `received_at:` / `update_id:` header from a persisted inbox file.

    Persisted format (from `_persist_inbox`):
        received_at: 2026-05-20T22:00:00+00:00
        update_id: 514106214
        <blank>
        <message body>
    """
    parts = inbox_text.split("\n\n", 1)
    return (parts[1] if len(parts) == 2 else inbox_text).rstrip("\n")


def defer_ancient(
    threshold_seconds: float = DEFER_THRESHOLD_SECONDS,
    now: float | None = None,
) -> list[Path]:
    """Move inbox files older than `threshold_seconds` into `.deferred/`.

    Returns the new paths of moved files. No-op if `INBOX_DIR` doesn't exist.
    Files moved here are no longer alert-eligible. They stay on disk for
    forensics — never auto-deleted.

    Rationale (2026-05-22): a "Yes" message from 2026-05-20 was still
    re-firing stale alerts 42h later because every bridge restart cleared
    the in-memory `alerted_stale` set. After 24h the context is gone —
    replaying "Yes" out-of-band is actively harmful. Archive and move on.
    """
    if not INBOX_DIR.exists():
        return []
    now = now if now is not None else time.time()
    deferred_dir = INBOX_DIR / DEFERRED_SUBDIR
    moved: list[Path] = []
    for path in INBOX_DIR.iterdir():
        if not path.is_file() or path.suffix != ".txt":
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age < threshold_seconds:
            continue
        deferred_dir.mkdir(parents=True, exist_ok=True)
        new_path = deferred_dir / path.name
        try:
            path.replace(new_path)
            moved.append(new_path)
            logger.info("deferred ancient inbox file: %s → %s (age=%.0fs)",
                        path.name, new_path, age)
        except OSError as e:
            logger.warning("failed to defer %s: %s", path.name, e)
    return moved


def _valid_inbox_files(inbox_dir: Path) -> list[Path]:
    """Inbox files with integer stems, sorted by stem (monotonic update_id order).

    Filters out:
      - Subdirectories (processed/, .deferred/)
      - Non-.txt suffixes (.tmp atomic-write fragments)
      - Files whose stem isn't a valid int (manual debugging artifacts,
        editor backups). One such file used to crash the entire replay
        loop via uncaught ValueError in the sort key.
    """
    valid: list[tuple[int, Path]] = []
    for p in inbox_dir.iterdir():
        if not p.is_file() or p.suffix != ".txt":
            continue
        try:
            uid = int(p.stem)
        except ValueError:
            logger.warning("inbox: ignoring file with non-integer stem: %s", p.name)
            continue
        valid.append((uid, p))
    valid.sort(key=lambda t: t[0])
    return [p for _, p in valid]


def replay_queued(
    state: State,
    pane: str,
    inject_fn: Callable[..., None] = inject,
    replay_spacing: float = REPLAY_SPACING_SECONDS,
    max_per_file: int = MAX_REINJECTS_PER_FILE,
    max_files: int = MAX_REPLAY_BATCH,
) -> int:
    """Re-inject queued inbox files into the monitor pane (capped per call).

    Called when the health probe detects an auth_failed/api_error → healthy edge.
    Iterates files in monotonic `update_id` order (chronological from the user's
    perspective) and re-injects each, sleeping `replay_spacing` seconds between
    so claude can consume each before the next arrives.

    Per-file `inject_count` is tracked in `state.inbox_tracking`. After
    `max_per_file` attempts, the file is skipped — prevents a poison message
    (e.g., a transcript that crashes the routing logic) from infinite-looping.

    `max_files` caps how many files we process this call so the main loop
    yields back to polling. The caller (main loop) sets `state.replay_pending`
    based on the return value to drive subsequent batches without re-entering
    the recovery-edge path.

    Returns the count of files actually re-injected this call.
    """
    if not INBOX_DIR.exists():
        return 0
    files = _valid_inbox_files(INBOX_DIR)[:max_files]
    count = 0
    for path in files:
        key = path.stem
        track = state.inbox_tracking.setdefault(key, {"inject_count": 0})
        if track.get("inject_count", 0) >= max_per_file:
            logger.info("replay skipping %s (inject_count=%d >= %d)",
                        key, track["inject_count"], max_per_file)
            continue
        if not wait_for_pane(pane, timeout=30):
            logger.warning("pane not ready during replay; aborting after %d files", count)
            break
        try:
            text = _extract_body(path.read_text())
            if not text.strip():
                # Empty body (atomic-write crash mid-_persist_inbox, or
                # corrupted file). Injecting an empty line is useless and
                # pollutes the pane. Drain it without bumping inject_count.
                logger.warning("replay skipping %s: empty body after _extract_body", key)
                try:
                    processed_dir = INBOX_DIR / "processed"
                    processed_dir.mkdir(parents=True, exist_ok=True)
                    path.replace(processed_dir / path.name)
                except OSError as e:
                    logger.warning("replay drain move failed for empty %s: %s", key, e)
                continue
            inject_fn(text, pane=pane)
            track["inject_count"] = track.get("inject_count", 0) + 1
            track["last_inject_at"] = datetime.now(timezone.utc).isoformat()
            count += 1
            # Drain to processed/ after successful inject. Monitor's
            # CLAUDE.md startup-drain only fires on fresh claude restart,
            # not on mid-session replay — without this, stale-alert keeps
            # firing on the same file even though claude already responded.
            # Best-effort: failed move logs but does not block subsequent
            # injects (the inject already succeeded; this is cleanup).
            try:
                processed_dir = INBOX_DIR / "processed"
                processed_dir.mkdir(parents=True, exist_ok=True)
                path.replace(processed_dir / path.name)
            except OSError as e:
                logger.warning("replay drain move failed for %s: %s", key, e)
            if replay_spacing > 0:
                time.sleep(replay_spacing)
        except Exception as e:
            logger.exception("replay inject failed for %s: %s", key, e)
            break
    state.save()
    return count


def main(
    token: str,
    paired_user_id: int,
    pane: str,
    state_path: Path,
) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    state = State.load(state_path)
    if state.paired_user_id is None:
        state.paired_user_id = paired_user_id
        state.save()

    backoff = Backoff()
    last_heartbeat = 0.0
    alerted_stale: set[str] = set()
    # Inject-stuck detector state (Gap-1 fix 2026-05-29): how long we've been
    # unable to deliver pending updates because the pane is wedged.
    inject_blocked_since: float | None = None
    inject_stuck_alerted = False

    # Monitor health is restored from persisted state so the targeted-alert
    # dedup survives bridge restart. Otherwise every restart during an
    # auth_failed episode would re-fire the "/login" alert (was the
    # 2026-05-22 behavior — 6 alerts for 3 stale files because the in-memory
    # set wiped on each restart).
    health = MonitorHealth()
    health.state = state.monitor_status  # type: ignore[assignment]
    health._alerted_for_episode = state.monitor_alerted_for_episode

    with TelegramClient(token=token) as client:
        while True:
            blocked = False  # True if we have pending updates we can't inject
            try:
                updates = client.get_updates(offset=state.offset, timeout=25)
                progress = False
                for update in updates:
                    if not health.can_inject:
                        # Wedged monitor: persist via process_update but skip inject.
                        # Replay happens when health flips back to healthy
                        # (Commit 3: replay-on-recovery; for now, monitor's
                        # startup-drain contract in CLAUDE.md handles backlog).
                        process_update(
                            update, state, client,
                            inject_fn=inject,
                            transcribe_fn=transcribe_via_shell,
                            pane=pane,
                            should_inject=False,
                        )
                        progress = True
                        continue
                    if not wait_for_pane(pane):
                        logger.error("pane %s not ready; will sleep before retry", pane)
                        blocked = True
                        break
                    process_update(
                        update, state, client,
                        inject_fn=inject,
                        transcribe_fn=transcribe_via_shell,
                        pane=pane,
                    )
                    progress = True
                # Hot-loop guard: if updates available but none processed (pane wedged),
                # sleep to avoid spinning on the same offset every iteration.
                if updates and not progress:
                    time.sleep(5)
                backoff.reset()
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                wait = backoff.next()
                logger.warning("network error %s; sleeping %ds", e, wait)
                time.sleep(wait)
            except Exception as e:
                logger.exception("unexpected error: %s", e)
                time.sleep(5)
            finally:
                now = time.time()
                if now - last_heartbeat > 30:
                    heartbeat(state)
                    last_heartbeat = now
                # Move day-old files out of the stale-alert window. Best-effort —
                # OSError on individual files is logged inside, not raised.
                defer_ancient(now=now)
                # Out-of-band auth probe — every PROBE_INTERVAL seconds, costs ~2s.
                # No-op within the interval (cheap throttle inside maybe_probe).
                health.maybe_probe(now=now)
                # Replay queued messages on recovery edge OR while drain pending
                # from a prior batch that hit MAX_REPLAY_BATCH cap.
                edge = health.recovered_in_last_probe
                if edge or (state.replay_pending and health.state == "healthy"):
                    n = replay_queued(state, pane=pane)
                    # If we hit the batch cap, more files may remain — keep
                    # draining on subsequent ticks without needing another edge.
                    state.replay_pending = (n >= MAX_REPLAY_BATCH)
                    if edge and n > 0:
                        text = (f"✅ monitor recovered — re-injected {n} queued "
                                f"message{'s' if n != 1 else ''}")
                        text += " (more queued, draining)." if state.replay_pending else "."
                        _send_alert(text)
                    if edge:
                        health.consume_recovery()
                state.monitor_status = health.state
                state.monitor_last_probe_at = datetime.now(timezone.utc).isoformat()
                # Targeted health alert (one per unhealthy episode).
                queued = _count_queued(INBOX_DIR)
                msg = health.alert_text(queued_count=queued)
                if msg:
                    _send_alert(msg)
                state.monitor_alerted_for_episode = health._alerted_for_episode
                state.save()
                # Inject-stuck detector: pending updates we can't deliver because
                # the pane is wedged (NOT working). The undelivered update was
                # never persisted, so check_stale_inbox can't catch it — this is
                # the only signal. Gap found 2026-05-29: a draft stuck in the
                # prompt black-holed a message for ~2h with no alert.
                if blocked:
                    if inject_blocked_since is None:
                        inject_blocked_since = now
                    elif not inject_stuck_alerted and _inject_stuck_due(
                        inject_blocked_since, now,
                        busy=_pane_busy_for_detector(pane),
                        health_state=health.state,
                    ):
                        _send_alert(INJECT_STUCK_TEXT)
                        inject_stuck_alerted = True
                        logger.warning("inject-stuck alert fired (blocked %.0fs)",
                                       now - inject_blocked_since)
                else:
                    inject_blocked_since = None
                    inject_stuck_alerted = False
                # Per-file stale alerts — gated on health to avoid duplicating
                # the health-class targeted alert during an unhealthy episode.
                check_stale_inbox(alerted_stale, pane=pane, now=now,
                                  health_state=health.state,
                                  latest_outbound=_latest_outbound_epoch())
