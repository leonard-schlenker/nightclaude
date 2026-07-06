# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this tool is

nightclaude lets a Claude Pro subscriber queue heavy Claude Code tasks during
the day and run them unattended at night, when the interactive quota is not
being used. Tasks run via `claude -p` (headless). Two deployment modes, chosen
by `role` in the config:

- **worker** (default): this machine runs the queue itself, started nightly at
  01:30 by a scheduler.
- **controller**: this machine only queues tasks and rsyncs them (plus their
  workdirs) over ssh to a remote worker — typically a low-power always-on box
  like a Raspberry Pi — and pulls results back on login.

Core promise: never touch the user's daytime quota. The runner stops at the
morning `cutoff` (default 07:00), waits out rate limits, and refuses daytime
starts (see guard below).

## Development commands

There is no build, lint, or test suite. Checks used before committing:

```bash
bash -n install.sh deploy-worker.sh
python3 -c "import ast; ast.parse(open('nightclaude.py').read())"
python3 -c "import tomllib; tomllib.loads(open('config.example.toml').read())"
```

Run the CLI against throwaway state instead of the user's real queue
(`~/.local/share/nightclaude` and `~/.config/nightclaude/config.toml`):

```bash
S=$(mktemp -d)
NIGHTCLAUDE_DATA=$S NIGHTCLAUDE_CONFIG=$S/config.toml \
    python3 nightclaude.py add --title test --workdir . --prompt "..."
NIGHTCLAUDE_DATA=$S NIGHTCLAUDE_CONFIG=$S/config.toml \
    python3 nightclaude.py run --force
```

`run` without `--force` exits immediately during the day (by design). `run
--force` actually invokes the `claude` binary and spends quota; point
`claude_bin` in the test config at a stub script to test runner logic. `push`
and `pull` accept `--dry-run` to print the ssh/rsync commands instead of
running them. Do not run `./install.sh` casually — it enables timers/agents on
this machine.

`install.sh` is an interactive wizard when stdin is a tty (it writes the
config from the user's answers); with piped stdin it falls back to the old
non-interactive behavior (keep existing config, else copy the example). In
interactive mode it requires `claude` on PATH up front — offering to
curl-install it, aborting if declined — and afterwards offers to install the
skill; when testing with a PATH that lacks `claude`, feed the extra answers
(or stub `curl`). Test either path without touching the real system by
faking `HOME`, stubbing the system commands on `PATH`, and using `script` to
provide a pty:

```bash
S=$(mktemp -d); mkdir "$S/bin" "$S/home"
for c in systemctl launchctl ssh scp ssh-copy-id rsync; do
    printf '#!/bin/sh\necho "[stub %s] $*"\n' "$c" > "$S/bin/$c"; chmod +x "$S/bin/$c"
done
printf '1\n\n\n\ny\n' | script -qec "env HOME=$S/home PATH=$S/bin:$PATH ./install.sh" /dev/null
```

## Architecture

`nightclaude.py` is the entire program and **must stay a single, stdlib-only,
Python 3.11+ file**: `deploy-worker.sh` installs it by scp'ing just this one
file to the worker's `~/.local/bin/nightclaude`. Everything else in the repo
is packaging (install/deploy scripts, systemd units, launchd plists, example
configs) plus `skills/nightclaude/SKILL.md`, a Claude Code skill teaching
Claude to queue night tasks from conversations — `install.sh` offers to
symlink it into `~/.claude/skills/`. Keep its CLI examples in sync with the
`add` flags in `nightclaude.py`.

### Task files are the database

A task is one Markdown file in `$NIGHTCLAUDE_DATA/tasks/`: naive `key: value`
frontmatter (no YAML library — values must stay single-line) plus the prompt
as the body. All state transitions (pending → running → done/failed) are
rewrites of that file, which is what makes rsync the entire sync protocol.
Only keys listed in `write_task()` are persisted; runtime-only fields start
with `_` and are stripped on save. The only other mutable state is
`runner.lock` and the logs directory.

### Controller ↔ worker sync (the subtle part)

- Several controllers can share one worker. Every task is stamped with a
  `controller` id at `add` time (`controller_id` config key, hostname by
  default), and all sync is scoped to it: `push` only sends this machine's
  tasks, `pull` asks the worker (a remote `grep` over the task files, see
  `remote_owned_task_files()`) which task files it owns and fetches only
  those plus their logs. Tasks without a `controller` key are legacy,
  visible to every controller, and claimed by the first `push` from a
  machine where the task's workdir exists. The worker runner ignores the
  stamp and just runs everything in `tasks/`.
- On `push`, each pending task's `workdir` is rewritten to the worker-side
  mirror path (`nightclaude-work/<controller>/<original absolute path>`,
  relative to the worker's home) and the original is kept in
  `origin_workdir`. This rewrite happens once; the presence of
  `origin_workdir` marks an already-pushed task.
- `push` always pulls first so a task the worker finished is never reset to
  pending by a stale local copy. `pull` copies task files/logs back, then
  rsyncs finished tasks' mirrors onto the original workdirs with `-u` (never
  overwrite files newer on the controller) — only for tasks this controller
  owns and whose `origin_workdir` exists locally.
- `run --local` on a controller sets `_use_origin` so pushed tasks run in
  their original workdirs, not the (nonexistent locally) mirror paths.

### Runner protections

All in `_run_queue()`/`run_one()`:

- Morning cutoff; also a daytime-start guard: if the cutoff is more than 10 h
  away the run is a wake-from-sleep catch-up (launchd fires missed jobs on
  wake) and exits. `--force` bypasses both.
- Rate limits are detected by regex on claude's JSON output/stderr; the task
  is reset to pending (attempt not counted) and the runner sleeps until the
  parsed reset epoch (`limit reached|<epoch>`) or `retry_wait_minutes`.
- Every run appends `NIGHT_SYSTEM_PROMPT` (top of the file) via
  `--append-system-prompt`, with `{workdir}`/`{title}`/`{timeout_minutes}`
  filled by plain string replacement — not `str.format()`, so braces in
  prompts are safe. Overridable/disableable via `system_prompt` in config.

### Duplicated-by-design places to keep in sync

- **Scheduling exists twice**: systemd units (`systemd/`) for Linux and
  launchd plists (`launchd/`, with `__HOME__` substituted by `install.sh` at
  install time) for macOS. The 01:30 start time is hardcoded in both and in
  the README.
- **The worker sandbox** (`systemd/nightclaude-sandbox.conf`, installed only
  by `deploy-worker.sh`, not by `install.sh`) makes the filesystem read-only
  for night runs except an allowlist. Its `ReadWritePaths` hardcode the
  defaults of `[remote] work_root` and the data dir — if the runner gains a
  new write location or those defaults change, the drop-in must be updated or
  night runs fail with EROFS. It also sets `DISABLE_AUTOUPDATER=1`; worker
  updates happen only via re-running `deploy-worker.sh`.
- `config.worker.toml` is the config `deploy-worker.sh` seeds the worker
  with when run standalone; the `install.sh` wizard instead passes a
  generated worker config as `deploy-worker.sh`'s optional second argument
  (an existing config on the worker is never overwritten). The wizard also
  writes the local config itself (`write_runner_config`/
  `write_controller_config` in `install.sh`); `config.example.toml` remains
  the documented reference and the non-interactive fallback. New config keys
  need a commented entry in the example, a default in `DEFAULT_CONFIG` (the
  code never assumes a key exists in the file), and — if users should set
  them at install time — a prompt in the wizard's config writers.
