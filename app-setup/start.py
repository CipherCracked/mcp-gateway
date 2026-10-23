#!/usr/bin/env python3
"""Start the Secure MCP Gateway inside a Docker container.

Replaces start.sh.
Usage: start.py [workspace|reference]
  workspace  -> uvicorn workspace.mcp_gateway.main:app (model output)
  reference  -> uvicorn reference.mcp_gateway.main:app (reference solution)
"""

import sys
import subprocess
import time
import httpx

# Docker executable path on Windows
DOCKER_EXE = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
IMAGE_NAME = "secure-mcp-gateway"
TAG = "latest"
CONTAINER_NAME = "secure-mcp-gateway"
HOST_PORT = 8000


def run_docker(*args, **kwargs):
    """Run docker command via PowerShell to ensure Docker Desktop context."""
    cmd = ["powershell", "-Command", f"& '{DOCKER_EXE}' {' '.join(args)}"]
    return subprocess.run(cmd, **kwargs)


def main():
    if len(sys.argv) < 2:
        print("Usage: start.py [workspace|reference]", file=sys.stderr)
        return 1

    target = sys.argv[1]
    workspace_host = sys.argv[2] if len(sys.argv) > 2 else str(Path(__file__).resolve().parents[1])

    if target == "reference":
        gateway_module = "reference.mcp_gateway.main:app"
    elif target == "workspace":
        gateway_module = "workspace.mcp_gateway.main:app"
    else:
        print(f"[FAIL] Unknown target: {target}", file=sys.stderr)
        return 1

    # Use forward slashes for Docker volume mounts
    repo_root_win = workspace_host.replace("\\", "/")
    volume_workspace = f"{repo_root_win}:/workspace"
    volume_config = f"{repo_root_win}/config/gateway.yaml:/app/config/gateway.yaml"

    # Stop/remove any previous container
    run_docker("rm", "-f", CONTAINER_NAME, capture_output=True)

    # Start new container
    docker_cmd = [
        "powershell", "-Command",
        f"& '{DOCKER_EXE}' run -d "
        f"--name {CONTAINER_NAME} "
        f"--publish {HOST_PORT}:8000 "
        f"--publish 9001:9001 "
        f"--publish 9002:9002 "
        f"-v {volume_workspace} "
        f"-v {volume_config} "
        f"-w /workspace "
        f"-e GATEWAY_MODULE={gateway_module} "
        f"-e PYTHONPATH=/workspace "
        f"{IMAGE_NAME}:{TAG} "
        f"uvicorn {gateway_module} --host 0.0.0.0 --port 8000"
    ]

    print(f"[INFO] Starting gateway in Docker (target={target})...")
    result = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return result.returncode

    print(f"Started {CONTAINER_NAME} on host port {HOST_PORT} (target={target})")
    print("Waiting for gateway to be ready...")

    # Wait for readiness
    for i in range(60):
        try:
            r = httpx.get(f"http://127.0.0.1:{HOST_PORT}/tools/list",
                          headers={"x-api-key": "key-admin-789"}, timeout=2.0)
            if r.status_code == 200:
                print("Gateway is ready.")
                return 0
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
            pass
        time.sleep(0.5)

    # If not ready, show logs and fail
    log_result = run_docker("logs", CONTAINER_NAME, capture_output=True, text=True)
    print(log_result.stdout, file=sys.stderr)
    print(log_result.stderr, file=sys.stderr)
    print("ERROR: Gateway did not become ready in time.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    from pathlib import Path
    sys.exit(main())