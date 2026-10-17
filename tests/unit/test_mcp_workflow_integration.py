"""Cross-step contracts that single-tool mocks previously missed."""
import json
from uuid import uuid4

import pytest

from agent.mcp import server
from agent.mcp.auth import MCPPrincipal
from agent.api import active_project


def test_active_project_state_is_valid_atomic_json(tmp_path, monkeypatch):
    state = tmp_path / 'runtime' / 'active_project.json'
    monkeypatch.setattr(active_project, '_STATE_FILE', state)
    active_project._write_state({'project_id': 'one'})
    assert json.loads(state.read_text()) == {'project_id': 'one'}
    active_project._write_state({'project_id': 'two'})
    assert active_project._read_state() == {'project_id': 'two'}
    assert not list(state.parent.glob('*.tmp'))
    active_project._clear_state()
    assert not state.exists()


@pytest.mark.asyncio
async def test_first_scene_image_does_not_require_video(monkeypatch):
    sid, vid, pid = [str(uuid4()) for _ in range(3)]
    async def request(method, path, **kwargs):
        if path == f'/api/scenes/{sid}':
            return {'id': sid, 'video_id': vid, 'character_names': []}
        if path == f'/api/videos/{vid}':
            return {'id': vid, 'project_id': pid}
        pytest.fail(path)
    async def characters(project_id):
        return []
    monkeypatch.setattr(server.flowkit_client, '_request', request)
    monkeypatch.setattr(server.flowkit_client, 'project_characters', characters)
    token = server._current_principal.set(MCPPrincipal('generator', frozenset({'generate'})))
    try:
        mcp = server.create_mcp()
        result = await mcp.call_tool('flowkit_generate_scene_image', {'scene_id': sid, 'dry_run': True})
        assert result
        with pytest.raises(Exception, match='video must exist'):
            await mcp.call_tool('flowkit_upscale_scene_video', {'scene_id': sid, 'dry_run': True})
    finally:
        server._current_principal.reset(token)


@pytest.mark.asyncio
async def test_registry_covers_original_photo_to_finished_video():
    names = {tool.name for tool in await server.create_mcp().list_tools()}
    assert len(names) == 66
    assert {'flowkit_upload_image', 'flowkit_set_character_reference', 'flowkit_set_scene_image',
            'flowkit_create_scene', 'flowkit_generate_scene_video', 'flowkit_review_video',
            'flowkit_concat_video', 'flowkit_finalize_video', 'flowkit_get_scene',
            'flowkit_narrate_video', 'flowkit_generate_music'} <= names
