"""Async SQLite persistence for execution state.

Single `executions` table with JSON columns. Terminal states survive gateway
restart; non-terminal states are marked EXECUTION_INTERRUPTED on recovery.
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

try:
    import aiosqlite
except ImportError:  # pragma: no cover - fallback if aiosqlite unavailable
    aiosqlite = None

DB_PATH = Path("/app/data/executions.db")


class ExecutionStore:
    """Async SQLite store for execution records."""

    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    async def _connect(self):
        if aiosqlite is not None:
            return await aiosqlite.connect(str(self._db_path))
        conn = sqlite3.connect(str(self._db_path))
        return conn

    async def init_schema(self) -> None:
        """Create the executions table if it doesn't exist."""
        conn = await self._connect()
        try:
            if aiosqlite is not None:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS executions (
                        execution_id TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        tool TEXT NOT NULL,
                        server TEXT NOT NULL,
                        state TEXT NOT NULL,
                        arguments TEXT NOT NULL,
                        result TEXT,
                        error_code TEXT,
                        error_message TEXT,
                        owner_client_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                await conn.commit()
            else:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS executions (
                        execution_id TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        tool TEXT NOT NULL,
                        server TEXT NOT NULL,
                        state TEXT NOT NULL,
                        arguments TEXT NOT NULL,
                        result TEXT,
                        error_code TEXT,
                        error_message TEXT,
                        owner_client_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.commit()
        finally:
            if aiosqlite is not None:
                await conn.close()
            else:
                conn.close()

    async def create(
        self,
        execution_id: str,
        client_id: str,
        tool: str,
        server: str,
        arguments: dict,
        owner_client_id: str,
        created_at: float,
    ) -> None:
        """Insert a new execution in 'pending' state."""
        conn = await self._connect()
        try:
            if aiosqlite is not None:
                await conn.execute(
                    """
                    INSERT INTO executions
                    (execution_id, client_id, tool, server, state, arguments,
                     result, error_code, error_message, owner_client_id,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
                    """,
                    (
                        execution_id,
                        client_id,
                        tool,
                        server,
                        "pending",
                        json.dumps(arguments),
                        owner_client_id,
                        created_at,
                        created_at,
                    ),
                )
                await conn.commit()
            else:
                conn.execute(
                    """
                    INSERT INTO executions
                    (execution_id, client_id, tool, server, state, arguments,
                     result, error_code, error_message, owner_client_id,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
                    """,
                    (
                        execution_id,
                        client_id,
                        tool,
                        server,
                        "pending",
                        json.dumps(arguments),
                        owner_client_id,
                        created_at,
                        created_at,
                    ),
                )
                conn.commit()
        finally:
            if aiosqlite is not None:
                await conn.close()
            else:
                conn.close()

    async def get(self, execution_id: str) -> Optional[dict[str, Any]]:
        """Fetch an execution record by ID. Returns None if not found."""
        conn = await self._connect()
        try:
            if aiosqlite is not None:
                async with conn.execute(
                    "SELECT * FROM executions WHERE execution_id = ?",
                    (execution_id,),
                ) as cursor:
                    row = await cursor.fetchone()
            else:
                cursor = conn.execute(
                    "SELECT * FROM executions WHERE execution_id = ?",
                    (execution_id,),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))
        finally:
            if aiosqlite is not None:
                await conn.close()
            else:
                conn.close()

    async def update_state(
        self,
        execution_id: str,
        state: str,
        result: Optional[Any] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        updated_at: Optional[float] = None,
    ) -> bool:
        """Update an execution's state and optional result/error. Returns True if a row was updated."""
        if updated_at is None:
            updated_at = time.monotonic()

        conn = await self._connect()
        try:
            if aiosqlite is not None:
                cursor = await conn.execute(
                    """
                    UPDATE executions
                    SET state = ?, result = ?, error_code = ?, error_message = ?, updated_at = ?
                    WHERE execution_id = ?
                    """,
                    (
                        state,
                        json.dumps(result) if result is not None else None,
                        error_code,
                        error_message,
                        updated_at,
                        execution_id,
                    ),
                )
                await conn.commit()
                rowcount = cursor.rowcount
            else:
                cursor = conn.execute(
                    """
                    UPDATE executions
                    SET state = ?, result = ?, error_code = ?, error_message = ?, updated_at = ?
                    WHERE execution_id = ?
                    """,
                    (
                        state,
                        json.dumps(result) if result is not None else None,
                        error_code,
                        error_message,
                        updated_at,
                        execution_id,
                    ),
                )
                conn.commit()
                rowcount = cursor.rowcount
            return rowcount > 0
        finally:
            if aiosqlite is not None:
                await conn.close()
            else:
                conn.close()

    async def list_all(self) -> list[dict[str, Any]]:
        """Fetch all execution records (used for restart recovery)."""
        conn = await self._connect()
        try:
            if aiosqlite is not None:
                async with conn.execute("SELECT * FROM executions") as cursor:
                    rows = await cursor.fetchall()
            else:
                cursor = conn.execute("SELECT * FROM executions")
                rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]
        finally:
            if aiosqlite is not None:
                await conn.close()
            else:
                conn.close()
