#!/usr/bin/env bash
# GAP-006: manage the local TimescaleDB container (rootless podman, zero cost).
#
# Usage:
#   bash scripts/timescaledb.sh up       # create+start (idempotent)
#   bash scripts/timescaledb.sh down     # stop (data persists in volume)
#   bash scripts/timescaledb.sh status   # health + connection info
#   bash scripts/timescaledb.sh destroy  # stop + remove container AND data volume
#   bash scripts/timescaledb.sh psql     # open a psql shell inside the container
#
# The container binds to 127.0.0.1:5433 only — unreachable from the network.
# DSN: postgresql://tradebot:tradebot-local@127.0.0.1:5433/tradebot  # pragma: allowlist secret
set -euo pipefail

IMAGE="docker.io/timescale/timescaledb:2.17.2-pg17"
NAME="tradebot-timescaledb"
VOLUME="tradebot-timescale-data"
PORT="127.0.0.1:5433:5432"

# Prefer podman (rootless); fall back to docker if podman is unavailable.
if command -v podman >/dev/null 2>&1; then
    ENGINE=podman
elif command -v docker >/dev/null 2>&1; then
    ENGINE=docker
else
    echo "ERROR: neither podman nor docker found" >&2
    exit 1
fi

cmd="${1:-status}"

case "$cmd" in
    up)
        if $ENGINE container exists "$NAME" 2>/dev/null || $ENGINE inspect "$NAME" >/dev/null 2>&1; then
            $ENGINE start "$NAME" >/dev/null
            echo "started existing container $NAME"
        else
            $ENGINE volume create "$VOLUME" >/dev/null 2>&1 || true
            $ENGINE run -d \
                --name "$NAME" \
                --restart unless-stopped \
                -p "$PORT" \
                -e POSTGRES_USER=tradebot \
                -e POSTGRES_PASSWORD=tradebot-local \
                -e POSTGRES_DB=tradebot \
                -e TS_TUNE_MEMORY=2GB \
                -e TS_TUNE_NUM_CPUS=2 \
                -v "$VOLUME":/var/lib/postgresql/data \
                --health-cmd "pg_isready -U tradebot -d tradebot" \
                --health-interval 5s \
                --health-retries 12 \
                "$IMAGE" >/dev/null
            echo "created container $NAME"
        fi
        # Wait for readiness (fresh init can take ~10s)
        for _ in $(seq 1 30); do
            if $ENGINE exec "$NAME" pg_isready -U tradebot -d tradebot >/dev/null 2>&1; then
                echo "TimescaleDB ready on 127.0.0.1:5433 (db=tradebot user=tradebot)"
                exit 0
            fi
            sleep 1
        done
        echo "ERROR: container started but postgres not ready after 30s" >&2
        exit 1
        ;;
    down)
        $ENGINE stop "$NAME"
        ;;
    status)
        $ENGINE ps -a --filter "name=$NAME" --format "{{.Names}}  {{.Status}}"
        $ENGINE exec "$NAME" pg_isready -U tradebot -d tradebot 2>/dev/null || true
        ;;
    destroy)
        $ENGINE rm -f "$NAME" 2>/dev/null || true
        $ENGINE volume rm "$VOLUME" 2>/dev/null || true
        echo "removed container and data volume"
        ;;
    psql)
        exec $ENGINE exec -it "$NAME" psql -U tradebot -d tradebot
        ;;
    *)
        echo "usage: $0 {up|down|status|destroy|psql}" >&2
        exit 2
        ;;
esac
