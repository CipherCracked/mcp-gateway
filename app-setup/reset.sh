#!/usr/bin/env bash
# Reset the Secure MCP Gateway container: stop, remove, and clear persisted state.
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-secure-mcp-gateway}"

# Stop and remove the container.
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

# Clear persisted execution state inside the container (if it ever existed).
docker exec "${CONTAINER_NAME}" rm -f /app/data/executions.db /app/data/executions.db-wal /app/data/executions.db-shm 2>/dev/null || true

# Clear persisted execution state on the host (bind-mounted /app/data if used).
rm -rf /app/data/executions.db 2>/dev/null || true

echo "Reset complete."
