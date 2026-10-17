"""Security and transport regression tests for the MCP adapter."""
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from agent.mcp.auth import MCPPrincipal, authenticate_bearer, configured_tokens
from agent.mcp.client import FlowKitClient
from agent.mcp.server import AuthenticatedMCPApp, _current_principal, create_mcp


def test_token_identity_survives_reordering_and_explicit_rotation():
    first = configured_tokens("alice=old:read,bob=other:read")
    second = configured_tokens("bob=other:read,alice=new:read")
    assert authenticate_bearer("Bearer old", first).subject == authenticate_bearer("Bearer new", second).subject
    assert authenticate_bearer("Bearer plain", configured_tokens("plain:read")).subject == authenticate_bearer("Bearer plain", configured_tokens("other:read,plain:read")).subject


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", ["../models", "%2e%2e/models", "x?foo=bar", "x#frag", "x/y"])
async def test_project_update_rejects_path_injection(bad_id):
    with pytest.raises(ValueError):
        await FlowKitClient().update_project(bad_id, {})


@pytest.mark.asyncio
async def test_composite_generation_requires_generate_before_any_mutation(monkeypatch):
    from agent.mcp import server
    async def forbidden(*args, **kwargs):
        pytest.fail("mutation occurred before scope validation")
    monkeypatch.setattr(server.flowkit_client, "create_character", forbidden)
    token = _current_principal.set(MCPPrincipal("writer", frozenset({"write"})))
    try:
        result = await create_mcp().call_tool("flowkit_build_character", {"project_id": str(uuid4()), "name": "Test"})
        # FastMCP raises ToolError for execution failures on supported SDK versions.
        pytest.fail(f"unexpected tool success: {result}")
    except Exception as exc:
        assert "generate" in str(exc)
    finally:
        _current_principal.reset(token)


@pytest.mark.asyncio
async def test_credit_adapter_handles_unwrapped_rest_payload(monkeypatch):
    client = FlowKitClient()
    async def request(*args, **kwargs):
        return {"userPaygateTier": "PAYGATE_TIER_TWO"}
    monkeypatch.setattr(client, "_request", request)
    assert (await client.credits_status())["tier"] == "PAYGATE_TIER_TWO"


@pytest.mark.asyncio
async def test_http_mount_initialize_auth_origin_and_scope(monkeypatch):
    from agent.mcp import server
    mcp = create_mcp()
    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield
    app = FastAPI(lifespan=lifespan)
    app.mount("/mcp", AuthenticatedMCPApp(mcp.streamable_http_app(), configured_tokens("reader:read,writer:write")))
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer reader", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            assert (await client.post("/mcp/", json=initialize)).status_code == 401
            response = await client.post("/mcp/", headers=headers, json=initialize)
            assert response.status_code == 200, response.text
            assert "FlowKit" in response.text
            assert "mcp-session-id" not in response.headers
            rejected = await client.post("/mcp/", headers={**headers, "Origin": "https://evil.example"}, json=initialize)
            assert rejected.status_code == 403
            rejected_host = await client.post("/mcp/", headers={**headers, "Host": "evil.example"}, json=initialize)
            assert rejected_host.status_code == 421
            discovery = await client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert "flowkit_generate_scene_image" in discovery.text
            denied = await client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "flowkit_create_project", "arguments": {"name": "No mutation", "dry_run": True}}})
            assert '"isError":true' in denied.text
            assert "write" in denied.text
            assert server._current_principal.get() is None


@pytest.mark.asyncio
async def test_review_adapter_uses_query_contract(monkeypatch):
    client = FlowKitClient()
    video_id, project_id = str(uuid4()), str(uuid4())
    calls = []
    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"project_id": project_id} if method == "GET" else {"score": 8}
    monkeypatch.setattr(client, "_request", request)
    assert await client.review_video(video_id, "deep") == {"score": 8}
    assert calls[-1][2] == {"params": {"project_id": project_id, "mode": "deep"}}


@pytest.mark.asyncio
async def test_remote_idempotency_keys_are_principal_scoped(monkeypatch):
    from agent.mcp import server
    bodies = []
    async def submit(body, username, password):
        bodies.append(body)
        return {"job_id": str(uuid4()), "status": "QUEUED"}
    async def record(job_id, subject):
        pass
    monkeypatch.setattr(server, "_flowcommand_credentials", lambda: ("test", "test"))
    monkeypatch.setattr(server.flowkit_client, "remote_image_job", submit)
    monkeypatch.setattr(server.mcp_remote_job_registry, "record", record)
    mcp = create_mcp()
    for subject in ["alice", "bob", "alice"]:
        token = _current_principal.set(MCPPrincipal(subject, frozenset({"remote_jobs"})))
        try:
            await mcp.call_tool("flowkit_submit_remote_image_job", {"prompt": "test", "idempotency_key": "same"})
        finally:
            _current_principal.reset(token)
    assert bodies[0]["idempotency_key"] != bodies[1]["idempotency_key"]
    assert bodies[0]["idempotency_key"] == bodies[2]["idempotency_key"]


@pytest.mark.asyncio
async def test_dry_run_validates_without_mutation(monkeypatch):
    from agent.mcp import server
    async def forbidden(*args, **kwargs):
        pytest.fail("dry run mutated backend")
    monkeypatch.setattr(server.flowkit_client, "create_project", forbidden)
    mcp = create_mcp()
    token = _current_principal.set(MCPPrincipal("writer", frozenset({"write"})))
    try:
        result = await mcp.call_tool("flowkit_create_project", {"name": "Preview", "dry_run": True})
        assert result
        with pytest.raises(Exception, match="video_model_family"):
            await mcp.call_tool("flowkit_create_project", {"name": "Invalid", "video_model_family": "unknown", "dry_run": True})
        with pytest.raises(Exception, match="Unsupported fields"):
            await mcp.call_tool("flowkit_update_scene", {"scene_id": str(uuid4()), "updates": {"vertical_video_status": "COMPLETED"}, "dry_run": True})
    finally:
        _current_principal.reset(token)


@pytest.mark.asyncio
async def test_gateway_body_and_rate_limits(monkeypatch):
    monkeypatch.setenv("FLOWKIT_MCP_MAX_BODY_BYTES", "1024")
    monkeypatch.setenv("FLOWKIT_MCP_REQUESTS_PER_MINUTE", "2")
    calls = []
    async def downstream(scope, receive, send):
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    app = AuthenticatedMCPApp(downstream, configured_tokens("reader:read"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        headers = {"Authorization": "Bearer reader"}
        assert (await client.post("/", headers=headers, content=b"x" * 1025)).status_code == 413
        assert (await client.post("/", headers=headers, content=b"{}" )).status_code == 204
        assert (await client.post("/", headers=headers, content=b"{}" )).status_code == 429
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_auth_context_fails_closed():
    token = _current_principal.set(None)
    try:
        with pytest.raises(Exception, match="not authenticated"):
            await create_mcp().call_tool("flowkit_list_models", {})
    finally:
        _current_principal.reset(token)


@pytest.mark.asyncio
async def test_generation_rejects_missing_reference_uuid(monkeypatch):
    from agent.mcp import server
    scene_id, project_id = str(uuid4()), str(uuid4())
    async def request(*args, **kwargs):
        return {"character_names": ["Hero"]}
    async def characters(project_id):
        return [{"name": "Hero", "media_id": "CAMS-invalid"}]
    monkeypatch.setattr(server.flowkit_client, "_request", request)
    monkeypatch.setattr(server.flowkit_client, "project_characters", characters)
    with pytest.raises(ValueError, match="UUID"):
        await server._generation_dependencies(scene_id, project_id, "VERTICAL", "image")
