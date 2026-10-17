"""Music MCP contract tests: mocked HTTP only, never a Suno provider call."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("mcp")

from agent.api.music import (
    ConvertToWavRequest, ExtendRequest, GenerateLyricsRequest,
    GenerateRequest, VocalRemovalRequest,
)
from agent.mcp import server
from agent.mcp.auth import MCPPrincipal
from agent.mcp.client import FlowKitAPIError
from agent.mcp.music import register_music_tools


@pytest.fixture
async def registry(monkeypatch):
    request = AsyncMock(return_value={"task_id": "suno_task-1"})
    monkeypatch.setattr(server.flowkit_client, "_request", request)
    mcp = server.create_mcp()
    if "flowkit_generate_music" not in {tool.name for tool in await mcp.list_tools()}:
        register_music_tools(mcp)
    token = server._current_principal.set(MCPPrincipal("music-test", frozenset({"read", "generate"})))
    try:
        yield mcp, request
    finally:
        server._current_principal.reset(token)


async def invoke(mcp, name, **arguments):
    result = await mcp.call_tool(name, arguments)
    if isinstance(result, tuple):
        content, structured = result
        if structured is not None:
            return structured
    else:
        content = result
    return json.loads(content[0].text)


CASES = [
    ("flowkit_generate_music", {"prompt": "[Verse] Hello", "style": "jazz", "title": "Song", "instrumental": False, "model": "V5", "custom_mode": True, "template_id": "jazz_1"}, GenerateRequest, "/api/music/generate"),
    ("flowkit_generate_lyrics", {"prompt": "An adventure", "template_id": "adventure-1"}, GenerateLyricsRequest, "/api/music/generate-lyrics"),
    ("flowkit_extend_music", {"audio_id": "audio_1", "prompt": "Continue", "continue_at": 12.5, "model": "V5", "default_param_flag": False}, ExtendRequest, "/api/music/extend"),
    ("flowkit_vocal_removal", {"task_id": "task_1", "audio_id": "clip-1"}, VocalRemovalRequest, "/api/music/vocal-removal"),
    ("flowkit_convert_to_wav", {"task_id": "task_1", "audio_id": "clip-1"}, ConvertToWavRequest, "/api/music/convert-to-wav"),
]
READS = [
    ("flowkit_list_music_templates", {}, "/api/music/templates"),
    ("flowkit_get_music_template", {"template_id": "children_adventure"}, "/api/music/templates/children_adventure"),
    ("flowkit_get_music_task", {"task_id": "suno-task_1"}, "/api/music/tasks/suno-task_1"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,model,path", CASES)
@pytest.mark.parametrize("dry_run", [True, False])
async def test_submit_contract(registry, tool, arguments, model, path, dry_run):
    mcp, request = registry
    expected = model.model_validate({**arguments, "poll": False}).model_dump(exclude_unset=True, exclude_none=True)
    result = await invoke(mcp, tool, **arguments, dry_run=dry_run)
    if dry_run:
        assert result == {"dry_run": True, "operation": tool.removeprefix("flowkit_"), "body": expected}
        request.assert_not_awaited()
    else:
        assert result == request.return_value
        request.assert_awaited_once_with("POST", path, json=expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,path", READS)
async def test_read_routes(registry, tool, arguments, path):
    mcp, request = registry
    request.return_value = {"status": "SUCCESS", "clips": []}
    assert await invoke(mcp, tool, **arguments) == request.return_value
    request.assert_awaited_once_with("GET", path)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,model,path", CASES)
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("scopes", [None, set(), {"read"}, {"write"}])
async def test_generation_scope_first(registry, tool, arguments, model, path, dry_run, scopes):
    mcp, request = registry
    # Invalid identifiers must not mask an auth failure.
    arguments = {k: ("../bad" if k.endswith("_id") else v) for k, v in arguments.items()}
    principal = None if scopes is None else MCPPrincipal("restricted", frozenset(scopes))
    token = server._current_principal.set(principal)
    try:
        with pytest.raises(Exception, match="authenticated" if scopes is None else "generate"):
            await invoke(mcp, tool, **arguments, dry_run=dry_run)
    finally:
        server._current_principal.reset(token)
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,path", READS)
@pytest.mark.parametrize("scopes", [None, {"generate"}, {"write"}, set()])
async def test_read_scope_first(registry, tool, arguments, path, scopes):
    mcp, request = registry
    token = server._current_principal.set(None if scopes is None else MCPPrincipal("restricted", frozenset(scopes)))
    try:
        with pytest.raises(Exception, match="authenticated" if scopes is None else "read"):
            await invoke(mcp, tool, **{k: "../bad" for k in arguments})
    finally:
        server._current_principal.reset(token)
    request.assert_not_awaited()


ID_FIELDS = [
    ("flowkit_get_music_template", {}, "template_id"),
    ("flowkit_get_music_task", {}, "task_id"),
    ("flowkit_generate_music", {"prompt": "hello"}, "template_id"),
    ("flowkit_generate_lyrics", {"prompt": "hello"}, "template_id"),
    ("flowkit_extend_music", {}, "audio_id"),
    ("flowkit_vocal_removal", {"audio_id": "clip1"}, "task_id"),
    ("flowkit_vocal_removal", {"task_id": "task1"}, "audio_id"),
    ("flowkit_convert_to_wav", {"audio_id": "clip1"}, "task_id"),
    ("flowkit_convert_to_wav", {"task_id": "task1"}, "audio_id"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,field", ID_FIELDS)
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("bad", ["", "../secret", "/tmp/audio.wav", "https://example.com", "a/b", "a\\b", "a?poll=true", "a#fragment", "a%2fb", "é", "a\n", "x" * 129])
async def test_unsafe_ids_never_reach_http(registry, tool, arguments, field, bad, dry_run):
    mcp, request = registry
    options = {} if tool.startswith("flowkit_get_") else {"dry_run": dry_run}
    with pytest.raises(Exception):
        await invoke(mcp, tool, **arguments, **{field: bad}, **options)
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["a", "A9_-", "x" * 128])
async def test_safe_opaque_ids_not_uuid_only(registry, identifier):
    mcp, request = registry
    await invoke(mcp, "flowkit_get_music_task", task_id=identifier)
    request.assert_awaited_once_with("GET", f"/api/music/tasks/{identifier}")


@pytest.mark.asyncio
async def test_defaults_and_template_only_generation(registry):
    mcp, request = registry
    result = await invoke(mcp, "flowkit_generate_music", template_id="children_adventure", dry_run=True)
    assert result["body"] == GenerateRequest(template_id="children_adventure").model_dump(exclude_none=True)
    request.assert_not_awaited()
    with pytest.raises(Exception, match="Provide prompt"):
        await invoke(mcp, "flowkit_generate_music", dry_run=True)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_models_validate_inside_scope_wrapper(registry, monkeypatch):
    mcp, request = registry
    # Test model validation itself, independent of FastMCP argument coercion.
    def reject(cls, value, **kwargs):
        raise ValueError("real REST model validation")
    monkeypatch.setattr(GenerateRequest, "model_validate", classmethod(reject))
    with pytest.raises(Exception, match="real REST model validation"):
        await invoke(mcp, "flowkit_generate_music", prompt="hello", dry_run=True)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_only_safe_typed_arguments(registry):
    mcp, _ = registry
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name, arguments, model, _ in CASES:
        fields = set(tools[name].inputSchema["properties"])
        assert fields == (set(model.model_fields) - {"poll"}) | {"dry_run"}
        assert not fields & {"poll", "url", "audio_url", "path", "output_path", "body", "endpoint"}
    for name, arguments, _ in READS:
        assert set(tools[name].inputSchema["properties"]) == set(arguments)
    assert not any("music" in name and ("download" in name or "poll" in name) for name in tools)


@pytest.mark.asyncio
async def test_safe_error_translation(registry):
    mcp, request = registry
    request.side_effect = FlowKitAPIError("FlowKit request returned HTTP 503")
    with pytest.raises(Exception, match="FlowKit request returned HTTP 503"):
        await invoke(mcp, "flowkit_generate_music", prompt="Hello")
