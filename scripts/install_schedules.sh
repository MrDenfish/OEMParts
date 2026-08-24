#!/bin/bash
#
# OEMParts — install the launchd schedule (macOS).
#
# Writes one LaunchAgent plist per job to ~/Library/LaunchAgents and loads
# them. Safe to re-run (idempotent): existing agents are replaced. Remove
# everything with scripts/uninstall_schedules.sh.
#
# Schedule (local time):
#   nightly        03:00 daily            full refresh of active searches
#   intraday       09:00, 14:00, 20:00    high-priority searches only
#   cleanup        03:30 daily            expire/archive old listings
#   taxonomy-sync  Sunday 04:00           re-resolve missing categories
#
# launchd (unlike cron) coalesces a missed StartCalendarInterval: if the Mac
# is asleep at the scheduled time, the job fires once on wake. If the Mac is
# powered off, the run is skipped.
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$HOME/Library/LaunchAgents"
RUNNER="$PROJECT_DIR/scripts/scheduled_job.sh"
PREFIX="com.mrdenfish.oemparts"

mkdir -p "$AGENT_DIR" "$PROJECT_DIR/logs"

# write_plist LABEL JOB CALENDAR_XML
write_plist() {
    local label="$1" job="$2" calendar="$3"
    local plist="$AGENT_DIR/$label.plist"
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$RUNNER</string>
        <string>$job</string>
    </array>
    <key>StartCalendarInterval</key>
    $calendar
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/logs/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/logs/launchd.log</string>
</dict>
</plist>
PLIST
    # Replace any loaded copy with the new definition.
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "installed $label"
}

write_plist "$PREFIX.nightly" "nightly" \
    '<dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>'

write_plist "$PREFIX.intraday" "intraday" \
    '<array>
        <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Hour</key><integer>14</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>0</integer></dict>
    </array>'

write_plist "$PREFIX.cleanup" "cleanup" \
    '<dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer></dict>'

write_plist "$PREFIX.taxonomy-sync" "taxonomy-sync" \
    '<dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>'

echo ""
echo "Done. Jobs run on schedule while the Mac is awake (missed runs fire on wake)."
echo "  Check status:   launchctl list | grep oemparts"
echo "  Force a run:    launchctl kickstart gui/\$(id -u)/$PREFIX.nightly"
echo "  Activity log:   tail -f logs/scheduled.log"
echo "  Remove all:     scripts/uninstall_schedules.sh"
