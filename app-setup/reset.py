#!/usr/bin/env python3
"""Reset the Secure MCP Gateway container: stop, remove, and clear persisted state.

Replaces reset.sh.
"""

import sys
import subprocess
from pathlib import Path

# Docker executable path on Windows
DOCKER_EXE = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
CONTAINER_NAME = "secure-mcp-gateway"


def run_docker(*args, **kwargs):
    """Run docker command via PowerShell to ensure Docker Desktop context."""
    cmd = ["powershell", "-Command", f"& '{DOCKER_EXE}' {' '.join(args)}"]
    return subprocess.run(cmd, **kwargs)


def main():
    repo_root = Path(__file__).resolve().parents[1]

    # Stop and remove the container
    run_docker("rm", "-f", CONTAINER_NAME, capture_output=True)

    # Clear persisted execution state on the host (bind-mounted /app/data if used)
    host_db_dir = repo_root / "app" / "data"
    for db_file in host_db_dir.glob("executions.db*"):
        try:
            db_file.unlink()
        except OSError:
            pass

    print("Reset complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())