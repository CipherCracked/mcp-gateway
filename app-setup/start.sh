#!/usr/bin/env bash
# Start the Secure MCP Gateway inside a Docker container.
# Usage: start.sh [workspace|reference]
#   workspace  -> uvicorn workspace.mcp_gateway.main:app (model output)
#   reference  -> uvicorn reference.mcp_gateway.main:app (reference solution)
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-secure-mcp-gateway}"
TAG="${TAG:-latest}"
CONTAINER_NAME="${CONTAINER_NAME:-secure-mcp-gateway}"
HOST_PORT="${HOST_PORT:-8000}"
WORKSPACE_HOST="${WORKSPACE_HOST:-$(pwd)}"
TARGET="${1:-workspace}"

# Determine the gateway module based on target.
if [ "$TARGET" = "reference" ]; then
    GATEWAY_MODULE="reference.mcp_gateway.main:app"
else
    GATEWAY_MODULE="workspace.mcp_gateway.main:app"
fi

# Stop/remove any previous container with the same name.
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

docker run -d \
    --name "${CONTAINER_NAME}" \
    --publish "${HOST_PORT}:8000" \
    --publish 9001:9001 \
    --publish 9002:9002 \
    -v "${WORKSPACE_HOST}:/workspace" \
    -w /workspace \
    -e GATEWAY_MODULE="${GATEWAY_MODULE}" \
    -e PYTHONPATH=/workspace \
    "${IMAGE_NAME}:${TAG}" \
    uvicorn "${GATEWAY_MODULE}" --host 0.0.0.0 --port 8000

echo "Started ${CONTAINER_NAME} on host port ${HOST_PORT} (target=${TARGET})"
echo "Waiting for gateway to be ready..."

for i in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:${HOST_PORT}/tools/list" -H "x-api-key: key-admin-789" >/dev/null 2>&1; then
        echo "Gateway is ready."
        exit 0
    fi
    sleep 0.5
done

echo "ERROR: Gateway did not become ready in time." >&2
docker logs "${CONTAINER_NAME}" >&2 || true
exit 1
