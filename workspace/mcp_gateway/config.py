"""Configuration loading and validation for the Secure MCP Gateway."""

from __future__ import annotations

import hmac
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = "/app/config/gateway.yaml"
DEFAULT_DB_PATH = "/app/data/executions.db"

SERVER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
TOOL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

#: A namespaced tool name is ``{server}.{tool}``; the split happens on the first dot.
NAMESPACED_TOOL_RE = re.compile(
    r"^(?P<server>[a-z][a-z0-9-]*)\.(?P<tool>[a-zA-Z][a-zA-Z0-9_]*)$"
)


logger = logging.getLogger("mcp_gateway.config")


class ConfigError(Exception):
    """Raised when the gateway configuration is missing or malformed."""


@dataclass(frozen=True)
class GatewaySettings:
    host: str = "0.0.0.0"
    port: int = 8000
    request_max_bytes: int = 1048576
    rate_limit_per_minute: int = 60
    concurrency_limit: int = 10
    execution_timeout_seconds: float = 30.0
    gateway_timeout_seconds: float = 35.0


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    api_key: str
    #: Ordered, de-duplicated list of permitted ``{server}.{tool}`` names.
    allowlist: tuple[str, ...]


@dataclass(frozen=True)
class GatewayConfig:
    gateway: GatewaySettings
    servers: dict[str, ServerConfig]
    clients: dict[str, ClientConfig]
    path: Path | None = None

    def client_for_key(self, api_key: str) -> ClientConfig | None:
        """Resolve an API key to its client, using a constant-time comparison."""
        if not api_key:
            return None
        for client in self.clients.values():
            if hmac.compare_digest(client.api_key.encode("utf-8"), api_key.encode("utf-8")):
                return client
        return None


def _as_int(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid value for {name}: {value!r}") from exc


def _as_float(value: Any, name: str, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid value for {name}: {value!r}") from exc


def parse_config(data: dict[str, Any], path: Path | None = None) -> GatewayConfig:
    """Validate a raw configuration mapping into a :class:`GatewayConfig`."""
    if not isinstance(data, dict):
        raise ConfigError("Configuration root must be a mapping")

    raw_gateway = data.get("gateway") or {}
    if not isinstance(raw_gateway, dict):
        raise ConfigError("'gateway' section must be a mapping")

    settings = GatewaySettings(
        host=str(raw_gateway.get("host", "0.0.0.0")),
        port=_as_int(raw_gateway.get("port"), "gateway.port", 8000),
        request_max_bytes=_as_int(
            raw_gateway.get("request_max_bytes"), "gateway.request_max_bytes", 1048576
        ),
        rate_limit_per_minute=_as_int(
            raw_gateway.get("rate_limit_per_minute"), "gateway.rate_limit_per_minute", 60
        ),
        concurrency_limit=_as_int(
            raw_gateway.get("concurrency_limit"), "gateway.concurrency_limit", 10
        ),
        execution_timeout_seconds=_as_float(
            raw_gateway.get("execution_timeout_seconds"),
            "gateway.execution_timeout_seconds",
            30.0,
        ),
        gateway_timeout_seconds=_as_float(
            raw_gateway.get("gateway_timeout_seconds"),
            "gateway.gateway_timeout_seconds",
            35.0,
        ),
    )

    raw_servers = data.get("servers") or {}
    if not isinstance(raw_servers, dict):
        raise ConfigError("'servers' section must be a mapping")

    servers: dict[str, ServerConfig] = {}
    for name, spec in raw_servers.items():
        if not isinstance(spec, dict):
            logger.warning("Ignoring server %r: its definition is not a mapping", name)
            continue
        if not SERVER_NAME_RE.match(str(name)):
            logger.warning("Ignoring server %r: the name is not valid", name)
            continue
        command = spec.get("command")
        if isinstance(command, str):
            command = [command]
        if not command or not isinstance(command, list):
            logger.warning("Ignoring server %r: it defines no usable 'command'", name)
            continue
        env = spec.get("env") or {}
        if not isinstance(env, dict):
            logger.warning("Ignoring the 'env' of server %r: it is not a mapping", name)
            env = {}
        servers[str(name)] = ServerConfig(
            name=str(name),
            command=[str(part) for part in command],
            env={str(k): str(v) for k, v in env.items()},
            cwd=str(spec["cwd"]) if spec.get("cwd") else None,
        )

    raw_clients = data.get("clients") or {}
    if not isinstance(raw_clients, dict):
        raise ConfigError("'clients' section must be a mapping")

    clients: dict[str, ClientConfig] = {}
    for client_id, spec in raw_clients.items():
        if not isinstance(spec, dict):
            logger.warning("Ignoring client %r: its definition is not a mapping", client_id)
            continue
        api_key = spec.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            logger.warning("Ignoring client %r: it defines no usable 'api_key'", client_id)
            continue
        allowlist = spec.get("allowlist")
        if allowlist is None:
            allowlist = spec.get("tools") or []
        if not isinstance(allowlist, list):
            logger.warning("Ignoring client %r: its allowlist is not a list", client_id)
            allowlist = []
        # The order of the allowlist is preserved so tool discovery is stable.
        ordered: list[str] = []
        for item in allowlist:
            name = str(item)
            if name not in ordered:
                ordered.append(name)
        clients[str(client_id)] = ClientConfig(
            client_id=str(client_id),
            api_key=api_key,
            allowlist=tuple(ordered),
        )

    return GatewayConfig(
        gateway=settings, servers=servers, clients=clients, path=path
    )


#: Locations searched for the configuration file, in order of preference.
CONFIG_SEARCH_PATHS = (
    DEFAULT_CONFIG_PATH,
    "/app/gateway.yaml",
    "./config/gateway.yaml",
    "./gateway.yaml",
)


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Resolve the configuration file location.

    An explicit path (argument or ``GATEWAY_CONFIG_PATH``) is used as-is so
    that a missing file is reported instead of silently falling back.
    """
    explicit = path or os.environ.get("GATEWAY_CONFIG_PATH")
    if explicit:
        return Path(explicit)
    for candidate in CONFIG_SEARCH_PATHS:
        candidate_path = Path(candidate)
        if candidate_path.exists():
            return candidate_path
    return Path(DEFAULT_CONFIG_PATH)


def load_config(path: str | Path | None = None) -> GatewayConfig:
    """Load the gateway configuration from disk."""
    config_path = resolve_config_path(path)
    if not config_path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Malformed configuration file: {config_path}") from exc
    return parse_config(raw or {}, path=config_path)


def database_path() -> Path:
    """Return the resolved SQLite database path."""
    explicit = os.environ.get("GATEWAY_DB_PATH")
    if explicit:
        return Path(explicit)
    for candidate in (DEFAULT_DB_PATH, "/app/data/executions.db", "./data/executions.db"):
        candidate_path = Path(candidate)
        if candidate_path.parent.exists():
            return candidate_path
    return Path(DEFAULT_DB_PATH)


def split_tool_name(name: str) -> tuple[str, str] | None:
    """Split a namespaced tool name into ``(server, tool)``.

    The split happens on the *first* dot only. Returns ``None`` when the name
    does not match the ``{server}.{tool}`` format.
    """
    if not isinstance(name, str):
        return None
    idx = name.find(".")
    if idx <= 0 or idx == len(name) - 1:
        return None
    server = name[:idx]
    tool = name[idx + 1 :]
    if not SERVER_NAME_RE.match(server) or not TOOL_NAME_RE.match(tool):
        return None
    return server, tool
