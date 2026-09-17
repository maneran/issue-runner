#!/usr/bin/env bash
# Install or refresh the launchd job that runs the loop daily at the time set in repos.yml.
# launchd runs a missed job at the next wake, so a closed laptop means "later", not "never".
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
CFG="${1:-$HERE/repos.yml}"
PY="$HERE/.venv/bin/python"
HOUR=$("$PY" -c "import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))['schedule']['hour'])" "$CFG")
MIN=$("$PY" -c "import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))['schedule']['minute'])" "$CFG")
LABEL=com.maneran.issue-runner
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/Logs/issue-runner" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-m</string><string>runner.loop</string>
    <string>--config</string><string>$CFG</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONPATH</key><string>$HERE</string>
  </dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MIN</integer></dict>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/issue-runner/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/issue-runner/launchd.err.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
printf 'installed %s: daily at %02d:%02d, plist %s\n' "$LABEL" "$HOUR" "$MIN" "$PLIST"
echo "run now:  launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "remove:   launchctl bootout gui/$(id -u)/$LABEL"
