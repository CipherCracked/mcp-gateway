"""YAML configuration loading and validation.

Reads /app/config/gateway.yaml at runtime. The verifier uses its own test
fixtures and does not trust this file for assertions — it exists to configure
the gateway's runtime behavior (limits, servers, clients).
"""

from pathlib import Path
from typing import Any, Optional

import yaml


DEFAULT_CONFIG_PATH = Path("/app/config/gateway.yaml")


class GatewayConfig:
    """Parsed and validated gateway configuration."""

    def __init__(self, raw: dict[str, Any]):
        self._raw = raw

        gw = raw.get("gateway", {})
        self.host: str = gw.get("host", "0.0.0.0")
        self.port: int = gw.get("port", 8000)
        self.request_max_bytes: int = gw.get("request_max_bytes", 1048576)
        self.rate_limit_per_minute: int = gw.get("rate_limit_per_minute", 60)
        self.concurrency_limit: int = gw.get("concurrency_limit", 10)
        self.execution_timeout_seconds: int = gw.get("execution_timeout_seconds", 30)
        self.gateway_timeout_seconds: int = gw.get("gateway_timeout_seconds", 35)

        servers_raw = raw.get("servers", {})
        self.servers: dict[str, dict[str, Any]] = {}
        for name, cfg in servers_raw.items():
            self.servers[name] = {
                "command": cfg.get("command", []),
            }

        clients_raw = raw.get("clients", {})
        self.clients: dict[str, dict[str, Any]] = {}
        for key, cfg in clients_raw.items():
            self.clients[key] = {
                "api_key": cfg.get("api_key", ""),
                "allowlist": cfg.get("allowlist", []),
            }

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "GatewayConfig":
        """Load configuration from a YAML file."""
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(raw)

    @classmethod
    def load_or_default(cls, path: Path = DEFAULT_CONFIG_PATH) -> "GatewayConfig":
        """Load config, falling back to built-in defaults if file is missing."""
        try:
            return cls.load(path)
        except FileNotFoundError:
            return cls({})

    def client_for_api_key(self, api_key: str) -> Optional[tuple[str, dict[str, Any]]]:
        """Resolve an API key to (client_id, client_config). Returns None if invalid."""
        for client_id, client_cfg in self.clients.items():
            if client_cfg.get("api_key") == api_key:
                return client_id, client_cfg
        return None

    def is_tool_allowed(self, client_id: str, tool: str) -> bool:
        """Check whether a client is permitted to call a namespaced tool."""
        client_cfg = self.clients.get(client_id)
        if client_cfg is None:
            return False
        return tool in client_cfg.get("allowlist", [])

    def server_command(self, server_name: str) -> Optional[list[str]]:
        """Get the startup command for an upstream MCP server."""
        srv = self.servers.get(server_name)
        if srv is None:
            return None
        return srv.get("command")
