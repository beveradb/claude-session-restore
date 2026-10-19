#!/bin/sh
# Install claude-session-restore for the current user: venv, CLI symlink, LaunchAgent, shell hook.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL=uk.beveridge.claude-session-snapshot
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -x "$DIR/.venv/bin/python" ] || python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" -q install iterm2 lz4
[ -f "$DIR/tabs.json" ] || cp "$DIR/tabs.example.json" "$DIR/tabs.json"

mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/claude-restore" "$HOME/.local/bin/claude-restore"

sed -e "s#__DIR__#$DIR#g" -e "s#__HOME__#$HOME#g" "$DIR/launchagent.plist.template" > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

if ! grep -q claude-session-restore "$HOME/.zshrc" 2>/dev/null; then
  printf '\n# >>> claude-session-restore >>>\n[ -f "%s/pane-hook.zsh" ] && source "%s/pane-hook.zsh"\n# <<< claude-session-restore <<<\n' "$DIR" "$DIR" >> "$HOME/.zshrc"
fi
# Shared tmux servers (claude-auto-retry) must not leak one project's CLAUDE_CONFIG_DIR into another.
if ! grep -q "update-environment CLAUDE_CONFIG_DIR" "$HOME/.tmux.conf" 2>/dev/null; then
  printf '\n# claude-session-restore: never inherit CLAUDE_CONFIG_DIR from the tmux server\nset -ga update-environment CLAUDE_CONFIG_DIR\n' >> "$HOME/.tmux.conf"
fi
echo "installed. Enable iTerm2 > Settings > General > Magic > Enable Python API for exact split layouts."
