from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api import remote_jobs
from agent.services import remote_security


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(remote_jobs, "FLOWCOMMAND_CLIENT_USERNAME", "client")
    monkeypatch.setattr(remote_jobs, "FLOWCOMMAND_CLIENT_PASSWORD", "secret")
    monkeypatch.setattr(remote_jobs, "FLOWCOMMAND_WORKER_TOKEN", "worker-secret")
    monkeypatch.setattr(remote_jobs, "FLOWCOMMAND_PUBLIC_BASE_URL", "https://flowcommand.gunamaya.id")
    app = FastAPI()
    app.include_router(remote_jobs.router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def auth():
    return ("client", "secret")


@pytest.mark.asyncio
async def test_create_job_returns_async_contract(client, auth, monkeypatch):
    async def create(owner, prompt, aspect_ratio, key):
        assert (owner, prompt, aspect_ratio, key) == ("client", "hello", "1:1", "client-1")
        return {"id": "job-1", "status": "QUEUED"}

    monkeypatch.setattr(remote_jobs.remote_job_store, "create", create)
    response = client.post("/api/v1/image-jobs", auth=auth, json={"prompt": " hello ", "idempotency_key": "client-1"})
    assert response.status_code == 202
    assert response.json() == {"job_id": "job-1", "status": "QUEUED", "status_url": "/api/v1/image-jobs/job-1"}


def test_client_routes_require_basic_auth(client):
    assert client.post("/api/v1/image-jobs", json={"prompt": "hello"}).status_code == 401


def test_job_status_rejects_invalid_uuid(client, auth):
    response = client.get("/api/v1/image-jobs/JOB_ID", auth=auth)
    assert response.status_code == 422


def test_create_job_rejects_unknown_aspect_ratio(client, auth):
    response = client.post("/api/v1/image-jobs", auth=auth, json={"prompt": "hello", "aspect_ratio": "2:1"})
    assert response.status_code == 422


def test_worker_routes_require_separate_token(client):
    response = client.post("/api/v1/worker/jobs/claim", auth=("client", "secret"), json={"worker_id": "chrome-a"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_client_rate_limit_rejects_excess_requests(client, auth, monkeypatch):
    limiter = remote_security.SlidingWindowRateLimiter()
    monkeypatch.setattr(remote_security, "rate_limiter", limiter)
    monkeypatch.setattr(remote_security, "FLOWCOMMAND_CLIENT_RATE_LIMIT", 1)

    async def create(owner, prompt, aspect_ratio, key):
        return {"id": "job-1", "status": "QUEUED"}

    monkeypatch.setattr(remote_jobs.remote_job_store, "create", create)
    assert client.post("/api/v1/image-jobs", auth=auth, json={"prompt": "one"}).status_code == 202
    assert client.post("/api/v1/image-jobs", auth=auth, json={"prompt": "two"}).status_code == 429


@pytest.mark.asyncio
async def test_claim_returns_required_extension_payload(client, monkeypatch):
    async def claim(worker_id):
        assert worker_id == "chrome-a"
        return {
            "id": "job-1", "prompt": "a prompt", "attempt_count": 1,
            "lease_token": "lease", "lease_until": datetime.now(timezone.utc) + timedelta(minutes=5),
        }

    monkeypatch.setattr(remote_jobs.remote_job_store, "claim", claim)
    response = client.post("/api/v1/worker/jobs/claim", headers={"X-Flowcommand-Worker-Token": "worker-secret"}, json={"worker_id": "chrome-a"})
    assert response.status_code == 200
    body = response.json()
    assert body["idjob"] == "job-1"
    assert body["prompt"] == "a prompt"
    assert body["image_submit_url"] == "https://flowcommand.gunamaya.id/api/v1/image-jobs/job-1/upload"
    assert body["lease_token"] == "lease"


@pytest.mark.asyncio
async def test_status_scoped_to_client_and_completed_has_url(client, auth, monkeypatch):
    async def get_owned(job_id, owner):
        assert (job_id, owner) == ("550e8400-e29b-41d4-a716-446655440000", "client")
        return {"id": "job-1", "status": "COMPLETED", "attempt_count": 1, "created_at": None, "completed_at": None}

    monkeypatch.setattr(remote_jobs.remote_job_store, "get_owned", get_owned)
    response = client.get("/api/v1/image-jobs/550e8400-e29b-41d4-a716-446655440000", auth=auth)
    assert response.status_code == 200
    assert response.json()["image_url"] == "https://flowcommand.gunamaya.id/api/v1/image-jobs/job-1/image"


@pytest.mark.asyncio
async def test_upload_rejects_invalid_image_before_storage(client, monkeypatch):
    async def transition(*args):
        raise AssertionError("transition should not run for invalid image")

    monkeypatch.setattr(remote_jobs.remote_job_store, "transition", transition)
    response = client.post(
        "/api/v1/image-jobs/550e8400-e29b-41d4-a716-446655440000/upload",
        headers={"X-Flowcommand-Worker-Token": "worker-secret", "X-Worker-Id": "chrome-a", "X-Lease-Token": "lease"},
        files={"image": ("bad.png", b"not-an-image", "image/png")},
    )
    assert response.status_code == 415


@pytest.mark.asyncio
async def test_upload_completes_job_and_uses_storage(client, monkeypatch):
    calls = []

    async def transition(*args):
        calls.append(("transition", args))
        return {"id": "job-1"}

    async def put_image(job_id, data, content_type, digest):
        calls.append(("put", job_id, content_type))
        return type("Stored", (), {"key": "flowcommand/images/job-1.png", "content_type": content_type, "content_length": len(data)})()

    async def complete(*args):
        calls.append(("complete", args))
        return {"id": "job-1", "status": "COMPLETED"}

    monkeypatch.setattr(remote_jobs.remote_job_store, "transition", transition)
    monkeypatch.setattr(remote_jobs.object_storage, "put_image", put_image)
    monkeypatch.setattr(remote_jobs.remote_job_store, "complete", complete)
    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    response = client.post(
        "/api/v1/image-jobs/550e8400-e29b-41d4-a716-446655440000/upload",
        headers={"X-Flowcommand-Worker-Token": "worker-secret", "X-Worker-Id": "chrome-a", "X-Lease-Token": "lease"},
        files={"image": ("result.png", png, "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert [call[0] for call in calls] == ["transition", "put", "complete"]


@pytest.mark.asyncio
async def test_generate_failure_requeues_before_third_attempt(client, monkeypatch):
    async def transition(*args):
        return {"id": "job-1"}

    async def dispatch(*args):
        raise RuntimeError("Flow unavailable")

    async def fail(*args, **kwargs):
        return {"id": "job-1", "status": "QUEUED", "attempt_count": 1, "error_code": "FLOW_GENERATION_FAILED"}

    monkeypatch.setattr(remote_jobs.remote_job_store, "transition", transition)
    monkeypatch.setattr(remote_jobs, "dispatch_image_job", dispatch)
    monkeypatch.setattr(remote_jobs.remote_job_store, "fail", fail)
    response = client.post(
        "/api/v1/worker/jobs/550e8400-e29b-41d4-a716-446655440000/generate",
        headers={"X-Flowcommand-Worker-Token": "worker-secret"},
        json={"worker_id": "chrome-a", "lease_token": "lease"},
    )
    assert response.status_code == 200
    assert response.json() == {"job_id": "550e8400-e29b-41d4-a716-446655440000", "status": "QUEUED", "attempt_count": 1, "error": "FLOW_GENERATION_FAILED"}


@pytest.mark.asyncio
async def test_unusual_activity_failure_applies_retry_cooldown(client, monkeypatch):
    captured = {}

    async def transition(*args):
        return {"id": "job-1"}

    async def dispatch(*args):
        raise RuntimeError("PUBLIC_ERROR_UNUSUAL_ACTIVITY local cooldown active")

    async def fail(*args, **kwargs):
        captured.update(kwargs)
        return {"id": "job-1", "status": "QUEUED", "attempt_count": 1, "error_code": "FLOW_GENERATION_FAILED"}

    monkeypatch.setattr(remote_jobs.remote_job_store, "transition", transition)
    monkeypatch.setattr(remote_jobs, "dispatch_image_job", dispatch)
    monkeypatch.setattr(remote_jobs.remote_job_store, "fail", fail)
    response = client.post(
        "/api/v1/worker/jobs/550e8400-e29b-41d4-a716-446655440000/generate",
        headers={"X-Flowcommand-Worker-Token": "worker-secret"},
        json={"worker_id": "chrome-a", "lease_token": "lease"},
    )
    assert response.status_code == 200
    assert captured["retry_after_seconds"] == remote_jobs.FLOWCOMMAND_UNUSUAL_ACTIVITY_RETRY_SECONDS
