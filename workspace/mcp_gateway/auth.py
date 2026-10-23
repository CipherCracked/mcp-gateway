"""API key authentication and client resolution."""

from __future__ import annotations

from fastapi import Request

from .config import ClientConfig
from .errors import AUTH_INVALID, AUTH_MISSING, GatewayError

API_KEY_HEADER = "X-API-Key"


def extract_api_key(request: Request) -> str | None:
    """Return the raw API key header value, or ``None`` when absent."""
    return request.headers.get(API_KEY_HEADER)


async def authenticate(request: Request) -> ClientConfig:
    """Resolve the authenticated client for the current request.

    Raises :class:`GatewayError` with ``AUTH_MISSING`` when the header is
    absent and ``AUTH_INVALID`` when it is empty or unknown.
    """
    raw = extract_api_key(request)
    if raw is None:
        raise GatewayError(AUTH_MISSING, "Missing API key", status_code=401)
    if not raw.strip():
        raise GatewayError(AUTH_INVALID, "Invalid API key", status_code=401)

    # The gateway may not be running yet when the application is used
    # without its lifespan; start it on demand so that the key can be
    # resolved against the configured clients.
    from .main import _ensure_started

    try:
        gateway = await _ensure_started(request.app)
    except Exception:  # noqa: BLE001 - the error is sanitised by the handler
        raise GatewayError(AUTH_INVALID, "Invalid API key", status_code=401) from None

    client = gateway.config.client_for_key(raw)
    if client is None:
        raise GatewayError(AUTH_INVALID, "Invalid API key", status_code=401)
    return client
