#!/usr/bin/env bash
# Local TimescaleDB container lifecycle.
#
# tests/test_timescale_storage.py and STORAGE_BACKEND=timescale both expect a
# TimescaleDB on 127.0.0.1:5433. This script is the local half of that; the
# other half is the `timescaledb` service in .github/workflows/ci.yml, and the
# two MUST describe the same database. If they drift, the 92 tests in that
# module pass against one engine locally and a different one in CI, which is
# the failure mode nobody attributes to anything.
# tests/test_timescaledb_script.py asserts the image, credentials, database
# and port here match that service block.
#
# Rootless podman is preferred over docker when both are present: this needs
# no daemon and no root, and the container is bound to the loopback interface
# only. The password is a fixed local-only credential, identical to the one in
# ci.yml and the STORAGE_TIMESCALE_DSN default in src/config.py. It guards a
# throwaway database that is only reachable from this machine; it is not a
# secret and must not be treated as one.
set -euo pipefail

IMAGE="timescale/timescaledb:2.17.2-pg17"
CONTAINER="tradebot-timescaledb"
POSTGRES_USER="tradebot"
POSTGRES_PASSWORD="tradebot-local"
POSTGRES_DB="tradebot"
HOST_ADDR="127.0.0.1"
HOST_PORT="5433"
DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${HOST_ADDR}:${HOST_PORT}/${POSTGRES_DB}"

if command -v podman >/dev/null 2>&1; then
  ENGINE="podman"
elif command -v docker >/dev/null 2>&1; then
  ENGINE="docker"
else
  echo "timescaledb: neither podman nor docker is on PATH" >&2
  exit 127
fi

exists() { "$ENGINE" container inspect "$CONTAINER" >/dev/null 2>&1; }
running() { [ "$("$ENGINE" container inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; }

# Readiness is asked of the database, not the container: a started container
# whose postgres is still recovering accepts the TCP connection and refuses
# the query, which reads as a flaky test rather than as a race here.
wait_ready() {
  local attempt
  for attempt in $(seq 1 60); do
    if "$ENGINE" exec "$CONTAINER" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
      echo "timescaledb: ready on ${HOST_ADDR}:${HOST_PORT} (after ${attempt}s)"
      return 0
    fi
    sleep 1
  done
  echo "timescaledb: not ready after 60s -- '$ENGINE logs $CONTAINER' has the reason" >&2
  return 1
}

up() {
  if running; then
    echo "timescaledb: already running"
  else
    if exists; then
      echo "timescaledb: starting existing container"
      "$ENGINE" start "$CONTAINER" >/dev/null
    else
      echo "timescaledb: creating $CONTAINER from $IMAGE"
      # -p binds the loopback address explicitly. A bare "5433:5432" publishes
      # on every interface, which on a laptop on a shared network exposes the
      # database to it.
      "$ENGINE" run --detach \
        --name "$CONTAINER" \
        --publish "${HOST_ADDR}:${HOST_PORT}:5432" \
        --env "POSTGRES_USER=${POSTGRES_USER}" \
        --env "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}" \
        --env "POSTGRES_DB=${POSTGRES_DB}" \
        "$IMAGE" >/dev/null
    fi
  fi
  wait_ready
  echo "timescaledb: export STORAGE_TIMESCALE_DSN=${DSN}"
}

down() {
  if exists; then
    echo "timescaledb: removing $CONTAINER"
    "$ENGINE" rm --force "$CONTAINER" >/dev/null
  else
    echo "timescaledb: nothing to remove"
  fi
}

status() {
  if running; then
    echo "timescaledb: running on ${HOST_ADDR}:${HOST_PORT} (engine: $ENGINE)"
  elif exists; then
    echo "timescaledb: stopped (engine: $ENGINE)"
    exit 1
  else
    echo "timescaledb: no container (engine: $ENGINE)"
    exit 1
  fi
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status) status ;;
  logs) "$ENGINE" logs "$CONTAINER" ;;
  dsn) echo "$DSN" ;;
  *)
    echo "usage: bash scripts/timescaledb.sh {up|down|status|logs|dsn}" >&2
    exit 2
    ;;
esac
