FROM python:3.12-slim

WORKDIR /app

# System deps (none beyond python)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies (gateway runtime + tests)
COPY environment/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# OpenHands SDK for the agent sandbox (installed here so the agent runs
# inside the container with no access to the host filesystem except /workspace).
# openhands-sdk (not full openhands CLI) avoids pulling browser-use/playwright.
RUN pip install --no-cache-dir openhands-sdk==1.21.0

# Agent workspace — the only host directory mounted into this container.
RUN mkdir -p /workspace

EXPOSE 8000
