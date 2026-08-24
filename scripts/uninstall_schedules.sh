#!/bin/bash
#
# OEMParts — remove the launchd schedule installed by install_schedules.sh.
#
set -euo pipefail

AGENT_DIR="$HOME/Library/LaunchAgents"
PREFIX="com.mrdenfish.oemparts"

for job in nightly intraday cleanup taxonomy-sync digest; do
    label="$PREFIX.$job"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$AGENT_DIR/$label.plist"
    echo "removed $label"
done

echo "Done. No OEMParts jobs remain scheduled."
