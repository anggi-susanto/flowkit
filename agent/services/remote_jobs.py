"""PostgreSQL-backed remote image jobs for FlowCommand.

This module is intentionally separate from the existing SQLite project queue.
The latter remains the local FlowKit workflow; this store owns public remote jobs.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from agent.config import (
    FLOWCOMMAND_DATABASE_URL,
    FLOWCOMMAND_JOB_LEASE_SECONDS,
    FLOWCOMMAND_MAX_ATTEMPTS,
)

STATUSES = ("QUEUED", "LEASED", "PROCESSING", "UPLOADING", "COMPLETED", "FAILED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS image_jobs (
    id UUID PRIMARY KEY,
    client_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    aspect_ratio TEXT NOT NULL DEFAULT '1:1' CHECK (aspect_ratio IN ('1:1','9:16','16:9','3:4','4:3')),
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','LEASED','PROCESSING','UPLOADING','COMPLETED','FAILED')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    worker_id TEXT,
    lease_token_hash TEXT,
    lease_until TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    storage_key TEXT,
    content_type TEXT,
    content_length BIGINT,
    sha256 TEXT,
    error_code TEXT,
    error_message TEXT,
    idempotency_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    UNIQUE (client_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_image_jobs_queue ON image_jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_image_jobs_lease ON image_jobs (status, lease_until);
CREATE INDEX IF NOT EXISTS idx_image_jobs_client ON image_jobs (client_id, id);
"""


class RemoteJobStore:
    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or FLOWCOMMAND_DATABASE_URL
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if not self.dsn:
            raise RuntimeError("FLOWCOMMAND_DATABASE_URL is not configured")
        if self.pool is None:
            self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
            async with self.pool.acquire() as conn:
                await conn.execute(SCHEMA)
                await conn.execute("ALTER TABLE image_jobs ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ")
                await conn.execute("ALTER TABLE image_jobs ADD COLUMN IF NOT EXISTS aspect_ratio TEXT NOT NULL DEFAULT '1:1'")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_image_jobs_next_attempt ON image_jobs (status, next_attempt_at)")

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("RemoteJobStore is not connected")
        return self.pool

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _row(row: asyncpg.Record | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    async def create(self, client_id: str, prompt: str, aspect_ratio: str = "1:1", idempotency_key: str | None = None) -> dict[str, Any]:
        job_id = uuid.uuid4()
        query = """
            INSERT INTO image_jobs (id, client_id, prompt, aspect_ratio, idempotency_key)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (client_id, idempotency_key) DO UPDATE SET updated_at = image_jobs.updated_at
            RETURNING *
        """
        async with self._require_pool().acquire() as conn:
            row = await conn.fetchrow(query, job_id, client_id, prompt, aspect_ratio, idempotency_key)
        return self._row(row)  # type: ignore[return-value]

    async def get_owned(self, job_id: str, client_id: str) -> dict[str, Any] | None:
        async with self._require_pool().acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM image_jobs WHERE id=$1 AND client_id=$2", job_id, client_id)
        return self._row(row)

    async def claim(self, worker_id: str) -> dict[str, Any] | None:
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_token(token)
        lease_until = datetime.now(timezone.utc) + timedelta(seconds=FLOWCOMMAND_JOB_LEASE_SECONDS)
        async with self._require_pool().acquire() as conn:
            async with conn.transaction():
                await self._recover_expired_with_conn(conn)
                row = await conn.fetchrow(
                    """WITH next_job AS (
                         SELECT id FROM image_jobs
                         WHERE status='QUEUED' AND attempt_count < $1
                           AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
                         ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
                       )
                       UPDATE image_jobs j
                        SET status='LEASED', worker_id=$2, lease_token_hash=$3, lease_until=$4, next_attempt_at=NULL,
                           attempt_count=j.attempt_count + 1, updated_at=NOW()
                       FROM next_job WHERE j.id=next_job.id RETURNING j.*""",
                    FLOWCOMMAND_MAX_ATTEMPTS, worker_id, token_hash, lease_until,
                )
        result = self._row(row)
        if result:
            result["lease_token"] = token
        return result

    async def _recover_expired_with_conn(self, conn) -> dict[str, int]:
        requeued = await conn.execute(
            """UPDATE image_jobs SET status='QUEUED', worker_id=NULL, lease_token_hash=NULL, lease_until=NULL,
                   error_code='LEASE_EXPIRED', error_message='Worker lease expired; job requeued', updated_at=NOW()
               WHERE status IN ('LEASED','PROCESSING','UPLOADING') AND lease_until < NOW() AND attempt_count < $1""",
            FLOWCOMMAND_MAX_ATTEMPTS,
        )
        failed = await conn.execute(
            """UPDATE image_jobs SET status='FAILED', worker_id=NULL, lease_token_hash=NULL, lease_until=NULL,
                   error_code='LEASE_EXPIRED', error_message='Worker lease expired after final attempt', updated_at=NOW()
               WHERE status IN ('LEASED','PROCESSING','UPLOADING') AND lease_until < NOW() AND attempt_count >= $1""",
            FLOWCOMMAND_MAX_ATTEMPTS,
        )
        return {"requeued": int(requeued.split()[-1]), "failed": int(failed.split()[-1])}

    async def recover_expired_leases(self) -> dict[str, int]:
        async with self._require_pool().acquire() as conn:
            async with conn.transaction():
                return await self._recover_expired_with_conn(conn)

    async def list_expired_for_cleanup(self, retention_days: int, limit: int) -> list[dict[str, Any]]:
        async with self._require_pool().acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, storage_key FROM image_jobs
                   WHERE status IN ('COMPLETED', 'FAILED') AND updated_at < NOW() - ($1 * INTERVAL '1 day')
                   ORDER BY updated_at LIMIT $2 FOR UPDATE SKIP LOCKED""",
                retention_days, limit,
            )
        return [dict(row) for row in rows]

    async def delete_completed_job(self, job_id: str) -> bool:
        async with self._require_pool().acquire() as conn:
            result = await conn.execute(
                "DELETE FROM image_jobs WHERE id=$1 AND status IN ('COMPLETED', 'FAILED')", job_id,
            )
        return result.endswith("1")

    async def transition(self, job_id: str, worker_id: str, token: str, status: str) -> dict[str, Any] | None:
        if status not in ("PROCESSING", "UPLOADING"):
            raise ValueError(f"Invalid worker transition: {status}")
        async with self._require_pool().acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE image_jobs SET status=$1, lease_until=NOW() + ($2 * INTERVAL '1 second'), updated_at=NOW()
                   WHERE id=$3 AND worker_id=$4 AND lease_token_hash=$5 AND status IN ('LEASED','PROCESSING','UPLOADING')
                   RETURNING *""",
                status, FLOWCOMMAND_JOB_LEASE_SECONDS, job_id, worker_id, self._hash_token(token),
            )
        return self._row(row)

    async def heartbeat(self, job_id: str, worker_id: str, token: str) -> dict[str, Any] | None:
        return await self.transition(job_id, worker_id, token, "PROCESSING")

    async def complete(self, job_id: str, worker_id: str, token: str, storage_key: str, content_type: str, content_length: int, sha256: str) -> dict[str, Any] | None:
        async with self._require_pool().acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE image_jobs SET status='COMPLETED', storage_key=$1, content_type=$2, content_length=$3,
                       sha256=$4, lease_until=NULL, updated_at=NOW(), completed_at=NOW()
                   WHERE id=$5 AND worker_id=$6 AND lease_token_hash=$7 AND status IN ('LEASED','PROCESSING','UPLOADING')
                   RETURNING *""",
                storage_key, content_type, content_length, sha256, job_id, worker_id, self._hash_token(token),
            )
        return self._row(row)

    async def fail(self, job_id: str, worker_id: str, token: str, error_code: str, error_message: str, retry_after_seconds: int = 0) -> dict[str, Any] | None:
        retry_after_seconds = max(0, retry_after_seconds)
        async with self._require_pool().acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE image_jobs SET status=CASE WHEN attempt_count >= $1 THEN 'FAILED' ELSE 'QUEUED' END,
                       error_code=$2, error_message=$3,
                       next_attempt_at=CASE WHEN attempt_count >= $1 THEN NULL WHEN $7 > 0 THEN NOW() + ($7 * INTERVAL '1 second') ELSE NULL END,
                       worker_id=NULL, lease_token_hash=NULL, lease_until=NULL, updated_at=NOW()
                   WHERE id=$4 AND worker_id=$5 AND lease_token_hash=$6 AND status IN ('LEASED','PROCESSING','UPLOADING')
                   RETURNING *""",
                FLOWCOMMAND_MAX_ATTEMPTS, error_code[:80], error_message[:1000], job_id, worker_id, self._hash_token(token), retry_after_seconds,
            )
        return self._row(row)


remote_job_store = RemoteJobStore()
