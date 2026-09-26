from __future__ import annotations

import asyncio
from dataclasses import dataclass

import boto3
from botocore.config import Config

from agent.config import (
    FLOWCOMMAND_S3_ACCESS_KEY,
    FLOWCOMMAND_S3_BUCKET,
    FLOWCOMMAND_S3_ENDPOINT,
    FLOWCOMMAND_S3_PATH_STYLE,
    FLOWCOMMAND_S3_PREFIX,
    FLOWCOMMAND_S3_REGION,
    FLOWCOMMAND_S3_SECRET_KEY,
)


@dataclass(frozen=True)
class StoredObject:
    key: str
    content_type: str
    content_length: int


class ObjectStorage:
    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not FLOWCOMMAND_S3_ACCESS_KEY or not FLOWCOMMAND_S3_SECRET_KEY:
                raise RuntimeError("FlowCommand S3 credentials are not configured")
            self._client = boto3.client(
                "s3",
                endpoint_url=FLOWCOMMAND_S3_ENDPOINT,
                region_name=FLOWCOMMAND_S3_REGION,
                aws_access_key_id=FLOWCOMMAND_S3_ACCESS_KEY,
                aws_secret_access_key=FLOWCOMMAND_S3_SECRET_KEY,
                config=Config(s3={"addressing_style": "path" if FLOWCOMMAND_S3_PATH_STYLE else "virtual"}),
            )
        return self._client

    @staticmethod
    def image_key(job_id: str, content_type: str) -> str:
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[content_type]
        return f"{FLOWCOMMAND_S3_PREFIX}images/{job_id}.{extension}"

    async def put_image(self, job_id: str, data: bytes, content_type: str, sha256: str) -> StoredObject:
        key = self.image_key(job_id, content_type)

        def put() -> None:
            self._get_client().put_object(
                Bucket=FLOWCOMMAND_S3_BUCKET,
                Key=key,
                Body=data,
                ContentType=content_type,
                Metadata={"sha256": sha256, "source": "flowcommand"},
            )

        await asyncio.to_thread(put)
        return StoredObject(key=key, content_type=content_type, content_length=len(data))

    async def get_image(self, key: str) -> tuple[bytes, str]:
        def get() -> tuple[bytes, str]:
            response = self._get_client().get_object(Bucket=FLOWCOMMAND_S3_BUCKET, Key=key)
            return response["Body"].read(), response.get("ContentType") or "application/octet-stream"

        return await asyncio.to_thread(get)

    async def delete(self, key: str) -> None:
        if not key.startswith(FLOWCOMMAND_S3_PREFIX):
            raise ValueError("Refusing to delete object outside FlowCommand prefix")

        def delete() -> None:
            self._get_client().delete_object(Bucket=FLOWCOMMAND_S3_BUCKET, Key=key)

        await asyncio.to_thread(delete)

    async def check(self) -> None:
        """Verify the configured bucket is reachable without mutating it."""
        def head() -> None:
            self._get_client().head_bucket(Bucket=FLOWCOMMAND_S3_BUCKET)

        await asyncio.to_thread(head)


object_storage = ObjectStorage()
