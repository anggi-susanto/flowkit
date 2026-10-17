"""Read-only FlowKit MCP surface tests."""
from __future__ import annotations

import pytest

from agent.mcp import auth
from agent.mcp.auth import READ_SCOPE, authenticate_bearer, configured_tokens, has_scope
from agent.mcp.client import FlowKitAPIError, FlowKitClient


def test_token_parsing_uses_plus_separated_scopes():
    tokens = configured_tokens("reader:read,operator:read+write+generate")
    assert len(tokens) == 2
    reader = authenticate_bearer("Bearer reader", tokens)
    operator = authenticate_bearer("Bearer operator", tokens)
    assert reader is not None and reader.scopes == frozenset({READ_SCOPE})
    assert operator is not None and has_scope(operator, "generate")


def test_bearer_auth_rejects_missing_and_invalid_values():
    tokens = configured_tokens("reader:read")
    assert authenticate_bearer(None, tokens) is None
    assert authenticate_bearer("Bearer wrong", tokens) is None
    assert authenticate_bearer("Basic reader", tokens) is None


def test_read_scope_cannot_be_escalated_without_matching_token():
    tokens = configured_tokens("reader:read")
    reader = authenticate_bearer("Bearer reader", tokens)
    assert reader is not None
    assert has_scope(reader, READ_SCOPE)
    assert not has_scope(reader, "generate")


@pytest.mark.asyncio
async def test_client_reports_credit_limitation_explicitly(monkeypatch):
    client = FlowKitClient(base_url="http://flowkit.test")

    async def fake_request(method, path, **kwargs):
        assert (method, path) == ("GET", "/api/flow/credits")
        return {"status": 200, "data": {"userPaygateTier": "PAYGATE_TIER_ONE"}}

    monkeypatch.setattr(client, "_request", fake_request)
    result = await client.credits_status()
    assert result["live_balance_available"] is False
    assert result["tier"] == "PAYGATE_TIER_ONE"
    assert result["source"] == "configured_default"


@pytest.mark.asyncio
async def test_client_surfaces_upstream_errors(monkeypatch):
    client = FlowKitClient(base_url="http://flowkit.test")

    async def fake_request(method, path, **kwargs):
        raise FlowKitAPIError("FlowKit GET /api/projects returned 503: unavailable")

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(FlowKitAPIError):
        await client.projects()


def test_authenticated_asgi_app_rejects_unknown_token(monkeypatch):
    from agent.mcp.server import AuthenticatedMCPApp

    calls = []

    async def downstream(scope, receive, send):
        calls.append(scope)

    async def receive():
        return {"type": "http.request"}

    sent = []

    async def send(message):
        sent.append(message)

    app = AuthenticatedMCPApp(downstream, tokens=configured_tokens("reader:read"))
    import asyncio
    asyncio.run(app({"type": "http", "headers": []}, receive, send))
    assert calls == []
    assert sent[0]["status"] == 401


def test_authenticated_asgi_app_passes_valid_token(monkeypatch):
    from agent.mcp.server import AuthenticatedMCPApp

    calls = []

    async def downstream(scope, receive, send):
        calls.append(scope)

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        pass

    app = AuthenticatedMCPApp(downstream, tokens=configured_tokens("reader:read"))
    import asyncio
    asyncio.run(app({"type": "http", "headers": [(b"authorization", b"Bearer reader")]}, receive, send))
    assert len(calls) == 1
