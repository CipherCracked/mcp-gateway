"""Jobs MCP server (stdio) used as an upstream fixture."""

from __future__ import annotations

import uuid
from typing import Any

from .framework import FixtureServer, ToolSpec, run_server

#: In-memory job registry; the fixture is single-process by design.
JOBS: dict[str, dict[str, Any]] = {}

PRIORITIES = ("low", "normal", "high")


async def execute_job(arguments: dict[str, Any]) -> dict[str, Any]:
    """Register a job and return its identifier."""
    payload = arguments.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    priority = arguments.get("priority")
    if not isinstance(priority, str) or priority not in PRIORITIES:
        priority = "normal"

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "job_id": job_id,
        "state": "completed",
        "payload": payload,
        "priority": priority,
    }
    return {"job_id": job_id}


async def get_job_status(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return the state of a previously registered job."""
    job_id = str(arguments.get("job_id") or "")
    job = JOBS.get(job_id)
    if job is None:
        return {"job_id": job_id, "state": "unknown"}
    return {
        "job_id": job["job_id"],
        "state": job["state"],
        "result": {"processed": True},
    }


def build_server() -> FixtureServer:
    server = FixtureServer(name="jobs")
    server.add_tool(
        ToolSpec(
            name="execute_job",
            description="Submit a job for asynchronous processing.",
            input_schema={
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "description": "Job payload.",
                        "additionalProperties": True,
                    },
                    "priority": {
                        "type": "string",
                        "description": "Job priority.",
                        "enum": list(PRIORITIES),
                    },
                },
                "required": ["payload"],
                "additionalProperties": False,
            },
            handler=execute_job,
        )
    )
    server.add_tool(
        ToolSpec(
            name="get_job_status",
            description="Fetch the status of a previously submitted job.",
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Job identifier."},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
            handler=get_job_status,
        )
    )
    return server


def main() -> None:
    run_server(build_server())


if __name__ == "__main__":
    main()
