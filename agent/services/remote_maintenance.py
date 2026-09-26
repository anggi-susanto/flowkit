"""Background maintenance for durable FlowCommand jobs."""
from __future__ import annotations

import asyncio
import logging

from agent.config import (
    FLOWCOMMAND_CLEANUP_BATCH_SIZE,
    FLOWCOMMAND_CLEANUP_INTERVAL_SECONDS,
    FLOWCOMMAND_RECOVERY_INTERVAL_SECONDS,
    FLOWCOMMAND_RETENTION_DAYS,
)
from agent.services.object_storage import object_storage
from agent.services.remote_jobs import remote_job_store
from agent.services.remote_observability import increment

logger = logging.getLogger(__name__)


async def cleanup_expired_jobs() -> int:
    jobs = await remote_job_store.list_expired_for_cleanup(FLOWCOMMAND_RETENTION_DAYS, FLOWCOMMAND_CLEANUP_BATCH_SIZE)
    deleted = 0
    for job in jobs:
        storage_key = job.get("storage_key")
        if storage_key:
            try:
                await object_storage.delete(storage_key)
            except Exception:
                logger.exception("Unable to delete expired object for job %s", job["id"])
                continue
        if await remote_job_store.delete_completed_job(str(job["id"])):
            deleted += 1
    return deleted


async def run_maintenance(stop: asyncio.Event) -> None:
    next_cleanup = 0.0
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        try:
            recovered = await remote_job_store.recover_expired_leases()
            increment("leases_requeued_total", recovered["requeued"])
            increment("leases_failed_total", recovered["failed"])
            if recovered["requeued"] or recovered["failed"]:
                logger.warning("Recovered FlowCommand leases: requeued=%d failed=%d", recovered["requeued"], recovered["failed"])
            if loop.time() >= next_cleanup:
                deleted = await cleanup_expired_jobs()
                increment("jobs_retention_deleted_total", deleted)
                if deleted:
                    logger.info("Cleaned up %d expired FlowCommand jobs", deleted)
                next_cleanup = loop.time() + FLOWCOMMAND_CLEANUP_INTERVAL_SECONDS
        except Exception:
            logger.exception("FlowCommand maintenance cycle failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=FLOWCOMMAND_RECOVERY_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
