# nightclaude

Queue heavy Claude Code tasks during the day, run them automatically at night
when you are not using your Pro quota interactively.

Runs either standalone on one machine, or split across two: your PC queues
tasks (**controller**) and a Raspberry Pi executes them at night (**worker**),
so the PC can stay off.

## How it works

- `nightclaude add` writes a task file (Markdown with frontmatter metadata,
  prompt as body) to `~/.local/share/nightclaude/tasks/`.
- A systemd **user timer** fires at 01:30 and runs `nightclaude run`.
- The runner executes pending tasks in priority + dependency order via
  `claude -p` (headless Claude Code, same Pro login as your terminal).
- If a task hits the rate limit, the runner sleeps until the limit resets and
  continues — but never starts a task after the morning **cutoff** (default
  07:00), so your daytime quota window stays untouched.
- Failed tasks are retried on the next night (up to `max_attempts`).

Note: night runs avoid the 5-hour session window but still count toward the
**weekly** Pro cap. Use `max_tasks_per_night` in the config to pace yourself.

## Install

```bash
./install.sh
```

This symlinks the CLI to `~/.local/bin/nightclaude`, copies the example config
to `~/.config/nightclaude/config.toml` (if none exists), and enables the
systemd user timer.

In standalone (worker) mode the machine must be on at night. If it suspends,
either disable suspend at night or schedule a wake, e.g. with a cron entry
running `rtcwake -m no -t $(date -d 'tomorrow 01:25' +%s)`.

## Remote worker (Raspberry Pi)

With `role = "controller"` and a `[remote]` host in the config, the PC never
runs tasks itself:

- `nightclaude add` writes the task and immediately **pushes** it: the task
  files and the task's whole workdir are rsynced to the Pi (workdir mirrored
  under `~/nightclaude-work/<original path>`). If the Pi is unreachable you
  get a note — run `nightclaude push` later (e.g. before shutting down).
- The Pi's own timer runs the queue at night, exactly like standalone mode
  (rate-limit waiting, cutoff, retries).
- On PC boot/login, `nightclaude-pull.service` runs `nightclaude pull`:
  statuses and logs come back, and finished tasks' mirrors are rsynced onto
  the original workdirs. Pull uses `rsync -u`, so files you edited on the PC
  in the meantime are never overwritten. If the Pi is off, pull is a silent
  no-op; run `nightclaude pull` manually any time.
- `push` always pulls first, so a task the Pi already finished is never reset
  to pending by a stale local copy. `remove` also deletes the task on the Pi.

### Pi setup (once)

1. 64-bit Raspberry Pi OS, reachable via ssh with key auth:
   `ssh-copy-id pi@raspberrypi.local`
2. Set the real host in `~/.config/nightclaude/config.toml` under `[remote]`,
   then deploy: `./deploy-pi.sh pi@raspberrypi.local`
3. Install Claude Code on the Pi and log in with your subscription:
   `curl -fsSL https://claude.ai/install.sh | bash`, then run `claude` and
   use `/login` (URL + paste-code flow works over ssh).
4. Re-run `./install.sh` on the PC (enables pull-on-boot, disables the local
   night timer).

The deploy script also enables systemd *lingering* on the Pi so the timer
fires without anyone logged in. A Pi 3/4/5 idles at ~2-4 W — running Claude
Code on it is fine since the heavy lifting happens on Anthropic's side.

`nightclaude run --local` still runs the queue on the PC if the Pi is ever
unavailable (uses the original workdirs).

## Usage

```bash
# Queue a task (prompt inline, from file, or piped via stdin)
nightclaude add --title "Lecture 5 to Obsidian" \
    --workdir ~/Obsidian/Uni \
    --prompt-file ~/prompts/lecture5.md

# A dependent task: flash cards only after the notes exist
nightclaude add --title "Flash cards lecture 5" \
    --workdir ~/Obsidian/Uni \
    --depends-on 20260702-2130 \
    --prompt "Create Anki-style flash cards from notes/lecture-05.md ..."

nightclaude list            # open tasks
nightclaude show <id>       # full task file (id prefixes are accepted)
nightclaude edit <id>       # open in $EDITOR
nightclaude log <id>        # claude's output for that task
nightclaude status          # queue summary + last run log
nightclaude retry <id>      # re-queue a failed/done task
nightclaude remove <id>

nightclaude run --force     # run the queue right now, ignoring the cutoff
```

Task files are plain text — you can also create or edit them directly in
`~/.local/share/nightclaude/tasks/`.

## Writing good night prompts

The task runs unattended, so the prompt must be self-contained:

- Name concrete input files/dirs and where output should go.
- State completion criteria ("one note per chapter, flash cards in
  cards/lecture-05.md").
- Default permission mode is `acceptEdits` (file edits allowed, shell commands
  blocked). Add `--permission-mode bypassPermissions` per task if it needs to
  run commands — only for prompts you wrote yourself.

## Monitoring

```bash
nightclaude status
systemctl --user list-timers nightclaude.timer
journalctl --user -u nightclaude.service --since today
```
