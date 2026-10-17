"""Image import and explicit media binding for remote MCP clients."""
from __future__ import annotations

import base64
import binascii
import io
import warnings
from typing import Any, Literal
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000


def decode_image(image_base64: str, mime_type: str) -> bytes:
    if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError("Only PNG, JPEG, and WebP images are supported")
    if len(image_base64) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError("Image exceeds 8 MiB; resize or compress before uploading")
    try:
        data = base64.b64decode(image_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("image_base64 must contain raw base64 image bytes, not a path, URL, or data URI") from exc
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image must contain 1 byte to 8 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                actual = Image.MIME.get(image.format)
                if actual != mime_type:
                    raise ValueError("mime_type does not match the actual image format")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("Image exceeds 25 megapixels")
                if getattr(image, "n_frames", 1) > 1:
                    raise ValueError("Animated images are not supported")
                image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Invalid or unsafe image payload") from exc
    return data


def upload_media_id(result: dict[str, Any]) -> str:
    from agent.mcp.client import resource_id
    try:
        return resource_id(result.get("media_id"))
    except ValueError:
        pass
    # Some Flow responses contain a CAMS handle; only trust a UUID in a media URL.
    def find(value: Any) -> str | None:
        if isinstance(value, dict):
            url = value.get("fifeUrl")
            if isinstance(url, str):
                path = urlparse(url).path
                if "/image/" in path:
                    try:
                        return resource_id(path.split("/image/", 1)[1].split("/", 1)[0])
                    except ValueError:
                        pass
            for nested in value.values():
                found = find(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = find(nested)
                if found:
                    return found
        return None
    found = find(result)
    if not found:
        raise ValueError("Upload returned no UUID media_id; do not use a CAMS handle")
    return found


def register_media_tools(mcp: Any) -> None:
    from agent.mcp.server import (
        READ_SCOPE, WRITE_SCOPE, GENERATE_SCOPE, _require_scope, _tool,
        _request_parent_ids, flowkit_client, resource_id,
    )

    async def ensure_idle(**filters: str) -> None:
        requests = await flowkit_client.requests(**filters)
        if any(row.get("status") in {"PENDING", "PROCESSING"} for row in requests):
            raise ValueError("Media cannot be replaced while related requests are pending/processing; wait for completion")

    async def character_binding(project_id: str, character_id: str, media_id: str) -> dict[str, Any]:
        characters = await flowkit_client.project_characters(project_id)
        if not any(row["id"] == character_id for row in characters):
            raise ValueError("Character must be attached to this project before binding a reference")
        await ensure_idle(character_id=character_id)
        # No caller-controlled URL. Existing signed URL must not describe the old image.
        return {"media_id": media_id, "reference_image_url": None}

    async def scene_binding(project_id: str, scene_id: str, media_id: str,
                            orientation: str, slot: str) -> dict[str, Any]:
        actual_project, _ = await _request_parent_ids(scene_id)
        if actual_project != project_id:
            raise ValueError("Scene does not belong to this project")
        await ensure_idle(scene_id=scene_id)
        prefix = orientation.lower()
        if slot == "end_frame":
            body = {f"{prefix}_end_scene_media_id": media_id}
        else:
            body = {f"{prefix}_image_media_id": media_id, f"{prefix}_image_status": "COMPLETED",
                    f"{prefix}_image_url": None}
        for stage in ("video", "upscale"):
            body.update({f"{prefix}_{stage}_media_id": None, f"{prefix}_{stage}_url": None,
                         f"{prefix}_{stage}_status": "PENDING"})
        return body

    @mcp.tool(name="flowkit_set_character_reference", description="Bind an uploaded UUID image to an attached character without regenerating its appearance. Rejects active character requests. dry_run previews only.")
    async def set_character_reference(project_id: str, character_id: str, media_id: str,
                                      dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)
        async def call():
            pid, cid, mid = map(resource_id, (project_id, character_id, media_id))
            body = await character_binding(pid, cid, mid)
            if dry_run:
                return {"dry_run": True, "character_id": cid, "updates": body}
            return await flowkit_client.update_character(cid, body)
        return await _tool("flowkit_set_character_reference", call, scope=WRITE_SCOPE)

    @mcp.tool(name="flowkit_set_scene_image", description="Use an uploaded image UUID as scene start/end frame. Clears orientation-specific video/upscale assets to prevent stale output. Rejects active scene requests.")
    async def set_scene_image(project_id: str, scene_id: str, media_id: str,
                              orientation: Literal["VERTICAL", "HORIZONTAL"] = "VERTICAL",
                              slot: Literal["start_frame", "end_frame"] = "start_frame",
                              dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)
        async def call():
            pid, sid, mid = map(resource_id, (project_id, scene_id, media_id))
            body = await scene_binding(pid, sid, mid, orientation, slot)
            if dry_run:
                return {"dry_run": True, "scene_id": sid, "updates": body,
                        "cascade": ["scene_video", "scene_upscale"]}
            return await flowkit_client.update_scene(sid, body)
        return await _tool("flowkit_set_scene_image", call, scope=WRITE_SCOPE)

    @mcp.tool(name="flowkit_upload_image", description="Upload caller-supplied raw base64 PNG/JPEG/WebP bytes (max 8 MiB, 25MP) to a project's Flow workspace. Never accepts server paths/URLs. Optionally bind to an attached character and/or scene start frame, preserving the exact original photo. Requires write; dry_run validates without uploading. Pass returned media_id to explicit binding tools when needed.")
    async def upload_image(project_id: str, image_base64: str,
                           mime_type: Literal["image/png", "image/jpeg", "image/webp"] = "image/png",
                           character_id: str | None = None, scene_id: str | None = None,
                           orientation: Literal["VERTICAL", "HORIZONTAL"] = "VERTICAL",
                           dry_run: bool = False) -> dict[str, Any]:
        _require_scope(WRITE_SCOPE)
        async def call():
            pid = resource_id(project_id)
            cid = resource_id(character_id) if character_id else None
            sid = resource_id(scene_id) if scene_id else None
            data = decode_image(image_base64, mime_type)
            project = await flowkit_client.project(pid)
            flow_pid = resource_id(project.get("flow_project_id") or pid)
            # Preflight all targets before the external upload side effect.
            placeholder = "00000000-0000-0000-0000-000000000000"
            if cid:
                await character_binding(pid, cid, placeholder)
            if sid:
                await scene_binding(pid, sid, placeholder, orientation, "start_frame")
            if dry_run:
                return {"dry_run": True, "project_id": pid, "bytes": len(data),
                        "mime_type": mime_type, "character_id": cid, "scene_id": sid,
                        "cascade": ["scene_video", "scene_upscale"] if sid else []}
            extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime_type]
            response = await flowkit_client.post("/api/flow/upload-image", {
                "image_base64": image_base64, "mime_type": mime_type,
                "project_id": flow_pid, "file_name": "reference." + extension,
            })
            mid = upload_media_id(response)
            result = {"media_id": mid, "project_id": pid, "bindings": {}, "errors": {}}
            # Upload cannot be rolled back. Preserve its ID if a later binding fails.
            for target, identifier in (("character", cid), ("scene", sid)):
                if not identifier:
                    continue
                try:
                    if target == "character":
                        body = await character_binding(pid, identifier, mid)
                        await flowkit_client.update_character(identifier, body)
                    else:
                        body = await scene_binding(pid, identifier, mid, orientation, "start_frame")
                        await flowkit_client.update_scene(identifier, body)
                    result["bindings"][target] = identifier
                except Exception:
                    result["errors"][target] = "Upload succeeded but binding failed; retry explicit binding with returned media_id"
            result["partial_failure"] = bool(result["errors"])
            return result
        return await _tool("flowkit_upload_image", call, scope=WRITE_SCOPE)

    @mcp.tool(name="flowkit_get_media", description="Get provider metadata and refreshed signed media URL by UUID. Requires read and a connected Flow extension.")
    async def get_media(media_id: str) -> dict[str, Any]:
        async def call():
            return await flowkit_client._request("GET", f"/api/flow/media/{resource_id(media_id)}")
        return await _tool("flowkit_get_media", call, scope=READ_SCOPE)

    @mcp.tool(name="flowkit_edit_scene_image", description="Queue an image edit using existing scene image/source UUID and the scene image_prompt. Update the scene prompt first; does not call direct provider RPC. dry_run only previews.")
    async def edit_scene_image(scene_id: str, source_media_id: str,
                               orientation: Literal["VERTICAL", "HORIZONTAL"] = "VERTICAL",
                               provider: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        _require_scope(GENERATE_SCOPE)
        async def call():
            sid, mid = resource_id(scene_id), resource_id(source_media_id)
            pid, vid = await _request_parent_ids(sid)
            body = {"type": "EDIT_IMAGE", "scene_id": sid, "project_id": pid, "video_id": vid,
                    "source_media_id": mid, "orientation": orientation}
            if provider:
                body["provider"] = provider
            if dry_run:
                return {"dry_run": True, "request": body, "cascade": ["scene_image", "scene_video", "scene_upscale"]}
            return {"accepted": True, "request": await flowkit_client.create_request(body)}
        return await _tool("flowkit_edit_scene_image", call, scope=GENERATE_SCOPE)
