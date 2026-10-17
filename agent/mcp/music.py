"""Scoped Suno workflows through fixed public REST routes.

Submissions never poll, download clips, or accept filesystem/URL inputs. Dry runs
validate request syntax only, not template existence, provider support or credits.
"""
from __future__ import annotations

import re
from typing import Any


_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _music_id(value: str) -> str:
    """Suno IDs and template names are opaque safe tokens, not necessarily UUIDs."""
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("Music IDs must be 1..128 ASCII letters, digits, underscores or hyphens")
    return value


def register_music_tools(mcp: Any) -> None:
    """Register separately; defer server imports to avoid construction cycles."""
    from agent.api.music import (
        ConvertToWavRequest, ExtendRequest, GenerateLyricsRequest,
        GenerateRequest, VocalRemovalRequest,
    )
    from agent.mcp.server import GENERATE_SCOPE, READ_SCOPE, _tool, flowkit_client

    async def submit(operation: str, path: str, model: Any,
                     options: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            # Use the actual REST models directly: server._validated treats every
            # *_id as a UUID, but Suno task/audio IDs and template names are not.
            body = model.model_validate({**options, "poll": False}).model_dump(
                exclude_unset=True, exclude_none=True,
            )
            for field in ("template_id", "task_id", "audio_id"):
                if field in body:
                    _music_id(body[field])
            if model is GenerateRequest and not (body.get("prompt") or body.get("template_id")):
                raise ValueError("Provide prompt (lyrics or description) or template_id")
            if dry_run:
                return {"dry_run": True, "operation": operation, "body": body}
            return await flowkit_client.post(path, body)
        return await _tool(f"flowkit_{operation}", call, scope=GENERATE_SCOPE)

    @mcp.tool(name="flowkit_list_music_templates", description="List available song templates. Reads metadata only; does not download audio.")
    async def list_music_templates() -> dict[str, Any]:
        async def call() -> Any:
            return await flowkit_client._request("GET", "/api/music/templates")
        return await _tool("flowkit_list_music_templates", call, scope=READ_SCOPE)

    @mcp.tool(name="flowkit_get_music_template", description="Read one song template by safe template ID, never a file path or URL.")
    async def get_music_template(template_id: str) -> dict[str, Any]:
        async def call() -> Any:
            identifier = _music_id(template_id)
            return await flowkit_client._request("GET", f"/api/music/templates/{identifier}")
        return await _tool("flowkit_get_music_template", call, scope=READ_SCOPE)

    @mcp.tool(name="flowkit_generate_music", description="Submit Suno music generation and return a task ID without polling or downloading. Custom mode uses lyrics; description mode writes lyrics automatically. dry_run validates only and consumes no credits.")
    async def generate_music(prompt: str = "", style: str = "", title: str = "",
                             instrumental: bool = False, model: str = "",
                             custom_mode: bool = True, template_id: str | None = None,
                             dry_run: bool = False) -> dict[str, Any]:
        return await submit("generate_music", "/api/music/generate", GenerateRequest, {
            "prompt": prompt, "style": style, "title": title,
            "instrumental": instrumental, "model": model,
            "custom_mode": custom_mode, "template_id": template_id,
        }, dry_run)

    @mcp.tool(name="flowkit_generate_lyrics", description="Submit Suno lyric generation, optionally using a song template. Returns a task ID without polling. dry_run validates only; no provider call or credits consumed.")
    async def generate_lyrics(prompt: str, template_id: str | None = None,
                              dry_run: bool = False) -> dict[str, Any]:
        return await submit("generate_lyrics", "/api/music/generate-lyrics", GenerateLyricsRequest, {
            "prompt": prompt, "template_id": template_id,
        }, dry_run)

    @mcp.tool(name="flowkit_get_music_task", description="Read a Suno task status and clip metadata by safe task ID. Does not poll or download clips.")
    async def get_music_task(task_id: str) -> dict[str, Any]:
        async def call() -> Any:
            identifier = _music_id(task_id)
            return await flowkit_client._request("GET", f"/api/music/tasks/{identifier}")
        return await _tool("flowkit_get_music_task", call, scope=READ_SCOPE)

    @mcp.tool(name="flowkit_extend_music", description="Submit continuation of an existing Suno audio ID. Returns a task ID without polling. dry_run validates only; no credits consumed.")
    async def extend_music(audio_id: str, prompt: str = "", continue_at: float | None = None,
                           model: str = "", default_param_flag: bool = True,
                           dry_run: bool = False) -> dict[str, Any]:
        return await submit("extend_music", "/api/music/extend", ExtendRequest, {
            "audio_id": audio_id, "prompt": prompt, "continue_at": continue_at,
            "model": model, "default_param_flag": default_param_flag,
        }, dry_run)

    @mcp.tool(name="flowkit_vocal_removal", description="Submit vocal/instrumental separation for stored Suno task and audio IDs, not URLs. Returns a new task ID without polling or downloading. dry_run validates only.")
    async def vocal_removal(task_id: str, audio_id: str, dry_run: bool = False) -> dict[str, Any]:
        return await submit("vocal_removal", "/api/music/vocal-removal", VocalRemovalRequest, {
            "task_id": task_id, "audio_id": audio_id,
        }, dry_run)

    @mcp.tool(name="flowkit_convert_to_wav", description="Submit WAV conversion for stored Suno task and audio IDs, not paths or URLs. Returns a new task ID without polling or downloading. dry_run validates only.")
    async def convert_to_wav(task_id: str, audio_id: str, dry_run: bool = False) -> dict[str, Any]:
        return await submit("convert_to_wav", "/api/music/convert-to-wav", ConvertToWavRequest, {
            "task_id": task_id, "audio_id": audio_id,
        }, dry_run)
