"""Durable ownership boundary for MCP-submitted FlowCommand jobs."""
from __future__ import annotations

from contextlib import asynccontextmanager

import aiosqlite

from agent.config import DB_PATH


class MCPRemoteJobRegistry:
    @asynccontextmanager
    async def _connect(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS mcp_remote_job (
                    job_id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            await db.commit()
            yield db

    async def record(self, job_id: str, subject: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO mcp_remote_job (job_id, subject) VALUES (?, ?)",
                (job_id, subject),
            )
            await db.commit()
            async with db.execute("SELECT subject FROM mcp_remote_job WHERE job_id=?", (job_id,)) as cursor:
                row = await cursor.fetchone()
            if row is None or row[0] != subject:
                raise PermissionError("Remote image job belongs to another MCP principal")

    async def owns(self, job_id: str, subject: str) -> bool:
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM mcp_remote_job WHERE job_id=? AND subject=?", (job_id, subject)
            ) as cursor:
                return await cursor.fetchone() is not None


mcp_remote_job_registry = MCPRemoteJobRegistry()
