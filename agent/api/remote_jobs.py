from __future__ import annotations

import hashlib
import hmac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from agent.config import (
    FLOWCOMMAND_CLIENT_PASSWORD,
    FLOWCOMMAND_CLIENT_USERNAME,
    FLOWCOMMAND_MAX_IMAGE_BYTES,
    FLOWCOMMAND_MAX_PROMPT_LENGTH,
    FLOWCOMMAND_PUBLIC_BASE_URL,
    FLOWCOMMAND_UNUSUAL_ACTIVITY_RETRY_SECONDS,
    FLOWCOMMAND_WORKER_TOKEN,
)
from agent.models.remote_job import (
    ImageJobAccepted,
    ImageJobCreate,
    ImageJobStatus,
    WorkerClaim,
    WorkerFailure,
    WorkerJob,
    WorkerLease,
)
from agent.services.object_storage import object_storage
from agent.services.remote_jobs import remote_job_store
from agent.services.remote_dispatcher import RemoteDispatchError, dispatch_image_job
from agent.services.remote_security import limit_client, limit_worker
from agent.services.remote_observability import increment

router = APIRouter(prefix="/v1", tags=["flowcommand"])
security = HTTPBasic(auto_error=False)


def _invalid_credentials() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})


async def client_id(credentials: Annotated[HTTPBasicCredentials | None, Depends(security)]) -> str:
    if not FLOWCOMMAND_CLIENT_USERNAME or not FLOWCOMMAND_CLIENT_PASSWORD or credentials is None:
        raise _invalid_credentials()
    valid_user = hmac.compare_digest(credentials.username, FLOWCOMMAND_CLIENT_USERNAME)
    valid_password = hmac.compare_digest(credentials.password, FLOWCOMMAND_CLIENT_PASSWORD)
    if not (valid_user and valid_password):
        raise _invalid_credentials()
    return credentials.username


async def authenticated_client(request: Request, owner: Annotated[str, Depends(client_id)]) -> str:
    await limit_client(request, owner)
    return owner


async def require_worker_token(x_flowcommand_worker_token: Annotated[str | None, Header()] = None) -> None:
    if not FLOWCOMMAND_WORKER_TOKEN or not x_flowcommand_worker_token:
        raise HTTPException(status_code=401, detail="Invalid worker token")
    if not hmac.compare_digest(x_flowcommand_worker_token, FLOWCOMMAND_WORKER_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid worker token")


def _status_payload(job: dict) -> ImageJobStatus:
    image_path = f"/api/v1/image-jobs/{job['id']}/image"
    image_url = f"{FLOWCOMMAND_PUBLIC_BASE_URL}{image_path}" if job["status"] == "COMPLETED" else None
    error = job.get("error_code") if job["status"] == "FAILED" else None
    return ImageJobStatus(
        job_id=str(job["id"]), status=job["status"], attempt_count=job["attempt_count"],
        aspect_ratio=job.get("aspect_ratio") or "1:1",
        image_url=image_url, error=error, created_at=job.get("created_at"), completed_at=job.get("completed_at"),
    )


@router.post("/worker/jobs/{job_id}/generate", dependencies=[Depends(require_worker_token), Depends(limit_worker)])
async def generate_job(job_id: UUID, body: WorkerLease):
    job_id = str(job_id)
    job = await remote_job_store.transition(job_id, body.worker_id, body.lease_token, "PROCESSING")
    if not job:
        raise HTTPException(409, "Job lease is no longer valid")
    try:
        completed = await dispatch_image_job(job, body.worker_id, body.lease_token)
    except Exception as exc:
        message = str(exc)
        cooldown = FLOWCOMMAND_UNUSUAL_ACTIVITY_RETRY_SECONDS if "UNUSUAL_ACTIVITY" in message.upper() else 0
        failed = await remote_job_store.fail(
            job_id, body.worker_id, body.lease_token, "FLOW_GENERATION_FAILED", message,
            retry_after_seconds=cooldown,
        )
        if not failed:
            raise HTTPException(409, "Job lease is no longer valid")
        if failed["status"] == "FAILED":
            increment("jobs_failed_total")
        else:
            increment("jobs_requeued_total")
        return {"job_id": job_id, "status": failed["status"], "attempt_count": failed["attempt_count"], "error": failed.get("error_code")}
    increment("jobs_completed_total")
    image_path = f"/api/v1/image-jobs/{job_id}/image"
    return {"job_id": job_id, "status": completed["status"], "image_url": _public_url(image_path)}


def _public_url(path: str) -> str:
    return f"{FLOWCOMMAND_PUBLIC_BASE_URL}{path}" if FLOWCOMMAND_PUBLIC_BASE_URL else path


def _image_type(data: bytes, declared: str | None) -> str:
    signatures = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"RIFF", "image/webp"))
    detected = next((mime for prefix, mime in signatures if data.startswith(prefix)), None)
    if detected == "image/webp" and data[8:12] != b"WEBP":
        detected = None
    if not detected or (declared and declared != detected):
        raise HTTPException(415, "Upload must be a valid PNG, JPEG, or WebP image")
    return detected


@router.post("/image-jobs", response_model=ImageJobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_image_job(body: ImageJobCreate, owner: Annotated[str, Depends(authenticated_client)]):
    prompt = body.prompt.strip()
    if not prompt or len(prompt) > FLOWCOMMAND_MAX_PROMPT_LENGTH:
        raise HTTPException(422, f"prompt must be between 1 and {FLOWCOMMAND_MAX_PROMPT_LENGTH} characters")
    job = await remote_job_store.create(owner, prompt, body.aspect_ratio, body.idempotency_key)
    increment("jobs_submitted_total")
    job_id = str(job["id"])
    return ImageJobAccepted(job_id=job_id, status=job["status"], status_url=f"/api/v1/image-jobs/{job_id}")


@router.get("/image-jobs/{job_id}", response_model=ImageJobStatus)
async def get_image_job(job_id: UUID, owner: Annotated[str, Depends(authenticated_client)]):
    job = await remote_job_store.get_owned(str(job_id), owner)
    if not job:
        raise HTTPException(404, "Image job not found")
    return _status_payload(job)


@router.get("/image-jobs/{job_id}/image")
async def get_image(job_id: UUID, owner: Annotated[str, Depends(authenticated_client)]):
    job = await remote_job_store.get_owned(str(job_id), owner)
    if not job or job["status"] != "COMPLETED" or not job.get("storage_key"):
        raise HTTPException(404, "Image not found")
    data, content_type = await object_storage.get_image(job["storage_key"])
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "private, max-age=300"})


@router.post("/worker/jobs/claim", response_model=WorkerJob, dependencies=[Depends(require_worker_token), Depends(limit_worker)])
async def claim_job(body: WorkerClaim):
    job = await remote_job_store.claim(body.worker_id)
    if not job:
        increment("worker_empty_claims_total")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    increment("jobs_claimed_total")
    job_id = str(job["id"])
    return WorkerJob(
        idjob=job_id, prompt=job["prompt"], image_submit_url=_public_url(f"/api/v1/image-jobs/{job_id}/upload"),
        aspect_ratio=job.get("aspect_ratio") or "1:1",
        attempt_count=job["attempt_count"], lease_token=job["lease_token"], lease_expires_at=job["lease_until"],
    )


@router.post("/worker/jobs/{job_id}/heartbeat", dependencies=[Depends(require_worker_token), Depends(limit_worker)])
async def heartbeat(job_id: UUID, body: WorkerLease):
    job = await remote_job_store.heartbeat(str(job_id), body.worker_id, body.lease_token)
    if not job:
        raise HTTPException(409, "Job lease is no longer valid")
    return {"job_id": job_id, "status": job["status"], "lease_expires_at": job["lease_until"]}


@router.post("/worker/jobs/{job_id}/fail", dependencies=[Depends(require_worker_token), Depends(limit_worker)])
async def fail_job(job_id: UUID, body: WorkerFailure):
    job = await remote_job_store.fail(str(job_id), body.worker_id, body.lease_token, body.error_code, body.error_message)
    if not job:
        raise HTTPException(409, "Job lease is no longer valid")
    return {"job_id": job_id, "status": job["status"], "attempt_count": job["attempt_count"]}


@router.post("/image-jobs/{job_id}/upload", dependencies=[Depends(require_worker_token), Depends(limit_worker)])
async def upload_image(
    job_id: UUID,
    x_worker_id: Annotated[str, Header()],
    x_lease_token: Annotated[str, Header()],
    image: UploadFile = File(...),
):
    data = await image.read(FLOWCOMMAND_MAX_IMAGE_BYTES + 1)
    if not data or len(data) > FLOWCOMMAND_MAX_IMAGE_BYTES:
        raise HTTPException(413, "Image exceeds upload size limit")
    content_type = _image_type(data, image.content_type)
    job_id = str(job_id)
    job = await remote_job_store.transition(job_id, x_worker_id, x_lease_token, "UPLOADING")
    if not job:
        raise HTTPException(409, "Job lease is no longer valid")
    digest = hashlib.sha256(data).hexdigest()
    stored = await object_storage.put_image(job_id, data, content_type, digest)
    completed = await remote_job_store.complete(job_id, x_worker_id, x_lease_token, stored.key, stored.content_type, stored.content_length, digest)
    if not completed:
        raise HTTPException(409, "Job lease is no longer valid")
    image_path = f"/api/v1/image-jobs/{job_id}/image"
    return {"job_id": job_id, "status": completed["status"], "image_url": f"{FLOWCOMMAND_PUBLIC_BASE_URL}{image_path}"}
