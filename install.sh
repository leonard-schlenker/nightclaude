#!/usr/bin/env bash
# Guided nightclaude setup. Asks a handful of questions, writes the config,
# schedules the nightly run (or pull-on-login in controller mode) and, for
# remote setups, can deploy the worker in the same go. Re-run it any time to
# reconfigure. With no terminal (piped stdin, scripts) it falls back to the
# non-interactive behavior: keep an existing config, otherwise copy
# config.example.toml as-is.
set -euo pipefail
cd "$(dirname "$0")"

CONFIG=${NIGHTCLAUDE_CONFIG:-$HOME/.config/nightclaude/config.toml}

# --- prompt helpers ---------------------------------------------------------

ask() { # ask <prompt> <default> -> answer on stdout
    local reply
    read -rp "$1 [$2]: " reply
    printf '%s' "${reply:-$2}"
}

ask_required() {
    local reply
    while true; do
        read -rp "$1: " reply
        [ -n "$reply" ] && { printf '%s' "$reply"; return; }
    done
}

ask_yn() { # ask_yn <question> <y|n default>; true if yes
    local hint reply
    [ "$2" = y ] && hint="Y/n" || hint="y/N"
    while true; do
        read -rp "$1 [$hint]: " reply
        case ${reply:-$2} in
            [Yy]*) return 0 ;;
            [Nn]*) return 1 ;;
        esac
    done
}

ask_time() { # 24h HH:MM, single-digit hour accepted
    local t
    while true; do
        t=$(ask "$1" "$2")
        [[ $t =~ ^[0-9]:[0-5][0-9]$ ]] && t="0$t"
        [[ $t =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] && { printf '%s' "$t"; return; }
        echo "  enter a 24h time like 07:00" >&2
    done
}

ask_int() {
    local n
    while true; do
        n=$(ask "$1" "$2")
        [[ $n =~ ^[0-9]+$ ]] && { printf '%s' "$n"; return; }
        echo "  enter a whole number" >&2
    done
}

# --- install steps ----------------------------------------------------------

install_cli() {
    mkdir -p ~/.local/bin "$(dirname "$CONFIG")" ~/.local/share/nightclaude/logs
    chmod +x nightclaude.py
    ln -sf "$PWD/nightclaude.py" ~/.local/bin/nightclaude
}

schedule_jobs() { # schedule_jobs worker|controller
    local controller=false
    [ "$1" = controller ] && controller=true

    if [ "$(uname)" = Darwin ]; then
        # macOS: launchd agents instead of systemd user units.
        mkdir -p ~/Library/LaunchAgents

        agent_load() {
            sed "s|__HOME__|$HOME|g" "launchd/$1.plist" > ~/Library/LaunchAgents/"$1.plist"
            launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true
            launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/"$1.plist"
        }
        agent_drop() {
            launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true
            rm -f ~/Library/LaunchAgents/"$1.plist"
        }

        if $controller; then
            agent_drop com.nightclaude.run
            agent_load com.nightclaude.pull
            echo "enabled pull-on-login (agent com.nightclaude.pull); no local night run"
        else
            agent_drop com.nightclaude.pull
            agent_load com.nightclaude.run
            echo "the queue runs nightly at 01:30 (agent com.nightclaude.run)"
        fi
    else
        mkdir -p ~/.config/systemd/user
        cp systemd/nightclaude.service systemd/nightclaude.timer \
           systemd/nightclaude-pull.service ~/.config/systemd/user/
        systemctl --user daemon-reload

        if $controller; then
            systemctl --user disable --now nightclaude.timer 2>/dev/null || true
            systemctl --user enable nightclaude-pull.service
            echo "enabled pull-on-login (nightclaude-pull.service); no local night run"
        else
            systemctl --user disable nightclaude-pull.service 2>/dev/null || true
            systemctl --user enable --now nightclaude.timer
            echo "the queue runs nightly at 01:30; next run:"
            systemctl --user list-timers nightclaude.timer --no-pager
        fi
    fi
}

write_runner_config() { # write_runner_config <path> <cutoff> <max_tasks> <model>
    cat > "$1" <<EOF
# nightclaude config - written by install.sh on $(date +%F).
# Every available option is documented in config.example.toml in the repo;
# re-run ./install.sh to reconfigure.
role = "worker"

# Runner refuses to start new tasks after this local time.
cutoff = "$2"

# Cap completed tasks per night to protect the weekly quota. 0 = unlimited.
max_tasks_per_night = $3

# Default model for tasks (override per task with --model).
default_model = "$4"

# acceptEdits lets tasks create/edit files in their workdir without prompting
# but blocks shell commands; queue trusted tasks that need to run commands
# with --permission-mode bypassPermissions.
default_permission_mode = "acceptEdits"

task_timeout_minutes = 90
retry_wait_minutes = 30
max_attempts = 2
claude_bin = "claude"
EOF
}

write_controller_config() { # write_controller_config <host> <controller-id>
    cat > "$CONFIG" <<EOF
# nightclaude config - written by install.sh on $(date +%F).
# Every available option is documented in config.example.toml in the repo;
# re-run ./install.sh to reconfigure.

# This machine only queues tasks; the worker below runs them at night.
role = "controller"

# Name stamped into tasks queued on this machine. Several controllers can
# share one worker; each syncs only its own tasks and results.
controller_id = "$2"

[remote]
host = "$1"
work_root = "nightclaude-work"
data_dir = ".local/share/nightclaude"
EOF
}

# --- wizard steps -----------------------------------------------------------

ensure_claude() {
    export PATH="$HOME/.local/bin:$PATH"
    if command -v claude >/dev/null 2>&1; then
        echo "found claude: $(claude --version 2>/dev/null | head -n1)"
        return
    fi
    echo "Claude Code is not installed on this machine - nightclaude needs it."
    if ! ask_yn "install it now (runs: curl -fsSL https://claude.ai/install.sh | bash)?" y; then
        echo "install Claude Code yourself first, then re-run ./install.sh:"
        echo "    curl -fsSL https://claude.ai/install.sh | bash"
        exit 1
    fi
    curl -fsSL https://claude.ai/install.sh | bash
    if ! command -v claude >/dev/null 2>&1; then
        echo "error: claude still not found on PATH after the install -" >&2
        echo "open a new shell and re-run ./install.sh" >&2
        exit 1
    fi
    echo "installed $(claude --version 2>/dev/null | head -n1)"
    echo "log in once with your subscription before the first night run:"
    echo "    claude    # then type /login"
}

install_skill() {
    local target=$HOME/.claude/skills/nightclaude
    if [ "$(readlink "$target" 2>/dev/null)" = "$PWD/skills/nightclaude" ]; then
        echo "nightclaude skill for Claude Code already installed ($target)"
        return
    fi
    echo
    echo "nightclaude ships a skill for Claude Code: with it installed, Claude can"
    echo "queue night tasks for you right from a conversation (\"do this overnight\")."
    if ask_yn "install the skill to ~/.claude/skills?" y; then
        mkdir -p ~/.claude/skills
        [ -e "$target" ] && { echo "replacing existing $target"; rm -rf "$target"; }
        ln -s "$PWD/skills/nightclaude" "$target"
        echo "installed (symlinked, so it stays up to date with this repo)"
    fi
}

ensure_ssh() { # ensure_ssh <host>
    local host=$1
    echo "checking ssh key access to $host ..."
    if ssh -o BatchMode=yes -o ConnectTimeout=5 "$host" true 2>/dev/null; then
        echo "  ok - passwordless ssh works"
        return
    fi
    echo "  no passwordless ssh yet. nightclaude syncs in the background, so key"
    echo "  authentication is required."
    if ! ls ~/.ssh/id_*.pub >/dev/null 2>&1; then
        if ask_yn "no ssh key found - create one now (ssh-keygen)?" y; then
            ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
        fi
    fi
    if ask_yn "copy your key to $host now (asks for the worker's password once)?" y; then
        ssh-copy-id "$host"
        if ssh -o BatchMode=yes -o ConnectTimeout=5 "$host" true 2>/dev/null; then
            echo "  ok - passwordless ssh works now"
        else
            echo "error: still can't log in without a password. Fix ssh access to $host," >&2
            echo "then re-run ./install.sh" >&2
            exit 1
        fi
    else
        echo "  continuing anyway - push/pull will fail until key auth works (ssh-copy-id $host)"
    fi
}

setup_single() {
    echo
    echo "A few settings for the nightly runs (Enter keeps the default):"
    local cutoff max_tasks model
    cutoff=$(ask_time "stop starting new tasks after (morning cutoff)" "07:00")
    max_tasks=$(ask_int "max tasks per night (0 = unlimited)" 4)
    model=$(ask "default model for tasks" "sonnet")

    install_cli
    write_runner_config "$CONFIG" "$cutoff" "$max_tasks" "$model"
    echo "wrote $CONFIG"
    schedule_jobs worker
    install_skill

    echo
    echo "Done. Queue your first task with:"
    echo '    nightclaude add --title "..." --workdir ~/some/dir --prompt "..."'
    echo
    echo "If this machine normally sleeps at night, schedule a wake-up before 01:30:"
    if [ "$(uname)" = Darwin ]; then
        echo "    sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00"
    else
        echo "    e.g. a root cron entry: rtcwake -m no -t \$(date -d 'tomorrow 01:25' +%s)"
    fi
}

setup_controller() {
    echo
    echo "The worker is the always-on machine that runs tasks at night: any Linux"
    echo "box with systemd - a Raspberry Pi, a home server, a spare laptop."
    echo "(To use a Mac as the worker, run ./install.sh on it and pick option 1.)"
    echo
    local host cid cutoff max_tasks model
    host=$(ask_required "worker ssh destination (e.g. pi@raspberrypi.local)")
    ensure_ssh "$host"
    cid=$(ask "name stamped into this machine's tasks (controller id)" "$(hostname -s)")
    echo
    echo "Night-run settings for the worker (Enter keeps the default):"
    cutoff=$(ask_time "stop starting new tasks after (morning cutoff)" "07:00")
    max_tasks=$(ask_int "max tasks per night (0 = unlimited)" 4)
    model=$(ask "default model for tasks" "sonnet")

    install_cli
    write_controller_config "$host" "$cid"
    echo "wrote $CONFIG"
    schedule_jobs controller
    install_skill

    local workercfg
    workercfg=$(mktemp)
    trap "rm -f '$workercfg'" EXIT
    write_runner_config "$workercfg" "$cutoff" "$max_tasks" "$model"

    echo
    if ask_yn "deploy the worker to $host now?" y; then
        ./deploy-worker.sh "$host" "$workercfg"
        echo
        echo "Checking the setup from this side:"
        ~/.local/bin/nightclaude status || true
        echo
        echo "If claude is missing on the worker, install and log in there (see above),"
        echo "then queue your first task here with: nightclaude add ..."
    else
        echo "deploy later with: ./deploy-worker.sh $host"
    fi
}

# --- main -------------------------------------------------------------------

run_noninteractive() {
    install_cli
    if [ ! -f "$CONFIG" ]; then
        cp config.example.toml "$CONFIG"
        echo "created $CONFIG"
    fi
    local role=worker
    if grep -Eq '^\s*role\s*=\s*"controller"' "$CONFIG"; then
        role=controller
    fi
    schedule_jobs "$role"
    if [ "$role" = controller ]; then
        echo "deploy the worker with: ./deploy-worker.sh user@host"
    fi
}

main() {
    if [ ! -t 0 ]; then
        run_noninteractive
        return
    fi

    echo "== nightclaude setup =="

    if ! command -v python3 >/dev/null 2>&1 \
       || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        echo "error: nightclaude needs Python 3.11+ (found: $(python3 --version 2>/dev/null || echo none))" >&2
        exit 1
    fi
    ensure_claude

    if [ -f "$CONFIG" ]; then
        echo "found an existing config at $CONFIG"
        if ask_yn "keep it and just (re)install the scheduled jobs?" y; then
            install_cli
            local role=worker
            if grep -Eq '^\s*role\s*=\s*"controller"' "$CONFIG"; then
                role=controller
            fi
            schedule_jobs "$role"
            install_skill
            return
        fi
        local backup
        backup="$CONFIG.bak.$(date +%Y%m%d-%H%M%S)"
        cp "$CONFIG" "$backup"
        echo "backed up the old config to $backup"
    fi

    echo
    echo "How do you want to run nightclaude?"
    echo "  1) single machine - queue and run tasks on this machine"
    echo "  2) controller     - queue here, run on a separate always-on worker over ssh"
    local mode
    while true; do
        mode=$(ask "choose" 1)
        case $mode in
            1|2) break ;;
            *) echo "  enter 1 or 2" >&2 ;;
        esac
    done

    if [ "$mode" = 1 ]; then
        setup_single
    else
        setup_controller
    fi
}

main "$@"
