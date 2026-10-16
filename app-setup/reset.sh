#!/usr/bin/env bash
# Reset the Secure MCP Gateway container: stop, remove, and clear persisted state.
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-secure-mcp-gateway}"

docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

# Clear persisted execution state on the host (bind-mounted /app/data if used)
rm -rf /app/data/executions.db 2>/dev/null || true

echo "Reset complete."
