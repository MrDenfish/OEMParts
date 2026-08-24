#!/bin/bash
#
# OEMParts — scheduled job runner, invoked by launchd.
#
#   scheduled_job.sh nightly|intraday|cleanup|taxonomy-sync|digest
#
# Installed by scripts/install_schedules.sh (see the plists it writes to
# ~/Library/LaunchAgents). Each run ensures Docker Desktop and the Postgres
# container are up, applies any pending migrations, then runs the matching
# oemparts CLI command. All output appends to logs/scheduled.log.
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# launchd agents run with a minimal PATH; locate the docker CLI (this
# machine's Docker.app lives in a non-standard folder — see _docker_env.sh).
# shellcheck source=/dev/null
source "$PROJECT_DIR/scripts/_docker_env.sh"
VENV="$PROJECT_DIR/.venv"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.local.yml"
JOB="${1:-}"

mkdir -p "$PROJECT_DIR/logs"
exec >> "$PROJECT_DIR/logs/scheduled.log" 2>&1

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [scheduled:$JOB] $1"; }

case "$JOB" in
    nightly|intraday|cleanup|taxonomy-sync|digest) ;;
    *) log "ERROR: unknown job '${JOB}' (expected nightly|intraday|cleanup|taxonomy-sync|digest)"; exit 64 ;;
esac

log "starting"

# --- Ensure Docker Desktop is running -------------------------------------
if ! docker info >/dev/null 2>&1; then
    log "Docker not running; starting Docker Desktop"
    open -a Docker || { log "ERROR: Docker Desktop is not installed"; exit 1; }
    for _ in $(seq 1 60); do
        docker info >/dev/null 2>&1 && break
        sleep 2
    done
    docker info >/dev/null 2>&1 || { log "ERROR: Docker did not become ready within 2 minutes"; exit 1; }
fi

# --- Ensure Postgres is up and migrated -----------------------------------
docker compose -f "$COMPOSE_FILE" up -d db >/dev/null
for _ in $(seq 1 30); do
    docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U oemparts >/dev/null 2>&1 && break
    sleep 1
done
docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U oemparts >/dev/null 2>&1 \
    || { log "ERROR: Postgres did not become ready within 30 seconds"; exit 1; }

cd "$PROJECT_DIR"
PYTHONPATH="$PROJECT_DIR" "$VENV/bin/alembic" upgrade head

# --- Run the job -----------------------------------------------------------
case "$JOB" in
    nightly)       "$VENV/bin/python" oemparts fetch --cycle=nightly ;;
    intraday)      "$VENV/bin/python" oemparts fetch --cycle=intraday ;;
    cleanup)       "$VENV/bin/python" oemparts cleanup ;;
    taxonomy-sync) "$VENV/bin/python" oemparts taxonomy-sync ;;
    digest)        "$VENV/bin/python" oemparts digest ;;
esac

log "done"
