"""Ownership boundary tests for MCP-managed FlowCommand jobs."""
from __future__ import annotations

import pytest

from agent.mcp.remote_jobs import MCPRemoteJobRegistry


@pytest.mark.asyncio
async def test_remote_job_registry_is_scoped_to_mcp_subject(tmp_path, monkeypatch):
    registry = MCPRemoteJobRegistry()
    monkeypatch.setattr("agent.mcp.remote_jobs.DB_PATH", tmp_path / "mcp.sqlite")
    await registry.record("job-a", "token-a")
    assert await registry.owns("job-a", "token-a")
    assert not await registry.owns("job-a", "token-b")
    assert not await registry.owns("job-b", "token-a")
    await registry.record("job-a", "token-a")
    with pytest.raises(PermissionError):
        await registry.record("job-a", "token-b")
    assert await registry.owns("job-a", "token-a")
