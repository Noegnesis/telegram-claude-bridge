"""Out-of-band auth probe for the monitor's `claude` process.

Why this exists (incident 2026-05-22): the monitor pane sat in a 401-logged-out
state since 2026-05-20. The bridge kept fire-and-forget injecting messages into
the pane; each one returned `API Error: 401 Invalid authentication credentials`
and claude couldn't drain the inbox. Existing CLI health checks lied:

    $ claude auth status         # → loggedIn: true (FALSE)
    $ claude doctor              # → interactive TUI, no auth info

The reliable signal is `claude -p <prompt> --output-format json`, which exits
in ~2 seconds with a structured payload:

    {"is_error": true, "api_error_status": 401, "result": "Failed to ..."}

This module wraps that probe and exposes `MonitorHealth.can_inject` (gate for
the bridge's main loop) and `alert_text(...)` (one targeted Telegram alert per
unhealthy episode, re-armed on healthy edges).
"""
import json
import logging
import subprocess
from typing import Literal

HealthState = Literal["healthy", "auth_failed", "api_error", "unknown"]

logger = logging.getLogger("bridge.health")


class MonitorHealth:
    """Tracks last-known auth state of the monitor's claude binary.

    Probe cadence: 300s (5 min). At ~2s per probe and zero token cost (401
    rejects before billing) this is ~24 CPU-seconds per day. Lowering to 60s
    is safe if a fresh wedge needs to be caught faster.
    """

    PROBE_INTERVAL = 300  # seconds
    PROBE_TIMEOUT = 20    # claude -p has been observed at ~2s; 20s cap covers cold-start
    PROBE_PROMPT = "ping"  # cheapest possible prompt; rejected before model invocation on 401

    def __init__(self) -> None:
        self.state: HealthState = "unknown"
        self.last_probe: float = 0.0
        self._alerted_for_episode: bool = False
        # Set True for the duration of one probe whenever the state transitions
        # from a known-bad state (auth_failed / api_error) to healthy. The main
        # loop reads this to trigger replay-on-recovery. Caller is responsible
        # for clearing via `consume_recovery()` after handling.
        self.recovered_in_last_probe: bool = False

    def probe(self, now: float) -> None:
        """Run the probe NOW (ignores PROBE_INTERVAL). Updates self.state + self.last_probe.

        Failure modes mapped to states:
          - HTTP 401/403 in payload → auth_failed
          - HTTP 5xx / other is_error → api_error
          - Timeout / FileNotFoundError / malformed JSON → unknown
                (degraded — don't block injection on uncertainty)
        """
        self.last_probe = now
        prev = self.state
        try:
            result = subprocess.run(
                ["claude", "-p", self.PROBE_PROMPT, "--output-format", "json"],
                capture_output=True, text=True, timeout=self.PROBE_TIMEOUT,
                check=False,
            )
            payload = json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            self.state = "unknown"
            logger.warning("health probe timed out after %ds", self.PROBE_TIMEOUT)
            return
        except FileNotFoundError:
            self.state = "unknown"
            logger.warning("claude binary not found on PATH for health probe")
            return
        except (json.JSONDecodeError, ValueError) as e:
            self.state = "unknown"
            logger.warning("health probe returned malformed JSON: %s", e)
            return

        if not payload.get("is_error"):
            new_state: HealthState = "healthy"
        else:
            code = payload.get("api_error_status")
            if code in (401, 403):
                new_state = "auth_failed"
            else:
                new_state = "api_error"

        # Edge: any transition INTO healthy re-arms the alert for the next episode.
        if new_state == "healthy" and prev != "healthy":
            self._alerted_for_episode = False
        # Recovery edge — but ONLY from a known-bad state, not from unknown.
        # unknown→healthy on first boot is just learning live state, not a
        # genuine recovery from a wedge. Avoid spurious "replayed N msgs" alerts.
        self.recovered_in_last_probe = (
            new_state == "healthy" and prev in ("auth_failed", "api_error")
        )
        self.state = new_state
        if new_state != prev:
            logger.info("monitor health: %s → %s", prev, new_state)

    def consume_recovery(self) -> None:
        """Clear the recovery flag — call after handling the replay."""
        self.recovered_in_last_probe = False

    def maybe_probe(self, now: float) -> None:
        """Run probe only if PROBE_INTERVAL has elapsed since last probe."""
        if now - self.last_probe < self.PROBE_INTERVAL:
            return
        self.probe(now=now)

    @property
    def can_inject(self) -> bool:
        """True if the bridge should attempt `tmux send-keys` into the monitor pane.

        `unknown` permits injection — false-negative on a real failure is worse
        than holding messages during a transient probe failure. The pane-side
        idle/safe checks (`pane_is_safe_to_inject`) still guard against the
        worst-case "inject into an open permission menu" scenario.
        """
        return self.state in ("healthy", "unknown")

    def alert_text(self, queued_count: int) -> str | None:
        """Build an actionable Telegram alert for the current unhealthy state.

        Returns None when the state is healthy OR when an alert was already
        fired this episode (an "episode" = stretch between two healthy probes).
        Caller is responsible for sending the returned text via tg-send.sh.
        """
        if self.state == "healthy" or self.state == "unknown":
            return None
        if self._alerted_for_episode:
            return None
        self._alerted_for_episode = True
        if self.state == "auth_failed":
            return (
                f"⚠️ monitor auth failed (401). Run /login in tmux:agents:monitor "
                f"({queued_count} {'message' if queued_count == 1 else 'messages'} queued)."
            )
        # api_error
        return (
            f"⚠️ monitor API error (5xx). Bridge is queuing — claude will replay "
            f"on next healthy probe ({queued_count} queued)."
        )
