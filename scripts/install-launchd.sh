#!/usr/bin/env bash
# Install or refresh the launchd job. launchd wakes the loop every TICK seconds (StartInterval);
# the loop runs the day's Run at the first tick at or after repos.yml's schedule, once per day.
# (StartCalendarInterval was tried first and never fired on the dev machine; StartInterval does.)
# A closed laptop means the Run happens at the first tick after wake.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
CFG="${1:-$HERE/repos.yml}"
PY="$HERE/.venv/bin/python"
HOUR=$("$PY" -c "import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))['schedule']['hour'])" "$CFG")
MIN=$("$PY" -c "import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))['schedule']['minute'])" "$CFG")
LABEL=com.maneran.issue-runner
TICK="${TICK:-900}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/Logs/issue-runner" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-m</string><string>runner.loop</string>
    <string>--config</string><string>$CFG</string><string>--tick</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONPATH</key><string>$HERE</string>
  </dict>
  <key>StartInterval</key><integer>$TICK</integer>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/issue-runner/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/issue-runner/launchd.err.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
printf 'installed %s: tick every %ss, Run daily at %02d:%02d, plist %s\n' "$LABEL" "$TICK" "$HOUR" "$MIN" "$PLIST"
echo "run now:  cd $HERE && PYTHONPATH=. $PY -m runner.loop   (a kickstart only ticks)"
echo "remove:   launchctl bootout gui/$(id -u)/$LABEL"
