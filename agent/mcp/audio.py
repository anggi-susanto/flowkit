"""Scoped audio workflows over public REST APIs, without filesystem inputs.

Only narration accepts a named voice template. TTS deliberately exposes neither
reference-audio paths nor provider/worker selection. Results are the synchronous
backend responses, not fabricated queue requests or downloadable audio bytes.
"""
from __future__ import annotations

from typing import Any, Literal

from agent.models.tts import NarrateVideoRequest, TTSGenerateRequest, VoiceTemplateRequest


def register_audio_tools(mcp: Any) -> None:
    """Register audio tools; defer server imports to avoid an import cycle."""
    from agent.mcp.server import (
        GENERATE_SCOPE, READ_SCOPE, _tool, _validated, flowkit_client, resource_id,
    )

    @mcp.tool(name="flowkit_list_voice_templates", description="List stored voice templates and their metadata. Does not read or download audio files.")
    async def list_voice_templates() -> list[dict[str, Any]]:
        async def call() -> list[dict[str, Any]]:
            return await flowkit_client._request("GET", "/api/tts/templates")
        return await _tool("flowkit_list_voice_templates", call, scope=READ_SCOPE)

    @mcp.tool(name="flowkit_create_voice_template", description="Generate and save a named voice template (an existing name may be overwritten). Synchronous REST operation. dry_run validates without generating audio or contacting the backend.")
    async def create_voice_template(text: str, instruct: str, name: str = "voice_template_1",
                                    speed: float = 1.0, dry_run: bool = False) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            body = _validated(VoiceTemplateRequest, {
                "text": text, "instruct": instruct, "name": name, "speed": speed,
            })
            if dry_run:
                return {"dry_run": True, "operation": "create_voice_template", "body": body}
            return await flowkit_client.post("/api/tts/templates", body)
        return await _tool("flowkit_create_voice_template", call, scope=GENERATE_SCOPE)

    @mcp.tool(name="flowkit_generate_tts", description="Synthesize text with the local TTS engine. Returns backend audio metadata, not audio bytes or a queue request. No reference paths, URLs or external provider selection. dry_run validates only.")
    async def generate_tts(text: str, instruct: str | None = None, speed: float = 1.0,
                           dry_run: bool = False) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            options = {"text": text, "speed": speed}
            if instruct is not None:
                options["instruct"] = instruct
            body = _validated(TTSGenerateRequest, options, {"text", "instruct", "speed"})
            if dry_run:
                return {"dry_run": True, "operation": "generate_tts", "body": body}
            return await flowkit_client.post("/api/tts/generate", body)
        return await _tool("flowkit_generate_tts", call, scope=GENERATE_SCOPE)

    @mcp.tool(name="flowkit_narrate_video", description="Generate narration for stored video scenes, optionally mixing into stored clips. Uses a safe template name or backend project voice defaults, never caller-supplied file paths. Synchronous operation; inspect scene failures. dry_run validates only and does not check resource/template existence.")
    async def narrate_video(video_id: str, project_id: str,
                            orientation: Literal["HORIZONTAL", "VERTICAL"] = "HORIZONTAL",
                            speed: float = 1.0, instruct: str | None = None,
                            template: str | None = None, mix: bool = True,
                            sfx_volume: float = 0.4, from_scene: int | None = None,
                            to_scene: int | None = None, dry_run: bool = False) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            identifier = resource_id(video_id)
            options = {
                "project_id": resource_id(project_id), "orientation": orientation,
                "speed": speed, "instruct": instruct, "template": template,
                "mix": mix, "sfx_volume": sfx_volume,
                "from_scene": from_scene, "to_scene": to_scene,
            }
            body = _validated(NarrateVideoRequest, {k: v for k, v in options.items() if v is not None}, {
                "project_id", "orientation", "speed", "instruct", "template", "mix",
                "sfx_volume", "from_scene", "to_scene",
            })
            if from_scene is not None and to_scene is not None and from_scene > to_scene:
                raise ValueError("from_scene must not exceed to_scene")
            if dry_run:
                return {"dry_run": True, "operation": "narrate_video", "video_id": identifier, "body": body}
            return await flowkit_client.post(f"/api/videos/{identifier}/narrate", body)
        return await _tool("flowkit_narrate_video", call, scope=GENERATE_SCOPE)
