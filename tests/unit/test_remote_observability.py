from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.services import remote_observability


def test_request_context_sets_and_echoes_request_id():
    app = FastAPI()

    @app.middleware("http")
    async def context(request, call_next):
        return await remote_observability.request_context_middleware(request, call_next)

    @app.get("/api/v1/ping")
    async def ping():
        return {"ok": True}

    client = TestClient(app)
    response = client.get("/api/v1/ping", headers={"X-Request-ID": "test-request"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-request"
    assert remote_observability.snapshot_metrics()["http_requests_total"] >= 1


def test_prometheus_output_is_text_format():
    remote_observability.increment("worker_jobs_completed_total")
    rendered = remote_observability.prometheus_metrics()
    assert "flowcommand_worker_jobs_completed_total" in rendered
