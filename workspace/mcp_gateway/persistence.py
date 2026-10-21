"""SQLite persistence for execution state."""

import aiosqlite
import json
import os
from typing import Dict, Any, Optional, List


DB_PATH = "/app/data/executions.db"


class ExecutionPersistence:
    """Async SQLite persistence for execution state."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    async def _ensure_conn(self):
        """Ensure database and table exist."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    arguments JSON NOT NULL DEFAULT '{}',
                    result JSON,
                    error JSON,
                    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
                    updated_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
                )"""
            )
            await db.commit()

    async def create_execution(
        self,
        execution_id: str,
        client_id: str,
        tool_name: str,
        state: str,
        arguments: Optional[Dict[str, Any]] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Create a new execution record."""
        await self._ensure_conn()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO executions 
                   (execution_id, client_id, tool_name, state, arguments, result, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    execution_id,
                    client_id,
                    tool_name,
                    state,
                    json.dumps(arguments) if arguments else json.dumps({}),
                    json.dumps(result) if result else None,
                    json.dumps(error) if error else None,
                ),
            )
            await db.commit()

    async def get_execution(self, execution_id: str) -> Optional[Dict[str, Any]]:
        """Get an execution record by ID."""
        await self._ensure_conn()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM executions WHERE execution_id = ?",
                (execution_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            return {
                "execution_id": row["execution_id"],
                "client_id": row["client_id"],
                "tool_name": row["tool_name"],
                "state": row["state"],
                "arguments": json.loads(row["arguments"]) if row["arguments"] else {},
                "result": json.loads(row["result"]) if row["result"] else None,
                "error": json.loads(row["error"]) if row["error"] else None,
            }

    async def update_execution(
        self,
        execution_id: str,
        state: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update an execution record's state, result, and/or error."""
        await self._ensure_conn()
        async with aiosqlite.connect(self.db_path) as db:
            updates = ["state = ?"]
            params = [state]

            if result is not None:
                updates.append("result = ?")
                params.append(json.dumps(result))

            if error is not None:
                updates.append("error = ?")
                params.append(json.dumps(error))

            params.append(execution_id)

            await db.execute(
                f"""UPDATE executions
                   SET state = ?, {', '.join(updates)}
                   WHERE execution_id = ?""",
                params,
            )
            await db.commit()

    async def list_executions_by_client(
        self,
        client_id: str,
        state_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List executions for a specific client."""
        await self._ensure_conn()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if state_filter:
                cursor = await db.execute(
                    "SELECT execution_id, client_id, tool_name, state, arguments, result, error FROM executions WHERE client_id = ? AND state = ?",
                    (client_id, state_filter),
                )
            else:
                cursor = await db.execute(
                    "SELECT execution_id, client_id, tool_name, state, arguments, result, error FROM executions WHERE client_id = ?",
                    (client_id,),
                )
            rows = await cursor.fetchall()
            await cursor.close()

            results = []
            for row in rows:
                results.append({
                    "execution_id": row["execution_id"],
                    "client_id": row["client_id"],
                    "tool_name": row["tool_name"],
                    "state": row["state"],
                    "arguments": json.loads(row["arguments"]) if row["arguments"] else {},
                    "result": json.loads(row["result"]) if row["result"] else None,
                    "error": json.loads(row["error"]) if row["error"] else None,
                })
            return results

    async def delete_execution(self, execution_id: str) -> None:
        """Delete an execution record."""
        await self._ensure_conn()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM executions WHERE execution_id = ?",
                (execution_id,),
            )
            await db.commit()
