"""Public Markdown documentation is a single fixed, credential-free asset."""
import httpx
import pytest

from agent.main import app


@pytest.mark.asyncio
async def test_public_markdown_guide():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get("/mcp-guide.md")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "# FlowCommand" in response.text
        assert "https://flowcommand.gunamaya.id/mcp/" in response.text
        assert "FLOWKIT_MCP_TOKENS=" not in response.text
        assert "10.0.0.23" not in response.text
        assert (await client.get("/FLOWKIT_MCP_RUNBOOK.md")).status_code == 404
