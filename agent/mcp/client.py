"""HTTP adapter for FlowKit's existing REST API."""
from __future__ import annotations

import os
import re
from uuid import UUID
from typing import Any

import httpx


class FlowKitAPIError(RuntimeError):
    """Raised when FlowKit returns an error response."""


def resource_id(value: str) -> str:
    """Reject URL syntax before interpolating caller-controlled resource IDs."""
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Resource ID must be a UUID") from exc


class FlowKitClient:
    """Small HTTP adapter shared by every MCP transport.

    MCP is an adapter, not a second implementation of FlowKit's queue or
    provider layer. Reads go through the public API so the same ownership and
    validation rules apply to agents and non-MCP clients.
    """

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or os.environ.get("FLOWKIT_API_BASE_URL", "http://127.0.0.1:8100")).rstrip("/")
        self.timeout = timeout or float(os.environ.get("FLOWKIT_MCP_HTTP_TIMEOUT_S", "20"))

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not re.fullmatch(r"/[a-zA-Z0-9_/-]+", path) or "//" in path:
            raise ValueError("Invalid API path")
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise FlowKitAPIError(f"FlowKit request returned HTTP {response.status_code}")
        return response.json()

    async def get_remote_image_output(self, job_id: str, username: str, password: str) -> tuple[bytes, str]:
        job_id = resource_id(job_id)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, auth=(username, password)) as client:
            async with client.stream("GET", f"/api/v1/image-jobs/{job_id}/image") as response:
                if response.status_code != 200:
                    raise FlowKitAPIError(f"FlowCommand image returned HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").split(";")[0]
                formats = {"image/png": "png", "image/jpeg": "jpeg", "image/webp": "webp"}
                if content_type not in formats:
                    raise FlowKitAPIError("FlowCommand returned an unsupported image type")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 20 * 1024 * 1024:
                        raise FlowKitAPIError("Image exceeds MCP output limit (20 MiB)")
                    chunks.append(chunk)
                return b"".join(chunks), formats[content_type]

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, json=body)

    async def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", path, json=body)

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.get("/health")
        if response.status_code >= 400:
            raise FlowKitAPIError(f"FlowKit health returned {response.status_code}")
        return response.json()

    async def ready(self) -> dict[str, Any]:
        return await self._request("GET", "/ready")

    async def flow_status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/flow/status")

    async def models(self) -> dict[str, Any]:
        return await self._request("GET", "/api/models")

    async def materials(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/materials")

    async def projects(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/projects")

    async def project(self, project_id: str) -> dict[str, Any]:
        project_id = resource_id(project_id)
        return await self._request("GET", f"/api/projects/{project_id}")

    async def videos(self, project_id: str | None = None) -> list[dict[str, Any]]:
        params = {"project_id": project_id} if project_id else None
        return await self._request("GET", "/api/videos", params=params)

    async def scenes(self, video_id: str | None = None) -> list[dict[str, Any]]:
        params = {"video_id": video_id} if video_id else None
        return await self._request("GET", "/api/scenes", params=params)

    async def characters(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/characters")

    async def project_characters(self, project_id: str) -> list[dict[str, Any]]:
        project_id = resource_id(project_id)
        return await self._request("GET", f"/api/projects/{project_id}/characters")

    async def requests(self, **filters: str | None) -> list[dict[str, Any]]:
        params = {key: value for key, value in filters.items() if value}
        return await self._request("GET", "/api/requests", params=params or None)

    async def request(self, request_id: str) -> dict[str, Any]:
        request_id = resource_id(request_id)
        return await self._request("GET", f"/api/requests/{request_id}")

    async def provider_status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/providers/status")

    async def credits_status(self) -> dict[str, Any]:
        data = await self._request("GET", "/api/flow/credits")
        return {
            "live_balance_available": False,
            "tier": (data.get("data") or data).get("userPaygateTier"),
            "source": "configured_default",
            "note": "Google Flow batch transport does not expose a live credit balance.",
        }

    async def create_project(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.post("/api/projects", body)

    async def update_project(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        project_id = resource_id(project_id)
        return await self.patch(f"/api/projects/{project_id}", body)

    async def create_character(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.post("/api/characters", body)

    async def update_character(self, character_id: str, body: dict[str, Any]) -> dict[str, Any]:
        character_id = resource_id(character_id)
        return await self.patch(f"/api/characters/{character_id}", body)

    async def attach_character(self, project_id: str, character_id: str) -> dict[str, Any]:
        project_id, character_id = resource_id(project_id), resource_id(character_id)
        return await self.post(f"/api/projects/{project_id}/characters/{character_id}", {})

    async def create_video(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.post("/api/videos", body)

    async def update_video(self, video_id: str, body: dict[str, Any]) -> dict[str, Any]:
        video_id = resource_id(video_id)
        return await self.patch(f"/api/videos/{video_id}", body)

    async def create_scene(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.post("/api/scenes", body)

    async def update_scene(self, scene_id: str, body: dict[str, Any]) -> dict[str, Any]:
        scene_id = resource_id(scene_id)
        return await self.patch(f"/api/scenes/{scene_id}", body)

    async def create_request(self, body: dict[str, Any]) -> dict[str, Any]:
        requests = await self.create_requests([body])
        return requests[0]

    async def create_requests(self, bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from agent.models.request import RequestCreate
        bodies = [RequestCreate.model_validate(body).model_dump(exclude_none=True) for body in bodies]
        return await self.post("/api/requests/batch", {"requests": bodies})

    async def review_video(self, video_id: str, mode: str = "light") -> dict[str, Any]:
        video_id = resource_id(video_id)
        video = await self._request("GET", f"/api/videos/{video_id}")
        return await self._request("POST", f"/api/videos/{video_id}/review", params={"project_id": video["project_id"], "mode": mode})

    async def remote_image_job(self, body: dict[str, Any], username: str, password: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, auth=(username, password)) as client:
            response = await client.post("/api/v1/image-jobs", json=body)
        if response.status_code >= 400:
            raise FlowKitAPIError(f"FlowCommand image job returned {response.status_code}")
        return response.json()

    async def get_remote_image_job(self, job_id: str, username: str, password: str) -> dict[str, Any]:
        job_id = resource_id(job_id)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, auth=(username, password)) as client:
            response = await client.get(f"/api/v1/image-jobs/{job_id}")
        if response.status_code >= 400:
            raise FlowKitAPIError(f"FlowCommand image job returned {response.status_code}")
        return response.json()


flowkit_client = FlowKitClient()
