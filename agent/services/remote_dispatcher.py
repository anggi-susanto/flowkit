from __future__ import annotations

import hashlib
from urllib.parse import urlparse

import httpx

from agent.config import FLOW_PROJECT_ID, FLOWCOMMAND_MAX_IMAGE_BYTES
from agent.services.flow_client import get_flow_client
from agent.services.object_storage import object_storage
from agent.services.remote_jobs import remote_job_store


class RemoteDispatchError(RuntimeError):
    pass


def _image_type(data: bytes, declared: str | None) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise RemoteDispatchError(f"Google Flow returned unsupported image content ({declared or 'unknown'})")


async def dispatch_image_job(job: dict, worker_id: str, lease_token: str) -> dict:
    """Generate through the lease owner's Flow tab and persist the output to MinIO."""
    extension = get_flow_client().remote_worker_extension(worker_id)
    if extension is None:
        raise RemoteDispatchError("Worker browser is not connected to the Flow bridge")
    if not FLOW_PROJECT_ID:
        raise RemoteDispatchError("FLOW_PROJECT_ID is not configured")

    processing = await remote_job_store.transition(str(job["id"]), worker_id, lease_token, "PROCESSING")
    if not processing:
        raise RemoteDispatchError("Job lease is no longer valid")

    generated = await get_flow_client().generate_images(
        job["prompt"], FLOW_PROJECT_ID, aspect_ratio=job.get("aspect_ratio") or "1:1", count=1, preferred_extension=extension,
    )
    if generated.get("error") or generated.get("status") != 200:
        raise RemoteDispatchError(generated.get("error") or "Google Flow did not return an image")
    media = (generated.get("data") or {}).get("media") or []
    image_url = (((media[0] if media else {}).get("image") or {}).get("generatedImage") or {}).get("fifeUrl")
    if not isinstance(image_url, str) or urlparse(image_url).scheme not in {"http", "https"}:
        raise RemoteDispatchError("Google Flow returned no usable image URL")

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        response = await client.get(image_url)
        response.raise_for_status()
        data = response.content
    if not data or len(data) > FLOWCOMMAND_MAX_IMAGE_BYTES:
        raise RemoteDispatchError("Generated image exceeds storage size limit")
    content_type = _image_type(data, response.headers.get("content-type"))
    digest = hashlib.sha256(data).hexdigest()
    uploading = await remote_job_store.transition(str(job["id"]), worker_id, lease_token, "UPLOADING")
    if not uploading:
        raise RemoteDispatchError("Job lease is no longer valid")
    stored = await object_storage.put_image(str(job["id"]), data, content_type, digest)
    completed = await remote_job_store.complete(
        str(job["id"]), worker_id, lease_token, stored.key, stored.content_type, stored.content_length, digest,
    )
    if not completed:
        raise RemoteDispatchError("Job lease is no longer valid")
    return completed
