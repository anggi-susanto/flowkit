# FlowCommand — Panduan MCP

Terakhir diperbarui: 17 Oktober 2026.

## Endpoint

- MCP: **https://flowcommand.gunamaya.id/mcp/**
- Dokumentasi Markdown: **https://flowcommand.gunamaya.id/mcp-guide.md**
- Transport: **Streamable HTTP**, stateless.
- Autentikasi: header `Authorization: Bearer <token>`.

Gunakan trailing slash pada `/mcp/`. URL `/mcp` melakukan redirect HTTP 307. Dokumentasi ini dapat dibaca tanpa token; pemanggilan MCP tetap membutuhkan token.

Versi workflow lengkap menyediakan 66 tools. Jumlah aktual deployment dapat diperiksa lewat `tools/list`. Kredensial awal deployment hanya memiliki scope `read`: daftar tools dapat ditemukan, tetapi operasi tulis dan generasi ditolak. Minta token dan scope yang sesuai kepada administrator melalui kanal aman. Jangan memasukkan token ke URL, repository, screenshot, atau percakapan publik.

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

Batas default HTTP versi workflow lengkap adalah 12 MiB per request dan 120 request/menit per principal per proses; konfigurasi server/proxy dapat berbeda. Output remote image dibatasi 20 MiB.

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


## Workflow foto asli → karakter → video (versi 66 tools)

Foto lampiran tidak otomatis dikirim oleh protokol MCP. Client/agent harus membaca bytes lampiran di sisi client, mengubahnya menjadi base64, lalu mengirimkannya sebagai argumen tool. Jangan memberikan path lokal client kepada server.

1. Buat proyek/video dengan tools authoring.
2. Buat karakter memakai `flowkit_create_character`, lalu `flowkit_attach_character_to_project`.
3. Buat scene yang menyebut nama karakter pada `character_names` dan aksi pada `video_prompt`.
4. Panggil `flowkit_upload_image` dengan `project_id`, `image_base64`, `mime_type`, `character_id`, `scene_id`, dan orientasi. Upload yang sama dipasang sebagai referensi karakter dan frame awal scene; wajah tidak digenerate ulang.
5. Periksa `media_id`, `bindings`, `partial_failure`, dan `errors`. Bila upload berhasil tetapi binding gagal, retry `flowkit_set_character_reference` atau `flowkit_set_scene_image` dengan UUID hasil upload, bukan upload ulang.
6. Jalankan `flowkit_generate_scene_video`, tunggu request selesai, lalu review. Inspeksi hasil dengan `flowkit_get_scene`/`flowkit_get_media`.
7. Susun klip menggunakan `flowkit_concat_video` atau `flowkit_finalize_video`.

Contoh argumen upload (placeholder base64 harus diganti bytes gambar sungguhan):

```json
{
  "project_id": "<UUID-proyek>",
  "image_base64": "<BASE64-BYTES-FOTO>",
  "mime_type": "image/jpeg",
  "character_id": "<UUID-karakter>",
  "scene_id": "<UUID-scene>",
  "orientation": "VERTICAL",
  "dry_run": true
}
```

Upload memerlukan `write`, mendukung PNG/JPEG/WebP statis hingga 8 MiB dan 25 megapiksel. MIME diverifikasi dari isi file. Tidak menerima URL, data URI, atau path filesystem. `dry_run` memvalidasi foto dan target tetapi tidak upload. Hapus `dry_run` atau set `false` untuk upload nyata.

`flowkit_set_scene_image` menerima `slot: "start_frame"` atau `"end_frame"`, membersihkan video/upscale lama untuk orientasi itu, dan menolak jika ada request aktif yang terdeteksi. Pemeriksaan aktif belum merupakan lock transactional terhadap submit bersamaan: jangan mengganti media saat agent lain menjalankan generasi pada scene/karakter yang sama.

### Tools tambahan

- Media: `flowkit_upload_image`, `flowkit_set_character_reference`, `flowkit_set_scene_image`, `flowkit_get_media`, `flowkit_edit_scene_image`.
- Inspeksi: `flowkit_get_video`, `flowkit_get_scene`, `flowkit_get_character`, `flowkit_list_project_characters`, `flowkit_get_batch_status`.
- Finishing: `flowkit_update_video`, `flowkit_concat_video`, `flowkit_finalize_video`, `flowkit_refresh_project_urls`.
- Voice: `flowkit_list_voice_templates`, `flowkit_create_voice_template`, `flowkit_generate_tts`, `flowkit_narrate_video`.
- Lifecycle: `flowkit_delete_project`, `flowkit_delete_video`, `flowkit_delete_scene`, `flowkit_delete_character`, `flowkit_detach_character_from_project`, `flowkit_set_active_project`, `flowkit_clear_active_project`.
- Musik: `flowkit_list_music_templates`, `flowkit_get_music_template`, `flowkit_generate_music`, `flowkit_generate_lyrics`, `flowkit_get_music_task`, `flowkit_extend_music`, `flowkit_vocal_removal`, `flowkit_convert_to_wav`.

Penghapusan permanen memerlukan `confirm: true`; gunakan `dry_run` untuk preview. Penggantian active-project memakai state runtime. Operasi voice/musik memerlukan engine/provider serta kredensial yang sudah dikonfigurasi; tersedianya tool bukan jaminan provider tersedia. TTS lokal membutuhkan interpreter dengan torch/torchaudio/OmniVoice; paket model besar tidak disertakan otomatis pada image server. Musik menggunakan task ID dan submit non-blocking, bukan polling panjang.

Concat/finalize menggunakan klip yang tersimpan, bukan path output/audio bebas. Mixing narasi yang berhasil memperbarui URL scene agar digunakan saat concat; periksa `mix_status` dan `scenes_failed`. Tidak semua integrasi eksternal tercakup: upload YouTube, impor voice dari file, arbitrary asset download, serta mixing musik dari path/URL bebas belum diekspos sebagai tools MCP. Jangan melewati batas ini dengan mengirim token ke REST legacy.

Untuk foto base64 besar, proxy harus mengizinkan body 12 MiB (`client_max_body_size 12m`); JSON/base64 lebih besar dari bytes gambar. Jika HTTP 413 terjadi sebelum server MCP, operator perlu menyesuaikan limit proxy. Operasi sinkron narasi/concat dapat melebihi timeout client; konfigurasi timeout operasi default 120 detik, maksimum 600. Timeout tidak membatalkan efek yang mungkin sudah berjalan; inspeksi hasil sebelum retry.
