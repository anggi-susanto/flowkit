"""Redacted structured audit logging for MCP tool invocations."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger("agent.mcp.audit")


@contextmanager
def audit_tool_call(tool: str, subject: str) -> Iterator[None]:
    """Log tool outcome and duration without prompts, IDs, or credentials."""
    started = time.monotonic()
    try:
        yield
    except Exception:
        logger.warning(
            "mcp_tool tool=%s subject=%s outcome=error duration_ms=%d",
            tool, subject, int((time.monotonic() - started) * 1000),
        )
        raise
    else:
        logger.info(
            "mcp_tool tool=%s subject=%s outcome=ok duration_ms=%d",
            tool, subject, int((time.monotonic() - started) * 1000),
        )
