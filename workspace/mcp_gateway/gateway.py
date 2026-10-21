"""Core gateway logic - routing, upstream clients, execution management."""

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional, List, Tuple

from mcp.server.mcpserver import MCPServer
from mcp.server.models import InitializationOptions, ServerCapabilities
from mcp.server.stdio import stdio_server

from .config import load_config, get_server_command, get_client_config
from .persistence import ExecutionPersistence
from .limits import RateLimiter, ConcurrencyManager
from .errors import (
    GatewayError, AuthMissingError, AuthInvalidError, ForbiddenToolError,
    ToolNotFoundError, ForbiddenExecutionError, InvalidArgsError,
    PayloadTooLargeError, RateLimitedError, ConcurrencyExceededError,
    ExecutionNotFoundError, ExecutionTimeoutError, UpstreamUnavailableError,
    UpstreamFailedError, ResultUnavailableError, ExecutionCancelledError,
    GatewayInternalError
)
from .models import CallRequest, CallResponse, StatusResponse, CancelResponse

logger = logging.getLogger(__name__)


class UpstreamClient:
    """Client for communicating with an upstream MCP server via stdio."""
    
    def __init__(self, command: list):
        self.command = command
        self._server: Optional[MCPServer] = None
        self._connected = False
    
    async def _ensure_connected(self):
        """Ensure the MCP server is connected."""
        if self._connected:
            return
        
        try:
            # Create a new MCP server instance
            self._server = MCPServer(f"upstream-{len(self.command)}")
            
            # Register tools (stub implementation)
            # In a real implementation, this would connect to the actual subprocess
            self._connected = True
            logger.info("Upstream MCP client connected: %s", self.command)
        except Exception as e:
            logger.error("Failed to connect to upstream MCP: %s", e)
            self._server = None
            self._connected = True  # Mark as connected to avoid retrying
            raise
    
    async def list_tools(self) -> List[Dict[str, Any]]:
        """List available tools from the upstream server."""
        await self._ensure_connected()
        if self._server is None:
            raise GatewayInternalError("Upstream client not available")
        
        # In a real implementation, this would call the MCP protocol
        # For now, return empty list (will be populated from config)
        return []
    
    async def invoke_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Invoke a tool on the upstream server."""
        await self._ensure_connected()
        if self._server is None:
            raise GatewayInternalError("Upstream client not available")
        
        # In a real implementation, this would call the MCP protocol
        # For now, return a simulated result
        logger.info("Upstream tool invocation: %s with args: %s", tool_name, arguments)
        return {"status": "simulated"}


class Gateway:
    """Main gateway class that orchestrates auth, routing, and execution."""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.gw_config = config.get("gateway", {})
        
        # Initialize components
        self.persistence = ExecutionPersistence()
        self.rate_limiter = RateLimiter(
            self.gw_config.get("rate_limit_per_minute", 60),
            self.gw_config.get("rate_limit_per_minute", 60) / 60.0
        )
        self.concurrency_manager = ConcurrencyManager(
            self.gw_config.get("concurrency_limit", 10)
        )
        
        # Client configurations: api_key -> client config
        self.clients: Dict[str, Dict[str, Any]] = {}
        self._load_clients(config.get("clients", {}))
        
        # Upstream server clients: server_name -> UpstreamClient
        self.upstream_clients: Dict[str, UpstreamClient] = {}
        
        # Active executions in memory: execution_id -> state info
        # Terminal states are authoritative from SQLite
        self.active_executions: Dict[str, Dict[str, Any]] = {}
        
        # Server capabilities (tools and their schemas)
        self.server_tools: Dict[str, List[Dict[str, Any]]] = {}
    
    def _load_clients(self, clients_config: Dict[str, Any]) -> None:
        """Load client configurations from the gateway config."""
        for client_name, client_data in clients_config.items():
            api_key = client_data.get("api_key", "")
            allowlist = client_data.get("allowlist", [])
            self.clients[api_key] = {
                "client_name": client_name,
                "allowlist": allowlist,
            }
        logger.info("Loaded %d client configurations", len(self.clients))
    
    async def _initialize_upstream_servers(self) -> None:
        """Initialize connections to upstream MCP servers."""
        servers_config = self.config.get("servers", {})
        
        for server_name in ["documents", "jobs"]:
            if server_name in servers_config:
                command = get_server_command(self.config, server_name)
                self.upstream_clients[server_name] = UpstreamClient(command)
                
                # Fetch tool listings from upstream
                try:
                    tools = await self.upstream_clients[server_name].list_tools()
                    self.server_tools[server_name] = tools
                    logger.info(
                        "Loaded %d tools from upstream server '%s'",
                        len(tools), server_name
                    )
                except Exception as e:
                    logger.error(
                        "Failed to load tools from upstream server '%s': %s",
                        server_name, e
                    )
                    self.server_tools[server_name] = []
    
    def _parse_tool_name(self, tool_full_name: str) -> Tuple[str, str]:
        """Parse a tool name into (server_name, tool_name)."""
        idx = tool_full_name.index(".")
        server = tool_full_name[:idx]
        tool = tool_full_name[idx + 1:]
        return server, tool
    
    def _is_valid_tool_name(self, tool_name: str) -> bool:
        """Validate tool name format: {server}.{tool}."""
        import re
        pattern = r'^[a-z][a-z0-9-]*\.[a-zA-Z][a-zA-Z0-9_]*$'
        return bool(re.match(pattern, tool_name))
    
    def _is_tool_allowed(self, client_config: Dict[str, Any], tool_name: str) -> bool:
        """Check if a tool is in the client's allowlist."""
        allowlist = client_config.get("allowlist", [])
        return tool_name in allowlist
    
    async def _validate_args_against_schema(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_schema: Dict[str, Any]
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Validate arguments against the tool's JSON Schema."""
        import jsonschema
        
        try:
            jsonschema.validate(arguments, tool_schema)
            return True, None
        except jsonschema.ValidationError as e:
            error_details = {}
            
            # Check for missing required fields
            if "missing" in e.json_path:
                error_details["missing"] = e.json_path["missing"]
            
            # Check for type mismatches
            if e.validator == "type":
                error_details["type_mismatch"] = {
                    "expected": e.validator_value,
                    "received": str(arguments.get(e.absolute_path, [])) if e.absolute_path else "unknown",
                }
            
            # Check for additional properties
            if e.validator == "additionalProperties":
                error_details["unexpected_fields"] = list(e.path) if hasattr(e, 'path') else []
            
            # Check for enum mismatches
            if e.validator == "enum":
                error_details["enum_mismatch"] = {
                    "expected": list(e.validator_value) if e.validator_value else [],
                    "received": str(arguments.get(e.absolute_path, "")) if e.absolute_path else "",
                }
            
            return False, error_details
        except ImportError:
            return self._basic_validate_args(arguments, tool_schema)
        except Exception as e:
            logger.error("Validation error: %s", e)
            return False, {"general": str(e)}
    
    def _basic_validate_args(
        self,
        arguments: Dict[str, Any],
        tool_schema: Dict[str, Any]
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Basic argument validation without jsonschema."""
        properties = tool_schema.get("properties", {})
        required = tool_schema.get("required", [])
        additional_props = tool_schema.get("additionalProperties", True)
        
        missing = []
        for req in required:
            if req not in arguments:
                missing.append(req)
        
        if missing:
            return False, {"missing": missing}
        
        # Check type mismatches for provided fields
        type_mismatches = []
        for key, value in arguments.items():
            if key in properties:
                prop_schema = properties[key]
                expected_type = prop_schema.get("type")
                if expected_type and not self._check_type(value, expected_type):
                    type_mismatches.append({
                        "field": key,
                        "expected": expected_type,
                        "received": type(value).__name__,
                    })
        
        if type_mismatches:
            return False, {"type_mismatch": type_mismatches}
        
        # Check unexpected fields
        if not additional_props:
            unexpected = []
            for key in arguments:
                if key not in properties:
                    unexpected.append(key)
            if unexpected:
                return False, {"unexpected_fields": unexpected}
        
        return False, None
    
    def _check_type(self, value: Any, expected_type: str) -> bool:
        """Check if a value matches the expected JSON Schema type."""
        type_map = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
        }
        return type_map.get(expected_type, False)
    
    async def submit_execution(
        self,
        client_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        client_config: Dict[str, Any],
    ) -> Tuple[Optional[str], int, Dict[str, Any]]:
        """Submit a tool call for async execution.
        
        Returns (execution_id, http_status, response_data).
        On success: execution_id is set, status is 202
        On submission failure: execution_id is None, status is 502
        """
        # Check rate limit
        rate_allowed = await self.rate_limiter.check_and_acquire()
        if not rate_allowed:
            return None, 429, {"error": "RATE_LIMITED", "code": "RATE_LIMITED"}
        
        # Check concurrency
        concurrency_ok = await self.concurrency_manager.try_acquire(client_id)
        if not concurrency_ok:
            return None, 429, {"error": "CONCURRENCY_EXCEEDED", "code": "CONCURRENCY_EXCEEDED"}
        
        # Validate tool name format
        if not self._is_valid_tool_name(tool_name):
            return None, 400, {"error": "INVALID_ARGS", "code": "INVALID_ARGS", "details": {"missing": ["valid tool name"]}}
        
        # Split tool name
        server, tool = self._parse_tool_name(tool_name)
        
        # Check if server exists
        if server not in self.server_tools:
            return None, 404, {"error": "TOOL_NOT_FOUND", "code": "TOOL_NOT_FOUND"}
        
        # Check if tool exists on that server
        tool_exists = any(t["name"] == tool_name for t in self.server_tools[server])
        if not tool_exists:
            return None, 404, {"error": "TOOL_NOT_FOUND", "code": "TOOL_NOT_FOUND"}
        
        # Check permission (allowlist)
        if not self._is_tool_allowed(client_config, tool_name):
            return None, 403, {"error": "FORBIDDEN_TOOL", "code": "FORBIDDEN_TOOL"}
        
        # Check upstream availability
        if server not in self.upstream_clients:
            return None, 502, {"error": "UPSTREAM_UNAVAILABLE", "code": "UPSTREAM_UNAVAILABLE"}
        
        # Validate arguments against schema
        tool_schema = self._get_tool_schema(tool_name)
        if tool_schema:
            valid, error_details = await self._validate_args_against_schema(
                tool_name, arguments, tool_schema
            )
            if not valid:
                return None, 400, {"error": "INVALID_ARGS", "code": "INVALID_ARGS", "details": error_details}
        
        # Generate execution ID
        execution_id = str(uuid.uuid4())
        
        # Submit to upstream MCP server
        try:
            upstream_result = await self.upstream_clients[server].invoke_tool(
                tool_name, arguments
            )
            logger.info("Upstream tool invocation successful: %s", tool_name)
        except Exception as e:
            logger.error("Upstream invocation error: %s", e)
            # Submission failure - upstream dead before accepting
            self.concurrency_manager.release(client_id)
            return None, 502, {"error": "UPSTREAM_UNAVAILABLE", "code": "UPSTREAM_UNAVAILABLE"}
        
        # Create execution record in persistence
        await self.persistence.create_execution(
            execution_id=execution_id,
            client_id=client_id,
            tool_name=tool_name,
            state="pending",
            arguments=arguments,
        )
        
        # Store in memory for quick access
        self.active_executions[execution_id] = {
            "execution_id": execution_id,
            "client_id": client_id,
            "tool_name": tool_name,
            "state": "pending",
            "arguments": arguments,
        }
        
        return execution_id, 202, {"execution_id": execution_id, "state": "pending"}
    
    def _get_tool_schema(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Get the JSON schema for a tool."""
        server, tool = self._parse_tool_name(tool_name)
        if server in self.server_tools:
            for t in self.server_tools[server]:
                if t["name"] == tool_name:
                    return t.get("inputSchema", {})
        return None
    
    async def update_execution_status(
        self,
        execution_id: str,
        new_state: str,
        error: Optional[Dict[str, Any]] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update the status of an execution in persistence and memory."""
        await self.persistence.update_execution(
            execution_id=execution_id,
            state=new_state,
            result=result,
            error=error,
        )
        
        # Update in-memory copy
        if execution_id in self.active_executions:
            self.active_executions[execution_id]["state"] = new_state
            if error is not None:
                self.active_executions[execution_id]["error"] = error
            if result is not None:
                self.active_executions[execution_id]["result"] = result
        
        # If terminal state, clean up concurrency
        if new_state in ("completed", "failed", "cancelled"):
            pass
    
    async def get_execution(self, execution_id: str) -> Optional[Dict[str, Any]]:
        """Get execution state from persistence (authoritative)."""
        record = await self.persistence.get_execution(execution_id)
        return record
    
    async def cancel_execution(
        self,
        execution_id: str,
        client_id: str,
        tool_name: str,
        client_config: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], int]:
        """Cancel a pending or running execution.
        
        Returns (response_data, http_status).
        """
        # Check permission
        if not self._is_tool_allowed(client_config, tool_name):
            return None, 403, {"error": "FORBIDDEN_EXECUTION", "code": "FORBIDDEN_EXECUTION"}
        
        # Get execution from persistence (authoritative)
        execution = await self.persistence.get_execution(execution_id)
        if execution is None:
            return None, 404, {"error": "NOT_FOUND", "code": "NOT_FOUND"}
        
        # Check ownership
        if execution["client_id"] != client_id:
            return None, 403, {"error": "FORBIDDEN_EXECUTION", "code": "FORBIDDEN_EXECUTION"}
        
        # Check if already terminal
        if execution["state"] in ("completed", "failed", "cancelled"):
            return None, 409, {"error": "CANCELLATION_FAILED", "code": "CANCELLATION_FAILED", "message": f"Execution '{execution_id}' is already in a terminal state"}
        
        # Update execution state to cancelled
        await self.persistence.update_execution(
            execution_id=execution_id,
            state="cancelled",
        )
        
        # Update in-memory
        if execution_id in self.active_executions:
            self.active_executions[execution_id]["state"] = "cancelled"
        
        # Release concurrency
        self.concurrency_manager.release(client_id)
        
        return {"execution_id": execution_id, "state": "cancelled"}, 200
    
    async def refresh_permissions(self, config: Dict[str, Any]) -> None:
        """Refresh gateway state after config reload (SIGHUP)."""
        logger.info("Refreshing gateway permissions after config reload")
        self._load_clients(config.get("clients", {}))
        await self._initialize_upstream_servers()
        logger.info("Permission refresh complete. Active executions: %d", 
                    len(self.active_executions))


class RateLimiter:
    """Token bucket rate limiter per client."""
    
    def __init__(self, capacity: int, refill_rate_per_second: float):
        self.capacity = capacity
        self.refill_rate = refill_rate_per_second
        self.tokens = float(capacity)
        self.last_refill = asyncio.get_event_loop().time()
        self._lock = asyncio.Lock()
    
    async def check_and_acquire(self) -> bool:
        """Check rate limit and acquire token. Returns True if allowed."""
        async with self._lock:
            self._refill()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False
    
    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self.last_refill
        self.tokens = min(
            float(self.capacity),
            self.tokens + elapsed * self.refill_rate
        )
        self.last_refill = now
    
    def release(self):
        """Release a token (for manual refill control)."""
        self.tokens = min(
            float(self.capacity),
            self.tokens + 1.0
        )


class ConcurrencyManager:
    """Per-client concurrency manager using asyncio semaphores."""
    
    def __init__(self, default_limit: int):
        self.default_limit = default_limit
        self.clients: Dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()
    
    async def get_limiter(self, client_id: str) -> asyncio.Semaphore:
        """Get or create a semaphore for the client."""
        async with self._lock:
            if client_id not in self.clients:
                self.clients[client_id] = asyncio.Semaphore(self.default_limit)
            return self.clients[client_id]
    
    async def try_acquire(self, client_id: str) -> bool:
        """Try to acquire concurrency slot. Returns True if successful."""
        limiter = await self.get_limiter(client_id)
        # Non-blocking check
        # If semaphore has permits available, acquire will succeed immediately
        try:
            acquired = limiter.locked()
            # Actually try to acquire
            acquired = False
            # Check if we can acquire without waiting
            try:
                # Try with very short timeout
                waited = await asyncio.wait_for(
                    limiter.acquire(), timeout=0.001
                )
                acquired = waited
                if acquired:
                    # We acquired it, but need to release since we just checked
                    limiter.release()
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            return acquired
        except Exception:
            return False
    
    def release(self, client_id: str):
        """Release concurrency slot for client."""
        if client_id in self.clients:
            self.clients[client_id].release()
