"""An MCP server built with the official mcp SDK (v1) for compatibility checks."""

from mcp.server.fastmcp import FastMCP

app = FastMCP(name="sdk-v1-docs")


@app.tool()
def search_documents(query: str, limit: int = 5) -> dict:
    """Search documents."""
    return {"documents": [{"id": "doc-1", "title": "SDK v1 doc", "snippet": query[:40]}]}


@app.tool()
def get_document(id: str) -> dict:
    """Get a document."""
    return {"id": id, "title": "SDK v1 doc", "content": "hello"}


if __name__ == "__main__":
    app.run("stdio")
