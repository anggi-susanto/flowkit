# FlowCommand Remote Image Engine — Task Tracker

**Project:** FlowCommand
**Branch:** `research/remote-flowkit`
**Target:** `flowcommand.gunamaya.id`
**Legend:** `[ ]` todo · `[-]` in progress · `[x]` done · `[!]` blocked/decision needed

## Phase 0 — Decisions and safety gates

- [x] Confirm target domain: `flowcommand.gunamaya.id`.
- [x] Confirm Nginx Proxy Manager will proxy to a private server port.
- [x] Confirm existing MinIO endpoint: `https://s3.gunamaya.id`.
- [x] Add `.venv/` to the repository `.gitignore`.
- [x] Audit current FlowKit API, DB queue, worker, extension bridge, and media handling.
- [x] Probe deployment host `10.0.0.23` over SSH.
- [x] Confirm server storage has been remediated; recheck during deployment smoke tests.
- [x] Use existing MinIO bucket `centracast` with object prefix `flowcommand/`.
- [x] Approve use of existing MinIO credentials from the CentraCast environment for the initial deployment; keep them server-side and schedule later least-privilege hardening.
- [x] Select PostgreSQL for deployment: host `10.0.0.15`, database `flowcommand`.
- [x] Transfer PostgreSQL and MinIO variables from CentraCast `.env` into the local FlowKit `.env` without committing them.
- [x] Confirm PostgreSQL connectivity to `10.0.0.15/flowcommand` using the transferred commented-block credentials.
- [x] Generate FlowCommand client credentials and worker token for initial static-token deployment; document rotation as follow-up.

## Phase 1 — Backend job domain

- [x] Add PostgreSQL `image_jobs` schema/table initialization.
- [x] Add job status constants and Pydantic request/response models.
- [x] Add repository functions for create, get-owned, claim, heartbeat, complete, fail, and expired-lease recovery.
- [x] Add conditional/atomic claim transaction using `FOR UPDATE SKIP LOCKED`.
- [x] Add idempotency key handling scoped to client identity.
- [x] Add lease token hashing and expiry handling.
- [x] Add periodic expired-lease recovery: requeue before attempt 3, terminal-fail at/after attempt 3.
- [x] Add periodic retention cleanup for terminal jobs and their `flowcommand/` MinIO objects.

## Phase 2 — Public API and authentication

- [x] Add Basic Auth dependency for external clients.
- [x] Add client credential configuration in ignored `.env`; production rotation procedure remains a deployment runbook task.
- [x] Add `POST /api/v1/image-jobs` returning `202` and `job_id`.
- [x] Add per-job aspect ratio (`1:1`, `9:16`, `16:9`, `3:4`, `4:3`) persisted to PostgreSQL and passed to Google Flow.
- [x] Add `GET /api/v1/image-jobs/{job_id}` with ownership enforcement.
- [x] Add stable `{error: {code, message}}` response format and validation handling for `/api/v1`.
- [x] Add in-process sliding-window rate limits and prompt/body/image size limits.
- [x] Replace production wildcard CORS with `FLOWCOMMAND_CORS_ORIGINS` allowlist.
- [ ] Add API documentation examples and curl smoke tests.

## Phase 3 — MinIO integration

- [x] Add S3-compatible storage dependency.
- [x] Add storage configuration for endpoint, region, bucket `centracast`, prefix `flowcommand/`, path-style, and existing server-side credentials.
- [x] Implement `ObjectStorage` adapter with put/get operations.
- [x] Add upload validation: content type, magic bytes, max bytes, and checksum.
- [x] Add authenticated upload endpoint using the active job lease.
- [x] Store object key and checksum on successful upload.
- [x] Generate image URL through authenticated API proxy.
- [x] Add mocked storage/API contract tests.
- [ ] Run one staging MinIO upload and retention-cleanup test with runtime credentials.

## Phase 4 — Remote worker API

- [x] Add worker authentication separate from client Basic Auth.
- [x] Add `POST /api/v1/worker/jobs/claim`.
- [x] Add `POST /api/v1/worker/jobs/{job_id}/heartbeat`.
- [x] Add upload/finalize endpoint.
- [x] Add `POST /api/v1/worker/jobs/{job_id}/fail`.
- [x] Enforce lease token and worker ownership on every worker mutation.
- [x] Return the worker payload with `idjob`, `prompt`, and `image_submit_url`.
- [x] Run concurrent PostgreSQL claim test against `flowcommand`; two workers claimed distinct jobs and test jobs were cleaned up.

## Phase 5 — Extension remote mode

- [x] Add persisted remote worker controller configuration plumbing (feature flag remains disabled until Flow dispatch is complete).
- [x] Add worker token storage and retrieval plumbing.
- [x] Enable remote HTTPS claim polling with single-job backpressure.
- [x] Add job state machine: claim → process → server-side upload → complete/fail.
- [x] Reuse existing Google Flow RPC submission path through a server-side dispatch adapter bound to the claiming browser.
- [x] Enforce maximum three total Flow generation attempts per job through PostgreSQL attempt count.
- [x] Store the generated image server-side in MinIO; extension never receives MinIO credentials.
- [x] Return a terminal `FAILED` state after the third failed Flow attempt.
- [x] Handle network failure/browser loss/lease expiry through worker-bound dispatch errors, lease expiry recovery, and stale-token rejection; browser E2E remains pending.
- [x] Add `flowcommand.gunamaya.id` host permissions to MV3 manifest.
- [x] Route remote extension bridge to `wss://flowcommand.gunamaya.id/ws/extension` from controller configuration.
- [x] Keep local mode as the default; remote controller mode activates only when persisted extension config enables it.
- [x] Run existing MV3 extension bootstrap regression test after remote config plumbing.
- [ ] Add extension unit/integration tests for remote claim/generate protocol with a mocked controller.

## Phase 6 — Runtime and deployment

- [x] Add Docker Compose production packaging with restart policy, read-only root filesystem, tmpfs, runtime volumes, and healthcheck.
- [x] Deploy FlowCommand private API to `10.0.0.23:8100` and WebSocket bridge to `:9222`.
- [x] Bind service and verify Docker healthcheck is healthy.
- [x] Configure local runtime PostgreSQL connection to `10.0.0.15`, database `flowcommand`, using `.env` secrets.
- [x] Configure and verify NPM proxy: `flowcommand.gunamaya.id` → `10.0.0.23:8100`.
- [x] Configure and verify NPM custom WebSocket location: `/ws/extension` → `10.0.0.23:9222`, preserving query string for worker-token authentication.
- [x] Keep `.env` ignored by Git and set local permissions to `600`; replicate secrets through server runtime secret injection during deployment.
- [x] Add and verify FlowCommand `/ready` against live PostgreSQL and MinIO.
- [x] Add startup configuration validation for remote PostgreSQL, auth credentials, HTTPS public URL, and storage prefix.
- [x] Add structured `/api/v1` logs, `X-Request-ID`, and Prometheus-style `/metrics` counters.
- [x] Add queue/worker/failure/lease/retention counters to `/metrics`; richer external monitoring remains optional.
- [x] Add application retention cleanup for terminal DB rows and FlowCommand MinIO objects; infrastructure backup policy remains pending.
- [x] Recheck host capacity before deployment: 20 GB free; image retention cleanup is configured.

## Phase 7 — End-to-end validation

- [x] Run mocked public API contract tests: submit, auth, claim, status, upload validation, completion.
- [x] Submit authenticated prompt to deployed private API and confirm immediate `job_id`; smoke job was removed.
- [x] Confirm immediate `job_id` response from deployed private API.
- [x] Verify public HTTPS `/health`, `/ready`, `/metrics`, Basic Auth, and authenticated WSS handshake.
- [ ] Confirm extension claims the job.
- [!] Add `FLOW_PROJECT_ID` to server runtime; Google Flow generation E2E cannot start until the pinned Flow project UUID is configured.
- [ ] Confirm Google Flow generation succeeds through the bound signed-in browser.
- [ ] Confirm server dispatcher downloads the generated image.
- [ ] Confirm object exists in MinIO under `centracast/flowcommand/`.
- [ ] Confirm status returns `COMPLETED` and usable `image_url`.
- [ ] Force one Flow failure and confirm retry.
- [ ] Force three Flow failures and confirm `FAILED`.
- [ ] Kill extension during processing and confirm lease recovery.
- [ ] Attempt cross-client status access and confirm denial.
- [ ] Test duplicate submit with same idempotency key.
- [ ] Test service restart while jobs are queued and processing.
- [ ] Verify NPM TLS, request size, timeout, and upload behavior.

## Suggested implementation order

1. Confirm PostgreSQL connectivity/secrets and recheck host disk.
2. Build schema/repository/lease tests before routes.
3. Implement storage adapter and upload validation.
4. Implement public submit/status API with auth and idempotency.
5. Implement worker claim/heartbeat/fail/upload API.
6. Add extension remote mode behind a feature flag.
7. Add deployment packaging and NPM smoke test.
8. Run end-to-end and failure-mode validation.

## Definition of done

- An authenticated external caller can submit a prompt and receive a `job_id` immediately.
- A remote extension can claim and process jobs without localhost dependencies.
- The extension never retries Google Flow more than three times for one job.
- Successful images are server-owned in MinIO and returned as stable/protected URLs.
- Failed jobs are visible as `FAILED` with sanitized diagnostics.
- Jobs survive worker disconnects and service restarts without duplicate completion.
- NPM can proxy the service at `flowcommand.gunamaya.id`.
- Automated tests cover auth, ownership, leases, retries, storage, and lifecycle transitions.
