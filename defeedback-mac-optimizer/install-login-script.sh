#!/bin/bash
#
# Compiles start-logic.applescript into an app, adds it to Login Items,
# and walks you through the one part Apple forces you to do by hand
# (Accessibility / Automation permission).
#
# Remember: every time you change and recompile the script you must
# REMOVE and RE-ADD it in Privacy & Security > Accessibility.

set -e

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This tool only runs on macOS." >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="/Applications/Start Logic.app"

echo "Compiling start-logic.applescript -> $APP"
sudo rm -rf "$APP"
sudo osacompile -o "$APP" "$HERE/start-logic.applescript"

echo "Adding to Login Items..."
osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$APP\", hidden:false}" >/dev/null

cat <<'EOF'

Installed. Two manual steps remain (Apple requires the UI for these):

  1. System Settings > Privacy & Security > Accessibility:
     add/enable "Start Logic".
  2. System Settings > Privacy & Security > Automation:
     allow "Start Logic" to control "System Events".
     (If it isn't listed yet, it will appear and prompt on first login.)

If you ever edit and re-run this installer, remove "Start Logic" from
Accessibility and add it again, or macOS will silently block it.
EOF
