#!/usr/bin/env bash
# Build the Secure MCP Gateway Docker image.
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-secure-mcp-gateway}"
TAG="${TAG:-latest}"

docker build \
  --tag "${IMAGE_NAME}:${TAG}" \
  --file environment/Dockerfile \
  .

echo "Built ${IMAGE_NAME}:${TAG}"
