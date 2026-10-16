"""Mutant MUT-AUTH-DISABLED: authentication check removed.

Overlay this file onto mcp_gateway/auth.py. resolve_client always resolves to
client_admin, so every request is treated as authenticated regardless of the
X-API-Key header. Expected to fail AUTH-001 and AUTH-002.
"""

from typing import Optional

from fastapi import Request

from .config import GatewayConfig
from .errors import AuthMissingError, AuthInvalidError  # noqa: F401 (kept for interface)


def resolve_client(
    request: Request,
    config: GatewayConfig,
) -> tuple[str, dict]:
    """Bypass authentication: always resolve to the admin client."""
    client_cfg = config.clients.get("client_admin")
    if client_cfg is None:
        # Fallback: first configured client.
        client_cfg = next(iter(config.clients.values()))
    return "client_admin", client_cfg
