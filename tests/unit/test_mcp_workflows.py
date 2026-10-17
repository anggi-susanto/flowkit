"""SDK-registry tests for public-REST workflow tools (no live API/ffmpeg)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytest.importorskip("mcp")

from agent.mcp import server
from agent.mcp.auth import MCPPrincipal
from agent.mcp.client import FlowKitAPIError
from agent.mcp.workflows import register_workflow_tools


@pytest.fixture
def registry(monkeypatch):
    request = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(server.flowkit_client, "_request", request)
    mcp = server.create_mcp()
    # Explicit registration until the parent change wires this into create_mcp.
    register_workflow_tools(mcp)
    token = server._current_principal.set(MCPPrincipal("workflow-test", frozenset({"read", "write"})))
    try:
        yield mcp, request
    finally:
        server._current_principal.reset(token)


async def invoke(registry, name, **arguments):
    result = await registry.call_tool(name, arguments)
    # FastMCP versions return (content, structured_content) or just content.
    if isinstance(result, tuple):
        content, structured = result
        if structured is not None:
            return structured
    else:
        content = result
    return json.loads(content[0].text)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,argument,collection", [
    ("flowkit_get_video", "video_id", "videos"),
    ("flowkit_get_scene", "scene_id", "scenes"),
    ("flowkit_get_character", "character_id", "characters"),
    ("flowkit_list_project_characters", "project_id", "projects"),
])
async def test_resource_reads_use_fixed_public_routes(registry, tool, argument, collection):
    mcp, request = registry
    identifier = str(uuid4())
    if collection == "projects":
        request.return_value = [{"id": str(uuid4()), "name": "Reference"}]
    await invoke(mcp, tool, **{argument: identifier})
    suffix = "/characters" if collection == "projects" else ""
    request.assert_awaited_once_with("GET", f"/api/{collection}/{identifier}{suffix}")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments", [
    ("flowkit_get_video", {"video_id": "../flow"}),
    ("flowkit_get_scene", {"scene_id": "https://example.com"}),
    ("flowkit_get_character", {"character_id": "x?y=z"}),
    ("flowkit_list_project_characters", {"project_id": "not-a-uuid"}),
    ("flowkit_update_video", {"video_id": "../video", "updates": {}, "dry_run": True}),
    ("flowkit_concat_video", {"video_id": "bad", "dry_run": True}),
    ("flowkit_finalize_video", {"video_id": "bad", "dry_run": True}),
    ("flowkit_refresh_project_urls", {"project_id": "bad", "dry_run": True}),
    ("flowkit_get_batch_status", {"video_id": "bad"}),
    ("flowkit_get_batch_status", {"project_id": "bad"}),
])
async def test_invalid_uuid_never_reaches_backend(registry, tool, arguments):
    mcp, request = registry
    with pytest.raises(Exception, match="UUID"):
        await invoke(mcp, tool, **arguments)
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["concat", "finalize"])
async def test_finishing_posts_backend_contract_and_returns_actual_result(registry, operation):
    mcp, request = registry
    identifier = str(uuid4())
    payload = {"video_id": identifier, "url": "file:///outputs/final.mp4", "scenes_missing": [str(uuid4())]}
    request.return_value = payload
    result = await invoke(mcp, f"flowkit_{operation}_video", video_id=identifier,
                          orientation="HORIZONTAL", target_duration_s=12.5)
    assert result == payload
    request.assert_awaited_once_with("POST", f"/api/videos/{identifier}/{operation}",
                                     json={"orientation": "HORIZONTAL", "target_duration_s": 12.5})


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["concat", "finalize"])
async def test_finishing_defaults_send_empty_backend_body(registry, operation):
    mcp, request = registry
    identifier = str(uuid4())
    await invoke(mcp, f"flowkit_{operation}_video", video_id=identifier)
    request.assert_awaited_once_with("POST", f"/api/videos/{identifier}/{operation}", json={})


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments", [
    ("flowkit_update_video", {"updates": {"title": "Ready", "status": "DRAFT"}}),
    ("flowkit_concat_video", {"orientation": "VERTICAL", "target_duration_s": 8}),
    ("flowkit_finalize_video", {}),
    ("flowkit_refresh_project_urls", {}),
])
async def test_mutation_dry_runs_do_not_make_even_read_requests(registry, tool, arguments):
    mcp, request = registry
    id_key = "project_id" if "refresh" in tool else "video_id"
    identifier = str(uuid4())
    result = await invoke(mcp, tool, **{id_key: identifier}, **arguments, dry_run=True)
    assert result["dry_run"] is True
    assert result[id_key] == identifier
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments", [
    ("flowkit_update_video", {"updates": {}}),
    ("flowkit_concat_video", {}),
    ("flowkit_finalize_video", {}),
    ("flowkit_refresh_project_urls", {}),
])
@pytest.mark.parametrize("dry_run", [True, False])
async def test_write_required_even_for_dry_run(registry, tool, arguments, dry_run):
    mcp, request = registry
    token = server._current_principal.set(MCPPrincipal("reader", frozenset({"read"})))
    try:
        id_key = "project_id" if "refresh" in tool else "video_id"
        with pytest.raises(Exception, match="write"):
            await invoke(mcp, tool, **{id_key: str(uuid4())}, **arguments, dry_run=dry_run)
    finally:
        server._current_principal.reset(token)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_scope_required_and_unauthenticated_denied(registry):
    mcp, request = registry
    for principal, message in [(None, "authenticated"), (MCPPrincipal("writer", frozenset({"write"})), "read")]:
        token = server._current_principal.set(principal)
        try:
            with pytest.raises(Exception, match=message):
                await invoke(mcp, "flowkit_get_batch_status")
        finally:
            server._current_principal.reset(token)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_video_uses_metadata_allowlist_and_patch(registry):
    mcp, request = registry
    identifier = str(uuid4())
    updates = {"title": "Final cut", "description": "A film", "display_order": 3,
               "status": "COMPLETED", "orientation": "VERTICAL", "target_duration_s": 15,
               "tags": "film,short", "privacy": "unlisted"}
    await invoke(mcp, "flowkit_update_video", video_id=identifier, updates=updates)
    request.assert_awaited_once_with("PATCH", f"/api/videos/{identifier}", json=updates)


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"vertical_url": "http://127.0.0.1/private"}, {"horizontal_url": "file:///etc/passwd"},
    {"thumbnail_url": "https://example.com"}, {"project_id": str(uuid4())},
    {"output_filename": "../../bad.mp4"}, {"music_path": "/etc/passwd"},
    {"status": "NOT_REAL"}, {"orientation": "DIAGONAL"},
    {"target_duration_s": -1}, {"target_duration_s": float("inf")},
])
async def test_update_rejects_unknown_asset_and_invalid_metadata(registry, updates):
    mcp, request = registry
    with pytest.raises(Exception):
        await invoke(mcp, "flowkit_update_video", video_id=str(uuid4()), updates=updates, dry_run=True)
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["concat", "finalize"])
@pytest.mark.parametrize("options", [
    {"target_duration_s": 0}, {"target_duration_s": -5},
    {"target_duration_s": float("inf")}, {"target_duration_s": float("nan")},
    {"orientation": "diagonal"},
])
async def test_finishing_rejects_invalid_options_including_dry_run(registry, operation, options):
    mcp, request = registry
    with pytest.raises(Exception):
        await invoke(mcp, f"flowkit_{operation}_video", video_id=str(uuid4()), dry_run=True, **options)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_uses_bodyless_endpoint(registry):
    mcp, request = registry
    identifier = str(uuid4())
    request.return_value = {"refreshed": 0, "skipped": "all local"}
    assert await invoke(mcp, "flowkit_refresh_project_urls", project_id=identifier) == request.return_value
    request.assert_awaited_once_with("POST", f"/api/flow/refresh-urls/{identifier}")


@pytest.mark.asyncio
async def test_batch_status_exact_filters_and_empty_batch_passthrough(registry):
    mcp, request = registry
    video_id, project_id = str(uuid4()), str(uuid4())
    request.return_value = {"total": 0, "done": True, "all_succeeded": False}
    result = await invoke(mcp, "flowkit_get_batch_status", video_id=video_id,
                          project_id=project_id, type="GENERATE_VIDEO", orientation="VERTICAL")
    assert result == request.return_value
    request.assert_awaited_once_with("GET", "/api/requests/batch-status", params={
        "video_id": video_id, "project_id": project_id, "type": "GENERATE_VIDEO", "orientation": "VERTICAL"})
    request.reset_mock()
    await invoke(mcp, "flowkit_get_batch_status")
    request.assert_awaited_once_with("GET", "/api/requests/batch-status", params=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"type": "WORKER_CLAIM"}, {"orientation": "diagonal"}])
async def test_batch_status_validates_filter_enums(registry, options):
    mcp, request = registry
    with pytest.raises(Exception):
        await invoke(mcp, "flowkit_get_batch_status", **options)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_exposes_only_typed_safe_finishing_inputs(registry):
    mcp, _ = registry
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert set(tools["flowkit_concat_video"].inputSchema["properties"]) == {
        "video_id", "orientation", "target_duration_s", "dry_run"}
    assert set(tools["flowkit_finalize_video"].inputSchema["properties"]) == {
        "video_id", "orientation", "target_duration_s", "dry_run"}
    assert set(tools["flowkit_get_batch_status"].inputSchema["properties"]) == {
        "video_id", "project_id", "type", "orientation"}


@pytest.mark.asyncio
async def test_upstream_error_is_translated_by_shared_tool_wrapper(registry):
    mcp, request = registry
    request.side_effect = FlowKitAPIError("FlowKit request returned HTTP 503")
    with pytest.raises(Exception, match="FlowKit request returned HTTP 503"):
        await invoke(mcp, "flowkit_concat_video", video_id=str(uuid4()))
