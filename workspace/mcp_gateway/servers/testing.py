"""Test-only fixture servers used to exercise failure paths.

These servers are not part of the shipped configuration; they exist so the
test-suite can drive slow, failing and crashing upstream behaviour.

``GATEWAY_TEST_TOOL_DELAY`` (seconds) makes tools slow.
``GATEWAY_TEST_TOOL_FAIL`` makes tools report an error.
``GATEWAY_TEST_CRASH_ON_CALL`` makes the server exit when a tool is called.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from .framework import FixtureServer, ToolSpec, run_server

DELAY = float(os.environ.get("GATEWAY_TEST_TOOL_DELAY", "0"))
FAIL = os.environ.get("GATEWAY_TEST_TOOL_FAIL", "").lower() in ("1", "true", "yes")
CRASH = os.environ.get("GATEWAY_TEST_CRASH_ON_CALL", "").lower() in ("1", "true", "yes")


async def _slow_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    delay = DELAY
    if delay <= 0:
        try:
            delay = float(arguments.get("delay") or 0)
        except (TypeError, ValueError):
            delay = 0
    if delay:
        await asyncio.sleep(delay)
    if FAIL:
        raise RuntimeError("fixture failure")
    if CRASH:
        sys.stdout.flush()
        os._exit(1)
    return {"ok": True, "echo": arguments}


def build_server() -> FixtureServer:
    server = FixtureServer(name="testing")
    schema = {
        "type": "object",
        "properties": {
            "delay": {"type": "number"},
            "value": {"type": "string"},
        },
        "required": [],
        "additionalProperties": False,
    }
    server.add_tool(
        ToolSpec(
            name="echo",
            description="Echo the supplied arguments, optionally slowly.",
            input_schema=schema,
            handler=_slow_tool,
        )
    )
    server.add_tool(
        ToolSpec(
            name="fail",
            description="Always fails.",
            input_schema=schema,
            handler=_slow_tool,
        )
    )
    return server


def main() -> None:
    run_server(build_server())


if __name__ == "__main__":
    main()
