#!/bin/sh
# Installiert Agent Inbox als launchd-Agent (macOS): Start beim Login, Neustart nach Absturz.
# ./install.sh            installieren / aktualisieren
# ./install.sh uninstall  entfernen
set -e
LABEL=local.agent-inbox
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/.local/state/agent-inbox/server.log"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "$1" = uninstall ]; then
  rm -f "$PLIST"
  echo "entfernt"
  exit 0
fi

PY="$(command -v python3)" || { echo "python3 fehlt" >&2; exit 1; }
command -v claude >/dev/null || echo "Warnung: claude nicht im PATH, Antworten aus der Inbox gehen dann nicht" >&2
mkdir -p "$(dirname "$LOG")" "$(dirname "$PLIST")"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$DIR/server.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$PATH</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "läuft: http://127.0.0.1:${AGENT_INBOX_PORT:-8787}  (Log: $LOG)"
