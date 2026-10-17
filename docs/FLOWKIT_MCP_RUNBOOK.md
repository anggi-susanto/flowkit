# FlowKit MCP operations

Status (2026-10-17): **deployed, read-only credential enabled** at `https://flowcommand.gunamaya.id/mcp/`. SSH connectivity recovered. Container health, PostgreSQL/storage readiness, extension reconnection, local/public SDK initialize and 33-tool discovery, list_models, read-only mutation denial, unauthenticated/invalid-token rejection, foreign-Origin rejection, and existing FlowCommand Basic Auth isolation passed. No billable media was submitted.

Deployment backup: `~/flowcommand-backups/20261017T065019Z/` on `10.0.0.23`; rollback image: `flowcommand-rollback:20261017T065019Z`. Source, previous environment, and runtime were backed up before deployment. Dedicated read-only credential is stored server-side at `~/flowcommand/.mcp-reader-token` (mode 600), never in this repository. Existing Compose ports, PostgreSQL/MinIO credentials, and worker credentials were preserved. No proxy configuration was changed; the existing proxy already forwards MCP correctly.

**Outstanding security finding:** the existing public proxy exposes legacy `/api/models` without authentication (HTTP 200 confirmed before deployment). MCP credentials do not secure legacy REST routes. Restrict legacy REST through a reviewed proxy allowlist or application authentication before treating this host as tenant-safe. Read-only MCP activation does not resolve that pre-existing exposure.

## Transport and startup

Install `requirements.txt` and `requirements-mcp.txt`. The tested SDK is MCP Python 1.30.0; 2.x is not supported by this FastMCP adapter.

The primary transport is authenticated, stateless Streamable HTTP mounted by `agent.main:app`. The main application's lifespan explicitly runs the SDK session manager. Stateless requests authenticate independently, so there is no cross-token session ownership ambiguity. Jobs remain asynchronous and durable in the existing FlowKit queues.

`/mcp` redirects to `/mcp/` with HTTP 307. Configure clients with the canonical trailing slash unless they follow redirects while preserving Authorization and POST bodies.

```dotenv
FLOWKIT_MCP_ENABLED=1
# Placeholder only. Generate real credentials outside tracked files.
FLOWKIT_MCP_TOKENS=reader=REPLACE_WITH_RANDOM_SECRET:read,operator=REPLACE_WITH_ANOTHER_SECRET:read+write+generate+remote_jobs
FLOWKIT_MCP_ALLOWED_HOSTS=flowcommand.gunamaya.id,127.0.0.1,127.0.0.1:*
FLOWKIT_MCP_ALLOWED_ORIGINS=https://flowcommand.gunamaya.id
FLOWKIT_MCP_MAX_BODY_BYTES=1048576
FLOWKIT_MCP_REQUESTS_PER_MINUTE=120
FLOWKIT_MCP_WAIT_MAX_S=120
```

The actual `.env` must remain ignored and mode 600. Never log its contents. The MCP credentials are independent of worker tokens and the internal FlowCommand Basic Auth client. The latter must already be configured for remote-image tools.

Named format: `subject=secret:scope+scope`, entries separated by commas. Keep the subject stable during credential rotation to preserve remote-job ownership. The legacy `secret:scope+scope` format also works, deriving a stable subject from the token digest; changing that secret changes its subject. Subject names are public audit identifiers, not secrets. Never assign the same subject to unrelated callers. Restart to apply token rotation/revocation. Remove old credentials after clients switch.

Scopes:

- `read`: discovery and shared FlowKit project/request inspection.
- `write`: authoring, no generation permission.
- `generate`: enqueue media/review/upscale operations. Composite character creation with reference generation requires both `write` and `generate`.
- `remote_jobs`: submit/status/download only remote image jobs owned by this subject; does not require `read`.
- `admin`: all scopes, including global model configuration. Remote-job ownership still applies.

**Shared workspace:** core FlowKit authoring/read tools operate in a shared trusted workspace. They do not provide per-project tenant isolation. Only the FlowCommand image-job bridge has MCP-subject ownership isolation. Do not issue these scopes to mutually untrusted tenants.

Rate limits are per subject, per process, and include protocol discovery/initialization; all scopes share a budget. Configure an edge limit for unauthenticated requests, concurrent connections, slow uploads, and aggregate limits across replicas. Body buffering is bounded to 1 MiB by default. Image output is bounded to 20 MiB.

## Public reverse proxy

Do not proxy the entire unauthenticated legacy REST API to the internet merely to enable MCP. Public exposure must be an explicit allowlist: `/mcp/` plus existing authenticated FlowCommand client/worker routes needed by the current deployment. Keep `/api/projects`, `/api/requests`, `/api/models`, docs/OpenAPI, and legacy Flow operations private or protected by separate authentication. Do not change existing worker routing without reviewing its consumers.

Suggested dedicated MCP location (adapt to the existing Nginx Proxy Manager host, do not replace the whole host config):

```nginx
location = /mcp {
    return 307 /mcp/;
}
location /mcp/ {
    proxy_pass http://flowcommand:8100;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 180s;
    client_max_body_size 1m;
}
```

Preserve Accept, Content-Type, Origin, and MCP protocol headers. Do not configure wildcard browser origins. SDK validates Host/Origin. Native MCP clients without Origin are supported. GET/DELETE may return 405 in stateless mode; that is not a transport failure.

Remote client configuration (exact keys vary by client):

```json
{
  "mcpServers": {
    "flowkit": {
      "type": "http",
      "url": "https://flowcommand.gunamaya.id/mcp/",
      "headers": {"Authorization": "Bearer ${FLOWKIT_MCP_TOKEN}"}
    }
  }
}
```

## Tools and contracts

33 tools currently share a registry:

- Health, models, materials, projects/get-project, videos, scenes, characters, requests/get-request, provider status, credit status.
- Project create/update, character create/build/update/attach/reference generation, video create, scene create/update, get-active-project.
- Scene image/video generation and regeneration, video batch, video review, upscale, bounded request wait.
- FlowCommand remote image submit/status/output.
- Admin model update.

`list_videos` requires project_id; `list_scenes` requires video_id. Resource IDs must be UUIDs. Authoring dry-runs validate payloads and do not mutate. Generation dry-runs may read dependencies but never enqueue. Entity locations generate landscape references; other entities use portrait. Scene references must exist as UUIDs. Frame video/upscale submission requires the preceding media UUID.

All media submissions use existing `/api/requests/batch`, even a single request. Generation respects backend scheduling/throttling; MCP adds no worker or generation queue. Regeneration previews disclose downstream cascade. Batch accepts 1–100 distinct scene IDs, validates all dependencies before submission, and reports per-scene request IDs returned by the backend.

Remote idempotency keys are hashed with the MCP subject so unrelated callers cannot collide through the shared backend Basic Auth identity. Ownership is persisted in `mcp_remote_job` in the existing runtime SQLite database and cannot be overwritten. Back up the runtime volume: losing the ledger loses MCP access to historical remote jobs. Use an idempotency key for retries: a crash after backend acceptance but before ledger recording can be recovered by resubmitting with the same subject/key. Calls without a key cannot be recovered automatically. Remote output returns MCP image content, never Basic credentials.

Audit events record tool/subject/outcome/duration, not arguments or credential material. Generation previews and early input/scope failures are not all emitted as audit events yet; use proxy access logs for transport coverage. API errors omit upstream response bodies. Do not log Authorization at the proxy.

## Deliberate limitations / remaining work

- No per-request image_model override: explicitly rejected, not silently ignored. Use admin global settings or existing direct REST workflows privately. Project video_model_family selects the existing Veo/Omni worker route.
- No generic N video variants, direct Omni duration/resolution tool, or tenant-specific FlowKit project ownership.
- Active project is read-only through MCP; upstream persistence writes into the source tree and is incompatible with the read-only production image. A safe runtime-backed setter needs upstream work first.
- Queue batch insertion is not transactional and its deduplication is not concurrency-safe. A backend timeout can hide partially accepted requests; inspect list_requests before retrying. MCP does not claim exactly-once core queue submission or structured partial-failure results for backend transport failures.
- Provider readiness, tier enforcement, and actual model support ultimately remain backend responsibilities. Discovery is available; dependency validation is not a guarantee that the external provider will accept a job.
- Review supports `light`/`deep`. Run light review before upscale and repair failing scenes (score below 7.5, at most two review/regenerate cycles). MCP does not yet persist/enforce a review approval gate automatically.
- Admin global model writes retain upstream filesystem restrictions in a read-only image. Do not grant admin to production clients until runtime model configuration is writable and tested.
- No distributed quotas, complete metrics/alerts, or automatic retention policy yet. Keep conservative credentials and edge limits.

## Verification and rollout

1. Back up runtime data and current image/config. Inspect existing proxy allowlist before enabling MCP.
2. Generate dedicated credentials securely; set `.env` without printing tokens. Preserve the existing PostgreSQL/MinIO/worker values.
3. Build and recreate only the app: `docker compose -f docker-compose.flowcommand.yml up -d --build flowcommand`.
4. Check `/ready`, authenticated FlowCommand job status, and existing extension connection/worker claims before touching proxy settings.
5. Test `/mcp/` without auth (401), invalid token (401), invalid Host/Origin (421/403), initialization and tools/list using the official SDK, read-only mutation denial, and owner isolation.
6. Test a read-only real tool such as list_models. Do not submit billable media as a deployment smoke test without explicit approval.
7. Verify public canonical URL and streaming through NPM, then compare workers/queue behavior with the pre-deployment baseline.
8. Roll back by setting `FLOWKIT_MCP_ENABLED=0` and recreating the app, or restoring the previous image. Existing queues and remote jobs must remain intact.

Local regression command:

```bash
uv run --with-requirements requirements.txt --with-requirements requirements-mcp.txt \
  --with 'pytest>=8' --with 'pytest-asyncio>=0.23' python -m pytest \
  tests/unit/test_mcp_surface.py tests/unit/test_mcp_remote_jobs.py tests/unit/test_mcp_security.py \
  tests/unit/test_remote_dispatcher.py tests/unit/test_remote_hardening.py \
  tests/unit/test_remote_jobs_api.py tests/unit/test_remote_observability.py -q
node tests/extension_mv3_bootstrap.test.cjs
```

Optional local wrapper: `python -m agent.mcp.server --transport stdio`. It shares the registry and defaults to `read`; explicit local scopes use `FLOWKIT_MCP_STDIO_SCOPES=read,write,generate`. The CLI intentionally cannot launch an unguarded HTTP server.
