from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ImageJobCreate(BaseModel):
    prompt: str = Field(min_length=1)
    aspect_ratio: Literal["1:1", "9:16", "16:9", "3:4", "4:3"] = "1:1"
    idempotency_key: str | None = Field(default=None, max_length=200)


class ImageJobAccepted(BaseModel):
    job_id: str
    status: str
    status_url: str


class ImageJobStatus(BaseModel):
    job_id: str
    status: Literal["QUEUED", "LEASED", "PROCESSING", "UPLOADING", "COMPLETED", "FAILED"]
    attempt_count: int
    aspect_ratio: str = "1:1"
    image_url: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None


class WorkerClaim(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)


class WorkerJob(BaseModel):
    idjob: str
    prompt: str
    aspect_ratio: str
    image_submit_url: str
    attempt_count: int
    lease_token: str
    lease_expires_at: datetime


class WorkerLease(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    lease_token: str = Field(min_length=1)


class WorkerFailure(WorkerLease):
    error_code: str = Field(min_length=1, max_length=80)
    error_message: str = Field(min_length=1, max_length=1000)
