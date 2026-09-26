from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter
from contextvars import ContextVar

from fastapi import Request

request_id_var: ContextVar[str] = ContextVar("flowcommand_request_id", default="-")
_metrics: Counter[str] = Counter()
logger = logging.getLogger("flowcommand.http")


def new_request_id(value: str | None = None) -> str:
    return value.strip()[:128] if value and value.strip() else str(uuid.uuid4())


def increment(name: str, value: int = 1) -> None:
    _metrics[name] += value


def snapshot_metrics() -> dict[str, int]:
    return dict(_metrics)


def prometheus_metrics() -> str:
    lines = ["# HELP flowcommand_http_requests_total HTTP requests handled by FlowCommand.", "# TYPE flowcommand_http_requests_total counter"]
    for name, value in sorted(_metrics.items()):
        safe = name.replace("-", "_")
        lines.append(f'flowcommand_{safe} {value}')
    return "\n".join(lines) + "\n"


async def request_context_middleware(request: Request, call_next):
    request_id = new_request_id(request.headers.get("X-Request-ID"))
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        increment("http_requests_total")
        if request.url.path.startswith("/api/v1/"):
            increment(f"http_status_{response.status_code}_total")
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if request.url.path.startswith("/api/v1/"):
            logger.info(json.dumps({
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": getattr(locals().get("response"), "status_code", 500),
                "duration_ms": duration_ms,
                "client": request.client.host if request.client else None,
            }, separators=(",", ":")))
        request_id_var.reset(token)
