#!/usr/bin/env bash
# Start the Secure MCP Gateway inside a Docker container.
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-secure-mcp-gateway}"
TAG="${TAG:-latest}"
CONTAINER_NAME="${CONTAINER_NAME:-secure-mcp-gateway}"
HOST_PORT="${HOST_PORT:-8000}"

# Stop/remove any previous container with the same name
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

docker run -d \
  --name "${CONTAINER_NAME}" \
  --publish "${HOST_PORT}:8000" \
  "${IMAGE_NAME}:${TAG}"

echo "Started ${CONTAINER_NAME} on host port ${HOST_PORT}"
echo "Waiting for gateway to be ready..."

for i in $(seq 1 50); do
  if curl -sf "http://127.0.0.1:${HOST_PORT}/tools/list" -H "x-api-key: key-admin-789" >/dev/null 2>&1; then
    echo "Gateway is ready."
    exit 0
  fi
  sleep 0.5
done

echo "ERROR: Gateway did not become ready in time." >&2
docker logs "${CONTAINER_NAME}" >&2 || true
exit 1
