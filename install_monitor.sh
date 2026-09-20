#!/bin/zsh
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
LABEL="com.lizhaohui.weather-market-monitor"
DOMAIN="gui/$(id -u)"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
ACTION="${1:-install}"

PYTHON="${WEATHER_PYTHON:-$(command -v python3)}"

case "$ACTION" in
  install)
    "$PYTHON" -m pip install --user --disable-pip-version-check -r "$ROOT/requirements.txt"
    mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs" "$ROOT/data/weather_monitor"
    python3 "$ROOT/scripts/render_launchagent.py" "$ROOT/$LABEL.plist" "$TARGET"
    plutil -lint "$TARGET"
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    launchctl enable "$DOMAIN/$LABEL"
    loaded=false
    for _attempt in {1..20}; do
      if launchctl bootstrap "$DOMAIN" "$TARGET" 2>/dev/null; then
        loaded=true
        break
      fi
      sleep 0.25
    done
    if [[ "$loaded" != true ]]; then
      echo "Failed to load $LABEL after waiting for the old service to exit" >&2
      exit 1
    fi
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
    echo "Usage: $0 [install|status|stop]" >&2
    exit 2
    ;;
esac
