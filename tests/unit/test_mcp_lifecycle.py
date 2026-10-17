"""Lifecycle tools use fixed routes and never mutate without write authority."""
import json
import subprocess
import sys
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent.mcp import server
from agent.mcp.auth import MCPPrincipal
from agent.mcp.client import FlowKitAPIError
from agent.mcp.lifecycle import register_lifecycle_tools


DELETES = [("project", "projects"), ("video", "videos"),
           ("scene", "scenes"), ("character", "characters")]
NAMES = {f"flowkit_delete_{kind}" for kind, _ in DELETES} | {
    "flowkit_detach_character_from_project", "flowkit_set_active_project",
    "flowkit_clear_active_project",
}


@pytest.fixture
async def lifecycle_env(monkeypatch):
    backend = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(server.flowkit_client, "_request", backend)
    token = server._current_principal.set(MCPPrincipal("lifecycle-test", frozenset({"write"})))
    try:
        mcp = server.create_mcp()
        if "flowkit_delete_project" not in {tool.name for tool in await mcp.list_tools()}:
            register_lifecycle_tools(mcp)
        yield mcp, backend
    finally:
        server._current_principal.reset(token)


def structured(result):
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


def test_module_import_does_not_import_server():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import agent.mcp.lifecycle; "
         "assert 'agent.mcp.server' not in sys.modules"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_explicit_tool_schemas(lifecycle_env):
    mcp, _ = lifecycle_env
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert NAMES <= tools.keys()
    for name in NAMES:
        properties = tools[name].inputSchema["properties"]
        assert not ({"path", "route", "method", "collection", "body"} & properties.keys())
        assert properties["dry_run"]["type"] == "boolean"
    for kind, _ in DELETES:
        schema = tools[f"flowkit_delete_{kind}"].inputSchema
        assert schema["properties"]["confirm"]["type"] == "boolean"
        assert f"{kind}_id" in schema["required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,collection", DELETES)
async def test_delete_fixed_route_and_response(lifecycle_env, kind, collection):
    mcp, backend = lifecycle_env
    identifier = str(uuid4())
    result = structured(await mcp.call_tool(f"flowkit_delete_{kind}", {
        f"{kind}_id": identifier.upper(), "confirm": True,
    }))
    assert result == {"status": "ok"}
    backend.assert_awaited_once_with("DELETE", f"/api/{collection}/{identifier}")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,collection", DELETES)
@pytest.mark.parametrize("confirm", [None, False])
async def test_delete_requires_explicit_confirmation(lifecycle_env, kind, collection, confirm):
    mcp, backend = lifecycle_env
    args = {f"{kind}_id": str(uuid4())}
    if confirm is not None:
        args["confirm"] = confirm
    with pytest.raises(Exception, match="confirm=true"):
        await mcp.call_tool(f"flowkit_delete_{kind}", args)
    backend.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,collection", DELETES)
@pytest.mark.parametrize("confirm", [False, True])
async def test_delete_dry_run_never_calls_backend(lifecycle_env, kind, collection, confirm):
    mcp, backend = lifecycle_env
    identifier = str(uuid4())
    result = structured(await mcp.call_tool(f"flowkit_delete_{kind}", {
        f"{kind}_id": identifier, "dry_run": True, "confirm": confirm,
    }))
    assert result == {"dry_run": True, "method": "DELETE", "path": f"/api/{collection}/{identifier}"}
    backend.assert_not_awaited()


def lifecycle_cases():
    pid, cid = str(uuid4()), str(uuid4())
    return [(f"flowkit_delete_{kind}", {f"{kind}_id": pid, "confirm": True},
             "DELETE", f"/api/{collection}/{pid}", {}) for kind, collection in DELETES] + [
        ("flowkit_detach_character_from_project", {"project_id": pid, "character_id": cid},
         "DELETE", f"/api/projects/{pid}/characters/{cid}", {}),
        ("flowkit_set_active_project", {"project_id": pid}, "PUT", "/api/active-project", {"json": {"project_id": pid}}),
        ("flowkit_clear_active_project", {}, "DELETE", "/api/active-project", {}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,method,path,kwargs", lifecycle_cases()[4:])
@pytest.mark.parametrize("dry_run", [False, True])
async def test_detach_and_active_project(lifecycle_env, name, args, method, path, kwargs, dry_run):
    mcp, backend = lifecycle_env
    result = structured(await mcp.call_tool(name, {**args, "dry_run": dry_run}))
    if dry_run:
        assert result["dry_run"] is True
        assert (result["method"], result["path"]) == (method, path)
        if kwargs:
            assert result["body"] == kwargs["json"]
        backend.assert_not_awaited()
    else:
        assert result == {"status": "ok"}
        backend.assert_awaited_once_with(method, path, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,method,path,kwargs", lifecycle_cases())
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("scopes", [frozenset({"read"}), frozenset({"generate"})])
async def test_all_tools_require_write_before_backend(lifecycle_env, name, args, method, path, kwargs, dry_run, scopes):
    mcp, backend = lifecycle_env
    token = server._current_principal.set(MCPPrincipal("not-a-writer", scopes))
    try:
        with pytest.raises(Exception, match="write"):
            await mcp.call_tool(name, {**args, "dry_run": dry_run})
    finally:
        server._current_principal.reset(token)
    backend.assert_not_awaited()


ID_CASES = [(name, args, key) for name, args, *_ in lifecycle_cases()
            for key in args if key.endswith("_id")]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,key", ID_CASES)
@pytest.mark.parametrize("bad_id", ["not-a-uuid", "../projects", "https://example.com", str(uuid4()) + "?force=true"])
@pytest.mark.parametrize("dry_run", [False, True])
async def test_uuid_validation_blocks_route_injection(lifecycle_env, name, args, key, bad_id, dry_run):
    mcp, backend = lifecycle_env
    with pytest.raises(Exception, match="UUID"):
        await mcp.call_tool(name, {**args, key: bad_id, "dry_run": dry_run})
    backend.assert_not_awaited()


@pytest.mark.asyncio
async def test_backend_failure_uses_tool_error_handling(lifecycle_env):
    mcp, backend = lifecycle_env
    backend.side_effect = FlowKitAPIError("FlowKit request returned HTTP 404")
    with pytest.raises(Exception, match="HTTP 404"):
        await mcp.call_tool("flowkit_delete_scene", {"scene_id": str(uuid4()), "confirm": True})
    backend.assert_awaited_once()
