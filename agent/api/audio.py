"""Local audio transcription and transcript-to-scene planning."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Literal, cast

import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

router = APIRouter(prefix="/audio", tags=["audio"])
logger = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 100 * 1024 * 1024
ALLOWED_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".webm"}
_model_cache: dict[str, object] = {}
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


class RemoteAudioTooLargeError(RuntimeError):
    """The configured remote provider rejected media over its contract limit."""


class TranscriptSegment(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str


class TranscriptionJobResponse(BaseModel):
    status: str
    job_id: str | None = None
    transcript: str | None = None
    language: str | None = None
    duration_seconds: float | None = None
    segments: list[TranscriptSegment] = Field(default_factory=list)
    error: str | None = None


class SceneDraft(BaseModel):
    display_order: int
    narrator_text: str
    prompt: str
    duration: float = Field(ge=2, le=12)


class ScenePlanRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=100_000)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    audio_duration_seconds: float | None = Field(default=None, gt=0)
    target_seconds: Literal[4, 6, 8] = 6


class ScenePlanResponse(BaseModel):
    scenes: list[SceneDraft]


def _transcribe(path: Path) -> tuple[str, str | None, float | None, list[TranscriptSegment], str, bool]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Audio transcription is unavailable. Install faster-whisper first.") from exc

    # Medium is materially more accurate for Indonesian than the small model.
    # Users can lower this on constrained machines with WHISPER_MODEL=small.
    model_name = os.environ.get("WHISPER_MODEL", "medium")
    model = _model_cache.get(model_name)
    cache_hit = model is not None
    if model is None:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        _model_cache[model_name] = model

    segments, info = model.transcribe(str(path), vad_filter=True)
    parts: list[str] = []
    timed_segments: list[TranscriptSegment] = []
    duration = 0.0
    for segment in segments:
        text = segment.text.strip()
        if text:
            parts.append(text)
            timed_segments.append(TranscriptSegment(start=float(segment.start), end=float(segment.end), text=text))
        duration = max(duration, float(segment.end))
    return " ".join(parts), getattr(info, "language", None), duration or None, timed_segments, model_name, cache_hit


def _media_duration(path: Path) -> float | None:
    try:
        output = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
            text=True,
        ).strip()
        return float(output) if output else None
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def _env_value(name: str) -> str | None:
    """Read a local development .env value without adding a dotenv dependency."""
    if value := os.environ.get(name):
        return value
    if not _ENV_PATH.exists():
        return None
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.partition("=")[2].strip().strip('"').strip("'") or None
    return None


def _remote_config() -> tuple[str, tuple[str, str]] | None:
    base_url = _env_value("TRANSCRIBE_BASE_URL")
    username = _env_value("TRANSCRIBE_USERNAME")
    password = _env_value("TRANSCRIBE_PASSWORD")
    return (base_url.rstrip("/"), (username, password)) if base_url and username and password else None


def _llm_config() -> tuple[str, str, str] | None:
    """Read the OpenAI-compatible provider used to write Google Flow visual prompts."""
    base_url = _env_value("LLM_PROVIDER_BASE_URL")
    token = _env_value("LLM_PROVIDER_TOKEN")
    model = _env_value("LLM_PROVIDER_MODEL")
    return (base_url.rstrip("/"), token, model) if base_url and token and model else None


def _remote_mode() -> Literal["transcribe", "lyrics"]:
    """Return the Gunamaya decoding mode selected for this local deployment."""
    mode = (_env_value("TRANSCRIBE_MODE") or "transcribe").lower()
    if mode not in {"transcribe", "lyrics"}:
        raise RuntimeError("TRANSCRIBE_MODE must be either 'transcribe' or 'lyrics'.")
    return cast(Literal["transcribe", "lyrics"], mode)


def _remote_timestamps(mode: Literal["transcribe", "lyrics"]) -> Literal["none", "segment"]:
    """Request the provider's opt-in lyric timeline segments when supported."""
    default = "segment" if mode == "lyrics" else "none"
    timestamps = (_env_value("TRANSCRIBE_TIMESTAMPS") or default).lower()
    if timestamps not in {"none", "segment"}:
        raise RuntimeError("TRANSCRIBE_TIMESTAMPS must be either 'none' or 'segment'.")
    if timestamps == "segment" and mode != "lyrics":
        raise RuntimeError("TRANSCRIBE_TIMESTAMPS=segment requires TRANSCRIBE_MODE=lyrics.")
    return cast(Literal["none", "segment"], timestamps)


def _remote_segments(payload: dict) -> list[TranscriptSegment]:
    raw_segments = payload.get("segments") or payload.get("data", {}).get("segments") or []
    segments: list[TranscriptSegment] = []
    for item in raw_segments:
        text = str(item.get("text") or item.get("transcript") or "").strip()
        start, end = item.get("start", item.get("start_time")), item.get("end", item.get("end_time"))
        if text and isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
            segments.append(TranscriptSegment(start=float(start), end=float(end), text=text))
    return sorted(segments, key=lambda segment: (segment.start, segment.end))


async def _remote_submit(path: Path, filename: str, media_type: str | None) -> tuple[dict, str]:
    config = _remote_config()
    if not config:
        raise RuntimeError("Remote transcription is not configured.")
    base_url, auth = config
    mode = _remote_mode()
    timestamps = _remote_timestamps(mode)

    async with httpx.AsyncClient(auth=auth, timeout=60.0) as client:
        with path.open("rb") as audio:
            response = await client.post(
                f"{base_url}/v1/transcriptions",
                # Gunamaya accepts both 202 (new job) and 200 (completed lyrics cache hit).
                data={"language": "id", "mode": mode, "timestamps": timestamps},
                files={"file": (filename, audio, media_type or "application/octet-stream")},
            )
        if response.status_code == 413:
            raise RemoteAudioTooLargeError("Audio file exceeds Gunamaya's 100 MiB upload limit. Compress, trim, or split the audio before transcribing.")
        response.raise_for_status()
        job = response.json()
        job_id = job.get("id") or job.get("process_id")
        if not job_id:
            raise RuntimeError("Remote transcriber accepted the upload but did not return a job ID.")
        initial_state = str(job.get("status", "")).lower()
        logger.info(
            "Remote transcription submitted: job=%s mode=%s timestamps=%s cached=%s state=%s",
            str(job_id)[:8], mode, timestamps, bool(job.get("cached")), initial_state or "unknown",
        )
        return job, str(job_id)


async def _remote_job_status(job_id: str) -> tuple[dict, dict | None]:
    config = _remote_config()
    if not config:
        raise RuntimeError("Remote transcription is not configured.")
    base_url, auth = config
    async with httpx.AsyncClient(auth=auth, timeout=60.0) as client:
        status_response = await client.get(f"{base_url}/v1/transcriptions/{job_id}")
        status_response.raise_for_status()
        status = status_response.json()
        if str(status.get("status", "")).lower() != "completed":
            return status, None
        result_response = await client.get(f"{base_url}/v1/transcriptions/{job_id}/result")
        result_response.raise_for_status()
        return status, result_response.json()


@router.post("/transcribe", response_model=TranscriptionJobResponse)
async def transcribe_audio(file: UploadFile = File(...)):
    suffix = Path(file.filename or "audio").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, "Unsupported audio format. Use MP3, WAV, M4A, AAC, OGG, FLAC, MP4, or WebM.")

    data = await file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio file is larger than 100 MiB.")
    if not data:
        raise HTTPException(400, "Audio file is empty.")

    logger.info("Audio transcribe started: format=%s bytes=%d", suffix, len(data))

    tmp = tempfile.NamedTemporaryFile(prefix="flowkit-audio-", suffix=suffix, delete=False)
    path = Path(tmp.name)
    try:
        tmp.write(data)
        tmp.close()
        media_duration = await asyncio.to_thread(_media_duration, path)
        if _remote_config():
            job, job_id = await _remote_submit(path, file.filename or "audio", file.content_type)
            state = str(job.get("status", "queued")).lower()
            # Cache hits are already complete, but fetch their result through the same status endpoint.
            return TranscriptionJobResponse(status=state, job_id=job_id, duration_seconds=media_duration)
        else:
            transcript, language, speech_duration, segments, model_name, cache_hit = await asyncio.to_thread(_transcribe, path)
    except RemoteAudioTooLargeError as exc:
        raise HTTPException(413, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, f"Could not transcribe audio: {exc}") from exc
    finally:
        path.unlink(missing_ok=True)

    if not transcript:
        raise HTTPException(422, "No speech was detected in this audio.")
    logger.info(
        "Audio transcribe completed: model=%s cache_hit=%s language=%s media_duration_s=%.1f speech_timeline_s=%.1f segments=%d words=%d",
        model_name, cache_hit, language or "unknown", media_duration or speech_duration or 0, speech_duration or 0, len(segments), len(transcript.split()),
    )
    return TranscriptionJobResponse(status="completed", transcript=transcript, language=language, duration_seconds=media_duration or speech_duration, segments=segments)


@router.get("/transcribe/{job_id}", response_model=TranscriptionJobResponse)
async def transcription_status(job_id: str):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", job_id):
        raise HTTPException(400, "Invalid transcription job ID.")
    try:
        status, result = await _remote_job_status(job_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, "Transcription job was not found.") from exc
        raise HTTPException(502, "Could not retrieve transcription status from Gunamaya.") from exc
    except Exception as exc:
        raise HTTPException(502, f"Could not retrieve transcription status: {exc}") from exc

    state = str(status.get("status", "unknown")).lower()
    if state in {"failed", "error", "cancelled", "canceled"}:
        return TranscriptionJobResponse(status=state, job_id=job_id, error=str(status.get("error") or status.get("message") or state))
    if result is None:
        return TranscriptionJobResponse(status=state, job_id=job_id)
    transcript = str(result.get("text") or result.get("transcript") or result.get("data", {}).get("text") or "").strip()
    if not transcript:
        return TranscriptionJobResponse(status="failed", job_id=job_id, error="Remote transcription completed without transcript text.")
    return TranscriptionJobResponse(
        status="completed", job_id=job_id, transcript=transcript,
        language=result.get("language") or status.get("language"), segments=_remote_segments(result),
    )


def _sentences(transcript: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", transcript).strip()
    return [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", cleaned) if sentence.strip()]


def _split_text(text: str, chunk_count: int) -> list[str]:
    """Split unpunctuated lyrics/narration into readable, evenly sized chunks."""
    words = text.split()
    if chunk_count <= 1 or len(words) < 2:
        return [text.strip()]
    size = max(1, round(len(words) / chunk_count))
    return [" ".join(words[index:index + size]) for index in range(0, len(words), size) if words[index:index + size]]


def _timed_chunks(segments: list[TranscriptSegment], target_seconds: int, audio_duration: float | None) -> list[tuple[str, float]]:
    """Build fixed timeline windows and attach speech which overlaps each window."""
    timeline_end = audio_duration or max(segment.end for segment in segments)
    windows: list[tuple[float, float, list[str]]] = []
    start = 0.0
    while start < timeline_end:
        end = min(timeline_end, start + target_seconds)
        windows.append((start, end, []))
        start = end

    for segment in segments:
        overlapping = [index for index, (start, end, _) in enumerate(windows) if segment.end > start and segment.start < end]
        if not overlapping:
            continue
        parts = _split_text(segment.text, len(overlapping))
        for index, part in zip(overlapping, parts):
            windows[index][2].append(part)

    return [
        (" ".join(texts) or "Instrumental or ambient moment; continue the visual story without dialogue.", end - start)
        for start, end, texts in windows
    ]


def _word_chunks(transcript: str, target_seconds: int) -> list[tuple[str, float]]:
    words = re.sub(r"\s+", " ", transcript).strip().split()
    words_per_scene = max(8, round(target_seconds * 2.5))
    return [
        (" ".join(words[index:index + words_per_scene]), min(12, max(2, len(words[index:index + words_per_scene]) / 2.5)))
        for index in range(0, len(words), words_per_scene)
    ]


def _duration_chunks(transcript: str, target_seconds: int, audio_duration: float) -> list[tuple[str, float]]:
    """Cover the media timeline when a provider returns text without word timestamps."""
    scene_count = max(1, -(-audio_duration // target_seconds))
    text_chunks = _split_text(transcript, int(scene_count))
    # _split_text can return a few more chunks because it keeps whole words.
    # Retain exact duration coverage by joining any overflow into the final window.
    if len(text_chunks) > scene_count:
        text_chunks = text_chunks[: int(scene_count) - 1] + [" ".join(text_chunks[int(scene_count) - 1 :])]
    windows = [min(float(target_seconds), audio_duration - index * target_seconds) for index in range(int(scene_count))]
    return [(text_chunks[index] if index < len(text_chunks) else "Instrumental or ambient moment; continue the visual story without dialogue.", duration) for index, duration in enumerate(windows)]


def _llm_chat_url(base_url: str) -> str:
    return base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"


async def _flow_prompt(client: httpx.AsyncClient, chat_url: str, model: str, lyric: str, index: int, scene_count: int) -> str:
    response = await client.post(
        chat_url,
        json={
            "model": model,
            "temperature": 0.8,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You write concise, production-ready visual prompts for Google Flow image-to-video scenes. "
                        "Return only one English visual prompt, no title, quotation marks, narration, or markdown. "
                        "Make it cinematic and specific: subject, action, setting, lighting, camera composition/movement, mood, "
                        "and visual style. Preserve emotional continuity across a music video. Do not include text, subtitles, logos, "
                        "or copyrighted characters."
                    ),
                },
                {"role": "user", "content": f"Music-video scene {index + 1} of {scene_count}. Indonesian lyric/timeline beat:\n{lyric}"},
            ],
        },
    )
    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
    prompt = str((message or {}).get("content") or "").strip()
    if not prompt:
        raise RuntimeError("LLM returned no visual prompt.")
    return prompt.strip('"')


async def _generate_flow_prompts(chunks: list[tuple[str, float]]) -> list[str]:
    config = _llm_config()
    if not config:
        raise RuntimeError("LLM prompt generation is not configured. Set LLM_PROVIDER_BASE_URL, LLM_PROVIDER_TOKEN, and LLM_PROVIDER_MODEL.")
    base_url, token, model = config
    chat_url = _llm_chat_url(base_url)
    # Keep a bounded number of parallel calls so long songs do not overwhelm the provider.
    semaphore = asyncio.Semaphore(5)

    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=90.0) as client:
        async def generate(index: int, chunk: tuple[str, float]) -> str:
            async with semaphore:
                return await _flow_prompt(client, chat_url, model, chunk[0], index, len(chunks))

        return await asyncio.gather(*(generate(index, chunk) for index, chunk in enumerate(chunks)))


@router.post("/propose-scenes", response_model=ScenePlanResponse)
async def propose_scenes(body: ScenePlanRequest):
    if not body.transcript.strip():
        raise HTTPException(422, "Transcript does not contain usable narration.")
    uses_timing = bool(body.segments)
    uses_duration = not uses_timing and body.audio_duration_seconds is not None
    chunks = (
        _timed_chunks(body.segments, body.target_seconds, body.audio_duration_seconds)
        if uses_timing
        else _duration_chunks(body.transcript, body.target_seconds, body.audio_duration_seconds)
        if uses_duration
        else _word_chunks(body.transcript, body.target_seconds)
    )
    logger.info(
        "Audio scene plan: method=%s target_s=%d audio_duration_s=%s source_segments=%d transcript_words=%d scenes=%d durations=%s",
        "segment_timed" if uses_timing else "duration_timed" if uses_duration else "word_fallback", body.target_seconds,
        round(body.audio_duration_seconds, 1) if body.audio_duration_seconds else "unknown", len(body.segments),
        len(body.transcript.split()), len(chunks), [round(duration, 1) for _, duration in chunks],
    )

    try:
        prompts = await _generate_flow_prompts(chunks)
    except httpx.HTTPStatusError as exc:
        logger.warning("LLM scene prompt request failed: status=%s", exc.response.status_code)
        raise HTTPException(502, "The LLM provider could not generate visual prompts.") from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        logger.exception("LLM scene prompt generation failed")
        raise HTTPException(502, "The LLM provider could not generate visual prompts.") from exc

    return ScenePlanResponse(scenes=[
        SceneDraft(display_order=index, narrator_text=chunk, prompt=prompts[index], duration=round(max(2, min(12, duration)), 1))
        for index, (chunk, duration) in enumerate(chunks)
    ])
