#!/usr/bin/env bash
# Build/prepare the model solution for scoring.
# Usage: build.sh [workspace|reference] [model_workspace_path]
#   workspace  -> copies model's mcp_gateway from model_workspace_path to workspace/mcp_gateway/
#   reference  -> no copy needed, uses reference/mcp_gateway directly
# Writes the harness gateway.yaml config to config/gateway.yaml.
set -euo pipefail

TARGET="${1:-workspace}"
MODEL_WORKSPACE="${2:-}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_DIR="${REPO_ROOT}/workspace"
CONFIG_DIR="${REPO_ROOT}/config"

mkdir -p "${CONFIG_DIR}"

if [ "${TARGET}" = "workspace" ]; then
    if [ -z "${MODEL_WORKSPACE}" ] || [ ! -d "${MODEL_WORKSPACE}" ]; then
        echo "[FAIL] Model workspace not provided or not found: ${MODEL_WORKSPACE}" >&2
        exit 1
    fi
    echo "[INFO] Preparing workspace target from ${MODEL_WORKSPACE}"
    rm -rf "${WORKSPACE_DIR}/mcp_gateway"
    cp -r "${MODEL_WORKSPACE}" "${WORKSPACE_DIR}/mcp_gateway"
    SERVER_PREFIX="mcp_gateway"
else
    echo "[INFO] Preparing reference target"
    SERVER_PREFIX="reference.mcp_gateway"
fi

# Write harness config (actual API keys and allowlists set by evaluation harness)
cat > "${CONFIG_DIR}/gateway.yaml" <<EOF
gateway:
  host: "0.0.0.0"
  port: 8000
  request_max_bytes: 1048576
  rate_limit_per_minute: 60
  concurrency_limit: 10
  execution_timeout_seconds: 30
  gateway_timeout_seconds: 35

servers:
  documents:
    command: ["python", "-m", "${SERVER_PREFIX}.servers.documents"]
  jobs:
    command: ["python", "-m", "${SERVER_PREFIX}.servers.jobs"]

clients:
  client_a:
    api_key: "key-a-123"
    allowlist: ["documents.search_documents", "documents.get_document"]
  client_b:
    api_key: "key-b-456"
    allowlist: ["jobs.execute_job", "jobs.get_job_status"]
  client_admin:
    api_key: "key-admin-789"
    allowlist: ["documents.search_documents", "documents.get_document", "jobs.execute_job", "jobs.get_job_status"]
EOF

echo "[INFO] Wrote harness config to ${CONFIG_DIR}/gateway.yaml"
echo "[INFO] Build/prepare complete for target: ${TARGET}"