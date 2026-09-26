# FlowCommand Deployment Runbook

## Target

- Host: `10.0.0.23`
- API container port: `8100`
- Extension WebSocket port: `9222`
- Public domain: `flowcommand.gunamaya.id`
- PostgreSQL: `10.0.0.15`, database `flowcommand`
- MinIO: `https://s3.gunamaya.id`, bucket `centracast`, prefix `flowcommand/`

## Server preparation

```bash
mkdir -p /opt/flowcommand
cd /opt/flowcommand
git clone <repository-url> .
chmod 600 .env
```

Copy the runtime `.env` to the server through a secure channel. Do not commit it or print it in logs.

Required remote values include:

- `FLOWCOMMAND_DATABASE_URL` or `FLOWCOMMAND_PG_*`
- `FLOWCOMMAND_CLIENT_USERNAME`
- `FLOWCOMMAND_CLIENT_PASSWORD`
- `FLOWCOMMAND_WORKER_TOKEN`
- `FLOWCOMMAND_PUBLIC_BASE_URL=https://flowcommand.gunamaya.id`
- `FLOWCOMMAND_S3_*`
- `FLOW_PROJECT_ID`

## Start and verify

```bash
docker compose -f docker-compose.flowcommand.yml up -d --build
docker compose -f docker-compose.flowcommand.yml ps
curl -fsS http://127.0.0.1:8100/health
curl -fsS http://127.0.0.1:8100/ready
```

`/ready` must report PostgreSQL and MinIO as available before public traffic is enabled.

## Nginx Proxy Manager

Create one Proxy Host:

- Domain: `flowcommand.gunamaya.id`
- Forward scheme: `http`
- Forward host: `10.0.0.23`
- Forward port: `8100`
- Enable WebSocket Support.
- Request a Let's Encrypt certificate and force SSL.

Add a Custom Location for `/ws/extension`:

- Forward scheme: `http`
- Forward host: `10.0.0.23`
- Forward port: `9222`
- Enable WebSocket Support.
- Preserve query strings; the worker token is sent as `?token=...`.

The extension derives:

```text
wss://flowcommand.gunamaya.id/ws/extension?token=<worker-token>
```

Do not expose PostgreSQL or MinIO credentials through NPM. Do not publish port `9000` from this service.

## Smoke test

```bash
curl --fail-with-body -u "$FLOWCOMMAND_CLIENT_USERNAME:$FLOWCOMMAND_CLIENT_PASSWORD" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: smoke-submit-1' \
  -d '{"prompt":"A simple blue geometric shape","idempotency_key":"smoke-submit-1"}' \
  https://flowcommand.gunamaya.id/api/v1/image-jobs
```

Then poll the returned `job_id`:

```bash
curl -u "$FLOWCOMMAND_CLIENT_USERNAME:$FLOWCOMMAND_CLIENT_PASSWORD" \
  https://flowcommand.gunamaya.id/api/v1/image-jobs/<job_id>
```

## Operations

- Logs: `docker compose -f docker-compose.flowcommand.yml logs -f flowcommand`
- Metrics: `https://flowcommand.gunamaya.id/metrics`
- Readiness: `https://flowcommand.gunamaya.id/ready`
- Restart: `docker compose -f docker-compose.flowcommand.yml restart flowcommand`
- Backup: back up PostgreSQL `flowcommand` and retain MinIO `flowcommand/` objects according to policy.

Never run `docker compose down -v` in production; it removes local runtime volumes.
