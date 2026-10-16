"""Jobs MCP server — stdio transport.

Exposes two tools:
  - execute_job(payload: object, priority?: "low"|"normal"|"high") → {job_id: string}
  - get_job_status(job_id: string) → {job_id, state, result?, error?}

Spawned by the gateway as a child subprocess via stdio. Also exposes a
separate HTTP control port for test harnesses (not part of the gateway API).
"""

import asyncio
import json
import os
import sys
import uuid as _uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Thread

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types


_control_state = {
    "fail_next_mode": None,   # "exception" | "mcp_error" | None
    "delay_seconds": 0.0,
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
            _control_state["delay_seconds"] = seconds
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif self.path == "/_test/crash":
            # Terminate immediately — simulates upstream process crash
            os._exit(1)

        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "not found"}).encode())

    def log_message(self, format, *args):
        pass


def start_control_port(port: int):
    """Start the HTTP control server on a daemon thread."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ControlHandler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


# ── MCP Server ──────────────────────────────────────────────────────────────

server = Server("jobs")


@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="execute_job",
            description="Execute a job with a payload and optional priority",
            inputSchema={
                "type": "object",
                "properties": {
                    "payload": {"type": "object"},
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                    },
                },
                "required": ["payload"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_job_status",
            description="Get the status of a job by ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    delay = _control_state.pop("delay_seconds", 0.0)
    if delay > 0:
        await asyncio.sleep(delay)

    mode = _control_state.pop("fail_next_mode", None)
    if mode == "exception":
        raise RuntimeError("Simulated upstream exception in jobs server")
    if mode == "mcp_error":
        return [
            types.TextContent(
                type="text",
                content=json.dumps({"error": "Simulated MCP error from jobs upstream"}),
            )
        ]

    if name == "execute_job":
        payload = arguments.get("payload", {})
        priority = arguments.get("priority", "normal")
        job_id = f"job-{_uuid.uuid4().hex[:8]}"
        return [
            types.TextContent(
                type="text",
                content=json.dumps(
                    {"job_id": job_id, "priority": priority, "payload_received": bool(payload)}
                ),
            )
        ]

    elif name == "get_job_status":
        job_id = arguments.get("job_id", "")
        return [
            types.TextContent(
                type="text",
                content=json.dumps(
                    {"job_id": job_id, "state": "completed", "result": {"status": "done"}}
                ),
            )
        ]

    else:
        raise ValueError(f"Unknown tool: {name}")


async def main():
    control_port = int(sys.argv[1]) if len(sys.argv) > 1 else 9002
    start_control_port(control_port)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
