"""End-to-end: spawn ``python -m mongo_mcp`` and talk MCP over stdio, exactly like VS Code does."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .conftest import RS_URI, TARGETS

EXPECTED_TOOLS = {
    "list_connections", "test_connection", "add_connection", "remove_connection", "list_databases",
    "list_collections", "collection_stats", "list_indexes", "infer_schema", "find", "aggregate", "count_documents",
    "distinct", "insert_documents", "update_documents", "replace_document", "delete_documents", "run_transaction",
    "create_index", "drop_index", "create_collection", "drop_collection", "rename_collection", "drop_database",
    "run_command", "server_status", "current_ops", "kill_op",
}


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MONGO_MCP_")}
    env.update(extra)
    return env


async def _session(env, fn):
    params = StdioServerParameters(command=sys.executable, args=["-m", "mongo_mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


async def _call(session, name, **args):
    res = await session.call_tool(name, args)
    return json.loads(res.content[0].text)


def test_stdio_without_configuration():
    async def scenario(s):
        tools = (await s.list_tools()).tools
        names = {t.name for t in tools}
        assert names == EXPECTED_TOOLS, names ^ EXPECTED_TOOLS
        by = {t.name: t for t in tools}
        assert by["find"].annotations.readOnlyHint is True
        assert by["delete_documents"].annotations.destructiveHint is True
        res = await _call(s, "find", collection="x")
        assert res["success"] is False and "MONGO_MCP_HOST" in res["error"]["message"]
    asyncio.run(_session(_env(), scenario))


def test_stdio_broken_profile_does_not_break_others(tmp_path):
    cfg = tmp_path / "connections.json"
    cfg.write_text(json.dumps({"default": "good", "connections": {
        "good": {"host": "127.0.0.1:1", "server_selection_timeout_ms": 500},
        "bad": {"host": "h", "user": "u", "password_env": "MONGO_PW_NOT_SET"}}}), encoding="utf-8")

    async def scenario(s):
        res = await _call(s, "list_connections")
        by = {c["name"]: c for c in res["connections"]}
        assert by["bad"]["broken"] is True and "MONGO_PW_NOT_SET" in by["bad"]["error"]
        assert by["good"]["host"] == "127.0.0.1:1"
        res = await _call(s, "find", collection="x", connection="bad", database="d")
        assert "MONGO_PW_NOT_SET" in res["error"]["message"]
        res = await _call(s, "find", collection="x", database="d")  # default profile still usable
        assert "Could not reach" in res["error"]["message"]
    asyncio.run(_session(_env(MONGO_MCP_CONFIG=str(cfg)), scenario))


@pytest.mark.integration
@pytest.mark.parametrize("target", TARGETS or [None], ids=[t[0] for t in TARGETS] or ["no-server"])
def test_stdio_end_to_end(target, tmp_path):
    if target is None:
        pytest.skip("MONGO_MCP_TEST_TARGETS not set")
    name, uri = target
    host = uri.split("@")[1].rstrip("/")
    cfg = tmp_path / "connections.json"
    cfg.write_text(json.dumps({"default": "main", "connections": {
        "main": {"host": host, "user": "root", "password_env": "MONGO_PW_MAIN", "database": "mcp_e2e"},
        "readonly": {"host": host, "user": "root", "password_env": "MONGO_PW_MAIN", "database": "mcp_e2e",
                     "readonly": True}}}), encoding="utf-8")
    env = _env(MONGO_MCP_CONFIG=str(cfg), MONGO_PW_MAIN="Str0ng!Pass")

    async def scenario(s):
        info = await _call(s, "test_connection")
        assert info["success"] and info["server"]["authenticated_users"] == ["root@admin"], info
        await _call(s, "drop_database", database="mcp_e2e", confirm_name="mcp_e2e")
        res = await _call(s, "insert_documents", collection="musteri",
                          documents=[{"ad": "Gökhan", "sehir": "İstanbul"}, {"ad": "Şule", "sehir": "Iğdır"}])
        assert res["inserted_count"] == 2, res
        res = await _call(s, "find", collection="musteri", filter={"sehir": "istanbul"},
                          collation={"locale": "tr", "strength": 2})
        assert [d["ad"] for d in res["documents"]] == ["Gökhan"], res
        oid = res["documents"][0]["_id"]
        res = await _call(s, "update_documents", collection="musteri", filter={"_id": oid},
                          update={"$set": {"vip": True}})
        assert res["modified_count"] == 1
        res = await _call(s, "insert_documents", collection="musteri", documents={"ad": "x"}, connection="readonly")
        assert res["error"]["type"] == "ReadOnlyViolation"
        res = await _call(s, "aggregate", collection="musteri", pipeline=[{"$group": {"_id": "$vip", "n": {"$sum": 1}}}],
                          connection="readonly")
        assert res["count"] == 2
        res = await _call(s, "drop_database", database="mcp_e2e", confirm_name="mcp_e2e")
        assert res["success"]
    asyncio.run(_session(env, scenario))


@pytest.mark.integration
def test_stdio_transaction():
    if not RS_URI:
        pytest.skip("MONGO_MCP_TEST_RS not set")

    async def scenario(s):
        await _call(s, "drop_database", database="mcp_e2e_tx", confirm_name="mcp_e2e_tx")
        res = await _call(s, "run_transaction", database="mcp_e2e_tx", operations=[
            {"op": "insert", "collection": "a", "document": {"_id": 1}},
            {"op": "insert", "collection": "b", "document": {"_id": 1}}])
        assert res["committed"] is True, res
        res = await _call(s, "run_transaction", database="mcp_e2e_tx", operations=[
            {"op": "insert", "collection": "a", "document": {"_id": 2}},
            {"op": "insert", "collection": "b", "document": {"_id": 1}}])
        assert res["committed"] is False and res["error"]["failed_step"]["index"] == 2
        res = await _call(s, "count_documents", collection="a", database="mcp_e2e_tx")
        assert res["count"] == 1
        await _call(s, "drop_database", database="mcp_e2e_tx", confirm_name="mcp_e2e_tx")
    asyncio.run(_session(_env(MONGO_MCP_URI=RS_URI), scenario))


def test_cli_check_and_version():
    out = subprocess.run([sys.executable, "-m", "mongo_mcp", "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and "mongo-mcp" in out.stdout
    out = subprocess.run([sys.executable, "-m", "mongo_mcp", "--check"], capture_output=True, text=True, env=_env())
    assert out.returncode == 1 and "No connection configured" in out.stdout
    if TARGETS:
        out = subprocess.run([sys.executable, "-m", "mongo_mcp", "--check"], capture_output=True, text=True,
                             env=_env(MONGO_MCP_URI=TARGETS[0][1]))
        assert out.returncode == 0, out.stdout + out.stderr
        report = json.loads(out.stdout)
        assert report["connections"]["default"]["success"] is True
        assert "Str0ng" not in out.stdout
