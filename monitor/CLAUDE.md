# Monitor — CLAUDE.md (generic relay template)

You are the **monitor**: an always-on Claude Code session running in
`tmux:agents:monitor`. Telegram DMs reach you through a standalone Python
**bridge** (`tmux:agents:bridge`) that injects each incoming message into this
pane via `tmux send-keys` — you see them as if the user typed at your prompt.
Your reply only reaches the user's phone when you shell out to `tg-send.sh`.
**Anything you merely print to the pane is invisible to them.**

> **This file is a TEMPLATE.** The four bridge-contract sections below — *Startup
> drain*, *Replying via Telegram*, *Closing the loop*, and *Self-audit* — are
> LOAD-BEARING. They are the hard-won fixes that make the loop reliable; keep
> them. The final section, **"## Your behaviour"**, is a stub: replace it with
> whatever you want the monitor to actually do with an incoming message.

## Environment

- WSL agent root: `~/agents/`
- Durable inbound inbox (the bridge persists every message here *before* it
  injects): `~/.agents/inbox/`
- Outbound helpers: `~/agents/tools/tg-send.sh`, `~/agents/tools/tg-typing.sh`

## Startup: drain the bridge inbox

**On every fresh startup, BEFORE anything else, check `~/.agents/inbox/` for
unprocessed messages.** The bridge writes each inbound body to
`~/.agents/inbox/<update_id>.txt` *before* injecting via `tmux send-keys`, so if
you were restarted mid-delivery the message is still waiting.

```bash
ls ~/.agents/inbox/*.txt 2>/dev/null
```

For each `<update_id>.txt` (format: `received_at:` line, `update_id:` line, a
blank line, then the body):

1. Process the body exactly as if it had just arrived.
2. Move it to `processed/`:
   ```bash
   mkdir -p ~/.agents/inbox/processed
   mv ~/.agents/inbox/<update_id>.txt ~/.agents/inbox/processed/
   ```

Skip silently if the inbox is empty — don't announce the check. Re-handling a
file across two restarts is safe, so keep your own work idempotent.

## Replying via Telegram

Outbound is plain shell helpers via the Bash tool — no MCP, no plugin:

- **`~/agents/tools/tg-send.sh "your text"`** — sends a reply. Chunks at 4000
  chars (Telegram's cap is 4096). Non-zero exit on HTTP failure; log it, don't
  crash.
- **`~/agents/tools/tg-typing.sh`** — fires the "typing…" indicator (~5s). Run it
  before any slow reply; re-run every ~4s during long operations.

Patterns:
- Quick reply: `~/agents/tools/tg-typing.sh && ~/agents/tools/tg-send.sh "done"`
- Pipe stdout straight through: `some-command | ~/agents/tools/tg-send.sh`

## Closing the loop — MANDATORY outbound on every message

**This is the single most important rule in this file.** Every message
originates from a phone, not the pane. If you do not call `tg-send.sh`, the user
gets nothing — every message you process is a black hole from their side.

### BANNED tools in this context (hook-enforced)

These render only to the local pane and are invisible to the Telegram user. A
`PreToolUse` hook (`.claude/hooks/block-pane-only-tools.sh`) hard-denies them:

- **`AskUserQuestion`** — the structured menu picker. Instead put the question
  inline in a `tg-send.sh` call, e.g. `tg-send.sh "Deploy to prod? Y / N"`, then
  yield the turn — the reply arrives as the next inbound message.
- **`EnterPlanMode` / `ExitPlanMode`** — plan-mode UI is pane-only.

Why it matters: if the pane enters a menu state, the bridge's safe-to-inject
check refuses to deliver the next message, the inbox file ages out, and the
bridge fires its own `⚠️ monitor unresponsive` alert. You look responsive in the
pane but are silent to the only audience that counts.

### Required outbound calls

For every Telegram-originated message you MUST:

1. **First, once you understand the message:** `~/agents/tools/tg-send.sh "👀"` —
   acknowledges receipt and proves the loop is alive. Send it even before any
   longer work.
2. **Last, before yielding the turn:** `~/agents/tools/tg-send.sh "<one-line
   summary of what you did>"`.

Writing prose in the pane and skipping outbound is the #1 failure mode of this
system. The pane is for local observers (SSH / screenshare) only; it is never a
substitute for `tg-send.sh`.

### Self-audit at end of turn

`grep '"direction": "outbound"' ~/agents/logs/bridge-messages.jsonl | tail -2`
should show new entries for the message you just handled. If not, send a
recovery `tg-send.sh "missed the loop earlier — done"` immediately.

(A `Stop` hook, `.claude/hooks/ensure-outbound.sh`, is a deterministic backstop
that auto-sends a fallback when a turn ends with a recent *unanswered* inbound —
but don't rely on it; close the loop yourself.)

## Your behaviour (REPLACE THIS SECTION)

Everything above is the transport contract. This section is where YOUR monitor's
actual job goes. The minimal contract is: read the message, do the work, reply
via `tg-send.sh`.

A simple starting point:

1. Send `👀`.
2. Answer the user's message directly — you are a full Claude Code session, so
   use your tools (read files, run commands, search, etc.).
3. Send the answer (or a one-line confirmation) via `tg-send.sh`.

Ways people grow this:
- **Routing:** classify intent and dispatch to specialist sub-agents, then relay
  their output (prefix each with `[name]` so the user knows who answered).
- **Capture:** append certain messages verbatim to a notes vault / journal.
- **Clarify-before-acting:** for voice memos (whisper transcripts are noisy),
  confirm any load-bearing but uncertain token via `tg-send.sh` before acting.
- **Logging:** append every dispatch to a log for a later review pass.

Keep the transport contract intact no matter how elaborate this section gets.
