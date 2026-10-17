"""FlowKit MCP surface for Streamable HTTP and stdio transports.

Run locally with:
    python -m agent.mcp.server --transport stdio

The FastAPI application mounts `streamable_http_app()` at `/mcp` when MCP is
installed and `FLOWKIT_MCP_ENABLED=1`.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import logging
import os
import hashlib
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from typing import Literal

from agent.mcp.audit import audit_tool_call
from agent.config import (
    FLOWCOMMAND_CLIENT_PASSWORD,
    FLOWCOMMAND_CLIENT_USERNAME,
    FLOWKIT_MCP_POLL_INTERVAL_S,
    FLOWKIT_MCP_STDIO_SCOPES,
    FLOWKIT_MCP_WAIT_MAX_S,
)
from agent.mcp.auth import (
    ADMIN_SCOPE,
    GENERATE_SCOPE,
    READ_SCOPE,
    REMOTE_JOBS_SCOPE,
    WRITE_SCOPE,
    MCPPrincipal,
    authenticate_bearer,
    configured_tokens,
    has_scope,
)
from agent.mcp.client import FlowKitAPIError, flowkit_client, resource_id
from agent.mcp.remote_jobs import mcp_remote_job_registry
from agent.models.character import CharacterCreate, CharacterUpdate
from agent.models.project import ProjectCreate, ProjectUpdate
from agent.models.scene import SceneCreate, SceneUpdate
from agent.models.video import VideoCreate

logger = logging.getLogger(__name__)
_current_principal: contextvars.ContextVar[MCPPrincipal | None] = contextvars.ContextVar(
    "flowkit_mcp_principal", default=None
)

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.utilities.types import Image
    from mcp.server.transport_security import TransportSecuritySettings
except ImportError:  # pragma: no cover - optional runtime dependency
    FastMCP = None  # type: ignore[assignment,misc]


def enabled() -> bool:
    """Return whether the server should expose a network MCP endpoint."""
    return os.environ.get("FLOWKIT_MCP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _principal() -> MCPPrincipal:
    principal = _current_principal.get()
    if principal is None:
        raise PermissionError("MCP caller is not authenticated")
    return principal


def _validated(model: Any, body: dict[str, Any], allowed: set[str] | None = None) -> dict[str, Any]:
    unknown = set(body) - (allowed if allowed is not None else set(model.model_fields))
    if unknown:
        raise ValueError("Unsupported fields: " + ", ".join(sorted(unknown)))
    result = model.model_validate(body).model_dump(exclude_unset=True, exclude_none=True)
    for key, value in result.items():
        if key.endswith("_id") and value is not None:
            resource_id(value)
    return result


def _require_scope(scope: str) -> MCPPrincipal:
    principal = _principal()
    if not has_scope(principal, scope):
        raise PermissionError(f"MCP token lacks required scope: {scope}")
    return principal


async def _tool(name: str, callback: Callable[..., Awaitable[Any]], scope: str = READ_SCOPE, **kwargs: Any) -> Any:
    principal = _principal()
    with audit_tool_call(name, principal.subject):
        _require_scope(scope)
        try:
            return await callback(**kwargs)
        except FlowKitAPIError as exc:
            # MCP callers receive a safe operational error; upstream response
            # bodies, prompts, and credential material never enter audit logs.
            raise RuntimeError(str(exc)) from exc


async def _request_parent_ids(scene_id: str) -> tuple[str, str]:
    """Resolve the project/video parent IDs used by the request queue."""
    scene = await flowkit_client._request("GET", f"/api/scenes/{resource_id(scene_id)}")
    video = await flowkit_client._request("GET", f"/api/videos/{resource_id(scene['video_id'])}")
    return str(video["project_id"]), str(video["id"])


async def _generation_dependencies(scene_id: str, project_id: str, orientation: str,
                                   stage: str) -> None:
    scene = await flowkit_client._request("GET", f"/api/scenes/{resource_id(scene_id)}")
    if stage == "image":
        characters = await flowkit_client.project_characters(project_id)
        names = scene.get("character_names") or []
        by_name = {item["name"]: item for item in characters}
        for name in names:
            entity = by_name.get(name)
            if entity is None or not entity.get("media_id"):
                raise ValueError(f"Reference is not ready: {name}")
            resource_id(entity["media_id"])
    else:
        required = "image" if stage == "video" else "video"
        field = f"{orientation.lower()}_{required}_media_id"
        if not scene.get(field):
            raise ValueError(f"Scene {required} must exist before {stage} generation")
        resource_id(scene[field])


async def _wait_for_request(request_id: str, timeout_s: int) -> dict[str, Any]:
    timeout = min(max(1, timeout_s), FLOWKIT_MCP_WAIT_MAX_S)
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        try:
            request = await asyncio.wait_for(flowkit_client.request(request_id), timeout=max(0.001, remaining))
        except TimeoutError:
            return {"request_id": request_id, "done": False, "timed_out": True}
        if request.get("status") in {"COMPLETED", "FAILED"}:
            return {"request": request, "done": True, "timed_out": False}
        if asyncio.get_running_loop().time() >= deadline:
            return {"request": request, "done": False, "timed_out": True}
        await asyncio.sleep(min(FLOWKIT_MCP_POLL_INTERVAL_S, max(0, deadline - asyncio.get_running_loop().time())))


def _flowcommand_credentials() -> tuple[str, str]:
    if not FLOWCOMMAND_CLIENT_USERNAME or not FLOWCOMMAND_CLIENT_PASSWORD:
        raise RuntimeError("FlowCommand client credentials are not configured")
    return FLOWCOMMAND_CLIENT_USERNAME, FLOWCOMMAND_CLIENT_PASSWORD


def create_mcp() -> Any:
    """Create the single tool registry shared by HTTP and stdio transports."""
    if FastMCP is None:
        raise RuntimeError("MCP support requires optional dependency: pip install -r requirements-mcp.txt")

    mcp = FastMCP(
        "FlowKit",
        instructions=(
            "FlowKit project authoring and asynchronous generation surface. Every "
            "generation tool supports dry_run and returns request IDs, not false "
            "synchronous completion. Credit status "
            "reports configured tier, not a live Google credit balance."
        ),
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=os.environ.get("FLOWKIT_MCP_ALLOWED_HOSTS", "127.0.0.1,127.0.0.1:*,localhost,localhost:*,flowcommand.gunamaya.id").split(","),
            allowed_origins=os.environ.get("FLOWKIT_MCP_ALLOWED_ORIGINS", "https://flowcommand.gunamaya.id,http://localhost,http://localhost:*,http://127.0.0.1,http://127.0.0.1:*").split(","),
        ),
    )

    @mcp.tool(name="flowkit_health", description="Return FlowKit health, readiness, and Flow extension status.")
    async def health() -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            health_data, ready_data, flow_data = await __import__("asyncio").gather(
                flowkit_client.health(), flowkit_client.ready(), flowkit_client.flow_status()
            )
            return {"health": health_data, "ready": ready_data, "flow": flow_data}
        return await _tool("flowkit_health", call)

    @mcp.tool(name="flowkit_list_models", description="List configured image, video, Omni Flash, and upscale models.")
    async def list_models() -> dict[str, Any]:
        return await _tool("flowkit_list_models", flowkit_client.models)

    @mcp.tool(name="flowkit_list_materials", description="List available visual materials/styles.")
    async def list_materials() -> list[dict[str, Any]]:
        return await _tool("flowkit_list_materials", flowkit_client.materials)

    @mcp.tool(name="flowkit_list_projects", description="List FlowKit projects.")
    async def list_projects() -> list[dict[str, Any]]:
        return await _tool("flowkit_list_projects", flowkit_client.projects)

    @mcp.tool(name="flowkit_get_project", description="Get one project by ID.")
    async def get_project(project_id: str) -> dict[str, Any]:
        return await _tool("flowkit_get_project", flowkit_client.project, project_id=project_id)

    @mcp.tool(name="flowkit_list_videos", description="List videos in one project.")
    async def list_videos(project_id: str) -> list[dict[str, Any]]:
        return await _tool("flowkit_list_videos", flowkit_client.videos, project_id=project_id)

    @mcp.tool(name="flowkit_list_scenes", description="List scenes in one video.")
    async def list_scenes(video_id: str) -> list[dict[str, Any]]:
        return await _tool("flowkit_list_scenes", flowkit_client.scenes, video_id=video_id)

    @mcp.tool(name="flowkit_list_characters", description="List characters and reference entities.")
    async def list_characters() -> list[dict[str, Any]]:
        return await _tool("flowkit_list_characters", flowkit_client.characters)

    @mcp.tool(name="flowkit_list_requests", description="List asynchronous FlowKit requests with optional filters.")
    async def list_requests(
        project_id: str | None = None,
        video_id: str | None = None,
        scene_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return await _tool(
            "flowkit_list_requests", flowkit_client.requests,
            project_id=project_id, video_id=video_id, scene_id=scene_id, status=status,
        )

    @mcp.tool(name="flowkit_get_request", description="Get one asynchronous request by ID.")
    async def get_request(request_id: str) -> dict[str, Any]:
        return await _tool("flowkit_get_request", flowkit_client.request, request_id=request_id)

    @mcp.tool(name="flowkit_get_provider_status", description="Return media-provider availability and throttling capabilities.")
    async def get_provider_status() -> dict[str, Any]:
        return await _tool("flowkit_get_provider_status", flowkit_client.provider_status)

    @mcp.tool(name="flowkit_get_credits_status", description="Return configured Flow tier and whether live credit balance is available.")
    async def get_credits_status() -> dict[str, Any]:
        return await _tool("flowkit_get_credits_status", flowkit_client.credits_status)

    @mcp.tool(name="flowkit_create_project", description="Create a FlowKit project. Set dry_run to validate without creating it.")
    async def create_project(name: str, material: str = "realistic", description: str | None = None,
                             story: str | None = None, language: str = "en",
                             video_model_family: str = "veo", dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)
        body = {"name": name, "material": material, "description": description, "story": story,
                "language": language, "video_model_family": video_model_family}
        body = {key: value for key, value in body.items() if value is not None}
        body = _validated(ProjectCreate, body)
        if dry_run:
            return {"dry_run": True, "operation": "create_project", "body": body}
        return await _tool("flowkit_create_project", flowkit_client.create_project, scope=WRITE_SCOPE, body=body)

    @mcp.tool(name="flowkit_update_project", description="Update non-generative project metadata.")
    async def update_project(project_id: str, updates: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)
        resource_id(project_id)
        updates = _validated(ProjectUpdate, updates, {"name", "description", "story", "language", "material", "allow_music", "allow_voice", "video_model_family", "status"})
        if dry_run:
            return {"dry_run": True, "operation": "update_project", "project_id": project_id, "updates": updates}
        return await _tool("flowkit_update_project", flowkit_client.update_project, scope=WRITE_SCOPE, project_id=project_id, body=updates)

    @mcp.tool(name="flowkit_build_character", description="Create and attach a character/entity; optionally queue its reference image.")
    async def build_character(project_id: str, name: str, entity_type: str = "character",
                              description: str | None = None, voice_description: str | None = None,
                              generate_reference: bool = True, provider: str | None = None,
                              dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)
        if generate_reference:
            _require_scope(GENERATE_SCOPE)
        resource_id(project_id)
        character_body = {"name": name, "entity_type": entity_type, "description": description,
                          "voice_description": voice_description}
        character_body = {key: value for key, value in character_body.items() if value is not None}
        character_body = _validated(CharacterCreate, character_body)
        if dry_run:
            return {"dry_run": True, "operation": "build_character", "project_id": project_id,
                    "character": character_body, "generate_reference": generate_reference}
        async def call() -> dict[str, Any]:
            character = await flowkit_client.create_character(character_body)
            await flowkit_client.attach_character(project_id, character["id"])
            response: dict[str, Any] = {"character": character, "attached": True, "reference_ready": False}
            if generate_reference:
                request = await flowkit_client.create_request({
                    "type": "GENERATE_CHARACTER_IMAGE", "character_id": character["id"],
                    "project_id": project_id, "provider": provider,
                    "orientation": "HORIZONTAL" if entity_type == "location" else "VERTICAL",
                })
                response["request"] = request
                response["next_action"] = {"tool": "flowkit_wait_for_job", "arguments": {"request_id": request["id"]}}
            return response
        return await _tool("flowkit_build_character", call, scope=WRITE_SCOPE)

    @mcp.tool(name="flowkit_create_character", description="Create a reference entity without attachment or generation.")
    async def create_character(name: str, entity_type: str = "character", description: str | None = None,
                               image_prompt: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)
        body = CharacterCreate(name=name, entity_type=entity_type, description=description,
                               image_prompt=image_prompt).model_dump(exclude_none=True)
        if dry_run:
            return {"dry_run": True, "body": body}
        return await _tool("flowkit_create_character", flowkit_client.create_character, scope=WRITE_SCOPE, body=body)

    @mcp.tool(name="flowkit_generate_character_reference", description="Queue an attached entity's reference image. Locations use landscape; characters use portrait.")
    async def generate_character_reference(project_id: str, character_id: str, regenerate: bool = False,
                                           provider: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        _require_scope(GENERATE_SCOPE)
        characters = await flowkit_client.project_characters(project_id)
        character = next((item for item in characters if item["id"] == character_id), None)
        if character is None:
            raise ValueError("Character must be attached to this project")
        body = {"type": "REGENERATE_CHARACTER_IMAGE" if regenerate else "GENERATE_CHARACTER_IMAGE",
                "project_id": project_id, "character_id": character_id,
                "orientation": "HORIZONTAL" if character.get("entity_type") == "location" else "VERTICAL"}
        if provider:
            body["provider"] = provider
        if dry_run:
            return {"dry_run": True, "request": body}
        return await _tool("flowkit_generate_character_reference", flowkit_client.create_request, scope=GENERATE_SCOPE, body=body)

    @mcp.tool(name="flowkit_get_active_project", description="Get the shared active-project selection.")
    async def get_active_project() -> dict[str, Any]:
        return await _tool("flowkit_get_active_project", flowkit_client._request, method="GET", path="/api/active-project")

    @mcp.tool(name="flowkit_update_character", description="Update a character or reference entity.")
    async def update_character(character_id: str, updates: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)
        resource_id(character_id)
        updates = _validated(CharacterUpdate, updates, {"name", "entity_type", "description", "image_prompt", "voice_description"})
        if dry_run: return {"dry_run": True, "operation": "update_character", "character_id": character_id, "updates": updates}
        return await _tool("flowkit_update_character", flowkit_client.update_character, scope=WRITE_SCOPE, character_id=character_id, body=updates)

    @mcp.tool(name="flowkit_attach_character_to_project", description="Attach an existing character/entity to a project.")
    async def attach_character_to_project(project_id: str, character_id: str, dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)
        resource_id(project_id)
        resource_id(character_id)
        if dry_run: return {"dry_run": True, "operation": "attach_character_to_project", "project_id": project_id, "character_id": character_id}
        return await _tool("flowkit_attach_character_to_project", flowkit_client.attach_character, scope=WRITE_SCOPE, project_id=project_id, character_id=character_id)

    @mcp.tool(name="flowkit_create_video", description="Create a video/episode inside a project.")
    async def create_video(project_id: str, title: str, description: str | None = None,
                           orientation: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)
        body = {key: value for key, value in {"project_id": project_id, "title": title, "description": description,
                                               "orientation": orientation}.items() if value is not None}
        body = _validated(VideoCreate, body)
        if dry_run: return {"dry_run": True, "operation": "create_video", "body": body}
        return await _tool("flowkit_create_video", flowkit_client.create_video, scope=WRITE_SCOPE, body=body)

    @mcp.tool(name="flowkit_create_scene", description="Create a scene. Scene prompts should describe action, not character appearance.")
    async def create_scene(video_id: str, prompt: str, display_order: int = 0, image_prompt: str | None = None,
                           video_prompt: str | None = None, character_names: list[str] | None = None,
                           chain_type: str = "ROOT", parent_scene_id: str | None = None,
                           transition_prompt: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)
        body = {key: value for key, value in {"video_id": video_id, "prompt": prompt, "display_order": display_order,
                                               "image_prompt": image_prompt, "video_prompt": video_prompt,
                                               "character_names": character_names, "chain_type": chain_type,
                                               "parent_scene_id": parent_scene_id, "transition_prompt": transition_prompt}.items() if value is not None}
        body = _validated(SceneCreate, body)
        if dry_run: return {"dry_run": True, "operation": "create_scene", "body": body}
        return await _tool("flowkit_create_scene", flowkit_client.create_scene, scope=WRITE_SCOPE, body=body)

    @mcp.tool(name="flowkit_update_scene", description="Update a scene's prompts, character names, or chain configuration.")
    async def update_scene(scene_id: str, updates: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)
        resource_id(scene_id)
        updates = _validated(SceneUpdate, updates, {"prompt", "image_prompt", "video_prompt", "character_names", "parent_scene_id", "chain_type", "source", "display_order", "transition_prompt", "trim_start", "trim_end", "duration", "narrator_text"})
        if dry_run: return {"dry_run": True, "operation": "update_scene", "scene_id": scene_id, "updates": updates}
        return await _tool("flowkit_update_scene", flowkit_client.update_scene, scope=WRITE_SCOPE, scene_id=scene_id, body=updates)

    @mcp.tool(name="flowkit_generate_scene_image", description="Queue scene image generation. Regeneration clears downstream video/upscale outputs.")
    async def generate_scene_image(scene_id: str, orientation: Literal["VERTICAL", "HORIZONTAL"] = "VERTICAL", regenerate: bool = False,
                                   provider: str | None = None, image_model: str | None = None,
                                   dry_run: bool = False) -> dict[str, Any]:
        _require_scope(GENERATE_SCOPE)
        if image_model is not None:
            raise ValueError("Per-request image_model is unsupported by the durable queue; use admin model configuration")
        project_id, video_id = await _request_parent_ids(scene_id)
        await _generation_dependencies(scene_id, project_id, orientation, "image")
        request_type = "REGENERATE_IMAGE" if regenerate else "GENERATE_IMAGE"
        body = {"type": request_type, "scene_id": scene_id, "project_id": project_id, "video_id": video_id,
                "orientation": orientation, "provider": provider}
        body = {key: value for key, value in body.items() if value is not None}
        if dry_run:
            return {"dry_run": True, "operation": "generate_scene_image", "request": body,
                    "cascade": ["scene_image", "scene_video", "scene_upscale"] if regenerate else []}
        async def call() -> dict[str, Any]:
            request = await flowkit_client.create_request(body)
            return {"accepted": True, "request": request, "next_action": {"tool": "flowkit_wait_for_job", "arguments": {"request_id": request["id"]}}}
        return await _tool("flowkit_generate_scene_image", call, scope=GENERATE_SCOPE)

    @mcp.tool(name="flowkit_generate_scene_video", description="Queue Veo or Omni Flash scene video generation; requires a scene image first.")
    async def generate_scene_video(scene_id: str, orientation: Literal["VERTICAL", "HORIZONTAL"] = "VERTICAL", references: bool = False,
                                   regenerate: bool = False, provider: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        _require_scope(GENERATE_SCOPE)
        if references and regenerate:
            raise ValueError("Reference-video regeneration is not supported by the queue")
        project_id, video_id = await _request_parent_ids(scene_id)
        await _generation_dependencies(scene_id, project_id, orientation, "image" if references else "video")
        request_type = "GENERATE_VIDEO_REFS" if references else ("REGENERATE_VIDEO" if regenerate else "GENERATE_VIDEO")
        body = {key: value for key, value in {"type": request_type, "scene_id": scene_id, "project_id": project_id,
                                               "video_id": video_id, "orientation": orientation, "provider": provider}.items() if value is not None}
        if dry_run: return {"dry_run": True, "operation": "generate_scene_video", "request": body,
                            "cascade": ["scene_video", "scene_upscale"] if regenerate else []}
        async def call() -> dict[str, Any]:
            request = await flowkit_client.create_request(body)
            return {"accepted": True, "request": request, "next_action": {"tool": "flowkit_wait_for_job", "arguments": {"request_id": request["id"]}}}
        return await _tool("flowkit_generate_scene_video", call, scope=GENERATE_SCOPE)

    @mcp.tool(name="flowkit_upscale_scene_video", description="Queue scene video upscaling after video generation has completed.")
    async def upscale_scene_video(scene_id: str, orientation: Literal["VERTICAL", "HORIZONTAL"] = "VERTICAL", provider: str | None = None,
                                  dry_run: bool = False) -> dict[str, Any]:
        _require_scope(GENERATE_SCOPE)
        project_id, video_id = await _request_parent_ids(scene_id)
        await _generation_dependencies(scene_id, project_id, orientation, "upscale")
        body = {key: value for key, value in {"type": "UPSCALE_VIDEO", "scene_id": scene_id, "project_id": project_id,
                                               "video_id": video_id, "orientation": orientation, "provider": provider}.items() if value is not None}
        if dry_run: return {"dry_run": True, "operation": "upscale_scene_video", "request": body}
        async def call() -> dict[str, Any]:
            request = await flowkit_client.create_request(body)
            return {"accepted": True, "request": request, "next_action": {"tool": "flowkit_wait_for_job", "arguments": {"request_id": request["id"]}}}
        return await _tool("flowkit_upscale_scene_video", call, scope=GENERATE_SCOPE)

    @mcp.tool(name="flowkit_generate_video_batch", description="Queue one video request per scene and preserve per-scene results.")
    async def generate_video_batch(scene_ids: list[str], orientation: Literal["VERTICAL", "HORIZONTAL"] = "VERTICAL", references: bool = False,
                                   provider: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        _require_scope(GENERATE_SCOPE)
        if not 1 <= len(scene_ids) <= 100 or len(set(scene_ids)) != len(scene_ids):
            raise ValueError("scene_ids must contain 1–100 unique IDs")
        bodies = []
        for scene_id in scene_ids:
            project_id, video_id = await _request_parent_ids(scene_id)
            await _generation_dependencies(scene_id, project_id, orientation, "image" if references else "video")
            bodies.append({key: value for key, value in {"type": "GENERATE_VIDEO_REFS" if references else "GENERATE_VIDEO",
                "scene_id": scene_id, "project_id": project_id, "video_id": video_id, "orientation": orientation, "provider": provider}.items() if value is not None})
        if dry_run: return {"dry_run": True, "operation": "generate_video_batch", "requests": bodies}
        async def call() -> dict[str, Any]:
            requests = await flowkit_client.create_requests(bodies)
            return {"accepted": True, "results": [{"scene_id": body["scene_id"], "request_id": req["id"], "status": req["status"]} for body, req in zip(bodies, requests)]}
        return await _tool("flowkit_generate_video_batch", call, scope=GENERATE_SCOPE)

    @mcp.tool(name="flowkit_wait_for_job", description="Bounded polling for a FlowKit request; maximum server wait is configured.")
    async def wait_for_job(request_id: str, timeout_s: int = 60) -> dict[str, Any]:
        return await _tool("flowkit_wait_for_job", _wait_for_request, scope=READ_SCOPE, request_id=request_id, timeout_s=timeout_s)

    @mcp.tool(name="flowkit_review_video", description="Run a video review after generation and before upscaling.")
    async def review_video(video_id: str, mode: str = "light", dry_run: bool = False) -> dict[str, Any]:
        _require_scope(GENERATE_SCOPE)
        if mode not in {"light", "deep"}: raise ValueError("mode must be light or deep")
        if dry_run: return {"dry_run": True, "operation": "review_video", "video_id": video_id, "mode": mode}
        return await _tool("flowkit_review_video", flowkit_client.review_video, scope=GENERATE_SCOPE, video_id=video_id, mode=mode)

    @mcp.tool(name="flowkit_submit_remote_image_job", description="Submit a FlowCommand remote image job. Output is asynchronous and owner-scoped.")
    async def submit_remote_image_job(prompt: str, aspect_ratio: str = "1:1", idempotency_key: str | None = None,
                                      dry_run: bool = False) -> dict[str, Any]:
        principal = _require_scope(REMOTE_JOBS_SCOPE)
        from agent.models.remote_job import ImageJobCreate
        ImageJobCreate(prompt=prompt, aspect_ratio=aspect_ratio, idempotency_key=idempotency_key)
        if idempotency_key is not None:
            idempotency_key = "mcp-" + hashlib.sha256((principal.subject + "\0" + idempotency_key).encode()).hexdigest()
        body = {key: value for key, value in {"prompt": prompt, "aspect_ratio": aspect_ratio,
                                               "idempotency_key": idempotency_key}.items() if value is not None}
        if dry_run: return {"dry_run": True, "operation": "submit_remote_image_job", "request": body}
        async def call() -> dict[str, Any]:
            username, password = _flowcommand_credentials()
            job = await flowkit_client.remote_image_job(body, username, password)
            await mcp_remote_job_registry.record(job["job_id"], principal.subject)
            return {"accepted": True, "job": job, "next_action": {"tool": "flowkit_get_remote_image_job", "arguments": {"job_id": job["job_id"]}}}
        return await _tool("flowkit_submit_remote_image_job", call, scope=REMOTE_JOBS_SCOPE)

    @mcp.tool(name="flowkit_get_remote_image_job", description="Get a remote image job previously submitted by this MCP principal.")
    async def get_remote_image_job(job_id: str) -> dict[str, Any]:
        principal = _require_scope(REMOTE_JOBS_SCOPE)
        async def call() -> dict[str, Any]:
            if not await mcp_remote_job_registry.owns(job_id, principal.subject):
                raise PermissionError("Remote image job was not submitted by this MCP principal")
            username, password = _flowcommand_credentials()
            return await flowkit_client.get_remote_image_job(job_id, username, password)
        return await _tool("flowkit_get_remote_image_job", call, scope=REMOTE_JOBS_SCOPE)

    @mcp.tool(name="flowkit_get_remote_image_output", description="Fetch a completed image submitted by this principal, without exposing backend credentials.")
    async def get_remote_image_output(job_id: str) -> Image:
        principal = _require_scope(REMOTE_JOBS_SCOPE)
        async def call() -> Image:
            if not await mcp_remote_job_registry.owns(job_id, principal.subject):
                raise PermissionError("Remote image job was not submitted by this MCP principal")
            username, password = _flowcommand_credentials()
            data, image_format = await flowkit_client.get_remote_image_output(job_id, username, password)
            return Image(data=data, format=image_format)
        return await _tool("flowkit_get_remote_image_output", call, scope=REMOTE_JOBS_SCOPE)

    @mcp.tool(name="flowkit_update_models", description="Admin-only update of global model configuration.")
    async def update_models(updates: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        _require_scope(ADMIN_SCOPE)
        if dry_run: return {"dry_run": True, "operation": "update_models", "updates": updates}
        return await _tool("flowkit_update_models", flowkit_client.patch, scope=ADMIN_SCOPE, path="/api/models", body=updates)

    from agent.mcp.media import register_media_tools
    from agent.mcp.workflows import register_workflow_tools
    from agent.mcp.audio import register_audio_tools
    from agent.mcp.lifecycle import register_lifecycle_tools
    from agent.mcp.music import register_music_tools
    register_media_tools(mcp)
    register_workflow_tools(mcp)
    register_audio_tools(mcp)
    register_lifecycle_tools(mcp)
    register_music_tools(mcp)
    return mcp


class AuthenticatedMCPApp:
    """ASGI guard for the remote MCP mount.

    MCP SDK auth supports OAuth resource servers, while FlowKit v1 uses scoped
    bearer tokens. This guard validates the dedicated MCP token before handing
    traffic to the SDK's Streamable HTTP session manager.
    """

    def __init__(self, app: Any, tokens: dict[str, MCPPrincipal] | None = None) -> None:
        self.app = app
        self.tokens = tokens if tokens is not None else configured_tokens()
        self.max_body_bytes = max(1024, int(os.environ.get("FLOWKIT_MCP_MAX_BODY_BYTES", "12582912")))
        self.requests_per_minute = max(1, int(os.environ.get("FLOWKIT_MCP_REQUESTS_PER_MINUTE", "120")))
        self._requests: dict[str, deque[float]] = {}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        principal = authenticate_bearer(headers.get("authorization"), self.tokens)
        if principal is None:
            await send({
                "type": "http.response.start", "status": 401,
                "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
            })
            await send({"type": "http.response.body", "body": b'{"detail":"Invalid MCP credentials"}'})
            return
        now = time.monotonic()
        requests = self._requests.setdefault(principal.subject, deque())
        while requests and requests[0] <= now - 60:
            requests.popleft()
        if len(requests) >= self.requests_per_minute:
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"retry-after", b"60")]})
            await send({"type": "http.response.body", "body": b"MCP request limit exceeded"})
            return
        requests.append(now)
        # Buffer at most one bounded JSON-RPC body before invoking the SDK.
        messages, size = [], 0
        if scope.get("method") == "POST":
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                size += len(message.get("body", b""))
                if size > self.max_body_bytes:
                    await send({"type": "http.response.start", "status": 413, "headers": []})
                    await send({"type": "http.response.body", "body": b"MCP request too large"})
                    return
                messages.append(message)
                if not message.get("more_body", False):
                    break

        async def bounded_receive():
            if messages:
                return messages.pop(0)
            return await receive()

        token = _current_principal.set(principal)
        try:
            await self.app(scope, bounded_receive, send)
        finally:
            _current_principal.reset(token)


def streamable_http_app(tokens: dict[str, MCPPrincipal] | None = None) -> Any:
    """Return the authenticated ASGI sub-application mounted at `/mcp`."""
    return AuthenticatedMCPApp(create_mcp().streamable_http_app(), tokens=tokens)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run FlowKit MCP server")
    parser.add_argument("--transport", choices=("stdio",), default="stdio")
    args = parser.parse_args()
    mcp = create_mcp()
    _current_principal.set(MCPPrincipal("local-stdio", FLOWKIT_MCP_STDIO_SCOPES))
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
