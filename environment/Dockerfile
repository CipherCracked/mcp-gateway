FROM python:3.12-slim

# Unified sandbox image for the Secure MCP Gateway RL Gym task.
# Used by BOTH generate (docker exec for OpenHands agent) and score
# (uvicorn serving workspace.mcp_gateway.main:app inside the container).
# All gateway runtime deps pre-installed so the model works in the
# same environment that scoring uses.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Install gateway runtime dependencies (fastapi, uvicorn, mcp, aiosqlite, PyYAML, pydantic).
COPY environment/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Keep the container alive so the host-based agent can docker exec into it.
CMD ["sleep", "infinity"]
