#!/usr/bin/env python3
"""nightclaude — queue Claude Code tasks during the day, run them at night.

Tasks are Markdown files with a simple frontmatter header (metadata) and the
prompt as the body. They live in ~/.local/share/nightclaude/tasks/ and can be
edited by hand at any time.

Usage:
  nightclaude add --title "Convert lecture 5" [--prompt "..."|--prompt-file f]
                  [--workdir DIR] [--depends-on ID,ID] [--model MODEL]
                  [--priority N] [--permission-mode MODE] [--timeout MIN]
  nightclaude list [--all]
  nightclaude show ID
  nightclaude edit ID
  nightclaude log ID
  nightclaude retry ID          # reset a failed/done task to pending
  nightclaude remove ID
  nightclaude run [--force]     # execute the queue (what the timer calls)
  nightclaude status            # summary + last runner log tail
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

DATA_DIR = Path(os.environ.get("NIGHTCLAUDE_DATA", Path.home() / ".local/share/nightclaude"))
TASKS_DIR = DATA_DIR / "tasks"
LOGS_DIR = DATA_DIR / "logs"
CONFIG_FILE = Path(os.environ.get("NIGHTCLAUDE_CONFIG", Path.home() / ".config/nightclaude/config.toml"))

DEFAULT_CONFIG = {
    # Runner refuses to start new tasks after this time (24h clock, local time).
    "cutoff": "07:00",
    # Minutes a single task may run before it is killed and marked failed.
    "task_timeout_minutes": 90,
    # When a rate limit is hit and no reset time can be parsed, wait this long.
    "retry_wait_minutes": 30,
    # 0 = no cap. Otherwise stop after this many completed tasks per night
    # (protects the weekly quota).
    "max_tasks_per_night": 0,
    # How often a failing task is retried on subsequent nights.
    "max_attempts": 2,
    "default_model": "sonnet",
    "default_permission_mode": "acceptEdits",
    "claude_bin": "claude",
    # "worker": this machine runs the queue at night (default, also the Pi).
    # "controller": this machine only queues tasks and syncs them to the
    # remote worker configured in the [remote] table.
    "role": "worker",
}

DEFAULT_REMOTE = {
    # ssh destination of the worker, e.g. "pi@raspberrypi.local"
    "host": "",
    # where workdirs are mirrored on the worker (relative to its home)
    "work_root": "nightclaude-work",
    # the worker's nightclaude data dir (relative to its home)
    "data_dir": ".local/share/nightclaude",
}

STATUSES = ("pending", "running", "done", "failed")


# ---------------------------------------------------------------- storage

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        cfg.update(tomllib.loads(CONFIG_FILE.read_text()))
    remote = dict(DEFAULT_REMOTE)
    remote.update(cfg.get("remote") or {})
    cfg["remote"] = remote
    return cfg


def parse_task(path: Path):
    text = path.read_text()
    m = re.match(r"\A---\n(.*?)\n---\n?", text, re.S)
    if not m:
        raise ValueError(f"{path.name}: missing frontmatter")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    meta["depends_on"] = [d for d in re.split(r"[,\s]+", meta.get("depends_on", "")) if d]
    meta["priority"] = int(meta.get("priority", 5))
    meta["attempts"] = int(meta.get("attempts", 0))
    meta["_path"] = path
    meta["_prompt"] = text[m.end():].strip()
    return meta


def write_task(meta: dict, prompt: str, path: Path):
    keys = ["id", "title", "status", "depends_on", "workdir", "origin_workdir", "model",
            "permission_mode", "priority", "timeout_minutes", "attempts",
            "created", "started", "finished", "cost_usd", "error"]
    lines = ["---"]
    for k in keys:
        v = meta.get(k, "")
        if isinstance(v, list):
            v = ", ".join(v)
        if v == "" and k not in ("id", "title", "status"):
            continue
        lines.append(f"{k}: {v}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n\n" + prompt.strip() + "\n")


def load_tasks():
    tasks = {}
    for p in sorted(TASKS_DIR.glob("*.md")):
        try:
            t = parse_task(p)
            tasks[t["id"]] = t
        except Exception as e:
            print(f"warning: skipping unreadable task file {p}: {e}", file=sys.stderr)
    return tasks


def save_task(t: dict):
    meta = {k: v for k, v in t.items() if not k.startswith("_")}
    write_task(meta, t["_prompt"], t["_path"])


def find_task(tasks, ident):
    if ident in tasks:
        return tasks[ident]
    matches = [t for t in tasks.values() if t["id"].startswith(ident)]
    if len(matches) == 1:
        return matches[0]
    sys.exit(f"error: {'ambiguous' if matches else 'unknown'} task id: {ident}")


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "task"


# ---------------------------------------------------------------- remote sync

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]


def remote_or_exit(cfg):
    r = cfg["remote"]
    if not r["host"]:
        sys.exit(f"error: no [remote] host configured in {CONFIG_FILE}")
    return r


def remote_reachable(r):
    return subprocess.run(["ssh", *SSH_OPTS, r["host"], "true"],
                          capture_output=True).returncode == 0


def remote_workdir(r, local_workdir):
    """Map a controller-side workdir to its mirror on the worker."""
    return r["work_root"].rstrip("/") + "/" + local_workdir.lstrip("/")


def _sync(cmds, dry):
    for cmd in cmds:
        if dry:
            print("would run:", " ".join(cmd))
            continue
        res = subprocess.run(cmd)
        if res.returncode != 0:
            sys.exit(f"error: command failed (exit {res.returncode}): {' '.join(cmd)}")


def do_push(cfg, dry=False):
    r = remote_or_exit(cfg)
    if not dry:
        if not remote_reachable(r):
            sys.exit(f"error: {r['host']} unreachable (worker off? ssh key missing?)")
        # Take over the worker's results first so a completed task is never
        # reset to pending by pushing a stale local status over it.
        do_pull(cfg)
    tasks = load_tasks()
    dirs = set()
    for t in tasks.values():
        if t["status"] != "pending":
            continue
        if not t.get("origin_workdir"):
            origin, mirror = t["workdir"], remote_workdir(r, t["workdir"])
            if not dry:
                t["origin_workdir"], t["workdir"] = origin, mirror
                save_task(t)
        else:
            origin, mirror = t["origin_workdir"], t["workdir"]
        dirs.add((origin, mirror))
    cmds = []
    for origin, mirror in sorted(dirs):
        cmds.append(["ssh", *SSH_OPTS, r["host"], f"mkdir -p '{mirror}'"])
        cmds.append(["rsync", "-a", origin.rstrip("/") + "/", f"{r['host']}:{mirror}/"])
    cmds.append(["ssh", *SSH_OPTS, r["host"],
                 f"mkdir -p '{r['data_dir']}/tasks' '{r['data_dir']}/logs'"])
    cmds.append(["rsync", "-a", str(TASKS_DIR) + "/", f"{r['host']}:{r['data_dir']}/tasks/"])
    _sync(cmds, dry)
    if not dry:
        n = len(dirs)
        print(f"pushed queue to {r['host']} ({n} pending task{'s' if n != 1 else ''})")


def do_pull(cfg, dry=False):
    r = remote_or_exit(cfg)
    cmds = [
        ["ssh", *SSH_OPTS, r["host"],
         f"mkdir -p '{r['data_dir']}/tasks' '{r['data_dir']}/logs'"],
        ["rsync", "-a", f"{r['host']}:{r['data_dir']}/tasks/", str(TASKS_DIR) + "/"],
        ["rsync", "-a", f"{r['host']}:{r['data_dir']}/logs/", str(LOGS_DIR) + "/"],
    ]
    _sync(cmds, dry)
    # Bring results home: sync finished tasks' mirrors back to the original
    # workdirs. -u never overwrites files that are newer on this machine.
    pulled = []
    for t in load_tasks().values():
        if t.get("origin_workdir") and t["status"] in ("done", "failed"):
            _sync([["rsync", "-au", f"{r['host']}:{t['workdir'].rstrip('/')}/",
                    t["origin_workdir"].rstrip("/") + "/"]], dry)
            pulled.append(f"{t['id']} [{t['status']}]")
    if not dry:
        print(f"pulled from {r['host']}: "
              + ("; ".join(pulled) if pulled else "no finished tasks"))


def cmd_push(args, cfg):
    do_push(cfg, dry=args.dry_run)


def cmd_pull(args, cfg):
    r = remote_or_exit(cfg)
    if not args.dry_run and not remote_reachable(r):
        print(f"{r['host']} unreachable, nothing pulled")
        return  # exit 0 so the on-boot service doesn't flag an error
    do_pull(cfg, dry=args.dry_run)


# ---------------------------------------------------------------- commands

def cmd_add(args, cfg):
    if args.prompt:
        prompt = args.prompt
    elif args.prompt_file:
        prompt = Path(args.prompt_file).read_text()
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        print("Enter the prompt, end with Ctrl-D:")
        prompt = sys.stdin.read()
    if not prompt.strip():
        sys.exit("error: empty prompt")

    tasks = load_tasks()
    deps = [d for d in re.split(r"[,\s]+", args.depends_on or "") if d]
    for d in deps:
        find_task(tasks, d)  # validate; exits on unknown

    now = dt.datetime.now()
    tid = f"{now:%Y%m%d-%H%M%S}-{slugify(args.title)}"
    meta = {
        "id": tid,
        "title": args.title,
        "status": "pending",
        "depends_on": [find_task(tasks, d)["id"] for d in deps],
        "workdir": str(Path(args.workdir).expanduser().resolve()) if args.workdir else str(Path.cwd()),
        "model": args.model or cfg["default_model"],
        "permission_mode": args.permission_mode or cfg["default_permission_mode"],
        "priority": args.priority,
        "timeout_minutes": args.timeout or cfg["task_timeout_minutes"],
        "attempts": 0,
        "created": now.isoformat(timespec="seconds"),
    }
    path = TASKS_DIR / f"{tid}.md"
    write_task(meta, prompt, path)
    print(f"queued: {tid}\n  file: {path}")

    if cfg["role"] == "controller" and cfg["remote"]["host"] and not args.no_push:
        if remote_reachable(cfg["remote"]):
            do_push(cfg)
        else:
            print(f"note: {cfg['remote']['host']} unreachable - "
                  "run `nightclaude push` before shutting down")


def cmd_list(args, cfg):
    tasks = load_tasks()
    if not tasks:
        print("no tasks queued")
        return
    shown = [t for t in tasks.values() if args.all or t["status"] in ("pending", "running", "failed")]
    if not shown:
        print("no open tasks (use --all to include finished ones)")
        return
    shown.sort(key=lambda t: (t["priority"], t.get("created", "")))
    for t in shown:
        deps = f"  deps: {', '.join(t['depends_on'])}" if t["depends_on"] else ""
        print(f"[{t['status']:>7}] p{t['priority']} {t['id']}  {t.get('title','')}{deps}")


def cmd_show(args, cfg):
    t = find_task(load_tasks(), args.id)
    print(t["_path"].read_text())


def cmd_edit(args, cfg):
    t = find_task(load_tasks(), args.id)
    editor = os.environ.get("EDITOR", "nano")
    subprocess.call([editor, str(t["_path"])])


def cmd_log(args, cfg):
    t = find_task(load_tasks(), args.id)
    log = LOGS_DIR / f"{t['id']}.log"
    if log.exists():
        print(log.read_text())
    else:
        print(f"no log yet for {t['id']}")


def cmd_retry(args, cfg):
    t = find_task(load_tasks(), args.id)
    t.update(status="pending", attempts=0, error="", started="", finished="")
    save_task(t)
    print(f"reset to pending: {t['id']}")


def cmd_remove(args, cfg):
    t = find_task(load_tasks(), args.id)
    t["_path"].unlink()
    print(f"removed: {t['id']}")
    r = cfg["remote"]
    if cfg["role"] == "controller" and r["host"]:
        gone = subprocess.run(
            ["ssh", *SSH_OPTS, r["host"], f"rm -f '{r['data_dir']}/tasks/{t['id']}.md'"],
            capture_output=True).returncode == 0
        print(f"  also removed on {r['host']}" if gone
              else f"  warning: could not remove on {r['host']} - it may still run there")


def cmd_status(args, cfg):
    tasks = load_tasks()
    counts = {}
    for t in tasks.values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    print("queue:", ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "empty")
    if cfg["role"] == "controller" and cfg["remote"]["host"]:
        r = cfg["remote"]
        up = remote_reachable(r)
        print(f"worker: {r['host']} ({'reachable' if up else 'unreachable'})")
    runner_logs = sorted(LOGS_DIR.glob("runner-*.log"))
    if runner_logs:
        last = runner_logs[-1]
        print(f"\nlast run ({last.name}):")
        print("\n".join(last.read_text().splitlines()[-15:]))


# ---------------------------------------------------------------- runner

def runnable(t, tasks):
    if t["status"] != "pending":
        return False
    for d in t["depends_on"]:
        if tasks.get(d, {}).get("status") != "done":
            return False
    return True


def cutoff_time(cfg):
    h, m = map(int, cfg["cutoff"].split(":"))
    now = dt.datetime.now()
    cut = now.replace(hour=h, minute=m, second=0, microsecond=0)
    # Runs typically start after midnight; if started before midnight the
    # cutoff is tomorrow morning.
    if cut <= now:
        cut += dt.timedelta(days=1)
    return cut


RATE_LIMIT_RE = re.compile(r"(usage limit|rate limit)", re.I)
RESET_EPOCH_RE = re.compile(r"limit reached\|(\d{9,11})", re.I)


def run_one(t, cfg, log):
    prompt = t["_prompt"]
    cmd = [
        cfg["claude_bin"], "-p",
        "--output-format", "json",
        "--model", t.get("model") or cfg["default_model"],
        "--permission-mode", t.get("permission_mode") or cfg["default_permission_mode"],
    ]
    # On a controller running --local, pushed tasks point at the worker's
    # mirror path; use the original local workdir instead.
    workdir = t.get("workdir") or str(Path.home())
    if t.get("_use_origin") and t.get("origin_workdir"):
        workdir = t["origin_workdir"]
    # Pushed tasks carry worker-relative mirror paths; anchor them at home.
    workdir = Path(workdir).expanduser()
    if not workdir.is_absolute():
        workdir = Path.home() / workdir
    workdir = str(workdir)
    timeout = int(t.get("timeout_minutes") or cfg["task_timeout_minutes"]) * 60
    task_log = LOGS_DIR / f"{t['id']}.log"
    log(f"  cmd: {' '.join(cmd)} (cwd={workdir}, timeout={timeout//60}m)")
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              cwd=workdir, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "failed", f"timed out after {timeout // 60} minutes", None
    except FileNotFoundError:
        return "failed", f"claude binary not found: {cfg['claude_bin']}", None

    out = proc.stdout or ""
    err = proc.stderr or ""
    with task_log.open("a") as f:
        f.write(f"\n===== {dt.datetime.now().isoformat(timespec='seconds')} =====\n")
        f.write(out + ("\n--- stderr ---\n" + err if err.strip() else "") + "\n")

    result_text, cost = "", None
    try:
        data = json.loads(out)
        result_text = data.get("result", "") or ""
        cost = data.get("total_cost_usd")
        is_error = data.get("is_error", proc.returncode != 0)
    except (json.JSONDecodeError, AttributeError):
        is_error = proc.returncode != 0

    haystack = f"{result_text}\n{err}"
    if RATE_LIMIT_RE.search(haystack):
        m = RESET_EPOCH_RE.search(haystack)
        reset = dt.datetime.fromtimestamp(int(m.group(1))) if m else None
        return "ratelimited", haystack.strip()[:200], reset

    if is_error:
        return "failed", (result_text or err).strip()[:200] or f"exit code {proc.returncode}", cost
    return "done", "", cost


def cmd_run(args, cfg):
    if cfg["role"] == "controller" and not args.local:
        sys.exit("this machine is a controller; the remote worker runs the queue.\n"
                 "use `nightclaude run --local` to run it here anyway.")
    lock = DATA_DIR / "runner.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 12 * 3600:
        sys.exit("another runner appears to be active (runner.lock); remove it if stale")
    lock.write_text(str(os.getpid()))
    runner_log = LOGS_DIR / f"runner-{dt.datetime.now():%Y%m%d-%H%M%S}.log"

    def log(msg):
        line = f"{dt.datetime.now():%H:%M:%S} {msg}"
        print(line)
        with runner_log.open("a") as f:
            f.write(line + "\n")

    try:
        _run_queue(args, cfg, log)
    finally:
        lock.unlink(missing_ok=True)


def _run_queue(args, cfg, log):
    cut = cutoff_time(cfg)
    log(f"nightclaude run starting (cutoff {cut:%Y-%m-%d %H:%M}, force={args.force})")

    # Give failed tasks from previous nights another chance, within the limit.
    tasks = load_tasks()
    for t in tasks.values():
        if t["status"] == "failed" and t["attempts"] < int(cfg["max_attempts"]):
            t["status"] = "pending"
            save_task(t)
        elif t["status"] == "running":  # stale from a crashed run
            t["status"] = "failed"
            t["error"] = "runner crashed or was killed"
            save_task(t)

    completed = 0
    while True:
        if not args.force and dt.datetime.now() >= cut:
            log("cutoff reached, stopping")
            break
        cap = int(cfg["max_tasks_per_night"])
        if cap and completed >= cap:
            log(f"max_tasks_per_night ({cap}) reached, stopping")
            break

        tasks = load_tasks()
        ready = [t for t in tasks.values() if runnable(t, tasks)]
        if not ready:
            open_deps = [t["id"] for t in tasks.values() if t["status"] == "pending"]
            if open_deps:
                log(f"no runnable tasks; blocked on dependencies: {', '.join(open_deps)}")
            else:
                log("queue empty, done")
            break
        ready.sort(key=lambda t: (t["priority"], t.get("created", "")))
        t = ready[0]

        if getattr(args, "local", False):
            t["_use_origin"] = True
        log(f"running {t['id']} ({t.get('title', '')})")
        t["status"] = "running"
        t["started"] = dt.datetime.now().isoformat(timespec="seconds")
        t["attempts"] = t["attempts"] + 1
        save_task(t)

        status, error, extra = run_one(t, cfg, log)

        if status == "ratelimited":
            t["status"] = "pending"  # not the task's fault; retry after reset
            t["attempts"] = t["attempts"] - 1
            save_task(t)
            reset = extra or dt.datetime.now() + dt.timedelta(minutes=int(cfg["retry_wait_minutes"]))
            reset += dt.timedelta(minutes=2)  # small buffer past the reset
            if not args.force and reset >= cut:
                log(f"rate limited; reset {reset:%H:%M} is past cutoff, stopping for tonight")
                break
            log(f"rate limited; sleeping until {reset:%H:%M}")
            time.sleep(max(60, (reset - dt.datetime.now()).total_seconds()))
            continue

        t["status"] = status
        t["finished"] = dt.datetime.now().isoformat(timespec="seconds")
        t["error"] = error
        if isinstance(extra, (int, float)):
            t["cost_usd"] = f"{extra:.4f}"
        save_task(t)
        if status == "done":
            completed += 1
            log(f"  -> done")
        else:
            log(f"  -> failed: {error}")

    tasks = load_tasks()
    summary = {}
    for t in tasks.values():
        summary[t["status"]] = summary.get(t["status"], 0) + 1
    log("finished: " + ", ".join(f"{v} {k}" for k, v in sorted(summary.items())))


# ---------------------------------------------------------------- main

def main():
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()

    p = argparse.ArgumentParser(prog="nightclaude", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="queue a new task")
    a.add_argument("--title", required=True)
    a.add_argument("--prompt", help="prompt text (or use --prompt-file / stdin)")
    a.add_argument("--prompt-file")
    a.add_argument("--workdir", help="directory Claude runs in (default: cwd)")
    a.add_argument("--depends-on", help="comma-separated task ids (prefixes ok)")
    a.add_argument("--model", help=f"claude model (default: {cfg['default_model']})")
    a.add_argument("--priority", type=int, default=5, help="1=first .. 9=last (default 5)")
    a.add_argument("--permission-mode", help=f"default: {cfg['default_permission_mode']}")
    a.add_argument("--timeout", type=int, help="minutes before the task is killed")
    a.add_argument("--no-push", action="store_true",
                   help="don't sync to the remote worker right away")
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="list open tasks")
    l.add_argument("--all", action="store_true", help="include done tasks")
    l.set_defaults(func=cmd_list)

    for name, fn, hlp in [("show", cmd_show, "print a task file"),
                          ("edit", cmd_edit, "open a task in $EDITOR"),
                          ("log", cmd_log, "print a task's claude output log"),
                          ("retry", cmd_retry, "reset a task to pending"),
                          ("remove", cmd_remove, "delete a task")]:
        s = sub.add_parser(name, help=hlp)
        s.add_argument("id")
        s.set_defaults(func=fn)

    r = sub.add_parser("run", help="run the queue now (used by the systemd timer)")
    r.add_argument("--force", action="store_true", help="ignore the morning cutoff")
    r.add_argument("--local", action="store_true",
                   help="on a controller: run the queue on this machine")
    r.set_defaults(func=cmd_run)

    for name, fn, hlp in [("push", cmd_push, "sync queue + workdirs to the remote worker"),
                          ("pull", cmd_pull, "fetch results + statuses from the remote worker")]:
        s = sub.add_parser(name, help=hlp)
        s.add_argument("--dry-run", action="store_true", help="print sync commands only")
        s.set_defaults(func=fn)

    sub.add_parser("status", help="queue summary + last run log").set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args, cfg)


if __name__ == "__main__":
    main()
