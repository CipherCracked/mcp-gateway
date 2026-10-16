FROM python:3.11-slim

WORKDIR /app

# System deps (none beyond python)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY environment/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY mcp_gateway ./mcp_gateway

# Default runtime config (the evaluation harness may replace this)
COPY environment/config/gateway.yaml /app/config/gateway.yaml

# Runtime directories
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "mcp_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
