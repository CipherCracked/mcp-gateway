FROM python:3.12-slim

# Minimal sandbox for agent command execution.
# No PyPI installs here — OpenHands SDK runs on the host, not in this image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /workspace

WORKDIR /workspace

# Keep the container alive so the host-based agent can docker exec into it.
CMD ["sleep", "infinity"]
