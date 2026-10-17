"""Audio MCP registry tests with mocked HTTP; no TTS engine or workers."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytest.importorskip("mcp")

from agent.mcp import server
from agent.mcp.audio import register_audio_tools
from agent.mcp.auth import MCPPrincipal
from agent.mcp.client import FlowKitAPIError
from agent.models.tts import NarrateVideoRequest, TTSGenerateRequest, VoiceTemplateRequest


@pytest.fixture
async def registry(monkeypatch):
    request = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(server.flowkit_client, "_request", request)
    mcp = server.create_mcp()
    # Parent integration may already register these tools; never register twice.
    if "flowkit_generate_tts" not in {tool.name for tool in await mcp.list_tools()}:
        register_audio_tools(mcp)
    token = server._current_principal.set(MCPPrincipal("audio-test", frozenset({"read", "generate"})))
    try:
        yield mcp, request
    finally:
        server._current_principal.reset(token)


async def invoke(mcp, tool_name, **arguments):
    result = await mcp.call_tool(tool_name, arguments)
    if isinstance(result, tuple):
        content, structured = result
        if structured is not None:
            return structured
    else:
        content = result
    return json.loads(content[0].text)


@pytest.mark.asyncio
async def test_list_uses_fixed_read_endpoint(registry):
    mcp, request = registry
    request.return_value = [{"name": "narrator", "audio_path": "/stored/narrator.wav", "duration": 2.0}]
    result = await invoke(mcp, "flowkit_list_voice_templates")
    # SDK structured output wraps list returns under result.
    assert result == {"result": request.return_value} or result == request.return_value
    request.assert_awaited_once_with("GET", "/api/tts/templates")


CASES = [
    ("flowkit_create_voice_template", {"text": "Sample", "instruct": "Warm", "name": "voice-1", "speed": 1.2}, VoiceTemplateRequest, "/api/tts/templates"),
    ("flowkit_generate_tts", {"text": "Hello", "instruct": "Calm", "speed": 0.8}, TTSGenerateRequest, "/api/tts/generate"),
    ("flowkit_narrate_video", {"video_id": str(uuid4()), "project_id": str(uuid4()), "orientation": "VERTICAL", "template": "voice_1", "speed": 1.2, "instruct": "Calm", "mix": False, "sfx_volume": 0.7, "from_scene": 0, "to_scene": 3}, NarrateVideoRequest, None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,model,path", CASES)
@pytest.mark.parametrize("dry_run", [True, False])
async def test_generation_contract_and_no_network_dry_run(registry, tool, arguments, model, path, dry_run):
    mcp, request = registry
    options = {k: v for k, v in arguments.items() if k != "video_id"}
    body = model.model_validate(options).model_dump(exclude_unset=True, exclude_none=True)
    # Narration includes the explicit adapter defaults, including mix/sfx.
    if model is NarrateVideoRequest:
        path = f"/api/videos/{arguments['video_id']}/narrate"
    request.return_value = {"audio_path": "/stored/generated.wav", "duration": 2.5, "scenes_failed": 1}
    result = await invoke(mcp, tool, **arguments, dry_run=dry_run)
    if dry_run:
        assert result["dry_run"] is True
        assert result["body"] == body
        assert result["operation"] == tool.removeprefix("flowkit_")
        request.assert_not_awaited()
    else:
        assert result == request.return_value
        request.assert_awaited_once_with("POST", path, json=body)


@pytest.mark.asyncio
async def test_narration_defaults_and_template_omission(registry):
    mcp, request = registry
    video, project = str(uuid4()), str(uuid4())
    await invoke(mcp, "flowkit_narrate_video", video_id=video, project_id=project)
    request.assert_awaited_once_with("POST", f"/api/videos/{video}/narrate", json={
        "project_id": project, "orientation": "HORIZONTAL", "speed": 1.0,
        "mix": True, "sfx_volume": 0.4,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,model,path", CASES)
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("scopes", [{"read"}, {"write"}, set()])
async def test_generate_scope_required_even_for_dry_run(registry, tool, arguments, model, path, dry_run, scopes):
    mcp, request = registry
    token = server._current_principal.set(MCPPrincipal("restricted", frozenset(scopes)))
    try:
        with pytest.raises(Exception, match="generate"):
            await invoke(mcp, tool, **arguments, dry_run=dry_run)
    finally:
        server._current_principal.reset(token)
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("principal,match", [(None, "authenticated"), (MCPPrincipal("generator", frozenset({"generate"})), "read")])
async def test_list_requires_auth_and_read(registry, principal, match):
    mcp, request = registry
    token = server._current_principal.set(principal)
    try:
        with pytest.raises(Exception, match=match):
            await invoke(mcp, "flowkit_list_voice_templates")
    finally:
        server._current_principal.reset(token)
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,model,path", CASES)
async def test_unauthenticated_generation_denied(registry, tool, arguments, model, path):
    mcp, request = registry
    token = server._current_principal.set(None)
    try:
        with pytest.raises(Exception, match="authenticated"):
            await invoke(mcp, tool, **arguments, dry_run=True)
    finally:
        server._current_principal.reset(token)
    request.assert_not_awaited()


INVALID = [
    ("flowkit_generate_tts", {"text": ""}),
    ("flowkit_generate_tts", {"text": "x" * 5001}),
    ("flowkit_generate_tts", {"text": "x", "instruct": "x" * 201}),
    ("flowkit_generate_tts", {"text": "x", "speed": 0.4}),
    ("flowkit_generate_tts", {"text": "x", "speed": 3.1}),
    ("flowkit_generate_tts", {"text": "x", "speed": float("nan")}),
    ("flowkit_generate_tts", {"text": "x", "speed": float("inf")}),
    ("flowkit_create_voice_template", {"text": "x", "instruct": "warm", "name": "../outside"}),
    ("flowkit_create_voice_template", {"text": "x", "instruct": "warm", "name": "https://example.com"}),
    ("flowkit_create_voice_template", {"text": "x", "instruct": "warm", "name": "x" * 65}),
    ("flowkit_create_voice_template", {"text": "", "instruct": "warm"}),
]
INVALID_NARRATION = [
    {"video_id": "../video"}, {"project_id": "https://example.com"},
    {"template": "../secret"}, {"template": "/tmp/audio.wav"}, {"template": ""},
    {"orientation": "diagonal"}, {"speed": 0}, {"sfx_volume": -1},
    {"sfx_volume": 2.1}, {"sfx_volume": float("inf")}, {"sfx_volume": float("nan")},
    {"from_scene": -1}, {"to_scene": -1}, {"from_scene": 5, "to_scene": 2},
    {"instruct": "x" * 201},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments", INVALID + [
    ("flowkit_narrate_video", {"video_id": str(uuid4()), "project_id": str(uuid4()), **overrides})
    for overrides in INVALID_NARRATION
])
@pytest.mark.parametrize("dry_run", [True, False])
async def test_invalid_inputs_fail_before_network(registry, tool, arguments, dry_run):
    mcp, request = registry
    with pytest.raises(Exception):
        await invoke(mcp, tool, **arguments, dry_run=dry_run)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_has_only_typed_safe_arguments(registry):
    mcp, _ = registry
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    expected = {
        "flowkit_list_voice_templates": set(),
        "flowkit_create_voice_template": {"text", "instruct", "name", "speed", "dry_run"},
        "flowkit_generate_tts": {"text", "instruct", "speed", "dry_run"},
        "flowkit_narrate_video": {"video_id", "project_id", "orientation", "speed", "instruct", "template", "mix", "sfx_volume", "from_scene", "to_scene", "dry_run"},
    }
    for name, fields in expected.items():
        assert set(tools[name].inputSchema["properties"]) == fields
        assert not ({"ref_audio", "ref_text", "provider", "url", "output_path", "body", "worker"} & fields)


@pytest.mark.asyncio
async def test_upstream_error_uses_shared_safe_translation(registry):
    mcp, request = registry
    request.side_effect = FlowKitAPIError("FlowKit request returned HTTP 503")
    with pytest.raises(Exception, match="FlowKit request returned HTTP 503"):
        await invoke(mcp, "flowkit_generate_tts", text="Hello")
