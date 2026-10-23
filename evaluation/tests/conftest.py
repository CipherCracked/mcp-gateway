"""Shared fixtures and helpers for the Secure MCP Gateway verifier suite.

The gateway is started by pipeline.py before tests run. This fixture just
verifies it's reachable.
"""

import os
import time
from pathlib import Path

import httpx
import pytest

GATEWAY_URL = "http://127.0.0.1:8000"
DOCUMENTS_CONTROL = "http://127.0.0.1:9001"
JOBS_CONTROL = "http://127.0.0.1:9002"

API_KEY_ADMIN = "key-admin-789"
API_KEY_A = "key-a-123"
API_KEY_B = "key-b-456"

BAD_KEY = "definitely-not-a-valid-key"

GATEWAY_MODULE = os.environ.get("GATEWAY_MODULE", "reference.mcp_gateway.main:app")


def _control_post(base: str, path: str, payload: dict) -> httpx.Response:
    """POST to a fixture control endpoint. Crashes may drop the connection."""
    try:
        return httpx.post(f"{base}{path}", json=payload, timeout=2.0)
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError):
        # Expected for /_test/crash (os._exit kills the server mid-response,
        # causing connection reset on Windows).
        return httpx.Response(0, text="control connection dropped")


def fail_next_documents(mode: str) -> httpx.Response:
    return _control_post(DOCUMENTS_CONTROL, "/_test/fail_next", {"mode": mode})


def fail_next_jobs(mode: str) -> httpx.Response:
    return _control_post(JOBS_CONTROL, "/_test/fail_next", {"mode": mode})


def delay_documents(seconds: float, count: int = 1) -> httpx.Response:
    """Set delay for the next N requests to the documents server.

    Args:
        seconds: Delay in seconds (0 to clear all pending delays)
        count: Number of requests to delay (default 1)
    """
    return _control_post(DOCUMENTS_CONTROL, "/_test/delay", {"seconds": seconds, "count": count})


def delay_jobs(seconds: float, count: int = 1) -> httpx.Response:
    """Set delay for the next N requests to the jobs server.

    Args:
        seconds: Delay in seconds (0 to clear all pending delays)
        count: Number of requests to delay (default 1)
    """
    return _control_post(JOBS_CONTROL, "/_test/delay", {"seconds": seconds, "count": count})


def crash_jobs() -> httpx.Response:
    return _control_post(JOBS_CONTROL, "/_test/crash", {})


@pytest.fixture(scope="session")
def gateway_process():
    """Gateway is already started by pipeline.py. Just verify it's reachable."""
    # Wait for gateway to be ready (pipeline.py already started it)
    for i in range(60):
        try:
            r = httpx.get(f"{GATEWAY_URL}/tools/list",
                          headers={"x-api-key": API_KEY_ADMIN}, timeout=2.0)
            if r.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            pass
        time.sleep(0.5)
    else:
        pytest.fail("Gateway not ready after 30s")

    yield None

    # Don't stop the container - pipeline.py handles reset


@pytest.fixture
def client(gateway_process):
    """HTTP client pointed at the running gateway."""
    with httpx.Client(base_url=GATEWAY_URL, timeout=10.0) as c:
        yield c


@pytest.fixture
def auth_headers_admin():
    return {"x-api-key": API_KEY_ADMIN}


@pytest.fixture
def auth_headers_a():
    return {"x-api-key": API_KEY_A}


@pytest.fixture
def auth_headers_b():
    return {"x-api-key": API_KEY_B}


@pytest.fixture(autouse=True)
def reset_limiters():
    """Reset rate limiters and concurrency limiters before each test for isolation.

    The gateway runs inside Docker, so its limiter objects are not directly
    accessible from the test process. We reset via the gateway's test-only
    HTTP endpoint (POST /_test/reset_limiters) so each test starts with a fresh
    token bucket and empty semaphores.
    """
    try:
        httpx.post(f"{GATEWAY_URL}/_test/reset_limiters", timeout=2.0)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
        pass  # Gateway not yet running or disconnected; reset is a no-op
    yield
    try:
        httpx.post(f"{GATEWAY_URL}/_test/reset_limiters", timeout=2.0)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
        pass


@pytest.fixture
def reset_control_state():
    """Clear any queued fixture control state before/after a test.

    fail_next and delay are single-shot (popped on use), but this ensures a
    clean slate by draining any lingering delay (delay 0) — fail_next can only
    be cleared by consuming it, so tests that set it must consume it.
    """
    delay_documents(0.0)
    delay_jobs(0.0)
    yield
    delay_documents(0.0)
    delay_jobs(0.0)


def wait_for_state(client: httpx.Client, execution_id: str, headers: dict,
                   target_states: set, timeout: float = 20.0, interval: float = 0.25):
    """Poll /executions/{id}/status until the state is in target_states."""
    deadline = time.time() + timeout
    last_state = None
    while time.time() < deadline:
        r = client.get(f"/executions/{execution_id}/status", headers=headers)
        assert r.status_code == 200, f"status poll failed: {r.status_code} {r.text}"
        record = r.json()
        last_state = record["state"]
        if last_state in target_states:
            return record
        time.sleep(interval)
    raise AssertionError(
        f"Execution {execution_id} did not reach {target_states}; last state={last_state}"
    )
