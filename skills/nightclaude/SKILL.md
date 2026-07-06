---
name: nightclaude
description: Queue heavy or long-running Claude Code work to run unattended tonight via the nightclaude night queue, instead of doing it now. Use when the user says to do something "tonight", "overnight", "in the night run", or wants to defer quota-heavy work (bulk conversions, large refactors, doc generation) — and for managing the queue (list, status, log, usage, retry, remove).
---

# nightclaude — queue work for the night run

nightclaude runs queued tasks via headless Claude Code (`claude -p`) at night
(01:30 until a morning cutoff), when the user's interactive Pro quota is
idle. On a controller machine, `add` also pushes the task and its workdir to
a remote worker automatically.

Queue a task only when the user asks for it (or agrees to your suggestion).
You may suggest queuing when work is obviously heavy or the user mentions
quota concerns.

## Queueing a task

```bash
nightclaude add --title "Short title" --workdir /abs/path/to/project <<'EOF'
The prompt...
EOF
```

Prints `queued: <id>` — mention the id to the user. Prompt can also be given
with `--prompt "..."` or `--prompt-file file.md`; use the heredoc/stdin form
for anything long. Further options:

- `--depends-on <id>[,<id>...]` — run only after those tasks succeed (id
  prefixes are accepted).
- `--priority N` — 1 runs first … 9 last (default 5).
- `--model <name>` — override the configured default (usually sonnet).
- `--timeout N` — minutes before the task is killed (default from config).
- `--permission-mode bypassPermissions` — the default (`acceptEdits`) lets
  the task create/edit files in its workdir but blocks shell commands.
  Only add bypassPermissions when the task must run commands, and confirm
  with the user first.

## Writing the night prompt

The task runs unattended, in one shot, with no way to ask questions — the
prompt must be self-contained:

- Name the concrete input files/directories and where output must go
  (paths relative to the workdir).
- State completion criteria ("one note per chapter; flash cards in
  cards/lecture-05.md").
- Include any context the night run cannot discover from the workdir alone
  (decisions made in this conversation, conventions, examples).

Do not explain the night setup itself — a built-in system prompt already
tells the model it is running unattended and must finish without questions.

## Managing the queue

```bash
nightclaude list            # open tasks (--all includes finished)
nightclaude show <id>       # full task file
nightclaude log <id>        # claude's output for that task
nightclaude status          # queue summary, worker reachability, last run log
nightclaude usage           # tokens/cost per task, grouped by night
nightclaude retry <id>      # re-queue a failed/done task
nightclaude remove <id>
```

Results land the next morning: task status/logs sync back to a controller on
login (or with `nightclaude pull`). If `add` reports the worker unreachable,
tell the user to run `nightclaude push` before shutting down — nothing else
to do.

Never run `nightclaude run --force` on your own: it starts the queue
immediately and spends the user's daytime quota. Only do it when the user
explicitly asks.
