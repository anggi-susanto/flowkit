"""Explicit, scope-checked resource lifecycle tools; no arbitrary REST access."""
from __future__ import annotations

from typing import Any


def register_lifecycle_tools(mcp: Any) -> None:
    # Defer server imports: the server may import this registrar during startup.
    from agent.mcp.server import WRITE_SCOPE, _tool, flowkit_client, resource_id

    async def delete_resource(collection: str, identifier: str, confirm: bool,
                              dry_run: bool) -> dict[str, Any]:
        identifier = resource_id(identifier)
        path = f"/api/{collection}/{identifier}"
        if dry_run:
            return {"dry_run": True, "method": "DELETE", "path": path}
        if confirm is not True:
            raise ValueError("Deletion requires confirm=true; use dry_run=true to preview")
        return await flowkit_client._request("DELETE", path)

    @mcp.tool(name="flowkit_delete_project", description="Permanently delete a project via the backend, including its backend-managed cascade. Requires write and confirm=true. dry_run previews without deleting.")
    async def delete_project(project_id: str, confirm: bool = False,
                             dry_run: bool = False) -> dict[str, Any]:
        async def call():
            return await delete_resource("projects", project_id, confirm, dry_run)
        return await _tool("flowkit_delete_project", call, scope=WRITE_SCOPE)

    @mcp.tool(name="flowkit_delete_video", description="Permanently delete a video via the backend, including its backend-managed cascade. Requires write and confirm=true. dry_run previews without deleting.")
    async def delete_video(video_id: str, confirm: bool = False,
                           dry_run: bool = False) -> dict[str, Any]:
        async def call():
            return await delete_resource("videos", video_id, confirm, dry_run)
        return await _tool("flowkit_delete_video", call, scope=WRITE_SCOPE)

    @mcp.tool(name="flowkit_delete_scene", description="Permanently delete one scene via the backend. Requires write and confirm=true. dry_run previews without deleting; prefer updating existing scenes when possible.")
    async def delete_scene(scene_id: str, confirm: bool = False,
                           dry_run: bool = False) -> dict[str, Any]:
        async def call():
            return await delete_resource("scenes", scene_id, confirm, dry_run)
        return await _tool("flowkit_delete_scene", call, scope=WRITE_SCOPE)

    @mcp.tool(name="flowkit_delete_character", description="Permanently delete a shared character, not just its project attachment. Prefer detach_character_from_project to remove only an attachment. Requires write and confirm=true. dry_run previews only.")
    async def delete_character(character_id: str, confirm: bool = False,
                               dry_run: bool = False) -> dict[str, Any]:
        async def call():
            return await delete_resource("characters", character_id, confirm, dry_run)
        return await _tool("flowkit_delete_character", call, scope=WRITE_SCOPE)

    @mcp.tool(name="flowkit_detach_character_from_project", description="Remove a character's project attachment without deleting the shared character. Requires write. dry_run previews without detaching.")
    async def detach_character_from_project(project_id: str, character_id: str,
                                            dry_run: bool = False) -> dict[str, Any]:
        async def call():
            pid, cid = resource_id(project_id), resource_id(character_id)
            path = f"/api/projects/{pid}/characters/{cid}"
            if dry_run:
                return {"dry_run": True, "method": "DELETE", "path": path}
            return await flowkit_client._request("DELETE", path)
        return await _tool("flowkit_detach_character_from_project", call, scope=WRITE_SCOPE)

    @mcp.tool(name="flowkit_set_active_project", description="Set the shared active-project selection. Requires write. dry_run previews without changing the selection.")
    async def set_active_project(project_id: str, dry_run: bool = False) -> dict[str, Any]:
        async def call():
            body = {"project_id": resource_id(project_id)}
            if dry_run:
                return {"dry_run": True, "method": "PUT", "path": "/api/active-project", "body": body}
            return await flowkit_client._request("PUT", "/api/active-project", json=body)
        return await _tool("flowkit_set_active_project", call, scope=WRITE_SCOPE)

    @mcp.tool(name="flowkit_clear_active_project", description="Clear the explicit shared active-project selection; the backend reverts to its most-recent-project fallback. Does not delete a project. Requires write. dry_run previews only.")
    async def clear_active_project(dry_run: bool = False) -> dict[str, Any]:
        async def call():
            if dry_run:
                return {"dry_run": True, "method": "DELETE", "path": "/api/active-project"}
            return await flowkit_client._request("DELETE", "/api/active-project")
        return await _tool("flowkit_clear_active_project", call, scope=WRITE_SCOPE)
