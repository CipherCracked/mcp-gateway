"""API key authentication and client resolution.

Validates the X-API-Key header against configured clients and resolves the
authenticated client. Authorization (permission checks) is performed separately
in the gateway layer using the resolved client_id.
"""

from typing import Optional

from fastapi import Request

from .config import GatewayConfig
from .errors import AuthMissingError, AuthInvalidError


async def resolve_client(
    request: Request,
    config: GatewayConfig,
) -> tuple[str, dict]:
    """Authenticate via X-API-Key header and return (client_id, client_config).

    Raises AuthMissingError or AuthInvalidError on failure.
    """
    api_key = request.headers.get("x-api-key")
    if api_key is None or api_key == "":
        raise AuthMissingError()

    resolved = config.client_for_api_key(api_key)
    if resolved is None:
        raise AuthInvalidError()

    return resolved
