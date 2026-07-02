#!/usr/bin/env bash
# Deploy the nightclaude worker to a Raspberry Pi (or any Linux box).
# Usage: ./deploy-pi.sh pi@raspberrypi.local
set -euo pipefail
cd "$(dirname "$0")"

HOST=${1:?usage: ./deploy-pi.sh user@host}

echo "== copying nightclaude to $HOST =="
ssh "$HOST" 'mkdir -p ~/.local/bin ~/.config/nightclaude ~/.config/systemd/user \
             ~/.local/share/nightclaude/tasks ~/.local/share/nightclaude/logs ~/nightclaude-work'
scp -q nightclaude.py "$HOST":.local/bin/nightclaude
ssh "$HOST" 'chmod +x ~/.local/bin/nightclaude'
scp -q systemd/nightclaude.service systemd/nightclaude.timer "$HOST":.config/systemd/user/
ssh "$HOST" '[ -f ~/.config/nightclaude/config.toml ] || cat > ~/.config/nightclaude/config.toml' \
    < config.worker.toml

echo "== enabling nightly timer =="
ssh "$HOST" 'systemctl --user daemon-reload && systemctl --user enable --now nightclaude.timer'

# User services must survive logout on a headless box.
echo "== enabling lingering (may ask for the Pi's sudo password) =="
ssh -t "$HOST" 'sudo loginctl enable-linger "$USER"' \
    || echo "warning: enable-linger failed - run it manually on the Pi, or the timer won't fire when you're logged out"

echo "== checking claude on the worker =="
if ssh "$HOST" 'PATH=$HOME/.local/bin:$PATH command -v claude >/dev/null'; then
    ssh "$HOST" 'PATH=$HOME/.local/bin:$PATH claude --version'
else
    cat <<'EOF'
claude is NOT installed on the worker yet. On the Pi (64-bit OS required):
    curl -fsSL https://claude.ai/install.sh | bash
then log in with your subscription (browser-less flow, paste the code back):
    claude
    /login
EOF
fi

echo "done. verify with: ssh $HOST 'systemctl --user list-timers nightclaude.timer'"
