#!/bin/zsh
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
LABEL="com.lizhaohui.weather-windy-token-keeper"
DOMAIN="gui/$(id -u)"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
ACTION="${1:-install}"

case "$ACTION" in
  install)
    mkdir -p "$HOME/.config/weather-market-monitor/chrome-profile" "$ROOT/logs"
    chmod 700 "$HOME/.config/weather-market-monitor" "$HOME/.config/weather-market-monitor/chrome-profile"
    python3 "$ROOT/scripts/render_launchagent.py" "$ROOT/$LABEL.plist" "$TARGET"
    plutil -lint "$TARGET"
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    launchctl enable "$DOMAIN/$LABEL"
    launchctl bootstrap "$DOMAIN" "$TARGET"
    launchctl print "$DOMAIN/$LABEL" | head -40
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL"
    python3 "$ROOT/windy_token_keeper.py" --status
    ;;
  stop)
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    launchctl disable "$DOMAIN/$LABEL"
    echo "Stopped and disabled $LABEL"
    ;;
  *)
    echo "Usage: $0 [install|status|stop]" >&2
    exit 2
    ;;
esac
