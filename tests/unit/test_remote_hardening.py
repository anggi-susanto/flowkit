import pytest

from agent import config
from agent.services.object_storage import ObjectStorage
from agent.services.remote_jobs import RemoteJobStore


def test_storage_refuses_delete_outside_flowcommand_prefix():
    assert ObjectStorage.image_key("job-1", "image/png").startswith("flowcommand/images/")
    # Delete itself is guarded before any client call.
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(ObjectStorage().delete("centracast/other/job.png"))


def test_config_validation_rejects_http_public_url(monkeypatch):
    values = {
        "FLOWCOMMAND_DATABASE_URL": "postgresql://user:pass@db/flowcommand",
        "FLOWCOMMAND_PUBLIC_BASE_URL": "http://flowcommand.example",
        "FLOWCOMMAND_CLIENT_USERNAME": "client",
        "FLOWCOMMAND_CLIENT_PASSWORD": "x" * 32,
        "FLOWCOMMAND_WORKER_TOKEN": "x" * 48,
        "FLOWCOMMAND_S3_ACCESS_KEY": "access",
        "FLOWCOMMAND_S3_SECRET_KEY": "secret",
        "FLOWCOMMAND_S3_PREFIX": "flowcommand/",
    }
    for name, value in values.items():
        monkeypatch.setattr(config, name, value)
    with pytest.raises(RuntimeError, match="HTTPS"):
        config.validate_flowcommand_config()


def test_store_requires_connected_pool():
    store = RemoteJobStore("postgresql://user:pass@db/flowcommand")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio
        asyncio.run(store.recover_expired_leases())
