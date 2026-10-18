"""Shared fixtures and helpers for the Secure MCP Gateway verifier suite.

The gateway is started once per session as a subprocess (uvicorn on port 8000).
Its lifespan spawns the two upstream MCP fixture servers as child processes.
Tests drive the gateway over HTTP and control fixture behavior through the
fixture servers' test-only HTTP control endpoints (documents:9001, jobs:9002).

The gateway itself never calls the control endpoints — only the tests do.
"""

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

import httpx
import pytest

GATEWAY_URL = "http://127.0.0.1:8000"
DOCUMENTS_CONTROL = "http://127.0.0.1:9001"
JOBS_CONTROL = "http://127.0.0.1:9002"

API_KEY_ADMIN = "key-admin-789"
API_KEY_A = "key-a-123"
API_KEY_B = "key-b-456"

BAD_KEY = "definitely-not-a-valid-key"


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its descendants (Windows: taskkill /T)."""
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
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
    """Start the gateway (uvicorn) once per session and tear it down at the end."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    proc = subprocess.Popen(
        ["uvicorn", "mcp_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"],
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    deadline = time.time() + 30
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            out_text = out.decode(errors="replace") if out is not None else "<no stdout (DEVNULL)>"
            err_text = err.decode(errors="replace") if err is not None else ""
            raise RuntimeError(
                f"Gateway exited early (code {proc.returncode}):\n"
                f"stdout: {out_text}\n"
                f"stderr: {err_text}"
            )
        try:
            r = httpx.get(
                f"{GATEWAY_URL}/tools/list",
                headers={"x-api-key": API_KEY_ADMIN},
                timeout=1.0,
            )
            if r.status_code == 200:
                ready = True
                break
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
            pass
        time.sleep(0.25)

    if not ready:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise RuntimeError("Gateway did not become ready within 30s")

    # Diagnostic: check jobs server availability right after startup
    try:
        r = httpx.get(f"{GATEWAY_URL}/tools/list", headers={"x-api-key": API_KEY_ADMIN}, timeout=1.0)
        tools = r.json().get("tools", [])
        print(f"\nDIAG: Gateway ready. Tools: {tools}")
        # Try a jobs call to check if upstream is alive
        r2 = httpx.post(f"{GATEWAY_URL}/tools/call", headers={"x-api-key": API_KEY_ADMIN},
                        json={"tool": "jobs.execute_job", "arguments": {"payload": {}}}, timeout=5.0)
        print(f"DIAG: Jobs execute_job immediately after startup: {r2.status_code} {r2.text[:200]}")
    except Exception as e:
        print(f"\nDIAG: Jobs check failed: {e}")

    yield proc

    _kill_process_tree(proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


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

    The gateway runs as a subprocess, so its limiter objects are not directly
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
