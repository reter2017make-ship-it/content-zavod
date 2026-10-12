#!/bin/bash
# Автозапуск веб-сервера (он же завод и планировщик) при входе в macOS: пользовательский LaunchAgent, без sudo.
#   scripts/autostart.sh on      — включить (и запустить сейчас)
#   scripts/autostart.sh off     — выключить и остановить
#   scripts/autostart.sh status  — запущен ли
# Если сервер упадёт, launchd перезапустит его через 30 секунд.
set -euo pipefail
LABEL="com.contentzavod.web"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "${1:-status}" in
  on)
    # launchd may not open files on the Desktop (macOS privacy), so its own output goes to ~/Library/Logs
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    # a server started by hand would hold the factory lock: stop it, launchd takes over
    pkill -f "$ROOT/.venv/bin/python -m app.cli web" 2>/dev/null || pkill -f "app.cli web" 2>/dev/null || true
    sleep 2
    cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-c</string><string>cd "$ROOT" &amp;&amp; exec .venv/bin/python -m app.cli web</string></array>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/contentzavod.out</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/contentzavod.err</string>
</dict>
</plist>
PL
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "Автозапуск включён: $PLIST"
    ;;
  off)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Автозапуск выключен, сервер остановлен. Запустить вручную: make web"
    ;;
  status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl print "$DOMAIN/$LABEL" | grep -E "state =|pid =|last exit" | sed 's/^\s*//'
    else
      echo "автозапуск выключен"
    fi
    ;;
  *) echo "использование: $0 on|off|status"; exit 1 ;;
esac
