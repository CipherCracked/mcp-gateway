"""Tests for authentication, authorization, discovery and error handling."""

from __future__ import annotations

import pytest

from .conftest import API_KEY_A, API_KEY_ADMIN, API_KEY_B, headers


async def test_tools_list_requires_authentication(client):
    response = await client.get("/tools/list")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_MISSING"


async def test_tools_list_rejects_invalid_key(client):
    response = await client.get("/tools/list", headers=headers("wrong-key"))
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID"


async def test_tools_list_rejects_empty_key(client):
    response = await client.get("/tools/list", headers=headers(""))
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID"


async def test_tools_list_is_filtered_by_allowlist(client):
    response = await client.get("/tools/list", headers=headers(API_KEY_A))
    assert response.status_code == 200
    assert response.json()["tools"] == [
        "documents.search_documents",
        "documents.get_document",
    ]

    response = await client.get("/tools/list", headers=headers(API_KEY_B))
    assert response.json()["tools"] == ["jobs.execute_job", "jobs.get_job_status"]

    response = await client.get("/tools/list", headers=headers(API_KEY_ADMIN))
    assert len(response.json()["tools"]) == 4


async def test_call_requires_authentication(client):
    response = await client.post(
        "/tools/call", json={"tool": "documents.search_documents", "arguments": {}}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_MISSING"


async def test_call_forbidden_tool(client):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "jobs.execute_job", "arguments": {"payload": {}}},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_TOOL"


async def test_call_unknown_tool(client):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.does_not_exist", "arguments": {}},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "TOOL_NOT_FOUND"


async def test_call_unknown_server(client):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "nosuchserver.tool", "arguments": {}},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "TOOL_NOT_FOUND"


@pytest.mark.parametrize(
    "arguments,expected_key,expected_field",
    [
        ({}, "missing", "query"),
        ({"query": 5}, "type_mismatch", "query"),
        ({"query": "x", "extra": 1}, "unexpected_fields", "extra"),
    ],
)
async def test_call_argument_validation(client, arguments, expected_key, expected_field):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": arguments},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_ARGS"
    assert expected_field in body["details"][expected_key]


async def test_call_enum_mismatch(client):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_B),
        json={
            "tool": "jobs.execute_job",
            "arguments": {"payload": {}, "priority": "urgent"},
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_ARGS"
    assert "priority" in body["details"]["enum_mismatch"]


async def test_error_response_shape(client):
    response = await client.get("/tools/list")
    body = response.json()
    assert set(body) == {"error", "code"}
    assert isinstance(body["error"], str)
    assert body["code"] == "AUTH_MISSING"


async def test_errors_do_not_leak_internals(client):
    response = await client.post(
        "/tools/call",
        headers=headers(API_KEY_A),
        json={"tool": "documents.search_documents", "arguments": {"query": "x", "bad": 1}},
    )
    body = response.json()
    text = str(body)
    assert "/app" not in text
    assert "Traceback" not in text
    assert API_KEY_A not in text
