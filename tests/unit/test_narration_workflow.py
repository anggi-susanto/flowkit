"""Narration finishing uses stored scene media and exposes/persists mix outcomes."""
import subprocess
import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from agent.api import tts, videos
from agent.models.tts import NarrateVideoRequest
from agent.services import tts as service


@pytest.fixture
def workflow(monkeypatch, tmp_path):
    audio = tmp_path / "narration.wav"
    audio.write_bytes(b"wav")
    scene = {"id": "scene-1", "display_order": 0, "narrator_text": "Hello"}
    monkeypatch.setattr(tts, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(tts, "get_video", AsyncMock(return_value={"project_id": "project-1"}))
    monkeypatch.setattr(tts, "get_project", AsyncMock(return_value={"name": "Demo"}))
    monkeypatch.setattr(tts, "list_scenes", AsyncMock(return_value=[scene]))
    monkeypatch.setattr(tts, "generate_video_narration", AsyncMock(return_value=[{
        "scene_id": "scene-1", "display_order": 0, "narrator_text": "Hello",
        "audio_path": str(audio), "status": "COMPLETED", "duration": 1.5,
    }]))
    monkeypatch.setattr(tts.crud, "update_scene", AsyncMock(return_value=scene))
    return scene, audio


async def test_rejects_mismatched_project_before_generation(workflow):
    with pytest.raises(HTTPException) as exc:
        await tts.narrate_video("video-1", NarrateVideoRequest(project_id="other"))
    assert exc.value.status_code == 400
    assert "project" in exc.value.detail.lower()
    tts.get_project.assert_not_awaited()
    tts.list_scenes.assert_not_awaited()
    tts.generate_video_narration.assert_not_awaited()


@pytest.mark.parametrize("source", ["path", "file", "signed"])
@pytest.mark.parametrize("orientation", ["HORIZONTAL", "VERTICAL"])
async def test_mix_resolves_stored_clip_and_persists_result(
    workflow, monkeypatch, tmp_path, source, orientation,
):
    scene, audio = workflow
    clip = tmp_path / "clip with spaces.mp4"
    clip.write_bytes(b"video")
    url = {"path": str(clip), "file": clip.as_uri(),
           "signed": "https://media.example/clip.mp4?signature=secret"}[source]
    key = f"{orientation.lower()}_video_url"
    scene[key] = url
    worker_threads = []
    download_paths = []

    def download(actual_url, dest):
        assert actual_url == url
        worker_threads.append(threading.get_ident())
        download_paths.append(Path(dest))
        Path(dest).write_bytes(b"video")

    monkeypatch.setattr(videos.urllib.request, "urlretrieve", download)

    def mix(**kwargs):
        worker_threads.append(threading.get_ident())
        assert Path(kwargs["video_path"]).read_bytes() == b"video"
        assert kwargs["narration_path"] == str(audio)
        assert kwargs["sfx_volume"] == 0.4
        Path(kwargs["output_path"]).write_bytes(b"mixed")
        return True

    monkeypatch.setattr(tts, "add_narration", mix)
    response = await tts.narrate_video("video-1", NarrateVideoRequest(
        project_id="project-1", orientation=orientation,
    ))
    result = response.scenes[0]
    assert result.status == result.mix_status == "COMPLETED"
    assert result.mixed_video_url.startswith("file://")
    tts.crud.update_scene.assert_awaited_once_with("scene-1", **{key: result.mixed_video_url})
    assert response.scenes_narrated == 1
    assert response.scenes_failed == 0
    assert all(ident != threading.get_ident() for ident in worker_threads)
    assert all(not path.exists() and not path.parent.exists() for path in download_paths)


@pytest.mark.parametrize("failure", ["no_url", "missing_clip", "missing_audio", "mixer_false", "no_output", "mixer_raises", "persist_raises"])
async def test_requested_mix_failure_is_not_false_success(workflow, monkeypatch, tmp_path, failure):
    scene, audio = workflow
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    if failure != "no_url":
        scene["horizontal_video_url"] = clip.as_uri()
    if failure == "missing_clip":
        clip.unlink()
    if failure == "missing_audio":
        audio.unlink()

    def mix(**kwargs):
        if failure == "mixer_raises":
            raise RuntimeError("ffmpeg unavailable")
        if failure == "mixer_false":
            return False
        if failure != "no_output":
            Path(kwargs["output_path"]).write_bytes(b"mixed")
        return True

    monkeypatch.setattr(tts, "add_narration", mix)
    if failure == "persist_raises":
        tts.crud.update_scene.side_effect = RuntimeError("DB unavailable")
    response = await tts.narrate_video("video-1", NarrateVideoRequest(project_id="project-1"))
    result = response.scenes[0]
    assert result.status == result.mix_status == "FAILED"
    assert result.error.startswith("Narration mix failed:")
    assert result.mixed_video_url is None
    assert response.scenes_narrated == 0
    assert response.scenes_failed == 1
    if failure != "persist_raises":
        tts.crud.update_scene.assert_not_awaited()


async def test_audio_only_does_not_require_clip_or_persist(workflow, monkeypatch):
    mixer = Mock()
    monkeypatch.setattr(tts, "add_narration", mixer)
    response = await tts.narrate_video("video-1", NarrateVideoRequest(project_id="project-1", mix=False))
    assert response.scenes[0].status == "COMPLETED"
    assert response.scenes[0].mix_status == "SKIPPED"
    assert response.scenes[0].mixed_video_url is None
    mixer.assert_not_called()
    tts.crud.update_scene.assert_not_awaited()


@pytest.mark.parametrize("runner,args", [
    (service._run_tts_subprocess, {}),
    (service._run_batch_subprocess, {"items": [{"id": "scene-1"}]}),
])
def test_missing_interpreter_has_actionable_error(monkeypatch, tmp_path, runner, args):
    interpreter = str(tmp_path / "missing-python")
    monkeypatch.setattr(service, "PYTHON_BIN", interpreter)
    with pytest.raises(RuntimeError, match="TTS interpreter.*was not found.*TTS_PYTHON_BIN") as exc:
        runner(args)
    assert interpreter in str(exc.value)
    assert "OmniVoice" in str(exc.value)


@pytest.mark.parametrize("runner,args", [
    (service._run_tts_subprocess, {}),
    (service._run_batch_subprocess, {"items": [{"id": "scene-1"}]}),
])
def test_missing_dependencies_have_actionable_error(monkeypatch, runner, args):
    def run(command, **kwargs):
        assert "TTS dependencies unavailable:" in command[2]
        return subprocess.CompletedProcess(command, 1, "", "RuntimeError: TTS dependencies unavailable: No module named 'torch'")

    monkeypatch.setattr(service.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="TTS dependencies unavailable.*TTS_PYTHON_BIN") as exc:
        runner(args)
    assert "compatible OmniVoice, torch and torchaudio" in str(exc.value)
