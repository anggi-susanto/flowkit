"""Public-REST finishing tools, registered separately from the core MCP surface.

No caller-supplied URLs, output filenames, or audio filesystem paths are exposed.
Finishing uses already stored scene clips and the backend's default output name;
finalize intentionally does not expose the REST API's local audio mixing inputs.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent.models.enums import Orientation, RequestType


class _FinishOptions(BaseModel):
    orientation: Orientation | None = None
    target_duration_s: float | None = Field(None, gt=0, allow_inf_nan=False)


def register_workflow_tools(mcp: Any) -> None:
    """Add scoped workflow tools to an existing FastMCP SDK registry.

    Import server helpers here so the server can call this during construction
    without a module import cycle. All effects still go through the REST adapter.
    """
    from agent.api.videos import ConcatRequest, FinalizeRequest
    from agent.mcp.server import (
        READ_SCOPE, WRITE_SCOPE, _require_scope, _tool, _validated,
        flowkit_client, resource_id,
    )
    from agent.models.video import VideoUpdate

    async def read_resource(name: str, collection: str, identifier: str) -> Any:
        async def call() -> Any:
            return await flowkit_client._request("GET", f"/api/{collection}/{resource_id(identifier)}")
        return await _tool(name, call, scope=READ_SCOPE)

    @mcp.tool(name="flowkit_get_video", description="Get a video by UUID, including its current output URLs and metadata.")
    async def get_video(video_id: str) -> dict[str, Any]:
        return await read_resource("flowkit_get_video", "videos", video_id)

    @mcp.tool(name="flowkit_get_scene", description="Get a scene by UUID, including stored generation assets.")
    async def get_scene(scene_id: str) -> dict[str, Any]:
        return await read_resource("flowkit_get_scene", "scenes", scene_id)

    @mcp.tool(name="flowkit_get_character", description="Get a character or reference entity by UUID.")
    async def get_character(character_id: str) -> dict[str, Any]:
        return await read_resource("flowkit_get_character", "characters", character_id)

    @mcp.tool(name="flowkit_list_project_characters", description="List characters/reference entities attached to one project UUID.")
    async def list_project_characters(project_id: str) -> list[dict[str, Any]]:
        async def call() -> list[dict[str, Any]]:
            return await flowkit_client.project_characters(resource_id(project_id))
        return await _tool("flowkit_list_project_characters", call, scope=READ_SCOPE)

    @mcp.tool(name="flowkit_update_video", description="Update video metadata only; asset URLs and filesystem paths are not accepted. dry_run validates without writing.")
    async def update_video(video_id: str, updates: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)

        async def call() -> dict[str, Any]:
            identifier = resource_id(video_id)
            body = _validated(VideoUpdate, updates, {
                "title", "description", "display_order", "status", "orientation",
                "target_duration_s", "privacy", "tags",
            })
            # VideoUpdate's REST fields are permissive strings/floats. Apply the
            # assembly endpoint's actual orientation/duration rules up front.
            _validated(_FinishOptions, {k: v for k, v in body.items() if k in _FinishOptions.model_fields})
            if dry_run:
                return {"dry_run": True, "operation": "update_video", "video_id": identifier, "updates": body}
            return await flowkit_client.update_video(identifier, body)
        return await _tool("flowkit_update_video", call, scope=WRITE_SCOPE)

    async def finish(operation: str, video_id: str, orientation: Orientation | None,
                     target_duration_s: float | None, dry_run: bool) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)

        async def call() -> dict[str, Any]:
            identifier = resource_id(video_id)
            options = {k: v for k, v in {"orientation": orientation, "target_duration_s": target_duration_s}.items() if v is not None}
            options = _validated(_FinishOptions, options)
            # Validate against the real backend request model as well as the
            # restricted MCP input. Never forward local paths/output filenames.
            model = ConcatRequest if operation == "concat" else FinalizeRequest
            body = _validated(model, options, {"orientation", "target_duration_s"})
            if dry_run:
                return {"dry_run": True, "operation": f"{operation}_video", "video_id": identifier, "body": body}
            return await flowkit_client.post(f"/api/videos/{identifier}/{operation}", body)
        return await _tool(f"flowkit_{operation}_video", call, scope=WRITE_SCOPE)

    @mcp.tool(name="flowkit_concat_video", description="Merge stored scene clips in display order, optionally trimming. Synchronous REST operation; may skip missing clips. No custom output path. dry_run performs validation only.")
    async def concat_video(video_id: str, orientation: Orientation | None = None,
                           target_duration_s: float | None = None, dry_run: bool = False) -> dict[str, Any]:
        return await finish("concat", video_id, orientation, target_duration_s, dry_run)

    @mcp.tool(name="flowkit_finalize_video", description="Assemble stored scene clips, optionally trim, and mark video COMPLETED. Synchronous REST operation, not a queue request. External music/narration paths are not supported. dry_run validates only.")
    async def finalize_video(video_id: str, orientation: Orientation | None = None,
                             target_duration_s: float | None = None, dry_run: bool = False) -> dict[str, Any]:
        return await finish("finalize", video_id, orientation, target_duration_s, dry_run)

    @mcp.tool(name="flowkit_refresh_project_urls", description="Refresh stored remote media URLs for a project UUID; local-only projects are a backend no-op. May require a connected Flow extension. dry_run does not contact the backend.")
    async def refresh_project_urls(project_id: str, dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)

        async def call() -> dict[str, Any]:
            identifier = resource_id(project_id)
            if dry_run:
                return {"dry_run": True, "operation": "refresh_project_urls", "project_id": identifier}
            # This endpoint has no request body (unlike queue/worker APIs).
            return await flowkit_client._request("POST", f"/api/flow/refresh-urls/{identifier}")
        return await _tool("flowkit_refresh_project_urls", call, scope=WRITE_SCOPE)

    @mcp.tool(name="flowkit_get_batch_status", description="Aggregate queued request status with optional project/video UUID, type and orientation filters. Empty batches can be done without all_succeeded; inspect total.")
    async def get_batch_status(video_id: str | None = None, project_id: str | None = None,
                               type: RequestType | None = None,
                               orientation: Orientation | None = None) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            params: dict[str, Any] = {}
            if video_id is not None:
                params["video_id"] = resource_id(video_id)
            if project_id is not None:
                params["project_id"] = resource_id(project_id)
            if type is not None:
                params["type"] = type
            if orientation is not None:
                params["orientation"] = orientation
            return await flowkit_client._request("GET", "/api/requests/batch-status", params=params or None)
        return await _tool("flowkit_get_batch_status", call, scope=READ_SCOPE)
