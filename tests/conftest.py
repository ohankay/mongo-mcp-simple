"""Shared fixtures.

Integration tests run against every server in ``MONGO_MCP_TEST_TARGETS`` (``name=uri;name=uri``)::

    MONGO_MCP_TEST_TARGETS="mongo82=mongodb://root:Str0ng%21Pass@localhost:27082/;mongo70=mongodb://root:Str0ng%21Pass@localhost:27070/;mongo44=mongodb://root:Str0ng%21Pass@localhost:27044/"
    MONGO_MCP_TEST_RS="mongodb://localhost:27019/?replicaSet=rs0"     # replica set, for transaction tests
    MONGO_MCP_TEST_TLS="dir=tests/tls-certs;good=localhost:27060;wronghost=localhost:27061;mtls=localhost:27063"

Start the servers with ``docker compose -f tests/docker-compose.yml up -d`` (see that file).
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from mongo_mcp import server
from mongo_mcp.client import ClientManager
from mongo_mcp.config import MongoProfile

TEST_DB = "mcp_test"


def _targets() -> list[tuple[str, str]]:
    raw = os.environ.get("MONGO_MCP_TEST_TARGETS", "")
    out = []
    for item in filter(None, (x.strip() for x in raw.split(";"))):
        name, _, uri = item.partition("=")
        out.append((name, uri))
    return out


TARGETS = _targets()
RS_URI = os.environ.get("MONGO_MCP_TEST_RS")


def call(tool, **kwargs):
    """Invoke an MCP tool coroutine and decode its JSON."""
    return json.loads(asyncio.run(tool(**kwargs)))


def ok(res):
    assert res["success"], res
    return res


def err(res):
    assert res["success"] is False, res
    return res["error"]


@pytest.fixture(scope="module", params=TARGETS or [None], ids=[t[0] for t in TARGETS] or ["no-server"])
def ctx(request):
    if request.param is None:
        pytest.skip("MONGO_MCP_TEST_TARGETS not set - integration tests skipped")
    name, uri = request.param
    mgr = ClientManager({"main": MongoProfile(name="main", uri=uri, database=TEST_DB,
                                              server_selection_timeout_ms=5000)}, "main")
    server.set_manager(mgr)
    mgr.client("main").drop_database(TEST_DB)
    yield {"name": name, "uri": uri, "mgr": mgr}
    mgr.client("main").drop_database(TEST_DB)
    mgr.close_all()
    server.set_manager(None)


@pytest.fixture
def rs():
    if not RS_URI:
        pytest.skip("MONGO_MCP_TEST_RS not set - transaction tests skipped")
    mgr = ClientManager({"rs": MongoProfile(name="rs", uri=RS_URI, database=TEST_DB,
                                            server_selection_timeout_ms=5000)}, "rs")
    previous = server._manager
    server.set_manager(mgr)
    mgr.client("rs").drop_database(TEST_DB)
    yield mgr
    mgr.client("rs").drop_database(TEST_DB)
    mgr.close_all()
    server.set_manager(previous)
