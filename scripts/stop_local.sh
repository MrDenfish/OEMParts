#!/bin/bash
#
# OEMParts — stop the local stack started by scripts/launch_local.sh:
# the uvicorn dashboard (via its pidfile) and the Postgres container.
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$PROJECT_DIR/.run/uvicorn.pid"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.local.yml"

if [ -f "$PIDFILE" ]; then
    PID="$(cat "$PIDFILE")"
    if kill -0 "$PID" 2>/dev/null; then
        echo "==> Stopping dashboard (pid $PID)"
        kill "$PID"
        for _ in $(seq 1 10); do
            kill -0 "$PID" 2>/dev/null || break
            sleep 1
        done
        kill -0 "$PID" 2>/dev/null && kill -9 "$PID"
    else
        echo "==> Dashboard not running (stale pidfile)"
    fi
    rm -f "$PIDFILE"
else
    echo "==> No pidfile — dashboard was not started by launch_local.sh"
    echo "    (a manually started 'uvicorn --reload' must be stopped in its own terminal)"
fi

echo "==> Stopping Postgres container"
docker compose -f "$COMPOSE_FILE" stop db

echo "Done."
