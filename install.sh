#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p ~/.local/bin ~/.config/nightclaude ~/.local/share/nightclaude/logs
chmod +x nightclaude.py
ln -sf "$PWD/nightclaude.py" ~/.local/bin/nightclaude

if [ ! -f ~/.config/nightclaude/config.toml ]; then
    cp config.example.toml ~/.config/nightclaude/config.toml
    echo "created ~/.config/nightclaude/config.toml"
fi

controller=false
if grep -Eq '^\s*role\s*=\s*"controller"' ~/.config/nightclaude/config.toml; then
    controller=true
    echo "controller mode: enabling pull-on-login, disabling the local night run"
fi

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
        echo "installed. deploy the worker with: ./deploy-worker.sh user@host"
    else
        agent_drop com.nightclaude.pull
        agent_load com.nightclaude.run
        echo "installed. the queue runs nightly at 01:30 (agent com.nightclaude.run)"
    fi
else
    mkdir -p ~/.config/systemd/user
    cp systemd/nightclaude.service systemd/nightclaude.timer \
       systemd/nightclaude-pull.service ~/.config/systemd/user/
    systemctl --user daemon-reload

    if $controller; then
        systemctl --user disable --now nightclaude.timer 2>/dev/null || true
        systemctl --user enable nightclaude-pull.service
        echo "installed. deploy the worker with: ./deploy-worker.sh user@host"
    else
        systemctl --user disable nightclaude-pull.service 2>/dev/null || true
        systemctl --user enable --now nightclaude.timer
        echo
        echo "installed. next run:"
        systemctl --user list-timers nightclaude.timer --no-pager
    fi
fi
