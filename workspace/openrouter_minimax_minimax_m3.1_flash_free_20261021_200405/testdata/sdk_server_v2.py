"""An MCP server built with the official mcp SDK (v2) for compatibility checks."""

from mcp.server.mcpserver import MCPServer

app = MCPServer(name="sdk-docs")


@app.tool()
def search_documents(query: str, limit: int = 5) -> dict:
    """Search documents."""
    return {"documents": [{"id": "doc-1", "title": "SDK doc", "snippet": query[:40]}]}


@app.tool()
def get_document(id: str) -> dict:
    """Get a document."""
    return {"id": id, "title": "SDK doc", "content": "hello"}


if __name__ == "__main__":
    app.run("stdio")
