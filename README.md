# Telegram ↔ Claude Code Bridge

A standalone Python long-poll process that connects a Telegram bot to an
always-on Claude Code session ("the monitor"), so you can DM your agent from
your phone — text, voice memos (auto-transcribed), photos, and documents all
land in the agent's hands — and get replies back. Replaces the brittle
`telegram@claude-plugins-official` MCP plugin with three isolated processes.

> Runs on Windows + WSL2 (Ubuntu). The bridge and monitor live inside WSL;
> Windows Task Scheduler keeps the WSL stack alive. **See [SETUP.md](SETUP.md)
> to stand this up on a fresh machine** — it is clone-and-go: the supervision
> scripts and a generic monitor brain ship in this repo.

## Architecture

Three independent processes, no shared runtime — any one can die without
taking the others down:

```
                 Telegram Bot API
                       │  long-poll getUpdates / sendMessage
                       ▼
   ┌────────────────────────┐   tmux send-keys (inbound)   ┌─────────────────┐
   │  bridge                │ ───────────────────────────▶ │  monitor        │
   │  telegram-bridge.py    │   + durable inbox files      │  claude session │
   │  (owns the bot + state)│ ◀─────────────────────────── │  in tmux:agents │
   └────────────────────────┘   tg-send.sh (curl, outbound)└─────────────────┘
```

- **bridge** owns the Telegram connection and all state. Every inbound message
  is persisted to `~/.agents/inbox/<update_id>.txt` *before* it is injected, so
  a crash mid-delivery never loses it.
- **monitor** is an always-on `claude` session in `tmux:agents:monitor`. It
  drains the inbox on startup, does the work, and replies via `tg-send.sh`.
  A PreToolUse hook blocks pane-only tools (AskUserQuestion / plan-mode) so
  replies always go to Telegram; a Stop hook is a last-resort outbound backstop.
- Communication is deliberately dumb: tmux keystrokes inbound, a shell-out
  `curl` outbound. No shared memory, no IPC daemon.

## What ships in this repo

| Path | What | Runs from |
|---|---|---|
| `telegram-bridge.py`, `bridge/` | The bridge: long-poll loop + all logic. | the clone (`~/agents/bridge/`) |
| `bridge-loop.sh` | While-true supervisor for the python proc + heartbeat watchdog. | the clone |
| `tools/tg-send.sh`, `tools/tg-typing.sh` | Outbound helpers (real files, path-portable). | deployed to `~/agents/tools/` |
| `scripts/start-bridge.sh` | Full-stack idempotent "ensure agents up" — what the scheduled tasks run. | deployed to `~/agents/scripts/` |
| `scripts/start-monitor.sh`, `scripts/monitor-loop.sh` | Session + claude supervisors. | deployed to `~/agents/scripts/` |
| `monitor/CLAUDE.md` | **Generic monitor brain — a template you customize.** Keeps the load-bearing bridge contract; the "Your behaviour" section is yours to fill. | deployed to `~/agents/monitor/` |
| `monitor/.claude/{settings.json,hooks/}` | PreToolUse loop-closure hook + Stop backstop, wired via `$CLAUDE_PROJECT_DIR`. | deployed to `~/agents/monitor/` |
| `tests/` | 128 pytest tests (hermetic). | the clone |
| `start-bridge.sh` | Convenience symlink → `scripts/start-bridge.sh`. | — |

`bridge/main.py` (update processing, inbox persist/drain, stale-alert, replay),
`bridge/inject.py` (tmux pane reasoning), `bridge/health.py` (out-of-band
`claude -p ping` auth probe), `bridge/state.py` (atomic JSON state),
`bridge/telegram.py`, `bridge/parse.py`. The Python is path-portable (uses
`$HOME` / `Path.home()`); the shell scripts use `$HOME`.

**Deploy model:** the bridge runs directly from the clone, so `git pull`
updates bridge code. `scripts/`, `tools/`, and `monitor/` are deployed *out* to
`~/agents/` by SETUP (a `cp -r`); re-copy after a pull if you want their updates.
`monitor/CLAUDE.md` is meant to be edited — keep your customized copy at
`~/agents/monitor/`.

## Self-healing — 4 layers

| # | Failure mode | Caught by | Recovery time |
|---|---|---|---|
| 1 | `claude` exits | `monitor-loop.sh` while-true | ~5s |
| 1 | `telegram-bridge.py` exits | `bridge-loop.sh` while-true | ~5s |
| 2 | python **hangs** (httpx long-poll wedge) | `bridge-loop.sh` heartbeat watchdog (SIGKILL if heartbeat-log > 120s stale) | ≤~120s |
| 3 | whole **tmux session** dies | `ClaudeAgentMonitor` / `ClaudeAgentBridge` tasks (every **2 min**) → `start-bridge.sh` | ≤~2min |
| 4 | whole **WSL2 VM** dies (sleep/Modern-Standby teardown) | same tasks boot the VM on next tick / on logon | ≤~2min |

Layer 3/4 cadence is the historical weak point: if the tasks repeat every 2
*hours* (a previous default), a death stays dark up to 2h. The setup registers
them at **`PT2M`**. A full-session kill recovers automatically in **80–110s**.

### Recovery design notes (why it's shaped this way)
- **`start-bridge.sh` is the single idempotent entrypoint** for *both* tasks and
  for manual revival. It ensures session + monitor + bridge window. (Don't point
  the monitor task at `start-monitor.sh` alone — that restores only the monitor
  and leaves the bridge lagging.)
- **Mutual exclusion is an atomic `mkdir` PID-lock, never `flock`.** An `flock`
  fd is inherited by the tmux server the first time `start-monitor` spawns it,
  pinning the lock for the server's whole life and silently breaking all later
  recovery. A lock *directory* has no fd to inherit; it's removed by an EXIT
  trap, with a PID-liveness check to steal a SIGKILL-stale lock.
- The bridge **persists before inject** and the monitor **drains on startup**,
  so a death between receive and reply loses nothing.

## Run / test / verify

```bash
# tests (hermetic — pass with or without a live agents session)
cd ~/agents/bridge && .venv/bin/python -m pytest -q        # 128 passed

# health snapshot
echo "heartbeat age: $(( $(date +%s) - $(stat -c %Y ~/agents/logs/bridge-heartbeat.log) ))s"   # healthy < 60s
pgrep -af telegram-bridge.py                                # exactly one
tmux list-windows -t agents                                 # monitor + bridge
grep '"direction": "outbound"' ~/agents/logs/bridge-messages.jsonl | tail   # replies flowing

# manual revival (full stack, lock-safe) — same script the tasks run
bash ~/agents/scripts/start-bridge.sh
```

## Operational runbook

- **"It's down / phone got no reply":** run the health snapshot. If no `agents`
  session or no bridge proc, run the manual-revival line above (or wait ≤2min
  for the task). Check `~/agents/logs/bridge-restart.log` and `start-bridge.log`.
- **Recurs roughly daily around early-AM:** that's Windows sleep/Modern-Standby
  tearing down the VM (Kernel-Power 42/107). Recovery is automatic in ≤2min; if
  you want it gone entirely, disable sleep on AC in Windows power settings.
- **`bridge-restart.log` shows `WATCHDOG: heartbeat stale`:** the python proc
  hung and was force-restarted — one prevented dark window. Investigate httpx
  timeouts if frequent.
- **Two pollers / Telegram `409 Conflict`:** something else is polling the same
  bot token (plugin re-enabled, a second bridge, a stray test bot). Kill the
  duplicate. The mkdir-lock prevents the task-vs-task race.
- **Restart ONLY claude (not the whole stack):** target the `claude` PID whose
  parent is `monitor-loop.sh` — never a child of the tmux *server* (killing that
  takes down both panes). Or just use the full revival recipe.

## State & logs

| File | Purpose |
|---|---|
| `~/.agents/inbox/*.txt` | pending inbound (drained to `processed/`, aged → `.deferred/`) |
| `~/.agents/bridge/state-prod.json` | offset, health, replay/inbox tracking (atomic, 0600) |
| `~/agents/logs/bridge-heartbeat.log` | liveness mtime (every ~30s) |
| `~/agents/logs/bridge-messages.jsonl` | inbound + outbound audit |
| `~/agents/logs/bridge-{restart,errors}.log` | supervisor + poll logs |
| `~/agents/logs/start-bridge.log` | task-fire forensic (rotated at 512KB) |
