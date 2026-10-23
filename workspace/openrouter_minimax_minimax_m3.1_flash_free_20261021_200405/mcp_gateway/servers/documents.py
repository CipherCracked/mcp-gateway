"""Documents MCP server (stdio) used as an upstream fixture."""

from __future__ import annotations

from typing import Any

from .framework import FixtureServer, ToolSpec, run_server

DOCUMENTS: list[dict[str, Any]] = [
    {
        "id": "doc-1",
        "title": "Gateway Architecture",
        "content": (
            "The secure MCP gateway aggregates upstream MCP servers and exposes "
            "a REST API with authentication, authorization and rate limiting."
        ),
    },
    {
        "id": "doc-2",
        "title": "Tool Namespacing",
        "content": (
            "Tools are namespaced as {server}.{tool} so that identically named "
            "tools on different servers never collide."
        ),
    },
    {
        "id": "doc-3",
        "title": "Async Execution",
        "content": (
            "Tool calls are accepted asynchronously: the client receives an "
            "execution id, polls the status and finally retrieves the result."
        ),
    },
    {
        "id": "doc-4",
        "title": "Revocation Semantics",
        "content": (
            "Revoking a permission blocks new calls immediately and hides the "
            "results of in-flight executions from the revoked client."
        ),
    },
]


def _snippet(text: str, query: str) -> str:
    lowered = text.lower()
    index = lowered.find(query.lower())
    if index < 0:
        return text[:120]
    start = max(0, index - 40)
    return text[start : start + 160]


async def search_documents(arguments: dict[str, Any]) -> dict[str, Any]:
    """Search the document corpus."""
    query = str(arguments.get("query") or "")
    limit = arguments.get("limit")
    if not isinstance(limit, int) or isinstance(limit, bool):
        limit = 5
    limit = max(0, min(limit, 50))

    matches = []
    for document in DOCUMENTS:
        haystack = f"{document['title']} {document['content']}".lower()
        if not query or query.lower() in haystack:
            matches.append(
                {
                    "id": document["id"],
                    "title": document["title"],
                    "snippet": _snippet(document["content"], query),
                }
            )
    return {"documents": matches[:limit]}


async def get_document(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fetch a single document by id."""
    document_id = str(arguments.get("id") or "")
    for document in DOCUMENTS:
        if document["id"] == document_id:
            return dict(document)
    return {"id": document_id, "title": "", "content": ""}


def build_server() -> FixtureServer:
    server = FixtureServer(name="documents")
    server.add_tool(
        ToolSpec(
            name="search_documents",
            description="Search the document corpus for a query string.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of documents to return.",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=search_documents,
        )
    )
    server.add_tool(
        ToolSpec(
            name="get_document",
            description="Fetch a single document by its id.",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Document identifier."},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=get_document,
        )
    )
    return server


def main() -> None:
    run_server(build_server())


if __name__ == "__main__":
    main()
