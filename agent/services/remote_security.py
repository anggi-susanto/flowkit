"""Security helpers scoped to the FlowCommand public API."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from agent.config import (
    FLOWCOMMAND_CLIENT_RATE_LIMIT,
    FLOWCOMMAND_RATE_LIMIT_WINDOW_SECONDS,
    FLOWCOMMAND_WORKER_RATE_LIMIT,
)


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str, limit: int) -> None:
        now = time.monotonic()
        threshold = now - FLOWCOMMAND_RATE_LIMIT_WINDOW_SECONDS
        async with self._lock:
            events = self._events[key]
            while events and events[0] <= threshold:
                events.popleft()
            if len(events) >= limit:
                raise HTTPException(status_code=429, detail="Rate limit exceeded", headers={"Retry-After": str(FLOWCOMMAND_RATE_LIMIT_WINDOW_SECONDS)})
            events.append(now)


rate_limiter = SlidingWindowRateLimiter()


def client_limit_key(request: Request, client_id: str) -> str:
    return f"client:{client_id}:{request.client.host if request.client else 'unknown'}"


async def limit_client(request: Request, client_id: str) -> None:
    await rate_limiter.check(client_limit_key(request, client_id), FLOWCOMMAND_CLIENT_RATE_LIMIT)


async def limit_worker(request: Request) -> None:
    worker_id = request.headers.get("x-worker-id", "unknown")[:120]
    await rate_limiter.check(f"worker:{worker_id}", FLOWCOMMAND_WORKER_RATE_LIMIT)
