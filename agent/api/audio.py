"""Local audio transcription and transcript-to-scene planning."""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

router = APIRouter(prefix="/audio", tags=["audio"])

MAX_AUDIO_BYTES = 100 * 1024 * 1024
ALLOWED_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".webm"}
_model_cache: dict[str, object] = {}


class TranscriptSegment(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str


class TranscriptResponse(BaseModel):
    transcript: str
    language: str | None = None
    duration_seconds: float | None = None
    segments: list[TranscriptSegment]


class SceneDraft(BaseModel):
    display_order: int
    narrator_text: str
    prompt: str
    duration: float = Field(ge=2, le=12)


class ScenePlanRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=100_000)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    target_seconds: Literal[4, 6, 8] = 6


class ScenePlanResponse(BaseModel):
    scenes: list[SceneDraft]


def _transcribe(path: Path) -> tuple[str, str | None, float | None, list[TranscriptSegment]]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Audio transcription is unavailable. Install faster-whisper first.") from exc

    # Medium is materially more accurate for Indonesian than the small model.
    # Users can lower this on constrained machines with WHISPER_MODEL=small.
    model_name = os.environ.get("WHISPER_MODEL", "medium")
    model = _model_cache.get(model_name)
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
    return " ".join(parts), getattr(info, "language", None), duration or None, timed_segments


@router.post("/transcribe", response_model=TranscriptResponse)
async def transcribe_audio(file: UploadFile = File(...)):
    suffix = Path(file.filename or "audio").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, "Unsupported audio format. Use MP3, WAV, M4A, AAC, OGG, FLAC, MP4, or WebM.")

    data = await file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio file is larger than 100 MB.")
    if not data:
        raise HTTPException(400, "Audio file is empty.")

    tmp = tempfile.NamedTemporaryFile(prefix="flowkit-audio-", suffix=suffix, delete=False)
    path = Path(tmp.name)
    try:
        tmp.write(data)
        tmp.close()
        transcript, language, duration, segments = await asyncio.to_thread(_transcribe, path)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, f"Could not transcribe audio: {exc}") from exc
    finally:
        path.unlink(missing_ok=True)

    if not transcript:
        raise HTTPException(422, "No speech was detected in this audio.")
    return TranscriptResponse(transcript=transcript, language=language, duration_seconds=duration, segments=segments)


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


def _timed_chunks(segments: list[TranscriptSegment], target_seconds: int) -> list[tuple[str, float]]:
    chunks: list[tuple[str, float]] = []
    pending: list[str] = []
    pending_duration = 0.0

    def flush() -> None:
        nonlocal pending, pending_duration
        if pending:
            chunks.append((" ".join(pending), pending_duration))
            pending, pending_duration = [], 0.0

    for segment in segments:
        text = segment.text.strip()
        duration = max(0.5, segment.end - segment.start)
        # Whisper may return a single long lyric segment; preserve its timing but
        # subdivide it so Flow receives useful 4–8 second scene beats.
        if duration > target_seconds * 1.35:
            flush()
            part_count = max(2, round(duration / target_seconds))
            parts = _split_text(text, part_count)
            part_duration = duration / len(parts)
            chunks.extend((part, part_duration) for part in parts)
            continue
        if pending and pending_duration + duration > target_seconds * 1.35:
            flush()
        pending.append(text)
        pending_duration += duration
        if pending_duration >= target_seconds * 0.8:
            flush()
    flush()
    return chunks


def _word_chunks(transcript: str, target_seconds: int) -> list[tuple[str, float]]:
    words = re.sub(r"\s+", " ", transcript).strip().split()
    words_per_scene = max(8, round(target_seconds * 2.5))
    return [
        (" ".join(words[index:index + words_per_scene]), min(12, max(2, len(words[index:index + words_per_scene]) / 2.5)))
        for index in range(0, len(words), words_per_scene)
    ]


@router.post("/propose-scenes", response_model=ScenePlanResponse)
async def propose_scenes(body: ScenePlanRequest):
    if not body.transcript.strip():
        raise HTTPException(422, "Transcript does not contain usable narration.")
    chunks = _timed_chunks(body.segments, body.target_seconds) if body.segments else _word_chunks(body.transcript, body.target_seconds)

    return ScenePlanResponse(scenes=[
        SceneDraft(
            display_order=index,
            narrator_text=chunk,
            prompt=(
                "Create a cinematic visual that directly supports this narration: "
                f"{chunk}"
            ),
            duration=round(max(2, min(12, duration)), 1),
        )
        for index, (chunk, duration) in enumerate(chunks)
    ])
