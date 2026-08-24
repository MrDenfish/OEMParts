#!/bin/bash
#
# OEMParts — one-command setup for macOS.
#
# Creates an isolated virtualenv, installs dependencies, builds a
# double-clickable macOS .app bundle in ~/Applications, and drops a
# shortcut on the Desktop. Safe to re-run (idempotent).
#
# The bundle runs scripts/launch_local.sh, which starts Docker/Postgres,
# applies migrations, starts the dashboard, and opens the browser — so
# launch-logic changes do NOT require re-running this script.
#
#   ./setup.sh
#
# (Pattern borrowed from the Recoup Tracker project's setup.sh.)
#
set -euo pipefail

APP_NAME="OEMParts"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/.venv"
APP="$HOME/Applications/$APP_NAME.app"

echo "==> Project:  $PROJECT_DIR"

# --- 1. Locate a Python 3 interpreter -------------------------------------
PY="$(command -v python3.11 || command -v python3 || true)"
if [ -z "$PY" ]; then
    echo "ERROR: python3 not found on PATH. Install Python 3.11+ first." >&2
    exit 1
fi
echo "==> Python:   $PY ($("$PY" --version 2>&1))"

# --- 2. Create / update the virtual environment ---------------------------
if [ ! -d "$VENV" ]; then
    echo "==> Creating virtualenv at .venv"
    "$PY" -m venv "$VENV"
fi
echo "==> Installing dependencies"
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"

# Sanity check: the web stack must be importable.
if ! "$VENV/bin/python" -c "import fastapi, uvicorn, alembic" 2>/dev/null; then
    echo "WARNING: fastapi/uvicorn/alembic not importable in this venv. The" >&2
    echo "         dashboard will not launch. Re-run after fixing pip errors." >&2
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "WARNING: no .env file found. Copy .env.example to .env and fill in" >&2
    echo "         your credentials before launching." >&2
fi

# --- 3. Build the macOS .app bundle ---------------------------------------
echo "==> Building app bundle: $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/MacOS/OEMParts" <<EOF
#!/bin/bash
# OEMParts launcher — delegates to the project's launch script.
mkdir -p "$PROJECT_DIR/logs"
exec /bin/bash "$PROJECT_DIR/scripts/launch_local.sh" >> "$PROJECT_DIR/logs/launcher.log" 2>&1
EOF
chmod +x "$APP/Contents/MacOS/OEMParts"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>OEMParts</string>
    <key>CFBundleDisplayName</key>
    <string>OEMParts</string>
    <key>CFBundleIdentifier</key>
    <string>com.mrdenfish.oemparts</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>OEMParts</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
</dict>
</plist>
EOF

if [ -f "$PROJECT_DIR/assets/AppIcon.icns" ]; then
    cp "$PROJECT_DIR/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
else
    echo "==> (no assets/AppIcon.icns found; app will use the default icon)"
fi

# --- 4. Register with LaunchServices & create Desktop shortcut ------------
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$APP" || true
touch "$APP"

# macOS privacy controls can deny Desktop writes to terminal processes
# ("Operation not permitted"). An existing shortcut keeps working, so warn
# instead of aborting the whole setup.
if ! ln -sfn "$APP" "$HOME/Desktop/$APP_NAME" 2>/dev/null; then
    echo "==> (couldn't update the Desktop shortcut — an existing one still works;"
    echo "     otherwise grant your terminal Desktop access in System Settings"
    echo "     > Privacy & Security > Files and Folders, then re-run)"
fi

echo ""
echo "Done. Launch it by:"
echo "  • double-clicking '$APP_NAME' on your Desktop, or"
echo "  • opening 'OEMParts' from Launchpad / Spotlight."
echo ""
echo "Stop the stack with: scripts/stop_local.sh"
