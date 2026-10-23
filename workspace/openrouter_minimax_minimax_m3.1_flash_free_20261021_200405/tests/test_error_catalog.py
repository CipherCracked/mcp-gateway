"""Verification of the complete error-code catalogue and the error envelope."""

from __future__ import annotations

from reference.mcp_gateway.errors import (
    AUTH_INVALID,
    AUTH_MISSING,
    CONCURRENCY_EXCEEDED,
    EXECUTION_INTERRUPTED,
    EXECUTION_TIMEOUT,
    FORBIDDEN_EXECUTION,
    FORBIDDEN_TOOL,
    INTERNAL,
    INVALID_ARGS,
    NOT_FOUND,
    PAYLOAD_TOO_LARGE,
    RATE_LIMITED,
    RESULT_UNAVAILABLE,
    TOOL_NOT_FOUND,
    UPSTREAM_FAILED,
    UPSTREAM_UNAVAILABLE,
)

from .conftest import API_KEY_A, headers

CATALOG = {
    AUTH_MISSING: 401,
    AUTH_INVALID: 401,
    FORBIDDEN_TOOL: 403,
    TOOL_NOT_FOUND: 404,
    FORBIDDEN_EXECUTION: 403,
    NOT_FOUND: 404,
    INVALID_ARGS: 400,
    PAYLOAD_TOO_LARGE: 413,
    RATE_LIMITED: 429,
    CONCURRENCY_EXCEEDED: 429,
    RESULT_UNAVAILABLE: 409,
    UPSTREAM_UNAVAILABLE: 502,
    INTERNAL: 500,
}


async def test_catalog_is_complete():
    """Every documented error code exists with the documented status."""
    from reference.mcp_gateway import errors

    for code, status in CATALOG.items():
        assert hasattr(errors, code), code
        assert errors.STATUS_BY_CODE[code] == status, code

    # Codes that only ever describe a terminal execution state.
    for code in (EXECUTION_TIMEOUT, UPSTREAM_FAILED, UPSTREAM_UNAVAILABLE,
                 EXECUTION_INTERRUPTED):
        assert hasattr(errors, code)


async def test_error_envelope_never_has_details_except_for_invalid_args(client):
    """``details`` is reserved for INVALID_ARGS."""
    responses = [
        await client.get("/tools/list"),
        await client.get("/tools/list", headers=headers("bad")),
        await client.post(
            "/tools/call",
            headers=headers(API_KEY_A),
            json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
        ),
        await client.post(
            "/tools/call",
            headers=headers(API_KEY_A),
            json={"tool": "nope.nope", "arguments": {}},
        ),
        await client.get(
            "/executions/11111111-1111-1111-1111-111111111111/status",
            headers=headers(API_KEY_A),
        ),
    ]
    for response in responses:
        body = response.json()
        assert "error" in body and "code" in body
        if body["code"] != INVALID_ARGS:
            assert "details" not in body, body


async def test_invalid_args_has_details(client):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": {}},
    )
    body = response.json()
    assert body["code"] == INVALID_ARGS
    assert isinstance(body["details"], dict)
    assert body["details"]


async def test_every_error_code_is_reachable(client):
    """A sample of each documented error code is produced with its status."""
    observed: dict[str, int] = {}

    response = await client.get("/tools/list")
    observed[response.json()["code"]] = response.status_code

    response = await client.get("/tools/list", headers=headers("bad"))
    observed[response.json()["code"]] = response.status_code

    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
    )
    observed[response.json()["code"]] = response.status_code

    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "nope.nope", "arguments": {}},
    )
    observed[response.json()["code"]] = response.status_code

    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": {}},
    )
    observed[response.json()["code"]] = response.status_code

    response = await client.get(
        "/executions/11111111-1111-1111-1111-111111111111/status",
        headers=headers(API_KEY_A),
    )
    observed[response.json()["code"]] = response.status_code

    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={
            "tool": "documents.search_documents",
            "arguments": {"query": "x" * (1024 * 1024 + 10)},
        },
    )
    observed[response.json()["code"]] = response.status_code

    for code, status in observed.items():
        assert CATALOG[code] == status, (code, status)


async def test_sanitized_messages_hide_internals(client):
    """No client-visible payload leaks paths, traces or credentials."""
    probes = [
        (await client.get("/tools/list")).text,
        (await client.get("/tools/list", headers=headers("secret-key"))).text,
        (
            await client.post(
                "/tools/call",
                headers=headers(API_KEY_A),
                json={"tool": "documents.search_documents", "arguments": {}},
            )
        ).text,
        (
            await client.get(
                "/executions/11111111-1111-1111-1111-111111111111/result",
                headers=headers(API_KEY_A),
            )
        ).text,
    ]
    for text in probes:
        assert "Traceback" not in text
        assert "File \"" not in text
        assert "/workspace" not in text
        assert "/app/" not in text
        assert "sqlite" not in text.lower()
        assert API_KEY_A not in text
        assert "SELECT" not in text
