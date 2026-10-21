"""Documents MCP server stub - provides search_documents and get_document.

This stub server can be used for testing the gateway's REST API endpoints.
"""

import asyncio
import json
import sys
from typing import Any, Dict, List

# In-memory document database
_documents_db: Dict[str, Dict[str, Any]] = {
    "doc-1": {"id": "doc-1", "title": "Introduction to Python", "content": "Python is a high-level programming language..."},
    "doc-2": {"id": "doc-2", "title": "Advanced MCP Patterns", "content": "MCP (Model-Controller-Provider) pattern..."},
    "doc-3": {"id": "doc-3", "title": "Async Programming", "content": "Asyncio enables concurrent programming in Python..."},
}


class DocumentsMCP:
    """Documents MCP server stub class."""
    
    def __init__(self, name: str = "documents"):
        self.name = name
    
    async def search_documents(self, query: str, limit: int = 10) -> Dict[str, Any]:
        """Search documents with a query and optional limit."""
        docs = [
            {"id": "doc-1", "title": "Introduction to Python", "snippet": "Python is a high-level programming language..."},
            {"id": "doc-2", "title": "Advanced MCP Patterns", "snippet": "MCP (Model-Controller-Provider) pattern..."},
            {"id": "doc-3", "title": "Async Programming", "snippet": "Asyncio enables concurrent programming in Python..."},
        ]
        results = [d for d in docs if query.lower() in d["title"].lower()][:limit]
        return {"documents": results}
    
    async def get_document(self, doc_id: str) -> Dict[str, Any]:
        """Get a document by ID."""
        if doc_id in _documents_db:
            return _documents_db[doc_id]
        raise ValueError(f"Document '{doc_id}' not found")


# Create singleton instance
_documents_server = DocumentsMCP("documents")


async def run_stdio():
    """Run the stub server via stdio (simplified protocol)."""
    try:
        while True:
            line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            
            line = line.strip()
            if not line:
                continue
            
            try:
                message = json.loads(line)
                method = message.get("method", "")
                params = message.get("params", {})
                msg_id = message.get("id")
                
                response = {}
                
                if method == "tools/list":
                    tools = [
                        {
                            "name": "documents.search_documents",
                            "description": "Search documents with a query and optional limit",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string"},
                                    "limit": {"type": "integer", "minimum": 1}
                                },
                                "required": ["query"],
                                "additionalProperties": False
                            }
                        },
                        {
                            "name": "documents.get_document",
                            "description": "Get a document by ID",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"}
                                },
                                "required": ["id"],
                                "additionalProperties": False
                            }
                        }
                    ]
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"tools": tools}
                    }
                
                elif method == "tools/call":
                    name = params.get("name", "")
                    arguments = params.get("arguments", {})
                    
                    if name == "documents.search_documents":
                        query = arguments.get("query", "")
                        limit = arguments.get("limit", 10)
                        result = await _documents_server.search_documents(query, limit)
                        response = {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": result
                        }
                    elif name == "documents.get_document":
                        doc_id = arguments.get("id", "")
                        result = await _documents_server.get_document(doc_id)
                        response = {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": result
                        }
                    else:
                        response = {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "error": {"code": -32601, "message": f"Tool '{name}' not found"}
                        }
                
                elif method == "initialize":
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "capabilities": {
                                "tools": {}
                            }
                        }
                    }
                
                else:
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"Method '{method}' not found"}
                    }
                
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
                
            except json.JSONDecodeError:
                pass
    
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(run_stdio())
