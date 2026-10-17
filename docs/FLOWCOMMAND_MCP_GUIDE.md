# FlowCommand — Panduan MCP

Terakhir diperbarui: 17 Oktober 2026.

## Endpoint

- MCP: **https://flowcommand.gunamaya.id/mcp/**
- Dokumentasi Markdown: **https://flowcommand.gunamaya.id/mcp-guide.md**
- Transport: **Streamable HTTP**, stateless.
- Autentikasi: header `Authorization: Bearer <token>`.

Gunakan trailing slash pada `/mcp/`. URL `/mcp` melakukan redirect HTTP 307. Dokumentasi ini dapat dibaca tanpa token; pemanggilan MCP tetap membutuhkan token.

Server menyediakan 33 tools. Kredensial awal deployment hanya memiliki scope `read`: daftar tools dapat ditemukan, tetapi operasi tulis dan generasi ditolak. Minta token dan scope yang sesuai kepada administrator melalui kanal aman. Jangan memasukkan token ke URL, repository, screenshot, atau percakapan publik.

## Konfigurasi client

Contoh generik untuk client yang mendukung remote HTTP MCP:

```json
{
  "mcpServers": {
    "flowcommand": {
      "type": "http",
      "url": "https://flowcommand.gunamaya.id/mcp/",
      "headers": {
        "Authorization": "Bearer ${FLOWCOMMAND_MCP_TOKEN}"
      }
    }
  }
}
```

Nama field dan dukungan interpolasi environment bergantung pada client. Simpan token pada secret manager atau konfigurasi lokal yang tidak dilacak Git. Ini bukan endpoint legacy SSE atau WebSocket.

## Verifikasi dengan Python SDK

Install SDK pada virtual environment:

```bash
pip install 'mcp>=1.30,<2' httpx
```

Set `FLOWCOMMAND_MCP_TOKEN` melalui mekanisme secret environment Anda, lalu jalankan:

```python
import asyncio
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    headers = {
        "Authorization": "Bearer " + os.environ["FLOWCOMMAND_MCP_TOKEN"]
    }
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        async with streamable_http_client(
            "https://flowcommand.gunamaya.id/mcp/", http_client=client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                print([tool.name for tool in tools.tools])
                result = await session.call_tool("flowkit_list_models", {})
                print(result)

asyncio.run(main())
```

Gunakan `tools/list` untuk schema parameter terbaru. Respons `tools/call` dapat memiliki `isError: true` walaupun HTTP 200: selalu periksa hasil tool, bukan hanya status HTTP.

## Scope

| Scope | Hak akses |
| --- | --- |
| `read` | Membaca state, konfigurasi, proyek, dan request pada workspace bersama |
| `write` | Membuat/mengubah proyek, karakter, video, dan scene |
| `generate` | Mengantrekan generasi, review, dan upscale |
| `remote_jobs` | Submit, status, dan output image job milik principal yang sama |
| `admin` | Semua scope, termasuk konfigurasi model global |

`flowkit_build_character` dengan `generate_reference: true` membutuhkan `write` dan `generate`. Scope `admin` tidak menghapus pembatasan kepemilikan remote image job.

**Batas isolasi:** proyek FlowKit merupakan workspace bersama, bukan tenant terpisah. Jangan gunakan scope workspace untuk pengguna yang tidak saling dipercaya. Remote image jobs memiliki pembatasan kepemilikan berdasarkan identitas token.

## Daftar tools

### Membaca — `read`

| Tool | Fungsi / parameter penting |
| --- | --- |
| `flowkit_health` | Health, readiness, dan koneksi extension |
| `flowkit_list_models` | Konfigurasi model image/video/upscale |
| `flowkit_list_materials` | Material/style yang tersedia |
| `flowkit_list_projects` | Daftar proyek |
| `flowkit_get_project` | Detail berdasarkan `project_id` |
| `flowkit_list_videos` | Memerlukan `project_id` |
| `flowkit_list_scenes` | Memerlukan `video_id` |
| `flowkit_list_characters` | Daftar entitas referensi |
| `flowkit_list_requests` | Antrean dan status dengan filter opsional |
| `flowkit_get_request` | Status berdasarkan `request_id` |
| `flowkit_get_provider_status` | Provider dan batas throttling |
| `flowkit_get_credits_status` | Tier konfigurasi; bukan saldo kredit live |
| `flowkit_get_active_project` | Pilihan active-project bersama |
| `flowkit_wait_for_job` | Poll request dengan `request_id`, `timeout_s` terbatas |

### Authoring — `write`

- `flowkit_create_project`
- `flowkit_update_project`
- `flowkit_create_character`
- `flowkit_build_character`
- `flowkit_update_character`
- `flowkit_attach_character_to_project`
- `flowkit_create_video`
- `flowkit_create_scene`
- `flowkit_update_scene`

Authoring mendukung `dry_run: true`. Preview tidak membuat/mengubah resource, tetapi tetap membutuhkan scope yang benar. Sebagian operasi backend dapat berinteraksi dengan provider; gunakan preview dan pastikan koneksi siap sebelum eksekusi nyata.

### Generasi — `generate`

- `flowkit_generate_character_reference`
- `flowkit_generate_scene_image`
- `flowkit_generate_scene_video`
- `flowkit_generate_video_batch`
- `flowkit_review_video`
- `flowkit_upscale_scene_video`

Generasi mendukung `dry_run: true`. Preview dapat membaca dependency, tetapi tidak mengantrekan generasi. `regenerate: true` pada image/video scene menghapus hasil downstream sesuai cascade backend.

### Remote image jobs — `remote_jobs`

- `flowkit_submit_remote_image_job`: `prompt`, `aspect_ratio`, `idempotency_key` opsional, `dry_run`.
- `flowkit_get_remote_image_job`: status job berdasarkan `job_id` milik principal yang sama.
- `flowkit_get_remote_image_output`: output sebagai MCP image content, setelah job selesai.

Gunakan `idempotency_key` stabil untuk retry submit yang sama. Key diisolasi per principal. Token lain dengan principal berbeda tidak dapat membaca job Anda. Poll status dengan jeda wajar; `flowkit_wait_for_job` hanya untuk request FlowKit, bukan remote image job.

### Administrasi — `admin`

- `flowkit_update_models`: konfigurasi model global. Tergantung izin tulis filesystem deployment; jangan mengandalkan keberhasilannya pada image read-only.

## Contoh alur read-only

Panggil tools melalui client MCP:

```text
flowkit_health({})
flowkit_list_models({})
flowkit_list_projects({})
flowkit_list_videos({"project_id": "<UUID-proyek>"})
flowkit_list_scenes({"video_id": "<UUID-video>"})
```

Placeholder `<UUID-...>` harus diganti dengan ID dari hasil tool, bukan dikirim secara literal.

## Contoh preview authoring

Memerlukan scope `write`, meskipun `dry_run` aktif:

```json
{
  "name": "flowkit_create_project",
  "arguments": {
    "name": "Contoh Proyek",
    "material": "realistic",
    "dry_run": true
  }
}
```

## Alur produksi media

1. Cek health/provider dan daftar material/model.
2. Buat proyek dengan `material`, lalu video dan entitas referensi.
3. Attach entitas ke proyek dan generate seluruh referensi terlebih dahulu.
4. Tunggu referensi selesai; `media_id` harus UUID, bukan string `CAMS...`.
5. Buat scene; prompt scene menjelaskan aksi, penampilan karakter berasal dari referensi.
6. Generate image scene dan tunggu selesai sebelum frame-based video.
7. Generate video scene/batch; hasil submit adalah request ID, bukan tanda media sudah selesai.
8. Jalankan review `light`; perbaiki scene dengan skor di bawah 7.5, maksimal dua siklus review/regenerasi.
9. Upscale setelah review memadai dan provider/tier mendukung.

Lokasi memakai referensi landscape; karakter memakai portrait. Orientasi scene adalah `VERTICAL` atau `HORIZONTAL`. Video prompt sebaiknya memakai segmen waktu, misalnya `0-3s: ... 3-6s: ... 6-8s: ...`.

## Antrean, timeout, dan retry

Status request FlowKit: `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`. `done: true` tidak selalu berarti sukses: periksa status/error hasilnya.

`flowkit_wait_for_job` memiliki batas server, default maksimum 120 detik. Timeout polling tidak membatalkan pekerjaan. Panggil lagi status request yang sama; jangan langsung submit ulang.

Batch video menerima 1–100 scene ID unik. Insert batch backend belum transactional dan deduplikasi belum menjamin exactly-once saat request bersamaan. Jika submit mengalami timeout, periksa antrean sebelum retry karena sebagian pekerjaan mungkin sudah diterima.

Batas default HTTP adalah 1 MiB per request dan 120 request/menit per principal per proses; konfigurasi server/proxy dapat berbeda. Output remote image dibatasi 20 MiB.

## Troubleshooting

| Gejala | Tindakan |
| --- | --- |
| HTTP 307 | Pakai URL `/mcp/` dengan trailing slash |
| HTTP 401 | Pastikan token valid dan header Bearer terkirim |
| Tool error kekurangan scope | Minta scope yang sesuai; read-only tidak dapat menulis/generate |
| HTTP 403 / 421 | Origin/Host ditolak; gunakan domain resmi dan jangan spoof header |
| HTTP 413 | Kurangi ukuran payload |
| HTTP 429 | Tunggu sesuai Retry-After dan kurangi polling |
| Extension disconnected | Minta operator menghubungkan extension, atau gunakan provider yang tidak membutuhkannya |
| Referensi belum siap | Tunggu generasi referensi dan pastikan media ID UUID |
| HTTP 405 pada GET/DELETE MCP | Dapat normal pada transport stateless; gunakan client Streamable HTTP |
| Timeout submit | Periksa status/antrean sebelum retry |

Per-request `image_model` belum didukung antrean dan akan ditolak. Pilih model melalui konfigurasi yang didukung, bukan mengandalkan parameter yang diabaikan. Kredensial read-only awal tidak mengizinkan administrasi model atau generasi berbayar.

## Keamanan

- Gunakan HTTPS dan token berbeda per pengguna/integrasi.
- Rotasi atau cabut token melalui administrator; jangan membagikan token di dokumen.
- Jangan mengirim worker token, cookie browser, atau kredensial storage/database sebagai argumen MCP.
- Izin MCP tidak otomatis melindungi endpoint REST legacy. Administrator harus membatasi exposure REST secara terpisah.
- Dokumentasi publik ini tidak menyertakan kredensial, alamat infrastruktur privat, atau lokasi backup.
