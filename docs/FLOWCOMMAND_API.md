# FlowCommand Image API

**Base URL:** `https://flowcommand.gunamaya.id`
**Authentication:** HTTP Basic Auth
**API version:** `v1`
**Host FlowCommand IP**: `10.0.0.23`
FlowCommand menerima prompt gambar secara asynchronous. Client mengirim prompt, menerima `job_id` segera, lalu melakukan polling status sampai gambar selesai atau gagal.

## Credentials

API memakai Basic Auth. **Jangan hard-code atau commit credential ke source code, request collection, atau file dokumentasi.**

Di host FlowCommand, credential berada pada runtime `.env`:

```bash
FLOWCOMMAND_CLIENT_USERNAME
FLOWCOMMAND_CLIENT_PASSWORD
```

Untuk shell lokal yang memiliki `.env` FlowKit:

```bash
set -a
. ./.env
set +a
```

Atau export dari secret manager/deployment environment aplikasi client:

```bash
export FLOWCOMMAND_CLIENT_USERNAME='...'
export FLOWCOMMAND_CLIENT_PASSWORD='...'
```

> Semua endpoint job dan download image memerlukan credential Basic Auth yang sama.

## Image aspect ratio

Saat membuat job, kirim `aspect_ratio` sesuai kebutuhan:

| Value | Output |
|---|---|
| `1:1` | Square — default |
| `9:16` | Portrait vertical |
| `16:9` | Landscape widescreen |
| `3:4` | Portrait 4:3 |
| `4:3` | Landscape 4:3 |

## 1. Create image job

`POST /api/v1/image-jobs`

### Request body

```json
{
  "prompt": "A wide cinematic sunrise over a volcanic valley, natural light, no text",
  "aspect_ratio": "16:9",
  "idempotency_key": "my-client-request-20260926-001"
}
```

Fields:

- `prompt` — required text prompt, maximum 4,000 characters.
- `aspect_ratio` — optional; one of the ratios in the table above. Defaults to `1:1`.
- `idempotency_key` — optional but strongly recommended client-generated unique key. Repeating the same key for the same client returns the same job rather than creating duplicate generation work.

### curl

```bash
response=$(curl --fail-with-body -sS \
  -u "$FLOWCOMMAND_CLIENT_USERNAME:$FLOWCOMMAND_CLIENT_PASSWORD" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: client-request-001' \
  -d '{
    "prompt": "A wide cinematic sunrise over a volcanic valley, natural light, no text",
    "aspect_ratio": "16:9",
    "idempotency_key": "client-request-001"
  }' \
  https://flowcommand.gunamaya.id/api/v1/image-jobs)

echo "$response"
job_id=$(printf '%s' "$response" | sed -n 's/.*"job_id":"\([^"]*\)".*/\1/p')
printf 'job_id=%s\n' "$job_id"
```

### Success response — `202 Accepted`

```json
{
  "job_id": "f97c1d0f-76ba-4a18-bc09-eeb063363a90",
  "status": "QUEUED",
  "status_url": "/api/v1/image-jobs/f97c1d0f-76ba-4a18-bc09-eeb063363a90"
}
```

`202` means the job has been accepted; it does **not** mean the image is ready.

## 2. Get job status

`GET /api/v1/image-jobs/{job_id}`

```bash
curl --fail-with-body -sS \
  -u "$FLOWCOMMAND_CLIENT_USERNAME:$FLOWCOMMAND_CLIENT_PASSWORD" \
  "https://flowcommand.gunamaya.id/api/v1/image-jobs/$job_id"
```

### Pending/processing response

```json
{
  "job_id": "f97c1d0f-76ba-4a18-bc09-eeb063363a90",
  "status": "PROCESSING",
  "attempt_count": 1,
  "aspect_ratio": "16:9",
  "image_url": null,
  "error": null,
  "created_at": "2026-09-26T11:41:15.090981Z",
  "completed_at": null
}
```

### Completed response

```json
{
  "job_id": "f97c1d0f-76ba-4a18-bc09-eeb063363a90",
  "status": "COMPLETED",
  "attempt_count": 1,
  "aspect_ratio": "16:9",
  "image_url": "https://flowcommand.gunamaya.id/api/v1/image-jobs/f97c1d0f-76ba-4a18-bc09-eeb063363a90/image",
  "error": null,
  "created_at": "2026-09-26T11:41:15.090981Z",
  "completed_at": "2026-09-26T11:41:52.927230Z"
}
```

### Failed response

```json
{
  "job_id": "...",
  "status": "FAILED",
  "attempt_count": 3,
  "aspect_ratio": "16:9",
  "image_url": null,
  "error": "FLOW_GENERATION_FAILED"
}
```

The service makes at most three Google Flow generation attempts. It may delay retries after Google Flow reports unusual activity; clients should keep polling rather than resubmitting the same request.

## 3. Poll until terminal status

Use an interval of 5–10 seconds. Do not create a new job while a submitted job is still `QUEUED`, `LEASED`, `PROCESSING`, or `UPLOADING`.

```bash
while true; do
  status=$(curl --fail-with-body -sS \
    -u "$FLOWCOMMAND_CLIENT_USERNAME:$FLOWCOMMAND_CLIENT_PASSWORD" \
    "https://flowcommand.gunamaya.id/api/v1/image-jobs/$job_id")

  echo "$status"
  state=$(printf '%s' "$status" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')

  case "$state" in
    COMPLETED)
      image_url=$(printf '%s' "$status" | sed -n 's/.*"image_url":"\([^"]*\)".*/\1/p')
      break
      ;;
    FAILED)
      echo 'Image job failed.' >&2
      exit 1
      ;;
    *) sleep 5 ;;
  esac
done
```

## 4. Download the completed image

`GET /api/v1/image-jobs/{job_id}/image`

The image endpoint is protected by Basic Auth. The URL returned by `image_url` is not a public MinIO URL.

```bash
curl --fail-with-body -sS \
  -u "$FLOWCOMMAND_CLIENT_USERNAME:$FLOWCOMMAND_CLIENT_PASSWORD" \
  -o "${job_id}.png" \
  "$image_url"

file "${job_id}.png"
```

If the result may be JPEG or WebP, inspect its actual MIME type and choose extension accordingly:

```bash
curl --fail-with-body -sS -D headers.txt \
  -u "$FLOWCOMMAND_CLIENT_USERNAME:$FLOWCOMMAND_CLIENT_PASSWORD" \
  -o "$job_id.image" \
  "$image_url"

grep -i '^content-type:' headers.txt
```

## Complete shell example

```bash
set -a
. ./.env
set +a

payload='{
  "prompt": "A dramatic wide shot of a volcanic valley at sunrise, natural light, no text",
  "aspect_ratio": "16:9",
  "idempotency_key": "demo-landscape-001"
}'

response=$(curl --fail-with-body -sS \
  -u "$FLOWCOMMAND_CLIENT_USERNAME:$FLOWCOMMAND_CLIENT_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d "$payload" \
  https://flowcommand.gunamaya.id/api/v1/image-jobs)

job_id=$(printf '%s' "$response" | sed -n 's/.*"job_id":"\([^"]*\)".*/\1/p')
test -n "$job_id" || { echo "Unable to parse job_id" >&2; exit 1; }

while true; do
  status=$(curl --fail-with-body -sS \
    -u "$FLOWCOMMAND_CLIENT_USERNAME:$FLOWCOMMAND_CLIENT_PASSWORD" \
    "https://flowcommand.gunamaya.id/api/v1/image-jobs/$job_id")
  state=$(printf '%s' "$status" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')

  if [ "$state" = "COMPLETED" ]; then
    image_url=$(printf '%s' "$status" | sed -n 's/.*"image_url":"\([^"]*\)".*/\1/p')
    curl --fail-with-body -sS \
      -u "$FLOWCOMMAND_CLIENT_USERNAME:$FLOWCOMMAND_CLIENT_PASSWORD" \
      -o "${job_id}.image" "$image_url"
    echo "Downloaded ${job_id}.image"
    break
  fi

  if [ "$state" = "FAILED" ]; then
    echo "Job failed: $status" >&2
    exit 1
  fi

  sleep 5
done
```

## Errors

FlowCommand API errors use this format:

```json
{
  "error": {
    "code": 401,
    "message": "Invalid credentials"
  }
}
```

Common status codes:

| HTTP | Meaning |
|---|---|
| `202` | Job accepted and queued |
| `200` | Status/image returned successfully |
| `401` | Missing or invalid Basic Auth / worker token |
| `404` | Job/image does not exist or does not belong to this client |
| `413` | Uploaded image exceeds size limit |
| `415` | Uploaded content is not PNG, JPEG, or WebP |
| `422` | Invalid payload, UUID, or aspect ratio |
| `429` | Rate limited; retry after the response `Retry-After` interval |
| `503` | Dependency not ready; retry later |

## Operational endpoints

These are not client generation endpoints:

```text
GET /health   Process and extension connection state
GET /ready    PostgreSQL and MinIO readiness
GET /metrics  Prometheus-style application counters
```
