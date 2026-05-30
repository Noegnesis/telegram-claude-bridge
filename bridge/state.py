import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class State:
    path: Path
    offset: int = 0
    paired_user_id: int | None = None
    policy: str = "allowlist"
    last_message_at: str | None = None
    # Monitor health tracking (added 2026-05-22 after the 401-zombie incident).
    # `monitor_status` is one of: healthy | auth_failed | api_error | unknown.
    # `monitor_alerted_for_episode` dedupes the targeted Telegram alert so we
    # don't re-fire every probe interval; reset by health.py on healthy edges.
    # `inbox_tracking` keys are update_id strings; values store per-file alert
    # state so dedup survives bridge restart (the in-memory `alerted_stale`
    # set used to wipe and re-flood every restart).
    monitor_status: str = "unknown"
    monitor_last_probe_at: str | None = None
    monitor_alerted_for_episode: bool = False
    inbox_tracking: dict = field(default_factory=dict)
    # Set True when replay_queued hit its per-call cap and more files may remain.
    # Main loop checks this each tick to drain in batches without blocking the
    # poll loop for minutes when many messages queued during a long unhealthy
    # episode (avoids the 50-file × 8s = 400s lockout).
    replay_pending: bool = False

    @classmethod
    def load(cls, path: Path) -> "State":
        path = Path(path)
        if not path.exists():
            return cls(path=path)
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable — start fresh rather than refusing to boot.
            # Worst case: replay one batch of Telegram updates (poller dedupes).
            return cls(path=path)
        return cls(
            path=path,
            offset=data.get("offset", 0),
            paired_user_id=data.get("paired_user_id"),
            policy=data.get("policy", "allowlist"),
            last_message_at=data.get("last_message_at"),
            monitor_status=data.get("monitor_status", "unknown"),
            monitor_last_probe_at=data.get("monitor_last_probe_at"),
            monitor_alerted_for_episode=data.get("monitor_alerted_for_episode", False),
            inbox_tracking=data.get("inbox_tracking", {}),
            replay_pending=data.get("replay_pending", False),
        )

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "offset": self.offset,
            "paired_user_id": self.paired_user_id,
            "policy": self.policy,
            "last_message_at": self.last_message_at,
            "monitor_status": self.monitor_status,
            "monitor_last_probe_at": self.monitor_last_probe_at,
            "monitor_alerted_for_episode": self.monitor_alerted_for_episode,
            "inbox_tracking": self.inbox_tracking,
            "replay_pending": self.replay_pending,
        }
        tmp.write_text(json.dumps(payload, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


class Backoff:
    def __init__(self, start: int = 1, cap: int = 60):
        self._start = start
        self._cap = cap
        self._current = start

    def next(self) -> int:
        value = self._current
        self._current = min(self._current * 2, self._cap)
        return value

    def reset(self) -> None:
        self._current = self._start
