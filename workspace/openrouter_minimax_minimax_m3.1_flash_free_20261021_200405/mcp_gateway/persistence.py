"""Persistent execution state backed by SQLite (via aiosqlite)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger("mcp_gateway.persistence")

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELLED})
NON_TERMINAL_STATES = frozenset({PENDING, RUNNING})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    server_name TEXT NOT NULL,
    state TEXT NOT NULL,
    arguments TEXT,
    result TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_executions_client ON executions(client_id);
CREATE INDEX IF NOT EXISTS idx_executions_state ON executions(state);
"""


@dataclass
class ExecutionRecord:
    """A persisted execution."""

    execution_id: str
    client_id: str
    tool_name: str
    server_name: str
    state: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def error_payload(self) -> dict[str, str] | None:
        if self.error_code is None:
            return None
        return {"code": self.error_code, "message": self.error_message or "Execution failed"}


def _row_to_record(row: aiosqlite.Row) -> ExecutionRecord:
    arguments: dict[str, Any] = {}
    if row["arguments"]:
        try:
            parsed = json.loads(row["arguments"])
            if isinstance(parsed, dict):
                arguments = parsed
        except ValueError:  # pragma: no cover - defensive
            arguments = {}
    result: Any = None
    if row["result"] is not None:
        try:
            result = json.loads(row["result"])
        except ValueError:  # pragma: no cover - defensive
            result = None
    return ExecutionRecord(
        execution_id=row["execution_id"],
        client_id=row["client_id"],
        tool_name=row["tool_name"],
        server_name=row["server_name"],
        state=row["state"],
        arguments=arguments,
        result=result,
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ExecutionStore:
    """Async SQLite store for execution state."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.executescript(_SCHEMA)
        await self._connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:  # pragma: no cover - defensive
            raise RuntimeError("Execution store is not connected")
        return self._connection

    async def create(self, record: ExecutionRecord) -> None:
        await self.connection.execute(
            """
            INSERT INTO executions (
                execution_id, client_id, tool_name, server_name, state,
                arguments, result, error_code, error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.execution_id,
                record.client_id,
                record.tool_name,
                record.server_name,
                record.state,
                json.dumps(record.arguments or {}),
                None if record.result is None else json.dumps(record.result),
                record.error_code,
                record.error_message,
                record.created_at,
                record.updated_at,
            ),
        )
        await self.connection.commit()

    async def update(
        self,
        execution_id: str,
        *,
        state: str | None = None,
        result: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
        updated_at: float | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if state is not None:
            sets.append("state = ?")
            params.append(state)
        if result is not None:
            sets.append("result = ?")
            params.append(json.dumps(result))
        if error_code is not None:
            sets.append("error_code = ?")
            params.append(error_code)
        if error_message is not None:
            sets.append("error_message = ?")
            params.append(error_message)
        if updated_at is not None:
            sets.append("updated_at = ?")
            params.append(updated_at)
        if not sets:
            return
        params.append(execution_id)
        await self.connection.execute(
            f"UPDATE executions SET {', '.join(sets)} WHERE execution_id = ?", params
        )
        await self.connection.commit()

    async def get(self, execution_id: str) -> ExecutionRecord | None:
        cursor = await self.connection.execute(
            "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _row_to_record(row)

    async def list_for_client(self, client_id: str) -> list[ExecutionRecord]:
        cursor = await self.connection.execute(
            "SELECT * FROM executions WHERE client_id = ? ORDER BY created_at", (client_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_record(row) for row in rows]

    async def mark_interrupted(self, now: float) -> int:
        """Mark every non-terminal execution as failed/interrupted."""
        cursor = await self.connection.execute(
            "SELECT execution_id FROM executions WHERE state IN (?, ?)", (PENDING, RUNNING)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        count = 0
        for row in rows:
            await self.connection.execute(
                """
                UPDATE executions
                   SET state = ?, error_code = ?, error_message = ?, updated_at = ?
                 WHERE execution_id = ?
                """,
                (
                    FAILED,
                    "EXECUTION_INTERRUPTED",
                    "Execution interrupted by gateway restart",
                    now,
                    row["execution_id"],
                ),
            )
            count += 1
        await self.connection.commit()
        return count
