#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p ~/.local/bin ~/.config/nightclaude ~/.config/systemd/user
chmod +x nightclaude.py
ln -sf "$PWD/nightclaude.py" ~/.local/bin/nightclaude

if [ ! -f ~/.config/nightclaude/config.toml ]; then
    cp config.example.toml ~/.config/nightclaude/config.toml
    echo "created ~/.config/nightclaude/config.toml"
fi

cp systemd/nightclaude.service systemd/nightclaude.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nightclaude.timer

echo
echo "installed. next run:"
systemctl --user list-timers nightclaude.timer --no-pager
