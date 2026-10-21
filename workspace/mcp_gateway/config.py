"""Configuration loading and validation for the gateway."""

import os
from typing import Dict, Any, Optional
import yaml


def load_config(config_path: str = "/app/config/gateway.yaml") -> Dict[str, Any]:
    """Load gateway configuration from YAML file."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    
    # Set defaults
    gw = config.get("gateway", {})
    defaults = {
        "host": "0.0.0.0",
        "port": 8000,
        "request_max_bytes": 1048576,
        "rate_limit_per_minute": 60,
        "concurrency_limit": 10,
        "execution_timeout_seconds": 30,
        "gateway_timeout_seconds": 35,
    }
    for key, default in defaults.items():
        if key not in gw:
            gw[key] = default
    config["gateway"] = gw
    
    # Validate
    if not isinstance(gw.get("port"), int) or gw["port"] <= 0 or gw["port"] > 65535:
        raise ValueError(f"Invalid port: {gw.get('port')}")
    if not isinstance(gw.get("request_max_bytes", 0), int) or gw["request_max_bytes"] <= 0:
        raise ValueError(f"Invalid request_max_bytes: {gw.get('request_max_bytes')}")
    if not isinstance(gw.get("rate_limit_per_minute"), int) or gw["rate_limit_per_minute"] <= 0:
        raise ValueError(f"Invalid rate_limit_per_minute: {gw.get('rate_limit_per_minute')}")
    if not isinstance(gw.get("concurrency_limit"), int) or gw["concurrency_limit"] <= 0:
        raise ValueError(f"Invalid concurrency_limit: {gw.get('concurrency_limit')}")
    
    # Validate servers
    servers = config.get("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("Servers must be an object")
    
    # Validate clients
    clients = config.get("clients", {})
    if not isinstance(clients, dict):
        raise ValueError("Clients must be an object")
    
    for client_name, client_data in clients.items():
        if not isinstance(client_data, dict):
            raise ValueError(f"Client '{client_name}' must be an object")
        if "api_key" not in client_data:
            raise ValueError(f"Client '{client_name}' must have api_key")
        if "allowlist" not in client_data:
            raise ValueError(f"Client '{client_name}' must have allowlist")
        if not isinstance(client_data["allowlist"], list):
            raise ValueError(f"Client '{client_name}' allowlist must be a list")
    
    return config


def get_server_command(config: Dict[str, Any], server_name: str) -> list:
    """Get the command to spawn an upstream MCP server."""
    servers = config.get("servers", {})
    server_config = servers.get(server_name, {})
    command = server_config.get("command", [])
    if not isinstance(command, list) or len(command) == 0:
        raise ValueError(f"No command configured for server '{server_name}'")
    return command


def get_client_config(config: Dict[str, Any], client_name: str) -> Optional[Dict[str, Any]]:
    """Get client configuration by name."""
    clients = config.get("clients", {})
    return clients.get(client_name)
