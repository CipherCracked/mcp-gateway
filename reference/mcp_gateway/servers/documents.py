"""Documents MCP server — stdio transport.

Exposes two tools:
  - search_documents(query: string, limit?: integer) → {documents: [{id, title, snippet}]}
  - get_document(id: string) → {id, title, content}

Spawned by the gateway as a child subprocess via stdio. Also exposes a
separate HTTP control port for test harnesses (not part of the gateway API).
"""

import asyncio
import json
import os
import sys
import socket
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Thread

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types


_control_state = {
    "fail_next_mode": None,   # "exception" | "mcp_error" | None
    "delay_seconds": 0.0,     # seconds to delay the next N calls
    "pending_delays": 0,      # how many calls remain to be delayed
}


class ControlHandler(BaseHTTPRequestHandler):
    """HTTP handler for verifier test control endpoints (POST only)."""

    def do_POST(self):
        if self.path == "/_test/fail_next":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                data = {}
            mode = data.get("mode")
            if mode in ("exception", "mcp_error"):
                _control_state["fail_next_mode"] = mode
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif self.path == "/_test/delay":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                data = {}
            seconds = float(data.get("seconds", 0))
            count = int(data.get("count", 1))
            if seconds > 0:
                _control_state["delay_seconds"] = seconds
                _control_state["pending_delays"] = _control_state.get("pending_delays", 0) + count
            else:
                # seconds == 0 clears any queued delay state (used by test fixtures
                # to reset control state between tests).
                _control_state["delay_seconds"] = 0.0
                _control_state["pending_delays"] = 0
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "not found"}).encode())

    def log_message(self, format, *args):
        pass  # Suppress stdout noise


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def start_control_port(port: int):
    """Start the HTTP control server on a daemon thread."""
    httpd = _ReusableHTTPServer(("0.0.0.0", port), ControlHandler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


# ── MCP Server ──────────────────────────────────────────────────────────────

server = Server("documents")


@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="search_documents",
            description="Search documents by query string",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_document",
            description="Get a document by its ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    # Apply queued delay before doing work
    pending = _control_state.get("pending_delays", 0)
    if pending > 0:
        _control_state["pending_delays"] = pending - 1
        delay = _control_state.get("delay_seconds", 0.0)
        if delay > 0:
            await asyncio.sleep(delay)
        if _control_state["pending_delays"] == 0:
            _control_state["delay_seconds"] = 0.0

    # Apply queued failure mode
    mode = _control_state.pop("fail_next_mode", None)
    if mode == "exception":
        raise RuntimeError("Simulated upstream exception")
    if mode == "mcp_error":
        return [
            types.TextContent(
                type="text",
                text=json.dumps({"error": "Simulated MCP error from upstream"}),
            )
        ]

    if name == "search_documents":
        query = arguments.get("query", "")
        limit = min(arguments.get("limit", 10), 100)
        docs = [
            {
                "id": f"doc-{i}",
                "title": f"Document {i}: {query}",
                "snippet": f"Snippet for query '{query}' result #{i}",
            }
            for i in range(1, min(limit, 5) + 1)
        ]
        return [
            types.TextContent(type="text", text=json.dumps({"documents": docs}))
        ]

    elif name == "get_document":
        doc_id = arguments.get("id", "")
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {"id": doc_id, "title": f"Document {doc_id}", "content": f"Content of {doc_id}"}
                ),
            )
        ]

    else:
        raise ValueError(f"Unknown tool: {name}")


async def main():
    control_port = int(sys.argv[1]) if len(sys.argv) > 1 else 9001
    start_control_port(control_port)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
