# nightclaude

Queue heavy Claude Code tasks during the day, run them automatically at night
when you are not using your Pro quota interactively.

Runs either standalone on one machine, or split across two: your PC queues
tasks (**controller**) and a second, always-on device executes them at night
(**worker**), so the PC can stay off. The worker can be any Linux machine
that runs Claude Code — a Raspberry Pi, a home server, a spare laptop.

## How it works

- `nightclaude add` writes a task file (Markdown with frontmatter metadata,
  prompt as body) to `~/.local/share/nightclaude/tasks/`.
- A scheduled job (systemd user timer on Linux, launchd agent on macOS) fires
  at 01:30 and runs `nightclaude run`.
- The runner executes pending tasks in priority + dependency order via
  `claude -p` (headless Claude Code, same Pro login as your terminal).
- If a task hits the rate limit, the runner sleeps until the limit resets and
  continues — but never starts a task after the morning **cutoff** (default
  07:00), so your daytime quota window stays untouched.
- Failed tasks are retried on the next night (up to `max_attempts`).

Note: night runs avoid the 5-hour session window but still count toward the
**weekly** Pro cap. Use `max_tasks_per_night` in the config to pace yourself.

## Setup

Requirements, on every machine that will *run* tasks:

- Linux (with systemd) or macOS
- Python 3.11+
- Claude Code installed and logged in with your subscription

Then pick a mode: **A** — everything on one machine, or **B** — queue on
the PC, run on a separate worker.

### A. Single machine

1. Clone and install:

   ```bash
   git clone https://github.com/leonard-schlenker/nightclaude.git
   cd nightclaude
   ./install.sh
   ```

   This symlinks the CLI to `~/.local/bin/nightclaude`, creates
   `~/.config/nightclaude/config.toml` from the example, and schedules the
   nightly run at 01:30.

2. *(Optional)* Adjust `~/.config/nightclaude/config.toml` — cutoff time,
   tasks per night, default model; every key is explained there.

3. Make sure the machine is awake at 01:30. If it normally suspends,
   schedule a wake-up:
   - Linux: a cron entry running
     `rtcwake -m no -t $(date -d 'tomorrow 01:25' +%s)`
   - macOS: `sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00`

   (If a Mac sleeps through 01:30 anyway, launchd fires the missed job on
   wake, but the runner notices the daytime start and exits without touching
   your quota.)

Done — queue your first task (see [Usage](#usage)).

### B. Controller (PC) + worker

The worker is any Linux machine that can stay on at night. It needs very
little compute — the heavy lifting happens on Anthropic's side — so even a
Raspberry Pi 3/4/5 (64-bit OS, idles at ~2-4 W) works fine. To use a Mac as
the worker instead, follow **A** on it directly (the deploy script in step 4
assumes systemd) and only do steps 1-3 here.

On the **PC**:

1. Clone the repo and create the config:

   ```bash
   git clone https://github.com/leonard-schlenker/nightclaude.git
   cd nightclaude
   mkdir -p ~/.config/nightclaude
   cp config.example.toml ~/.config/nightclaude/config.toml
   ```

2. In `~/.config/nightclaude/config.toml`, uncomment `role = "controller"`
   and the `[remote]` section, and set `host` to the worker's ssh
   destination (e.g. `pi@raspberrypi.local`).

3. Install:

   ```bash
   ./install.sh
   ```

   In controller mode this enables pull-on-login and no local night run.

4. Give the PC passwordless ssh access to the worker, then deploy:

   ```bash
   ssh-copy-id user@worker.local
   ./deploy-worker.sh user@worker.local
   ```

   This copies nightclaude to the worker, enables its night timer, and turns
   on systemd lingering so the timer fires with nobody logged in (asks for
   the worker's sudo password once).

On the **worker**:

5. Install Claude Code and log in with your subscription — the URL +
   paste-code `/login` flow works over ssh:

   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   claude        # then type /login
   ```

Check the result from the PC: `nightclaude status` should report
`worker: user@worker.local (reachable)`.

## How controller and worker cooperate

- `nightclaude add` writes the task and immediately **pushes** it: the task
  files and the task's whole workdir are rsynced to the worker (workdir
  mirrored under `~/nightclaude-work/<original path>`). If the worker is
  unreachable you get a note — run `nightclaude push` later (e.g. before
  shutting down).
- The worker's own timer runs the queue at night, exactly like standalone
  mode (rate-limit waiting, cutoff, retries).
- On PC login, a pull job (`nightclaude-pull.service` on Linux,
  `com.nightclaude.pull` on macOS) runs `nightclaude pull`:
  statuses and logs come back, and finished tasks' mirrors are rsynced onto
  the original workdirs. Pull uses `rsync -u`, so files you edited on the PC
  in the meantime are never overwritten. If the worker is off, pull is a
  silent no-op; run `nightclaude pull` manually any time.
- `push` always pulls first, so a task the worker already finished is never
  reset to pending by a stale local copy. `remove` also deletes the task on
  the worker.
- `nightclaude run --local` still runs the queue on the PC if the worker is
  ever unavailable (uses the original workdirs).

## Sandboxing (remote worker)

`deploy-worker.sh` installs a systemd drop-in
(`systemd/nightclaude-sandbox.conf`) that confines night runs at the kernel
level: for the runner and every `claude` it starts, the entire filesystem is
read-only except the mirrored workdirs (`~/nightclaude-work`), nightclaude's
own data dir, and claude's session state (`~/.claude`, `~/.claude.json`).
This holds even for tasks queued with `--permission-mode bypassPermissions` —
a task can trash its own work area at worst, never the system.

Two consequences to be aware of:

- Tasks can read the whole system but only write under `~/nightclaude-work`.
  A task added directly on the worker with a workdir elsewhere will fail its
  writes; queue tasks from the controller (or keep workdirs under the mirror
  root).
- claude's self-updater is disabled on the worker (the read-only filesystem
  would make it fail anyway); re-run `deploy-worker.sh` to update.

Single-machine mode is not sandboxed by default because task workdirs are
arbitrary local directories. To sandbox it anyway, copy the drop-in to
`~/.config/systemd/user/nightclaude.service.d/sandbox.conf` and add a
`ReadWritePaths=` line for each directory you queue tasks in
(`systemctl --user daemon-reload` afterwards). macOS/launchd has no
equivalent; there the permission mode is the only guard.

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

# Linux
systemctl --user list-timers nightclaude.timer
journalctl --user -u nightclaude.service --since today

# macOS
launchctl print "gui/$(id -u)/com.nightclaude.run"   # loaded? next run?
cat ~/.local/share/nightclaude/logs/launchd-run.log
```
