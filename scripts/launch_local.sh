#!/bin/bash
#
# OEMParts — local launcher.
#
# What a double-click of OEMParts.app actually runs. Brings up the full
# local stack in order, then opens the dashboard in the default browser:
#
#   1. Docker Desktop (started if not running)
#   2. Postgres container (docker-compose.local.yml, service "db")
#   3. Alembic migrations (idempotent; picks up new migrations after a pull)
#   4. uvicorn dashboard on port 8000 (skipped if already running)
#   5. open http://localhost:8000
#
# Safe to run repeatedly: if everything is already up, it just opens the
# browser. Server output goes to logs/app.log. Stop with scripts/stop_local.sh.
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_DIR/.venv"
LOG_DIR="$PROJECT_DIR/logs"
RUN_DIR="$PROJECT_DIR/.run"
PIDFILE="$RUN_DIR/uvicorn.pid"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.local.yml"
PORT=8000
URL="http://localhost:$PORT"

mkdir -p "$LOG_DIR" "$RUN_DIR"
cd "$PROJECT_DIR"

# Finder-launched apps get a minimal PATH; locate the docker CLI (this
# machine's Docker.app lives in a non-standard folder — see _docker_env.sh).
# shellcheck source=/dev/null
source "$PROJECT_DIR/scripts/_docker_env.sh"

fail() {
    echo "ERROR: $1" >&2
    # Surface the error in a dialog when launched from the .app bundle
    # (no terminal attached), so a failure isn't silent.
    if [ ! -t 1 ]; then
        osascript -e "display dialog \"OEMParts failed to start:\n\n$1\" buttons {\"OK\"} default button 1 with icon stop with title \"OEMParts\"" >/dev/null 2>&1 || true
    fi
    exit 1
}

[ -x "$VENV/bin/uvicorn" ] || fail "No .venv found. Run ./setup.sh first."
[ -f "$PROJECT_DIR/.env" ] || fail "No .env file found in $PROJECT_DIR."

# --- 1. Ensure Docker Desktop is running ----------------------------------
if ! docker info >/dev/null 2>&1; then
    echo "==> Starting Docker Desktop..."
    open -a Docker || fail "Docker Desktop is not installed."
    for _ in $(seq 1 60); do
        docker info >/dev/null 2>&1 && break
        sleep 2
    done
    docker info >/dev/null 2>&1 || fail "Docker Desktop did not become ready within 2 minutes."
fi

# --- 2. Start Postgres and wait for it to accept connections --------------
echo "==> Starting Postgres container"
docker compose -f "$COMPOSE_FILE" up -d db

echo "==> Waiting for Postgres to be ready"
for _ in $(seq 1 30); do
    docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U oemparts >/dev/null 2>&1 && break
    sleep 1
done
docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U oemparts >/dev/null 2>&1 \
    || fail "Postgres did not become ready within 30 seconds."

# --- 3. Apply any pending migrations --------------------------------------
echo "==> Running database migrations"
PYTHONPATH="$PROJECT_DIR" "$VENV/bin/alembic" upgrade head >> "$LOG_DIR/app.log" 2>&1 \
    || fail "Alembic migration failed — see logs/app.log."

# --- 4. Start the dashboard unless it is already up -----------------------
if curl -s -o /dev/null --max-time 2 "$URL"; then
    echo "==> Dashboard already running on port $PORT"
else
    if [ -f "$PIDFILE" ] && ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        rm -f "$PIDFILE"   # stale pidfile from an unclean shutdown
    fi
    echo "==> Starting dashboard (logs/app.log)"
    PYTHONPATH="$PROJECT_DIR" nohup "$VENV/bin/uvicorn" app.web.main:app \
        --host 127.0.0.1 --port "$PORT" >> "$LOG_DIR/app.log" 2>&1 &
    echo $! > "$PIDFILE"

    for _ in $(seq 1 30); do
        curl -s -o /dev/null --max-time 2 "$URL" && break
        sleep 1
    done
    curl -s -o /dev/null --max-time 2 "$URL" \
        || fail "Dashboard did not respond on port $PORT — see logs/app.log."
fi

# --- 5. Open the dashboard -------------------------------------------------
echo "==> Opening $URL"
open "$URL"
