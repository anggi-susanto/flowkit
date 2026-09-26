import pytest

from agent.services.flow_client import FlowClient
from agent.services import remote_dispatcher


class FakeSocket:
    pass


def test_remote_worker_binding_is_removed_when_socket_disconnects():
    client = FlowClient()
    socket = FakeSocket()
    client.set_extension(socket)
    client.bind_remote_worker("chrome-a", socket)
    assert client.remote_worker_extension("chrome-a") is socket
    client.clear_extension(socket)
    assert client.remote_worker_extension("chrome-a") is None


@pytest.mark.asyncio
async def test_dispatch_persists_generated_flow_image(monkeypatch):
    transitions = []

    class Client:
        def remote_worker_extension(self, worker_id):
            assert worker_id == "chrome-a"
            return object()

        async def generate_images(self, prompt, project_id, **kwargs):
            assert prompt == "sunrise"
            assert kwargs["count"] == 1
            assert kwargs["aspect_ratio"] == "16:9"
            assert kwargs["preferred_extension"] is not None
            return {"status": 200, "data": {"media": [{"image": {"generatedImage": {"fifeUrl": "https://images.example/result.png"}}}]}}

    async def transition(*args):
        transitions.append(args[3])
        return {"id": "job-1"}

    async def complete(*args):
        return {"id": "job-1", "status": "COMPLETED"}

    class Response:
        content = b"\x89PNG\r\n\x1a\nimage"
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            pass

    class HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            assert url == "https://images.example/result.png"
            return Response()

    class Stored:
        key = "flowcommand/images/job-1.png"
        content_type = "image/png"
        content_length = 13

    monkeypatch.setattr(remote_dispatcher, "get_flow_client", lambda: Client())
    monkeypatch.setattr(remote_dispatcher, "FLOW_PROJECT_ID", "550e8400-e29b-41d4-a716-446655440000")
    monkeypatch.setattr(remote_dispatcher.remote_job_store, "transition", transition)
    monkeypatch.setattr(remote_dispatcher.remote_job_store, "complete", complete)
    monkeypatch.setattr(remote_dispatcher.object_storage, "put_image", lambda *args: _stored(Stored()))
    monkeypatch.setattr(remote_dispatcher.httpx, "AsyncClient", lambda **kwargs: HttpClient())

    result = await remote_dispatcher.dispatch_image_job({"id": "job-1", "prompt": "sunrise", "aspect_ratio": "16:9"}, "chrome-a", "lease")
    assert result["status"] == "COMPLETED"
    assert transitions == ["PROCESSING", "UPLOADING"]


async def _stored(value):
    return value
