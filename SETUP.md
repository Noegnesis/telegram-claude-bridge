# Setup guide — Telegram ↔ Claude Code bridge (Windows + WSL2)

Stand up an always-on Telegram bot wired to a Claude Code session. DM the bot
from your phone → a `claude` instance on your PC does the work → reply comes
back to your phone. Self-heals: app crash, process hang, tmux-session death,
and full WSL2-VM teardown all recover automatically in ≤~2 minutes.

This is **clone-and-go**: the supervision scripts and a generic monitor brain
ship in the repo. You clone, deploy the helper trees, drop in your bot token,
customize one section of the monitor brain, and register two scheduled tasks.

> **Substitution:** every `emman` / WSL home path below = **your WSL username**.
> In the Windows PowerShell step, the scheduled-task argument hardcodes a WSL
> user + path — change both. This guide uses the tmux session name `agents`.

---

## 0. Prerequisites

- **Windows 10/11** with **WSL2 + Ubuntu** (`wsl --install -d Ubuntu`).
- Inside WSL: `sudo apt install tmux python3-venv`. (`mkdir`/`tmux` locking ship with the base system.)
- **Claude Code** installed and authenticated inside WSL — confirm `claude -p ping`
  returns JSON without a login error. (The bridge launches `claude` as the monitor.)
- A **Telegram bot**: message `@BotFather` → `/newbot` → save the **token**. DM your
  new bot once, then read your numeric **chat id** from
  `https://api.telegram.org/bot<TOKEN>/getUpdates` (it is `message.chat.id`).
- (Optional) `whisper` on PATH for voice-memo transcription.

> ⚠️ **Only ONE process may poll a bot token.** Do not also run the
> `telegram@claude-plugins-official` MCP plugin — it will fight the bridge for
> the token (Telegram `409 Conflict`). The bridge replaces the plugin entirely.

---

## 1. Clone the bridge + create its venv

```bash
mkdir -p ~/agents ~/agents/logs ~/agents/media/tg ~/.agents/{inbox/processed,bridge}
cd ~/agents
git clone <REPO-URL> bridge
cd bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # httpx, pytest, ...
.venv/bin/python -m pytest -q                     # expect: 128 passed
```

The bridge runs directly from this clone (so a later `git pull` updates bridge
code). The next step deploys the helper trees *out* of the clone.

---

## 2. Deploy the helper trees

The repo ships `scripts/`, `tools/`, and `monitor/` as templates. The runtime
expects them as siblings of `bridge/` under `~/agents/`. Copy them out:

```bash
cp -r ~/agents/bridge/scripts ~/agents/scripts
cp -r ~/agents/bridge/tools   ~/agents/tools
cp -r ~/agents/bridge/monitor ~/agents/monitor
chmod +x ~/agents/scripts/*.sh ~/agents/tools/*.sh ~/agents/monitor/.claude/hooks/*.sh
```

Resulting layout:

```
~/agents/
  bridge/      <- the git clone (bridge code + .venv + tests)
  scripts/     <- start-bridge.sh, start-monitor.sh, monitor-loop.sh
  tools/       <- tg-send.sh, tg-typing.sh   (the bridge venv is auto-detected)
  monitor/     <- CLAUDE.md (your brain) + .claude/{settings.json,hooks}
  logs/  media/
~/.agents/     <- runtime state: inbox/, bridge/
```

> To pick up later updates to the helper trees after a `git pull`, re-run the
> `cp -r` for `scripts/` and `tools/`. Do **not** blindly re-copy `monitor/` —
> you will have customized that in step 4; back it up first if you re-copy.

---

## 3. Telegram credentials

The bridge and the `tg-send.sh` / `tg-typing.sh` helpers read two variables —
**`TELEGRAM_BOT_TOKEN`** and **`PAIRED_USER_ID`** (your numeric chat id):

```bash
mkdir -p ~/.claude/channels/telegram
cat > ~/.claude/channels/telegram/.env <<'ENV'
TELEGRAM_BOT_TOKEN=123456:ABC-yourtoken
PAIRED_USER_ID=8873212188
ENV
chmod 600 ~/.claude/channels/telegram/.env
```

> The variable is `PAIRED_USER_ID`, not `TELEGRAM_ALLOWED_CHAT_ID`. The helpers
> run under `set -u`, so a wrong name fails fast with `PAIRED_USER_ID: unbound
> variable` on the first reply.

(Optional, for a parallel test bot during cutover: a second
`~/.claude/channels/telegram/.env.test` with a different token + chat id,
selected by `BRIDGE_ENV=test`.)

---

## 4. Customize the monitor brain

`~/agents/monitor/CLAUDE.md` is the claude session's instructions. It ships
**generic**: the top four sections are the load-bearing bridge contract (startup
inbox-drain, reply-via-`tg-send.sh`, closing-the-loop, self-audit) — **keep
those verbatim**. The final section, **"## Your behaviour (REPLACE THIS
SECTION)"**, is a stub. Replace it with whatever you want the monitor to do with
an incoming message: answer directly, route to sub-agents, capture to notes, etc.

The hooks ship ready to go and need no edits:
- `.claude/hooks/block-pane-only-tools.sh` — PreToolUse hook that hard-blocks
  `AskUserQuestion` / plan-mode (they render only to the pane, invisible to
  Telegram) and redirects to `tg-send.sh`.
- `.claude/hooks/ensure-outbound.sh` — Stop hook backstop: if a turn ends with a
  recent *unanswered* inbound, it auto-sends a fallback so you are never ghosted.
- `.claude/settings.json` — wires both via `$CLAUDE_PROJECT_DIR` (no absolute
  paths to edit).

> Without the monitor brain + hooks the bot receives but never replies — the
> "outbound loop closure" fixes are what make the round trip work.

---

## 5. Keep the WSL2 VM alive

Create `C:\Users\<youruser>\.wslconfig` (Windows side):

```ini
[wsl2]
vmIdleTimeout=-1
```

Then `wsl --shutdown` once so it takes effect. (This stops *idle* shutdown; it
does NOT stop *sleep* teardown — see the recovery model.)

---

## 6. Windows scheduled tasks (the layer-3/4 watchdog)

Run in **PowerShell** (no admin needed; runs as your interactive user). This
registers two tasks that each run `start-bridge.sh` at logon and **every 2
minutes**, on AC *or* battery. The 2-minute cadence bounds any dark window to
≤2 min. **Edit the `--user` and the path in `$act` to your WSL username.**

```powershell
$user = "$env:USERDOMAIN\$env:USERNAME"
$act  = New-ScheduledTaskAction -Execute 'wsl.exe' `
          -Argument '--user emman -- /home/emman/agents/scripts/start-bridge.sh'   # <-- change emman (x2)
$tLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$tRep   = New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Minutes 2) `
            -RepetitionDuration  (New-TimeSpan -Days 3650)   # NOT TimeSpan.MaxValue - Task Scheduler rejects it
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 72)
$prin = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive

foreach ($n in 'ClaudeAgentMonitor','ClaudeAgentBridge') {
  Register-ScheduledTask -TaskName $n -Action $act -Trigger @($tLogon,$tRep) `
    -Settings $set -Principal $prin -Force
}
```

> Both tasks running the same idempotent `start-bridge.sh` is intentional
> redundancy — if one is ever disabled, the other still recovers the whole
> stack. The `mkdir` lock makes concurrent fires safe (one creates, the rest
> skip). Do NOT enable `DisallowStartIfOnBatteries` — on a low/dead battery it
> blocks recovery.

Verify:

```powershell
Get-ScheduledTask ClaudeAgent* | ForEach-Object {
  $i = $_ | Get-ScheduledTaskInfo
  "{0}: Last={1} Result=0x{2:X} Next={3}" -f $_.TaskName,$i.LastRunTime,$i.LastTaskResult,$i.NextRunTime
}
```

---

## 7. Start & verify

```powershell
Start-ScheduledTask -TaskName ClaudeAgentBridge      # brings the whole stack up
```

```bash
# inside WSL:
tmux list-windows -t agents                          # -> monitor + bridge
pgrep -af telegram-bridge.py                          # exactly one
echo "hb age: $(( $(date +%s) - $(stat -c %Y ~/agents/logs/bridge-heartbeat.log) ))s"   # < 60s
```

Then DM the bot from your phone — you should get a `👀` then a reply, and see a
`"direction": "outbound"` line in `~/agents/logs/bridge-messages.jsonl`.

**Robustness self-test** (optional): `tmux kill-session -t agents`, then watch it
come back by itself within ~2 min:

```bash
while ! tmux has-session -t agents 2>/dev/null; do echo "down..."; sleep 10; done; echo "recovered"
```

---

## Recovery model & known issues

| Layer | Dies | Recovers via | Time |
|---|---|---|---|
| 1 | claude / python proc | while-true wrappers | ~5s |
| 2 | python hangs | bridge-loop heartbeat watchdog (SIGKILL) | ≤120s |
| 3 | tmux session | scheduled tasks → start-bridge.sh | ≤2min |
| 4 | WSL2 VM (sleep teardown) | scheduled tasks boot VM | ≤2min |

- **Roughly-daily early-AM death is normal:** Windows sleep / Modern-Standby tears
  down the WSL2 VM (check `Get-WinEvent -FilterHashtable @{LogName='System';Id=42,107}`).
  `vmIdleTimeout=-1` does not prevent this (sleep ≠ idle). It auto-recovers in
  ≤2 min. To eliminate it, disable sleep on AC in Windows power settings.
- **Manual full revival:** `bash ~/agents/scripts/start-bridge.sh` (lock-safe, full stack).
- **Never run two pollers on one token** (no MCP plugin alongside the bridge).
- **WSL UNC edits drop the exec bit** — `chmod +x` any `.sh` you edit from Windows.
