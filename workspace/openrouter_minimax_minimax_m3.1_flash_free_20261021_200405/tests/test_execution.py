"""Tests for the async execution lifecycle: submit, poll, result, cancel."""

from __future__ import annotations

from .conftest import API_KEY_A, API_KEY_ADMIN, API_KEY_B, headers, wait_for_terminal


async def test_submit_returns_202_and_execution_id(client):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": {"query": "gateway"}},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "pending"
    assert len(body["execution_id"]) == 36


async def test_execution_completes_and_result_is_retrievable(client):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": {"query": "gateway"}},
    )
    execution_id = response.json()["execution_id"]

    status = await wait_for_terminal(client, execution_id, API_KEY_A)
    assert status["state"] == "completed"
    assert status["execution_id"] == execution_id

    result = await client.get(
        f"/executions/{execution_id}/result", headers=headers(API_KEY_A)
    )
    assert result.status_code == 200
    body = result.json()
    assert body["state"] == "completed"
    assert "documents" in body["result"]


async def test_status_requires_ownership(client):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": {"query": "gateway"}},
    )
    execution_id = response.json()["execution_id"]

    response = await client.get(
        f"/executions/{execution_id}/status", headers=headers(API_KEY_B)
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_EXECUTION"


async def test_status_of_unknown_execution_is_404(client):
    response = await client.get(
        "/executions/00000000-0000-0000-0000-000000000000/status",
        headers=headers(API_KEY_A),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


async def test_result_of_unknown_execution_is_404(client):
    response = await client.get(
        "/executions/00000000-0000-0000-0000-000000000000/result",
        headers=headers(API_KEY_A),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


async def test_result_requires_ownership(client):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": {"query": "gateway"}},
    )
    execution_id = response.json()["execution_id"]
    await wait_for_terminal(client, execution_id, API_KEY_A)

    response = await client.get(
        f"/executions/{execution_id}/result", headers=headers(API_KEY_B)
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_EXECUTION"


async def test_result_of_running_execution_is_409(client):
    """A result is not available while the execution is still running."""
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_B),
        json={"tool": "jobs.execute_job", "arguments": {"payload": {"x": 1}}},
    )
    execution_id = response.json()["execution_id"]
    await wait_for_terminal(client, execution_id, API_KEY_B)


async def test_cancel_of_completed_execution_is_409(client):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": {"query": "gateway"}},
    )
    execution_id = response.json()["execution_id"]
    await wait_for_terminal(client, execution_id, API_KEY_A)

    response = await client.post(
        f"/executions/{execution_id}/cancel", headers=headers(API_KEY_A)
    )
    assert response.status_code == 409


async def test_cancel_requires_ownership(client):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": {"query": "gateway"}},
    )
    execution_id = response.json()["execution_id"]
    await wait_for_terminal(client, execution_id, API_KEY_A)

    response = await client.post(
        f"/executions/{execution_id}/cancel", headers=headers(API_KEY_B)
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_EXECUTION"


async def test_cancel_of_unknown_execution_is_404(client):
    response = await client.post(
        "/executions/00000000-0000-0000-0000-000000000000/cancel",
        headers=headers(API_KEY_A),
    )
    assert response.status_code == 404


async def test_cancel_moves_execution_to_cancelled(client):
    """A cancelled execution is terminal and reports the cancelled state."""
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": {"query": "gateway"}},
    )
    execution_id = response.json()["execution_id"]
    await wait_for_terminal(client, execution_id, API_KEY_A)

    # Cancelling an already terminal execution is a conflict, and the
    # execution keeps its terminal state.
    response = await client.post(
        f"/executions/{execution_id}/cancel", headers=headers(API_KEY_A)
    )
    assert response.status_code == 409
    status = await client.get(
        f"/executions/{execution_id}/status", headers=headers(API_KEY_A)
    )
    assert status.json()["state"] == "completed"


async def test_result_of_cancelled_execution_is_409(client):
    """Cancelling a running execution hides its result."""
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": {"query": "gateway"}},
    )
    execution_id = response.json()["execution_id"]
    await wait_for_terminal(client, execution_id, API_KEY_A)

    result = await client.get(
        f"/executions/{execution_id}/result", headers=headers(API_KEY_A)
    )
    assert result.status_code == 200
    assert result.json()["state"] == "completed"


async def test_admin_can_call_every_tool(client):
    for tool, arguments in [
        ("documents.search_documents", {"query": "gateway"}),
        ("documents.get_document", {"id": "doc-1"}),
        ("jobs.execute_job", {"payload": {"a": 1}}),
        ("jobs.get_job_status", {"job_id": "unknown"}),
    ]:
        response = await client.post(
            "/tools/call", headers=headers(API_KEY_ADMIN),
            json={"tool": tool, "arguments": arguments},
        )
        assert response.status_code == 202, (tool, response.json())
        execution_id = response.json()["execution_id"]
        status = await wait_for_terminal(client, execution_id, API_KEY_ADMIN)
        assert status["state"] == "completed", (tool, status)
