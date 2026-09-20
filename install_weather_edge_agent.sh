#!/bin/zsh
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
LABEL="com.lizhaohui.weather-edge-agent"
DOMAIN="gui/$(id -u)"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
ACTION="${1:-install}"

case "$ACTION" in
  install|restart)
    mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"
    python3 "$ROOT/scripts/render_launchagent.py" "$ROOT/$LABEL.plist" "$TARGET"
    plutil -lint "$TARGET"
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    launchctl enable "$DOMAIN/$LABEL"
    launchctl bootstrap "$DOMAIN" "$TARGET"
    launchctl print "$DOMAIN/$LABEL" | head -40
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL"
    ;;
  stop)
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    launchctl disable "$DOMAIN/$LABEL"
    echo "Stopped and disabled $LABEL"
    ;;
  *)
    echo "Usage: $0 [install|restart|status|stop]" >&2
    exit 2
    ;;
esac
