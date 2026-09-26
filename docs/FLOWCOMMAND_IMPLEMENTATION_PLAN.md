# FlowCommand Remote Image Engine — Implementation Plan

**Status:** Planning
**Branch:** `research/remote-flowkit`
**Target domain:** `flowcommand.gunamaya.id`
**Last updated:** 2026-09-26

## 1. Objective

Deploy FlowKit as a remote image-generation engine:

1. An external client submits a text prompt using Basic Auth.
2. The API creates a durable asynchronous job and immediately returns `job_id`.
3. A Chrome extension worker pulls jobs from the server.
4. The extension submits the prompt to Google Flow through the existing signed-in browser/RPC bridge.
5. The extension uploads the generated image to a server-owned upload endpoint.
6. The server stores the image in existing S3-compatible MinIO and exposes job status.
7. The client receives `image_url` when the job is complete, or `FAILED` after terminal failure.

This plan keeps the existing project/video workflow intact and adds a dedicated remote image-job path.

## 2. Current capability and constraints

### Reusable capabilities

- FastAPI application and lifecycle in `agent/main.py`.
- Persistent SQLite request queue and worker lifecycle.
- Existing Google Flow `batchexecute` transport in `agent/services/flow_client.py`.
- Chrome extension bridge that runs signed Flow requests inside `flow.google.com`.
- Existing media parsing and Flow media URL refresh logic.

### Missing capabilities

- Public authenticated image-job API.
- Remote worker registration, job pull, lease, heartbeat, and result reporting.
- A prompt-only job model independent of scene/video/project records.
- MinIO/S3-compatible storage adapter.
- Remote extension configuration; current endpoints are loopback-oriented.
- Explicit three-attempt job policy across extension retries.
- Production deployment packaging and service supervision.

## 3. Target architecture

```text
External client
  |
  | HTTPS + Basic Auth
  v
flowcommand.gunamaya.id
  |
  +-- POST /api/v1/image-jobs
  |     -> persist job, return 202 + job_id
  |
  +-- GET /api/v1/image-jobs/{job_id}
  |     -> status, image_url when COMPLETED
  |
  +-- POST /api/v1/worker/jobs/claim
  |     -> atomically lease one queued job
  |
  +-- POST /api/v1/worker/jobs/{job_id}/heartbeat
  +-- POST /api/v1/worker/jobs/{job_id}/complete
  +-- POST /api/v1/worker/jobs/{job_id}/fail
  |
  +-- POST /api/v1/image-jobs/{job_id}/upload
        -> validate image, store in MinIO, mark COMPLETED

Chrome extension worker
  -> claim job
  -> submit prompt to Google Flow via existing RPC bridge
  -> retry Flow generation up to 3 total attempts
  -> upload generated image to server

MinIO: https://s3.gunamaya.id
Bucket: existing `centracast`, isolated under the `flowcommand/` prefix
```

### Why HTTP pull/lease is the default

The current WebSocket bridge is designed for a local agent and an in-memory pending-future map. A remote worker needs durable job ownership and recovery after disconnects. HTTPS pull with a short lease and heartbeat is easier to retry, observe, secure, and operate behind Nginx Proxy Manager. WebSocket may be added later for low-latency wakeups, but it must not be the source of truth.

## 4. Job lifecycle

```text
QUEUED
  -> LEASED       worker claims job with lease_until
  -> PROCESSING   worker starts Google Flow attempt
  -> UPLOADING    image upload is being validated/stored
  -> COMPLETED    MinIO object exists and URL can be served

PROCESSING/LEASED -> QUEUED       lease expires and attempt budget remains
PROCESSING         -> FAILED      three attempts exhausted or terminal error
```

The database is authoritative. Every state transition must be conditional on the current job owner/token to prevent two workers from completing the same job.

### Attempt policy

- Maximum: **3 total Google Flow generation attempts per job**.
- An upload failure after a successful Flow generation must not silently consume a new Flow attempt; it should retry upload separately or become `FAILED` with a distinct error.
- A worker restart or lease expiry must not reset `attempt_count`.
- After attempt 3 fails, the extension calls the failure endpoint and the server marks the job `FAILED`.
- Server-side lease recovery may requeue a job only while `attempt_count < 3`.

## 5. API contract

All public endpoints use HTTPS. External client endpoints use Basic Auth. Worker endpoints use a separate worker credential/token and must not reuse client credentials.

### Submit job

`POST /api/v1/image-jobs`

Request:

```json
{
  "prompt": "A cinematic sunrise over a volcanic valley",
  "aspect_ratio": "16:9",
  "idempotency_key": "client-generated-unique-value"
}
```

Response: `202 Accepted`

```json
{
  "job_id": "uuid",
  "status": "QUEUED",
  "status_url": "/api/v1/image-jobs/{job_id}"
}
```

Validation: non-empty prompt, maximum prompt length, and `aspect_ratio` one of `1:1` (default), `9:16`, `16:9`, `3:4`, or `4:3`.

### Get status

`GET /api/v1/image-jobs/{job_id}`

Pending example:

```json
{
  "job_id": "uuid",
  "status": "PROCESSING",
  "attempt_count": 1
}
```

Completed example:

```json
{
  "job_id": "uuid",
  "status": "COMPLETED",
  "attempt_count": 1,
  "image_url": "https://flowcommand.gunamaya.id/api/v1/image-jobs/{job_id}/image"
}
```

Failed example:

```json
{
  "job_id": "uuid",
  "status": "FAILED",
  "attempt_count": 3,
  "error": "google_flow_generation_failed"
}
```

Job lookup must be scoped to the authenticated client. Do not rely on UUID secrecy alone.

### Claim job

`POST /api/v1/worker/jobs/claim`

Response `200` with a job, or `204 No Content` when the queue is empty:

```json
{
  "job_id": "uuid",
  "prompt": "...",
  "image_submit_url": "https://flowcommand.gunamaya.id/api/v1/image-jobs/{job_id}/upload",
  "attempt_count": 1,
  "lease_token": "opaque-token",
  "lease_expires_at": "2026-09-26T00:00:00Z"
}
```

### Heartbeat

`POST /api/v1/worker/jobs/{job_id}/heartbeat`

Requires the lease token. Extends the lease while Flow is processing.

### Complete/upload

Preferred flow:

1. Extension sends `PUT` or `POST` multipart image to `image_submit_url`.
2. Server authenticates the lease token.
3. Server validates content type, magic bytes, size, and checksum.
4. Server writes the object to MinIO.
5. Server atomically marks job `COMPLETED`.

The extension never receives MinIO credentials.

### Fail

`POST /api/v1/worker/jobs/{job_id}/fail`

```json
{
  "lease_token": "opaque-token",
  "attempt": 3,
  "error_code": "FLOW_GENERATION_FAILED",
  "error_message": "sanitized diagnostic"
}
```

The server rejects stale tokens and prevents a fourth generation attempt.

## 6. Data model

Add a dedicated `image_job` table rather than changing the existing scene-bound `request` table.

Recommended fields:

- `id` UUID primary key
- `client_id` authenticated client identity
- `prompt` text
- `status` enum/string: `QUEUED`, `LEASED`, `PROCESSING`, `UPLOADING`, `COMPLETED`, `FAILED`
- `attempt_count` integer, default `0`
- `worker_id` nullable
- `lease_token_hash` nullable
- `lease_until` nullable
- `storage_key` nullable
- `content_type`, `content_length`, `sha256` nullable
- `image_url` or URL-generation metadata nullable
- `error_code`, `error_message` nullable
- `idempotency_key` nullable with unique `(client_id, idempotency_key)`
- `created_at`, `updated_at`, `completed_at`

Required indexes:

- `(status, created_at)` for queue claims
- `(lease_until, status)` for expired lease recovery
- `(client_id, id)` for ownership checks
- unique client/idempotency key

PostgreSQL is the target database for the deployed service. Existing deployment target: host `10.0.0.15`, database `flowcommand`. The PostgreSQL variable names are present but commented out in the CentraCast `.env`; copy the values into the FlowCommand runtime secret environment without committing them. SQLite remains useful only for isolated local tests.

## 7. MinIO storage design

Existing S3-compatible service:

- Endpoint: `https://s3.gunamaya.id`
- Region: `eu-west-2`
- Path-style endpoint: enabled
- Existing bucket: `centracast`
- FlowCommand object prefix: `flowcommand/`

Production recommendation:

- Keep using the existing `centracast` bucket.
- Isolate all objects under `flowcommand/images/{job_id}.png` and enforce the prefix in the storage adapter.
- For the initial deployment, use the existing MinIO credentials from the CentraCast environment as explicitly approved. Do not expose them to the extension or commit them. A later hardening task should replace them with a prefix-scoped key if the MinIO policy supports it.
- Required server permissions: `PutObject`, `GetObject`, `HeadObject`; optionally `DeleteObject` for retention cleanup.
- Store object key and checksum as canonical job data.
- Serve through an authenticated API proxy or short-lived signed URL.
- Do not persist Google Flow signed URLs as the final public result.

Python dependency candidate: `boto3` or `aioboto3`. Keep storage calls behind an adapter so tests do not require a live MinIO service.

## 8. Extension changes

Current limitations:

- `extension/background.js` uses hard-coded loopback endpoints.
- `extension/manifest.json` only grants loopback host access.
- Existing callback/WS flow assumes a local agent and does not provide durable remote job ownership.

Required changes:

1. Add configurable controller base URL, defaulting to local development only.
2. Add a separate worker token/credential store; never embed MinIO secrets.
3. Add a polling loop with backoff for `POST /api/v1/worker/jobs/claim`.
4. For each claimed job, run Flow generation with exactly three total attempts.
5. Send heartbeat while Flow is processing.
6. Upload the resulting image to `image_submit_url`.
7. Report terminal failure after attempt 3.
8. Handle browser sleep, network errors, expired leases, and stale lease tokens.
9. Add HTTPS host permissions for `flowcommand.gunamaya.id`.
10. Preserve the existing local RPC path during migration behind a feature flag.

## 9. Deployment shape

Nginx Proxy Manager remains the public TLS reverse proxy.

```text
NPM / public HTTPS
  flowcommand.gunamaya.id
          |
          v
10.0.0.23:8100  FlowCommand API
```

The existing browser bridge remains a separate WebSocket listener on port `9222`.
Configure an NPM custom location so `wss://flowcommand.gunamaya.id/ws/extension`
forwards to `10.0.0.23:9222` with WebSocket support enabled. The extension derives
this WSS URL from its configured HTTPS controller URL.

The FlowCommand service should bind to `0.0.0.0:8100` inside its container/service but only expose the port on the private host/network as appropriate. NPM should proxy WebSocket upgrade headers only if a WS endpoint is enabled.

MinIO remains external at `s3.gunamaya.id`; do not start another MinIO instance on `10.0.0.23`. Use the existing `centracast` bucket with the `flowcommand/` prefix.
- PostgreSQL is external at `10.0.0.15`, database `flowcommand`; the application should use a connection pool and a dedicated table namespace as needed.

Deployment prerequisites:

- Server storage has been reported as remediated; recheck it during deployment smoke tests before enabling production traffic.
- Add a service definition (Docker Compose or systemd) with restart policy.
- Configure secrets via environment/secret manager, never commit `.env`.
- Add database backup and MinIO retention policy.
- Add health/readiness endpoints and structured logs.

## 10. Security requirements

- TLS at NPM; no public plain HTTP API.
- Basic Auth for external API clients, with hashed/rotatable credentials.
- Separate worker authentication and per-worker identity.
- Client ownership checks on status endpoints.
- Idempotency keys for safe client retries.
- Prompt length, request size, upload size, MIME, and image-dimension limits.
- CORS allowlist; remove wildcard CORS for production.
- Rate limiting on submit, status, claim, and upload endpoints.
- Lease token hashes in the database, not raw tokens.
- Sanitized error messages; detailed errors only in protected server logs.
- No MinIO credentials in extension code or public responses.
- Audit events for submit, claim, attempt, upload, completion, and failure.

## 11. Verification strategy

### Unit tests

- Basic Auth success/failure and client isolation.
- Idempotent submit behavior.
- Atomic job claim under concurrent workers.
- Lease expiry and requeue.
- Heartbeat ownership checks.
- Three-attempt enforcement.
- Upload validation and MinIO adapter behavior with a mocked client.
- Completed/failed status response contract.

### Integration tests

- Submit → claim → Flow attempt mock → upload → status `COMPLETED`.
- Three Flow failures → status `FAILED`.
- Worker disconnect → lease expiry → safe requeue.
- Duplicate submit with same idempotency key.
- Stale worker cannot complete a reassigned job.

### Staging checks

- NPM TLS proxy to port `8100`.
- Chrome extension can reach the domain and authenticate.
- Google Flow session remains in the signed-in browser tab.
- Image persists in MinIO after service restart.
- Signed/proxy image URL works externally and expires as intended.
- Disk, queue, upload, and failure metrics are visible.
