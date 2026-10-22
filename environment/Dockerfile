FROM python:3.12-slim

# Sandbox image that can run the Secure MCP Gateway inside Docker.
# Installs gateway runtime deps so uvicorn can serve workspace.mcp_gateway.main:app
# (or reference.mcp_gateway.main:app for reference scoring).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Install gateway runtime dependencies.
COPY environment/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Keep the container alive so the host-based agent can docker exec into it.
CMD ["sleep", "infinity"]
