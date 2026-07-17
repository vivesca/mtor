#!/usr/bin/env bash
# start.sh — launch Temporal infrastructure and ribosome worker.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -f "$HOME/.env.fly" ]]; then
    source "$HOME/.env.fly"
fi

durable_backend="${MTOR_DURABLE_BACKEND-temporal}"
durable_backend="$(printf '%s' "$durable_backend" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')"
if [[ "$durable_backend" != "temporal" ]]; then
    echo "Unsupported durable backend '${durable_backend:-<empty>}'; this release enables only temporal." >&2
    exit 3
fi

echo "==> Starting Temporal server (docker compose)..."
docker compose up -d

echo "==> Waiting for Temporal server to be healthy..."
for i in $(seq 1 30); do
    if docker compose exec -T temporal-server tctl --address localhost:7233 cluster health >/dev/null 2>&1; then
        echo "    Temporal server is healthy."
        break
    fi
    echo "    Waiting... ($i/30)"
    sleep 2
done

echo "==> Starting ribosome worker..."
exec "$HOME/germline/.venv/bin/python3" -m mtor.worker
