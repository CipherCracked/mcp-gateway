"""Jobs MCP server stub - provides execute_job and get_job_status.

This stub server can be used for testing the gateway's REST API endpoints.
"""

import asyncio
import json
import uuid
import sys
from typing import Any, Dict, List, Optional

# In-memory job database
_jobs_db: Dict[str, Dict[str, Any]] = {}


class JobsMCP:
    """Jobs MCP server stub class."""
    
    def __init__(self, name: str = "jobs"):
        self.name = name
    
    async def execute_job(self, payload: dict, priority: str = "normal") -> Dict[str, Any]:
        """Execute a job with payload and optional priority."""
        job_id = str(uuid.uuid4())
        self.jobs_db[job_id] = {
            "job_id": job_id,
            "payload": payload,
            "priority": priority,
            "state": "completed",
            "result": {"job_id": job_id, "payload": payload, "priority": priority}
        }
        return {"job_id": job_id}
    
    async def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """Get the status of a job by ID."""
        if job_id in self.jobs_db:
            job = self.jobs_db[job_id]
            return {"job_id": job_id, "state": job["state"], "result": job.get("result")}
        return {"job_id": job_id, "state": "not_found"}


# Create singleton instance
_jobs_server = JobsMCP("jobs")


async def handle_stdio_protocol(reader, writer):
    """Handle simplified MCP protocol over stdio."""
    try:
        # Send initialize response
        init_msg = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "capabilities": {
                    "tools": {}
                }
            }
        }
        writer.write(json.dumps(init_msg).encode())
        await writer.drain()
        
        # Send initialized notification
        init_result = {
            "jsonrpc": "2.0",
            "method": "initialized",
            "params": {"capabilities": {"tools": {}}}
        }
        writer.write(json.dumps(init_result).encode())
        await writer.drain()
        
        # Process requests
        while True:
            try:
                line = await reader.readline()
                if not line:
                    break
                
                message = json.loads(line.decode())
                method = message.get("method", "")
                params = message.get("params", {})
                msg_id = message.get("id")
                
                if method == "tools/list":
                    tools = [
                        {
                            "name": "jobs.execute_job",
                            "description": "Execute a job with payload and optional priority",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "payload": {"type": "object"},
                                    "priority": {"type": "string", "enum": ["low", "normal", "high"]}
                                },
                                "required": ["payload"],
                                "additionalProperties": False
                            }
                        },
                        {
                            "name": "jobs.get_job_status",
                            "description": "Get the status of a job by ID",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "job_id": {"type": "string"}
                                },
                                "required": ["job_id"],
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
                    
                    if name == "jobs.execute_job":
                        payload = arguments.get("payload", {})
                        priority = arguments.get("priority", "normal")
                        result = await _jobs_server.execute_job(payload, priority)
                        response = {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": result
                        }
                    elif name == "jobs.get_job_status":
                        job_id = arguments.get("job_id", "")
                        result = await _jobs_server.get_job_status(job_id)
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
                
                writer.write(json.dumps(response).encode())
                await writer.drain()
                
            except json.JSONDecodeError:
                pass
    
    except Exception as e:
        print(f"Error handling message: {e}", file=sys.stderr)
    finally:
        try:
            writer.close()
        except:
            pass


async def main():
    """Main entry point for the Jobs MCP server."""
    try:
        while True:
            line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
