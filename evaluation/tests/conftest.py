"""Shared fixtures and helpers for the Secure MCP Gateway verifier suite.

The gateway is started once per session inside Docker via app-setup/start.sh.
Its lifespan spawns the two upstream MCP fixture servers as child processes.
Tests drive the gateway over HTTP and control fixture behavior through the
fixture servers' test-only HTTP control endpoints (documents:9001, jobs:9002).

The gateway itself never calls the control endpoints — only the tests do.
"""

import os
import subprocess
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


def _free_port(port: int) -> None:
    """Kill any process already listening on ``port``.

    The gateway (8000) and the fixture control servers (documents:9001,
    jobs:9002) must bind cleanly. On Windows, SO_REUSEADDR lets multiple
    processes share a port, so stale upstream servers from a previous run can
    silently steal control-endpoint traffic (delay / fail_next / crash) away
    from the live gateway children, making tests flaky. Freeing the ports up
    front makes every test run self-contained.
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=10
        ).stdout
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid > 0:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True,
                        timeout=5,
                    )
    except Exception:
        pass


def _control_post(base: str, path: str, payload: dict) -> httpx.Response:
    """POST to a fixture control endpoint. Crashes may drop the connection."""
    try:
        return httpx.post(f"{base}{path}", json=payload, timeout=2.0)
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError):
        # Expected for /_test/crash (os._exit kills the server mid-response,
        # causing connection reset on Windows).
        return httpx.Response(0, text="control connection dropped")


def fail_next_documents(mode: str) -> httpx.Response:
    return _control_post(DOCUMENTS_CONTROL, "/_test/fail_next", {"mode": mode})


def fail_next_jobs(mode: str) -> httpx.Response:
    return _control_post(JOBS_CONTROL, "/_test/fail_next", {"mode": mode})


def delay_documents(seconds: float) -> httpx.Response:
    return _control_post(DOCUMENTS_CONTROL, "/_test/delay", {"seconds": seconds})


def delay_jobs(seconds: float) -> httpx.Response:
    return _control_post(JOBS_CONTROL, "/_test/delay", {"seconds": seconds})


def crash_jobs() -> httpx.Response:
    return _control_post(JOBS_CONTROL, "/_test/crash", {})


@pytest.fixture(scope="session")
def gateway_process():
    """Start the gateway inside Docker via app-setup/start.sh."""

    # Free the ports the gateway and its upstream fixtures bind so stale
    # processes from a previous run can't steal control-endpoint traffic
    # (delay / fail_next / crash) away from the live children.
    for port in (8000, 9001, 9002):
        _free_port(port)

    # Determine target from GATEWAY_MODULE env var.
    gateway_module = os.environ.get("GATEWAY_MODULE", "reference.mcp_gateway.main:app")
    target = "reference" if gateway_module.startswith("reference.") else "workspace"

    repo_root = Path(__file__).resolve().parents[2]
    start_script = repo_root / "app-setup" / "start.sh"

    env = dict(os.environ)
    env["WORKSPACE_HOST"] = str(repo_root)

    # Start the gateway inside Docker. start.sh waits for readiness internally.
    subprocess.run(
        ["bash", str(start_script), target],
        cwd=str(repo_root),
        env=env,
        check=True,
        timeout=120,
    )

    yield None

    # Stop and remove the container.
    reset_script = repo_root / "app-setup" / "reset.sh"
    subprocess.run(
        ["bash", str(reset_script)],
        cwd=str(repo_root),
        env=env,
    )


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
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        pass  # Gateway not yet running; reset is a no-op
    yield
    try:
        httpx.post(f"{GATEWAY_URL}/_test/reset_limiters", timeout=2.0)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
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
