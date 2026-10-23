#!/usr/bin/env python3
"""Build/prepare the model solution for scoring.

Replaces build.sh - prepares workspace and writes gateway.yaml config.
Usage: build.py [workspace|reference] [model_workspace_path]
  workspace  -> copies model's mcp_gateway from model_workspace_path to workspace/mcp_gateway/
  reference  -> no copy needed, uses reference/mcp_gateway directly
Writes the harness gateway.yaml config to config/gateway.yaml.
"""

import sys
import shutil
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("Usage: build.py [workspace|reference] [model_workspace_path]", file=sys.stderr)
        return 1

    target = sys.argv[1]
    model_workspace = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    repo_root = Path(__file__).resolve().parents[1]
    workspace_dir = repo_root / "workspace"
    config_dir = repo_root / "config"

    config_dir.mkdir(parents=True, exist_ok=True)

    if target == "workspace":
        if not model_workspace or not model_workspace.exists():
            print(f"[FAIL] Model workspace not provided or not found: {model_workspace}", file=sys.stderr)
            return 1
        print(f"[INFO] Preparing workspace target from {model_workspace}")
        if (workspace_dir / "mcp_gateway").exists():
            shutil.rmtree(workspace_dir / "mcp_gateway")
        shutil.copytree(model_workspace, workspace_dir / "mcp_gateway")
        server_prefix = "mcp_gateway"
    elif target == "reference":
        print("[INFO] Preparing reference target")
        server_prefix = "reference.mcp_gateway"
    else:
        print(f"[FAIL] Unknown target: {target}", file=sys.stderr)
        return 1

    # Write harness config
    config_content = f"""gateway:
  host: "0.0.0.0"
  port: 8000
  request_max_bytes: 1048576
  rate_limit_per_minute: 60
  concurrency_limit: 10
  execution_timeout_seconds: 30
  gateway_timeout_seconds: 35

servers:
  documents:
    command: ["python", "-m", "{server_prefix}.servers.documents"]
  jobs:
    command: ["python", "-m", "{server_prefix}.servers.jobs"]

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
"""
    config_file = config_dir / "gateway.yaml"
    config_file.write_text(config_content, encoding="utf-8")
    print(f"[INFO] Wrote harness config to {config_file}")
    print(f"[INFO] Build/prepare complete for target: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())