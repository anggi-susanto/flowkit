# FlowKit MCP Surface Plan

**Status:** 33 tools implemented; local validation passed; deployed with a read-only credential and public SDK verification on 2026-10-17. See [operations runbook](FLOWKIT_MCP_RUNBOOK.md) for actual contracts, rollout, and remaining limitations.
**Owner:** FlowKit / FlowCommand
**Last updated:** 2026-10-17

## 1. Purpose

Expose FlowKit capabilities as a safe, discoverable [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server so AI clients such as Codex, Claude Code, Cursor, and desktop MCP clients can operate FlowKit without direct database access or knowledge of the Chrome extension protocol.

The MCP layer is an adapter over FlowKit's existing REST API and service contracts. It must not duplicate generation logic, bypass provider controls, or expose operational secrets.

## 2. Goals

- Provide a **remote Streamable HTTP MCP server** as the primary integration.
- Keep local stdio as an optional development and local-only wrapper.
- Expose project, scene, character, model, request, and generation workflows as typed tools.
- Preserve FlowKit's asynchronous request/job model instead of presenting generation as synchronous.
- Support model discovery and controlled selection for image and video generation.
- Make character creation, reference generation, and attachment safe for agents.
- Validate authorization, ownership, and transport behavior locally before remote deployment.

## 3. Non-goals

- Do not expose the Chrome extension WebSocket protocol as MCP.
- Do not expose FlowCommand worker routes: claim, heartbeat, generate, upload, or fail.
- Do not return `FLOWCOMMAND_WORKER_TOKEN`, MinIO/S3 credentials, database URLs, Flow cookies, or callback secrets.
- Do not claim that the batch Flow transport can return a live Google credit balance. It currently returns configured tier information only.
- Do not create a second queue or generation implementation in the MCP server.

## 4. Architecture

### 4.1 Remote Streamable HTTP transport (primary)

```text
MCP client
  │ HTTPS: POST/GET /mcp
  ▼
FlowCommand MCP gateway (flowcommand.gunamaya.id)
  │ internal typed service/API client
  ▼
FlowKit services
  │
  ├── SQLite project/request workflow
  ├── provider layer
  ├── Chrome extension WebSocket bridge
  └── FlowCommand PostgreSQL/MinIO image-job workflow
```

Expose the server at:

```text
https://flowcommand.gunamaya.id/mcp
```

Use the current MCP **Streamable HTTP** transport. This supports ordinary HTTP tool calls and allows the server to stream protocol messages when a connected MCP client supports it. It is the preferred transport for a new remote deployment.

The gateway should call existing REST endpoints or a deliberately small internal service facade. It must not import private worker state directly.

### 4.2 Local stdio transport (development / local-only)

```text
MCP client ── stdio ──> FlowKit MCP server ── HTTP ──> FlowKit API (127.0.0.1:8100)
```

Stdio avoids network exposure and is useful for development, local workflows, and offline testing. It should use the same tool registry and schemas as Streamable HTTP.

### 4.3 Why Streamable HTTP instead of legacy SSE

Do not start a new FlowKit integration on the legacy standalone SSE transport. Streamable HTTP is the current MCP transport and is a better fit for FlowCommand:

- one authenticated `/mcp` endpoint rather than separate legacy SSE/message endpoints;
- normal request/response for short tool calls such as status, models, and CRUD;
- optional protocol streaming/progress delivery when client support is available;
- simpler reverse-proxy, token, observability, and reconnect handling;
- generation remains durable and asynchronous through request/job IDs, so clients can poll even when a streaming connection ends.

Nginx Proxy Manager must preserve HTTP streaming behavior for `/mcp`: disable proxy buffering for this location, use a long read timeout for active streams, preserve `Authorization`, and allow `POST`, `GET`, and `DELETE` methods required by the selected MCP SDK transport.

Remote MCP requires independently scoped MCP credentials and audit logging. It must not reuse the global FlowCommand worker token as a user credential.

## 5. Capability inventory and MCP mapping

### 5.1 Image model selection

FlowKit already supports model discovery and configuration:

- `GET /api/models` returns model configuration.
- `PATCH /api/models` updates configuration and hot-reloads model mappings.
- Image generation accepts `image_model`.
- Current configured image model aliases are:
  - `NANO_BANANA_PRO` → `GEM_PIX_2`
  - `NANO_BANANA_2` → `NARWHAL`
  - `NANO_BANANA_2_LITE` → `HARBOR_SEAL`

MCP mapping:

- `flowkit_list_models` — read-only; return aliases, wire IDs, and defaults.
- `flowkit_generate_scene_image` — uses configured model; explicitly rejects per-request `image_model` until the queue persists it.
- `flowkit_update_models` — admin-only; update model registry.

Unknown syntactically valid Flow image model wire IDs may pass through the FlowKit resolver; MCP should default to aliases and allow raw wire IDs only in an explicit advanced/admin mode.

### 5.2 Video model selection

FlowKit supports two video model families per project:

- `veo` — Veo 3.1 image-to-video batch path.
- `omni_flash` — Omni Flash workflow path, with 4/6/8/10-second durations and text/frame/reference modes.

Model mapping is configured in `agent/models.json` and exposed through `GET/PATCH /api/models`.

MCP should expose stable choices rather than legacy wire keys:

```json
{
  "video_model_family": "omni_flash",
  "mode": "reference_to_video",
  "duration_s": 8,
  "resolution": "720p"
}
```

For Veo, expose a supported quality abstraction such as `lite`, `lite_low_priority`, or `ultra`, then let FlowKit resolve the accepted batch wire model.

### 5.3 Credit monitoring

Existing endpoint: `GET /api/flow/credits`.

The current `batchexecute` path cannot capture a live Google Flow credit-balance RPC. The endpoint returns configured `DEFAULT_PAYGATE_TIER` and identifies this limitation.

MCP tool `flowkit_get_credits_status` must return an explicit structure:

```json
{
  "live_balance_available": false,
  "tier": "PAYGATE_TIER_ONE",
  "source": "configured_default",
  "note": "Google Flow batch transport does not expose a live credit balance."
}
```

Omni Flash duration cost estimates may be shown as informational estimates only, not as a balance deduction or pricing guarantee.

### 5.4 Multi-response / multi-job video generation

FlowKit uses asynchronous requests, provider jobs, and polling. It supports multiple scene requests and batch orchestration. Image generation supports 1–4 variants per request and preserves partial successes/failures.

Video generation is currently best modeled as a collection of scene/provider jobs, not a generic `count=N` multiple-variant video contract. MCP v1 should:

- submit one video request per scene;
- accept a list of scene IDs for controlled batch submission;
- return one request/job record per scene;
- provide a bounded `flowkit_wait_for_job` tool;
- report partial success or partial failure without hiding individual results.

A later enhancement may add an explicit `variant_count` capability only after provider behavior and output storage semantics are defined.

### 5.5 Character building and attachment

FlowKit already supports character/entity project inputs, character CRUD, reference image generation, and name-to-media resolution for image/video conditioning.

Important invariant: scene references must use valid UUID media IDs. `CAMS...` or base64 identifiers are not valid Flow media IDs.

MCP should provide a composite authoring tool:

```text
flowkit_build_character
```

It should:

1. create the character/entity;
2. attach it to the project;
3. optionally submit reference image generation;
4. return character ID and asynchronous request ID;
5. only report `reference_ready` after the generated media ID is a UUID.

Scene image/video generation should automatically resolve attached character names to reference media IDs. Manual attachment tools remain available for correction or advanced workflows.

## 6. MCP tool catalog

### 6.1 Read-only tools (v1)

| Tool | Backing capability | Scope |
|---|---|---|
| `flowkit_health` | `/health`, `/ready`, `/api/flow/status` | `read` |
| `flowkit_list_models` | `GET /api/models` | `read` |
| `flowkit_list_materials` | `GET /api/materials` | `read` |
| `flowkit_list_projects` | projects API | `read` |
| `flowkit_get_project` | projects API | `read` |
| `flowkit_list_videos` | videos API | `read` |
| `flowkit_list_scenes` | scenes API | `read` |
| `flowkit_list_characters` | characters API | `read` |
| `flowkit_list_requests` | requests API | `read` |
| `flowkit_get_request` | requests API | `read` |
| `flowkit_get_provider_status` | `/api/providers/status` | `read` |
| `flowkit_get_credits_status` | `/api/flow/credits` | `read` |
| `flowkit_get_remote_image_job` | FlowCommand public job status | `remote_jobs` |

### 6.2 Authoring tools (v2)

| Tool | Scope | Notes |
|---|---|---|
| `flowkit_create_project` | `write` | Includes material and `video_model_family`. |
| `flowkit_update_project` | `write` | Includes audio policies and video family. |
| `flowkit_create_video` | `write` | Creates an episode/video container. |
| `flowkit_create_scene` | `write` | Adds a scene to a video. |
| `flowkit_update_scene` | `write` | Updates prompts, narrator text, chain type, and character names. |
| `flowkit_build_character` | `write` | Composite character + project attachment workflow. |
| `flowkit_update_character` | `write` | Updates entity metadata. |
| `flowkit_attach_character_to_project` | `write` | Explicit advanced attachment. |
| `flowkit_set_active_project` | `write` | Sets local active project. |
| `flowkit_update_models` | `admin` | Global model registry mutation; disabled by default. |

### 6.3 Generation and review tools (v3)

| Tool | Scope | Asynchronous output |
|---|---|---|
| `flowkit_generate_character_reference` | `generate` | request ID |
| `flowkit_generate_scene_image` | `generate` | request ID(s) |
| `flowkit_generate_scene_video` | `generate` | request ID |
| `flowkit_generate_video_from_references` | `generate` | request ID |
| `flowkit_generate_omni_flash_video` | `generate` | provider job/workflow ID |
| `flowkit_generate_video_batch` | `generate` | one result per scene |
| `flowkit_upscale_scene_video` | `generate` | request ID |
| `flowkit_review_video` | `generate` | review result or request ID |
| `flowkit_wait_for_job` | `read` | bounded poll result |
| `flowkit_submit_remote_image_job` | `remote_jobs` | remote job ID |

All generation tools accept `dry_run: true`. A dry run validates IDs, model, material, aspect, duration, and intended references but submits no job.

## 7. Response contract

Generation tools must never imply completion merely because a request was accepted.

```json
{
  "accepted": true,
  "operation": "scene_image_generation",
  "request_ids": ["..."],
  "status": "PENDING",
  "next_action": {
    "tool": "flowkit_wait_for_job",
    "arguments": {"request_id": "..."}
  }
}
```

Batch responses must preserve each child result:

```json
{
  "accepted": true,
  "results": [
    {"scene_id": "...", "request_id": "...", "status": "PENDING"},
    {"scene_id": "...", "error": "Reference image is missing"}
  ],
  "partial_failure": true
}
```

## 8. Safety and authorization

### 8.1 Scopes

- `read` — inspect state and configuration.
- `write` — create or edit non-generative project resources.
- `generate` — submit image/video/upscale/review generation.
- `remote_jobs` — submit/read client-owned FlowCommand remote image jobs.
- `admin` — model registry and server-wide operations.

### 8.2 Required guardrails

Before a generation submission:

1. Confirm Flow extension or relevant provider is connected.
2. Validate project/video/scene ownership and existence.
3. Validate required reference media IDs are UUIDs.
4. Validate image/video model, aspect, duration, material, and provider constraints.
5. Respect existing concurrency and unusual-activity cooldown controls.
6. Explicitly disclose regeneration cascade impact: image regeneration clears downstream video and upscale outputs.
7. Apply bounded wait timeouts; never poll forever.

The MCP layer must not accept or return worker tokens, callback secrets, browser cookies, cloud credentials, or raw database configuration.

## 9. Implementation layout

```text
agent/mcp/
  __init__.py
  server.py              # MCP server bootstrap and transport selection
  config.py              # API base URL, scope policy, timeouts
  client.py              # typed FlowKit HTTP client
  schemas.py             # Pydantic MCP input/output models
  auth.py                # scoped credential validation
  audit.py               # structured redacted tool-invocation logging
  tools/
    discovery.py
    projects.py
    characters.py
    scenes.py
    generation.py
    remote_jobs.py
    administration.py

tests/mcp/
  test_server_discovery.py
  test_read_tools.py
  test_authoring_tools.py
  test_generation_tools.py
  test_scope_policy.py
  test_remote_job_ownership.py
```

Use the official Python MCP SDK. Keep MCP dependencies in `requirements-mcp.txt` or an optional dependency group so existing FlowKit deployments do not gain an exposed MCP service accidentally.

### 9.1 Client configuration examples

Remote Streamable HTTP:

```json
{
  "mcpServers": {
    "flowkit": {
      "type": "http",
      "url": "https://flowcommand.gunamaya.id/mcp",
      "headers": {
        "Authorization": "Bearer ${FLOWKIT_MCP_TOKEN}"
      }
    }
  }
}
```

Local stdio (development only):

```json
{
  "mcpServers": {
    "flowkit-local": {
      "command": "python3",
      "args": ["-m", "agent.mcp.server", "--transport", "stdio"],
      "env": {
        "FLOWKIT_API_BASE_URL": "http://127.0.0.1:8100"
      }
    }
  }
}
```

The exact client key (`type`, `url`, or `command`) depends on the MCP client version. Both configurations must resolve to the same tool registry.

### 9.2 Reverse proxy requirements

For the `/mcp` location on Nginx Proxy Manager:

- proxy to the FlowCommand MCP service port;
- preserve the request path `/mcp`;
- pass `Authorization`, `Content-Type`, `Accept`, and MCP session headers unchanged;
- allow `POST`, `GET`, and `DELETE`;
- disable response buffering for streaming responses;
- use a read timeout long enough for active protocol streams;
- do not expose worker routes or the extension WebSocket on the same location.

### 9.3 Transport decision summary

| Transport | Use | Status |
|---|---|---|
| Streamable HTTP at `/mcp` | Remote production clients | Primary |
| stdio | Local development and local-only agents | Optional wrapper |
| Legacy standalone SSE | New integrations | Not recommended |

SSE remains an implementation detail available inside Streamable HTTP when the client and server negotiate streaming. It is not a separate public legacy SSE endpoint.

## 10. Delivery phases

### Phase 0 — Contract and security review

- Approve tool names, scopes, input/output schemas, and async response format.
- Use Streamable HTTP as the primary release transport, with optional local stdio.
- Define audit-log retention and secret-redaction policy.

### Phase 1 — Remote Streamable HTTP read-only MCP

- [x] Add MCP server bootstrap mount at `/mcp` using Streamable HTTP.
- [x] Add dedicated scoped bearer credential validation and redacted audit logs.
- [x] Implement health, model, material, project, character, scene, request, provider, and credit-status tools.
- [x] Add tool-contract tests and an MCP SDK Streamable HTTP discovery smoke test.
- [ ] Deploy and verify Nginx Proxy Manager streaming behavior on the public host.

### Phase 1b — Local stdio wrapper

- Add an optional stdio entrypoint using the exact same tool registry and schemas.
- Configure it for local Claude/Codex development against `http://127.0.0.1:8100`.
- Verify stdio and HTTP expose identical `tools/list` results.

### Phase 2 — Authoring MCP

- Implement project, character, video, scene CRUD tools.
- Implement `flowkit_build_character`.
- Validate reference-image readiness and UUID media-ID requirements.
- Add structured audit events for every mutation.

### Phase 3 — Controlled generation MCP

- Implement image/reference, Veo, Omni Flash, review, and bounded polling tools.
- Implement `dry_run` everywhere generation is possible.
- Normalize Flow/provider errors to MCP-safe error responses.
- Add integration tests against a test FastAPI app and mock provider/extension responses.

### Phase 4 — FlowCommand bridge

- Add client-safe remote image-job submit/status/download tools.
- Enforce ownership and scope boundaries.
- Keep worker lifecycle endpoints private to extensions.
- Consider remote model selection only after the FlowCommand job schema and dispatcher support it.

### Phase 5 — Harden and operate remote MCP

- Enforce per-scope rate limits, request-size limits, token rotation, and revocation.
- Add `/mcp` reverse-proxy policy, streaming smoke tests, metrics, audit alerts, and abuse tests.
- Publish client configuration examples for Streamable HTTP and stdio.

## 11. Acceptance criteria

The MCP surface is ready when:

- an MCP client can discover all declared tools through `tools/list`;
- read-only callers cannot mutate or generate;
- an authorized agent can create a project, build/attach a character, create a scene, submit a reference image, and poll its completion;
- the system prevents a scene generation from using invalid/non-UUID reference media;
- model selection is discoverable and validated;
- `flowkit_get_credits_status` accurately says whether a live balance is available;
- generation responses return job/request IDs rather than false synchronous success;
- `dry_run` submits no request;
- no MCP response or log exposes worker token, callback secret, cookie, database URL, or storage credentials;
- tests cover authorization, ownership, error normalization, partial batch failure, and timeout behavior.

## 12. Recommended first milestone

Build **remote Phase 1 plus the read-only subset of Phase 2** first:

1. Streamable HTTP endpoint at `/mcp` with separate scoped MCP credentials;
2. health/models/materials/projects/scenes/characters/requests/provider/credit-status discovery;
3. Nginx streaming, authentication, and MCP client smoke tests;
4. then authoring tools;
5. then optional stdio wrapper for local development;
6. only then enable controlled image generation.

This creates useful remote agent visibility immediately while keeping provider credits, Chrome extension behavior, and remote worker credentials protected.

## 13. Deployment configuration (initial read-only example)

Install the optional MCP dependency in the FlowCommand image using
`requirements-mcp.txt`, then enable the endpoint only with a dedicated token:

```dotenv
FLOWKIT_MCP_ENABLED=1
# Named subjects preserve ownership during credential rotation.
FLOWKIT_MCP_TOKENS=reader=replace-with-long-read-token:read
```

`FLOWKIT_MCP_TOKENS` is independent of `FLOWCOMMAND_WORKER_TOKEN` and must be
rotated independently. Do not put a real token in tracked documentation.

The initial implementation mounts the authenticated endpoint at:

```text
https://flowcommand.gunamaya.id/mcp
```

The initial deployment should grant only read scope. The registry also includes authoring, generation, remote-job, and admin tools; their scope checks still apply. Read tools:

- `flowkit_health`
- `flowkit_list_models`
- `flowkit_list_materials`
- `flowkit_list_projects`, `flowkit_get_project`
- `flowkit_list_videos`, `flowkit_list_scenes`, `flowkit_list_characters`
- `flowkit_list_requests`, `flowkit_get_request`
- `flowkit_get_provider_status`
- `flowkit_get_credits_status`

The local development entrypoint uses the same registry:

```bash
python -m agent.mcp.server --transport stdio
```

Before public deployment, configure the Nginx Proxy Manager `/mcp` location to
proxy to port `8100`, preserve `Authorization`, and disable proxy buffering for
streaming responses. Verify both an unauthorized request (`401`) and a valid
MCP `initialize` / `tools/list` handshake.
