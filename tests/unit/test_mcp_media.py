"""Remote image import preserves caller bytes without filesystem access."""
import base64
import io
import json
from uuid import uuid4

import pytest
from PIL import Image

from agent.mcp import server
from agent.mcp.auth import MCPPrincipal
from agent.mcp.media import decode_image, upload_media_id


def photo():
    output = io.BytesIO()
    Image.new('RGB', (4, 4), 'blue').save(output, format='PNG')
    return base64.b64encode(output.getvalue()).decode()


@pytest.fixture
async def media_env(monkeypatch):
    pid, vid, sid, cid, mid = [str(uuid4()) for _ in range(5)]
    calls = []
    async def project(identifier):
        return {'id': pid, 'flow_project_id': pid}
    async def characters(identifier):
        return [{'id': cid, 'name': 'Photo character'}]
    async def requests(**filters):
        return []
    async def request(method, path, **kwargs):
        if path == f'/api/scenes/{sid}':
            return {'id': sid, 'video_id': vid}
        if path == f'/api/videos/{vid}':
            return {'id': vid, 'project_id': pid}
        raise AssertionError(path)
    async def post(path, body):
        calls.append(('upload', path, body))
        return {'media_id': mid}
    async def character(identifier, body):
        calls.append(('character', identifier, body))
        return {'id': identifier, **body}
    async def scene(identifier, body):
        calls.append(('scene', identifier, body))
        return {'id': identifier, **body}
    for name, callback in [('project', project), ('project_characters', characters), ('requests', requests), ('_request', request), ('post', post), ('update_character', character), ('update_scene', scene)]:
        monkeypatch.setattr(server.flowkit_client, name, callback)
    token = server._current_principal.set(MCPPrincipal('test', frozenset({'write', 'read', 'generate'})))
    yield server.create_mcp(), (pid, vid, sid, cid, mid), calls
    server._current_principal.reset(token)


def structured(result):
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_original_photo_upload_and_dual_binding(media_env):
    mcp, (pid, vid, sid, cid, mid), calls = media_env
    original = photo()
    result = structured(await mcp.call_tool('flowkit_upload_image', {'project_id': pid, 'image_base64': original, 'character_id': cid, 'scene_id': sid}))
    assert result['media_id'] == mid
    assert not result['partial_failure']
    assert calls[0][2]['image_base64'] == original
    assert calls[1][2] == {'media_id': mid, 'reference_image_url': None}
    assert calls[2][2]['vertical_image_media_id'] == mid
    assert calls[2][2]['vertical_video_media_id'] is None
    assert calls[2][2]['vertical_upscale_status'] == 'PENDING'
    assert 'raw' not in result


@pytest.mark.asyncio
async def test_upload_dry_run_never_uploads(media_env):
    mcp, (pid, vid, sid, cid, mid), calls = media_env
    result = structured(await mcp.call_tool('flowkit_upload_image', {'project_id': pid, 'image_base64': photo(), 'character_id': cid, 'scene_id': sid, 'dry_run': True}))
    assert result['dry_run']
    assert calls == []


@pytest.mark.asyncio
async def test_upload_requires_write_before_backend(media_env):
    mcp, (pid, *_), calls = media_env
    token = server._current_principal.set(MCPPrincipal('reader', frozenset({'read'})))
    try:
        with pytest.raises(Exception, match='write'):
            await mcp.call_tool('flowkit_upload_image', {'project_id': pid, 'image_base64': photo(), 'dry_run': True})
    finally:
        server._current_principal.reset(token)
    assert calls == []


@pytest.mark.asyncio
async def test_upload_binding_failure_preserves_media_id(media_env, monkeypatch):
    mcp, (pid, vid, sid, cid, mid), calls = media_env
    async def broken(*args):
        raise RuntimeError('secret upstream body')
    monkeypatch.setattr(server.flowkit_client, 'update_character', broken)
    result = structured(await mcp.call_tool('flowkit_upload_image', {'project_id': pid, 'image_base64': photo(), 'character_id': cid}))
    assert result['media_id'] == mid and result['partial_failure']
    assert 'secret' not in str(result)


@pytest.mark.asyncio
async def test_scene_binding_checks_project_before_mutation(media_env):
    mcp, (pid, vid, sid, cid, mid), calls = media_env
    with pytest.raises(Exception, match='does not belong'):
        await mcp.call_tool('flowkit_set_scene_image', {'project_id': str(uuid4()), 'scene_id': sid, 'media_id': mid})
    assert calls == []


@pytest.mark.asyncio
async def test_active_generation_blocks_rebinding(media_env, monkeypatch):
    mcp, (pid, vid, sid, cid, mid), calls = media_env
    async def active(**filters):
        return [{'status': 'PROCESSING'}]
    monkeypatch.setattr(server.flowkit_client, 'requests', active)
    with pytest.raises(Exception, match='pending/processing'):
        await mcp.call_tool('flowkit_set_scene_image', {'project_id': pid, 'scene_id': sid, 'media_id': mid})
    assert calls == []


@pytest.mark.asyncio
async def test_end_frame_binding_clears_downstream(media_env):
    mcp, (pid, vid, sid, cid, mid), calls = media_env
    await mcp.call_tool('flowkit_set_scene_image', {'project_id': pid, 'scene_id': sid, 'media_id': mid, 'slot': 'end_frame', 'orientation': 'HORIZONTAL'})
    body = calls[0][2]
    assert body['horizontal_end_scene_media_id'] == mid
    assert 'horizontal_image_media_id' not in body
    assert body['horizontal_video_media_id'] is None


@pytest.mark.parametrize('payload', ['', '/etc/passwd', 'https://example.com/image.png', 'data:image/png;base64,AAAA', base64.b64encode(b'not an image').decode()])
def test_invalid_images(payload):
    with pytest.raises(ValueError):
        decode_image(payload, 'image/png')


def test_mime_mismatch():
    with pytest.raises(ValueError, match='does not match'):
        decode_image(photo(), 'image/jpeg')


def test_upload_extracts_uuid_from_fife_url():
    mid = str(uuid4())
    assert upload_media_id({'media_id': 'CAMS-invalid', 'raw': {'fifeUrl': f'https://example.com/image/{mid}?x=y'}}) == mid
    with pytest.raises(ValueError, match='no UUID'):
        upload_media_id({'media_id': 'CAMS-invalid'})
