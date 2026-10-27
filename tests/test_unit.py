"""Unit tests - no MongoDB needed."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from bson import Decimal128, Int64, ObjectId, Regex

from mongo_mcp.bsonutil import (ReadOnlyViolation, check_command_readonly, check_pipeline_readonly,
                                collect_documents, parse_ejson, to_jsonable)
from mongo_mcp.client import ClientManager, client_kwargs
from mongo_mcp.config import (ConfigError, MongoProfile, load_config_file, load_profiles, profile_from_env,
                              redact_uri)
from mongo_mcp.server import _sort_spec, _walk

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- config

def test_profile_auth_inference():
    assert MongoProfile(name="a", host="h").auth == "none"
    assert MongoProfile(name="a", host="h", user="u", password="p").auth == "scram"
    assert MongoProfile(name="a", uri="mongodb://h/").auth == "uri"
    assert MongoProfile(name="a", host="h", auth="Kerberos").auth == "windows"
    assert MongoProfile(name="a", host="h", auth="PLAIN", user="u", password="").auth == "ldap"


@pytest.mark.parametrize("kwargs,msg", [
    (dict(), "'host' or 'uri'"),
    (dict(uri="http://x"), "must start with mongodb"),
    (dict(host="h", auth="magic"), "'auth' must be one of"),
    (dict(host="h", auth="scram"), "requires 'user'"),
    (dict(host="h", auth="scram", user="u"), "requires a password"),
    (dict(host="h", auth="ldap", user="u"), "requires a password"),
    (dict(host="h", auth="x509"), "tls_certificate_key_file"),
    (dict(host="h", auth="uri"), "requires 'uri'"),
    (dict(host="h", port=70000), "port"),
    (dict(host="h", port="x"), "port"),
    (dict(host="h", max_docs=0), "max_docs"),
    (dict(host="h", read_preference="closest"), "read_preference"),
    (dict(host="h", readonly="maybe"), "readonly"),
])
def test_profile_validation(kwargs, msg):
    with pytest.raises(ConfigError, match=msg):
        MongoProfile(name="x", **kwargs)


def test_public_dict_and_redaction():
    p = MongoProfile(name="x", uri="mongodb://app:s3cret@h1,h2/db?replicaSet=rs", user="app", password="pw")
    d = p.public_dict()
    text = json.dumps(d)
    assert "s3cret" not in text and '"pw"' not in text and d["has_password"] is True
    assert d["uri"] == "mongodb://app:***@h1,h2/db?replicaSet=rs"
    assert "s3cret" not in repr(p)
    assert redact_uri("mongodb://h1/db") == "mongodb://h1/db"


def test_profile_from_env():
    env = {"MONGO_MCP_HOST": "srv:27017", "MONGO_MCP_USER": "u", "MONGO_MCP_PASSWORD": "p",
           "MONGO_MCP_DATABASE": "app", "MONGO_MCP_READONLY": "true"}
    p = profile_from_env(env)
    assert p.auth == "scram" and p.database == "app" and p.readonly is True and p.name == "default"
    assert profile_from_env({"MONGO_MCP_PASSWORD": "x"}) is None
    p = profile_from_env({"MONGO_MCP_HOST": "h", "MONGO_MCP_PASSWORD": "${input:pw}"})
    assert p.auth == "none" and p.password is None


def _write(tmp_path, data):
    f = tmp_path / "c.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return str(f)


def test_config_file_with_broken_profile_keeps_others(tmp_path, monkeypatch):
    monkeypatch.delenv("PW_MISSING", raising=False)
    monkeypatch.setenv("PW_OK", "secret")
    path = _write(tmp_path, {"default": "a", "connections": {
        "a": {"host": "h1", "auth": "windows"},
        "b": {"host": "h2", "user": "u", "password_env": "PW_OK"},
        "c": {"host": "h3", "user": "u", "password_env": "PW_MISSING"},
    }})
    profiles, default, broken = load_config_file(path)
    assert set(profiles) == {"a", "b"} and default == "a" and profiles["b"].password == "secret"
    assert "PW_MISSING" in broken["c"] and "mcp.json" in broken["c"] and "${input:pw_missing}" in broken["c"]
    mgr = ClientManager(profiles, default, broken, opener=lambda p: object())
    assert mgr.profile("a").host == "h1"
    with pytest.raises(ConfigError, match="PW_MISSING"):
        mgr.client("c")
    described = {d["name"]: d for d in mgr.describe()}
    assert described["c"]["broken"] is True and described["b"]["has_password"] is True


def test_default_pointing_to_broken_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    path = _write(tmp_path, {"default": "c", "connections": {
        "a": {"host": "h1"}, "c": {"host": "h3", "user": "u", "password_env": "NOPE"}}})
    profiles, default, broken = load_config_file(path)
    mgr = ClientManager(profiles, default, broken, opener=lambda p: object())
    with pytest.raises(ConfigError, match="NOPE"):
        mgr.client(None)
    assert mgr.client("a") is not None


@pytest.mark.parametrize("content,msg", [
    ("nope", "not valid JSON"), ("[]", "root"), ('{"connections": {}}', "non-empty"),
    ('{"default": "zz", "connections": {"a": {"host": "h"}}}', "Default"),
])
def test_config_file_errors(tmp_path, content, msg):
    f = tmp_path / "c.json"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=msg):
        load_config_file(str(f))


def test_unknown_setting_is_a_broken_profile(tmp_path):
    profiles, _, broken = load_config_file(_write(tmp_path, {"connections": {"a": {"host": "h", "bogus": 1}}}))
    assert not profiles and "unknown setting 'bogus'" in broken["a"]


def test_load_profiles_merges_env_and_file(tmp_path):
    path = _write(tmp_path, {"connections": {"filep": {"host": "h"}}})
    profiles, default, broken = load_profiles({"MONGO_MCP_CONFIG": path, "MONGO_MCP_URI": "mongodb://x/",
                                               "MONGO_MCP_NAME": "envp"})
    assert set(profiles) == {"filep", "envp"} and default == "envp" and not broken
    path = _write(tmp_path, {"default": "filep", "connections": {"filep": {"host": "h"}}})
    assert load_profiles({"MONGO_MCP_CONFIG": path, "MONGO_MCP_HOST": "x"})[1] == "filep"


def test_manager_errors():
    mgr = ClientManager({}, None, opener=lambda p: object())
    with pytest.raises(ConfigError, match="MONGO_MCP_HOST"):
        mgr.client(None)
    mgr = ClientManager({"Prod": MongoProfile(name="Prod", host="h")}, "Prod", opener=lambda p: object())
    assert mgr.profile("prod").name == "Prod"
    with pytest.raises(ConfigError, match="Unknown connection 'x'"):
        mgr.client("x")
    with pytest.raises(ConfigError, match="No database given"):
        mgr.database_name(None, None)


# ---------------------------------------------------------------- client kwargs

def test_client_kwargs_per_auth(tmp_path):
    host, kw = client_kwargs(MongoProfile(name="a", host="h1:27017,h2:27017", user="u", password="p",
                                          replica_set="rs0", read_preference="secondarypreferred"))
    assert host == ["h1:27017", "h2:27017"] and kw["authSource"] == "admin" and kw["replicaSet"] == "rs0"
    assert kw["username"] == "u" and kw["password"] == "p" and kw["readPreference"] == "secondaryPreferred"
    host, kw = client_kwargs(MongoProfile(name="a", host="srv", port=27100))
    assert host == "srv:27100" and "username" not in kw
    _, kw = client_kwargs(MongoProfile(name="a", host="h", auth="ldap", user="u", password="p"))
    assert kw["authMechanism"] == "PLAIN" and kw["authSource"] == "$external"
    win_env = {"USERNAME": "jdoe", "USERDNSDOMAIN": "corp.example.com"}
    _, kw = client_kwargs(MongoProfile(name="a", host="h", auth="windows"), env=win_env)
    assert kw["authMechanism"] == "GSSAPI" and kw["username"] == "jdoe@CORP.EXAMPLE.COM" and "password" not in kw
    _, kw = client_kwargs(MongoProfile(name="a", host="h", auth="windows", user="svc@REALM"), env={})
    assert kw["username"] == "svc@REALM"
    with pytest.raises(ConfigError, match="USERDNSDOMAIN"):
        client_kwargs(MongoProfile(name="a", host="h", auth="windows"), env={"USERNAME": "jdoe"})
    pem = tmp_path / "c.pem"
    pem.write_text("x")
    _, kw = client_kwargs(MongoProfile(name="a", host="h", auth="x509", tls_certificate_key_file=str(pem)))
    assert kw["authMechanism"] == "MONGODB-X509" and kw["tls"] is True and kw["tlsCertificateKeyFile"] == str(pem)
    _, kw = client_kwargs(MongoProfile(name="a", host="h", tls=True, tls_allow_invalid_certificates=True,
                                       direct_connection=True))
    assert kw["tlsAllowInvalidCertificates"] is True and kw["directConnection"] is True
    host, kw = client_kwargs(MongoProfile(name="a", uri="mongodb+srv://c.example.net/", user="u", password="p"))
    assert host == "mongodb+srv://c.example.net/" and kw["password"] == "p"


def test_kerberos_kwargs_accepted_by_pymongo():
    import pymongo
    for prof in (MongoProfile(name="a", host="h", auth="windows"),
                 MongoProfile(name="a", host="h", auth="windows", user="svc@CORP.LOCAL")):
        host, kw = client_kwargs(prof, env={"USERNAME": "jdoe", "USERDNSDOMAIN": "corp.local"})
        cli = pymongo.MongoClient(host, connect=False, **kw)  # validates option combinations
        cli.close()


# ---------------------------------------------------------------- Extended JSON

def test_parse_ejson_types():
    oid = "65a1b2c3d4e5f60718293a4b"
    doc = parse_ejson({"_id": {"$oid": oid}, "at": {"$date": "2024-01-31T10:00:00Z"},
                       "amount": {"$numberDecimal": "10.25"}, "n": {"$numberLong": "5"},
                       "q": {"$gt": 5}, "name": {"$regex": "^a", "$options": "i"}}, "filter")
    assert doc["_id"] == ObjectId(oid) and doc["at"] == dt.datetime(2024, 1, 31, 10, tzinfo=dt.timezone.utc)
    assert doc["amount"] == Decimal128("10.25") and doc["n"] == Int64(5) and doc["q"] == {"$gt": 5}
    assert isinstance(doc["name"], Regex)
    assert parse_ejson('{"a": {"$oid": "%s"}}' % oid, "f")["a"] == ObjectId(oid)
    assert parse_ejson("", "f") is None and parse_ejson(None, "f") is None
    assert parse_ejson('[{"$match": {}}]', "pipeline", expect=list) == [{"$match": {}}]


@pytest.mark.parametrize("value,kwargs,msg", [
    ("{bad json", {}, "not valid"), ("[1]", {}, "JSON object"), ('{"a":1}', {"expect": list}, "JSON array"),
    (None, {"allow_none": False}, "required"), ("  ", {"allow_none": False}, "required"),
])
def test_parse_ejson_errors(value, kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        parse_ejson(value, "x", **kwargs)


def test_to_jsonable_round_trip():
    oid = ObjectId()
    doc = {"_id": oid, "d": dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc), "dec": Decimal128("1.5"),
           "big": Int64(2**40), "tr": "şğüİ", "b": b"\x01\x02"}
    j = to_jsonable(doc)
    assert j["_id"] == {"$oid": str(oid)} and j["d"] == {"$date": "2024-01-02T00:00:00Z"}
    assert j["dec"] == {"$numberDecimal": "1.5"} and j["big"] == 2**40 and j["tr"] == "şğüİ"
    assert parse_ejson(j, "x")["_id"] == oid  # output can be fed back as input


def test_collect_documents_limits():
    res = collect_documents(iter([{"i": i} for i in range(5)]), 3)
    assert res["count"] == 3 and res["truncated"] is True and "limit of 3" in res["note"]
    res = collect_documents(iter([{"i": i} for i in range(3)]), 3)
    assert res["count"] == 3 and "truncated" not in res
    res = collect_documents(iter([{"s": "x" * 1000} for _ in range(10)]), 100, budget=3000)
    assert res["count"] == 2 and "size limit" in res["note"]


# ---------------------------------------------------------------- read-only guards

@pytest.mark.parametrize("cmd", [{"ping": 1}, {"serverStatus": 1}, {"dbStats": 1}, {"usersInfo": 1},
                                 {"aggregate": "c", "pipeline": [{"$match": {}}], "cursor": {}},
                                 {"explain": {"find": "c", "filter": {}}}, {"listCollections": 1}])
def test_readonly_allows(cmd):
    check_command_readonly(cmd)


@pytest.mark.parametrize("cmd", [{"insert": "c", "documents": [{}]}, {"delete": "c", "deletes": []},
                                 {"drop": "c"}, {"dropDatabase": 1}, {"createUser": "x", "pwd": "y", "roles": []},
                                 {"aggregate": "c", "pipeline": [{"$out": "x"}], "cursor": {}},
                                 {"explain": {"delete": "c", "deletes": []}}, {"killOp": 1, "op": 5},
                                 {"shutdown": 1}, {"collMod": "c"}])
def test_readonly_blocks(cmd):
    with pytest.raises(ReadOnlyViolation):
        check_command_readonly(cmd)


def test_pipeline_guard():
    check_pipeline_readonly([{"$match": {}}, {"$group": {"_id": 1}}])
    for stage in ({"$out": "x"}, {"$merge": {"into": "x"}}):
        with pytest.raises(ReadOnlyViolation):
            check_pipeline_readonly([{"$match": {}}, stage])


# ---------------------------------------------------------------- helpers

def test_sort_spec():
    assert _sort_spec({"a": 1, "b": -1}) == [("a", 1), ("b", -1)]
    assert _sort_spec([["a", "desc"], ["b", "asc"]]) == [("a", -1), ("b", 1)]
    assert _sort_spec('{"a": -1}') == [("a", -1)]
    assert _sort_spec(None) is None and _sort_spec({}) is None
    with pytest.raises(ValueError):
        _sort_spec({"a": 2})


def test_schema_walk():
    stats: dict = {}
    for doc in ({"a": 1, "o": {"x": "s"}, "arr": [{"k": True}]}, {"a": "two", "o": None}):
        for k, v in doc.items():
            _walk(v, k, stats, 1)
    assert stats["a"] == {"int": 1, "string": 1} and stats["o.x"] == {"string": 1}
    assert stats["arr[].k"] == {"bool": 1} and stats["o"] == {"object": 1, "null": 1}


# ---------------------------------------------------------------- bundle files

def test_bundle_templates_are_valid():
    root = ROOT / "bundle"
    for tpl in (root / "templates").glob("*.json"):
        data = json.loads(tpl.read_text(encoding="utf-8"))
        if tpl.name.startswith("mcp."):
            srv = data["servers"]["mongo"]
            assert srv["type"] == "stdio" and srv["command"].endswith("mongo-mcp.exe")
            for key, value in srv["env"].items():
                if "PASSWORD" in key or key.startswith("MONGO_PW"):
                    assert value.startswith("${input:"), key


def test_install_cmd_uses_crlf_and_offline_install():
    raw = (ROOT / "bundle" / "install.cmd").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")
    assert b"--no-index" in raw and b"wheelhouse" in raw and b"pip check" in raw


# ---------------------------------------------------------------- network diagnostics (1.0.1)

def test_server_monitoring_mode():
    _, kw = client_kwargs(MongoProfile(name="a", host="h"))
    assert kw["serverMonitoringMode"] == "poll"
    _, kw = client_kwargs(MongoProfile(name="a", host="h", server_monitoring_mode="Stream"))
    assert kw["serverMonitoringMode"] == "stream"
    with pytest.raises(ConfigError, match="server_monitoring_mode"):
        MongoProfile(name="a", host="h", server_monitoring_mode="push")
    import pymongo
    pymongo.MongoClient("h", connect=False, **client_kwargs(MongoProfile(name="a", host="h"))[1]).close()


def test_operation_cancelled_is_explained_without_guessing():
    from pymongo import errors as pe

    from mongo_mcp.errors import describe_error, is_network_error
    exc = pe._OperationCancelled("operation cancelled")
    info = describe_error(exc)
    assert info["type"] == "_OperationCancelled" and "pool of the server it was using was cleared" in info["message"]
    assert "events" in info["message"]
    for guess in ("firewall", "VPN", "unreachable replica set member"):
        assert guess not in info["message"]  # the cause must come from recorded events, not from a fixed text
    assert is_network_error(exc) and is_network_error(pe.ServerSelectionTimeoutError("x"))
    assert "Lost the connection" in describe_error(pe.AutoReconnect("connection closed"))["message"]
    assert not is_network_error(ValueError("x"))


def test_existing_client_never_opens():
    opened = []
    mgr = ClientManager({"a": MongoProfile(name="a", host="h")}, "a", opener=lambda p: opened.append(p) or object())
    assert mgr.existing_client("a") is None and mgr.existing_client("missing") is None and not opened
    cli = mgr.client("a")
    assert mgr.existing_client(None) is cli and len(opened) == 1
    assert mgr.describe()[0]["client_open"] is True


# ---------------------------------------------------------------- replica set -> direct fallback (1.0.2)

from mongo_mcp.client import _wants_fallback, direct_profile, seed_hosts  # noqa: E402


def test_seed_hosts():
    assert seed_hosts(MongoProfile(name="a", uri="mongodb://u:p@h1:1,h2:2,h3:3/?replicaSet=rs")) == ["h1:1", "h2:2", "h3:3"]
    assert seed_hosts(MongoProfile(name="a", uri="mongodb://h1/db")) == ["h1"]
    assert seed_hosts(MongoProfile(name="a", uri="mongodb://h1?replicaSet=rs")) == ["h1"]
    assert seed_hosts(MongoProfile(name="a", uri="mongodb+srv://c.example.net/")) == []
    assert seed_hosts(MongoProfile(name="a", host="h1:1, h2:2")) == ["h1:1", "h2:2"]
    assert seed_hosts(MongoProfile(name="a", host="h1", port=27012)) == ["h1:27012"]


def test_direct_profile_rewrites_uri():
    p = MongoProfile(name="a", uri="mongodb://app:s3cr%40t@h1:27012,h2:27012/mydb?readPreference=secondaryPreferred"
                                   "&replicaSet=rs0&authSource=admin")
    d = direct_profile(p, "h2:27012")
    assert d.uri.startswith("mongodb://app:s3cr%40t@h2:27012/mydb?")
    assert "replicaSet" not in d.uri and "directConnection=true" in d.uri
    assert "readPreference=secondaryPreferred" in d.uri and "authSource=admin" in d.uri
    assert d.server_selection_timeout_ms == 5000 and p.uri.count("h1") == 1  # original untouched
    d = direct_profile(MongoProfile(name="a", uri="mongodb://h1:1,h2:2?replicaSet=rs"), "h1:1")
    assert d.uri == "mongodb://h1:1/?directConnection=true"
    d = direct_profile(MongoProfile(name="a", host="h1,h2", replica_set="rs", user="u", password="p"), "h2")
    assert d.host == "h2" and d.replica_set is None and d.direct_connection is True and d.password == "p"
    import pymongo
    host, kw = client_kwargs(direct_profile(p, "h1:27012"))
    pymongo.MongoClient(host, connect=False, **kw).close()


def test_wants_fallback():
    assert _wants_fallback(MongoProfile(name="a", host="h1,h2", replica_set="rs"))
    assert _wants_fallback(MongoProfile(name="a", host="h1"))
    assert not _wants_fallback(MongoProfile(name="a", host="h1", fallback_direct=False))
    assert not _wants_fallback(MongoProfile(name="a", host="h1", direct_connection=True))
    assert not _wants_fallback(MongoProfile(name="a", uri="mongodb://h1/?directConnection=true"))
    assert not _wants_fallback(MongoProfile(name="a", uri="mongodb+srv://c.example.net/"))
    assert profile_from_env({"MONGO_MCP_HOST": "h", "MONGO_MCP_FALLBACK_DIRECT": "false"}).fallback_direct is False


class _FakeAdmin:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def command(self, name, **kw):
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return self.behaviour


class _FakeClient:
    read_preference = None

    def __init__(self, behaviour):
        self.admin = _FakeAdmin(behaviour)
        self.closed = False

    def close(self):
        self.closed = True


def _manager_with(behaviours):
    """behaviours: target ('set' or a host) -> ping/hello result or exception."""
    from mongo_mcp.client import ClientManager as CM
    opened = []

    def opener(p):
        target = "set" if p.direct_connection is not True and "directConnection" not in (p.uri or "") else (
            p.host if p.host else p.uri.split("://")[1].split("/")[0])
        cli = _FakeClient(behaviours[target])
        opened.append((target, cli))
        return cli
    prof = MongoProfile(name="m", host="h1:1,h2:2,h3:3", replica_set="rs")
    return CM({"m": prof}, "m", opener=opener, probe=True), opened


def test_fallback_tries_hosts_in_order():
    from pymongo.errors import ServerSelectionTimeoutError
    sst = ServerSelectionTimeoutError("Could not reach any servers in [('internal', 27012)], Timeout: 5s")
    mgr, opened = _manager_with({"set": sst, "h1:1": ServerSelectionTimeoutError("h1 refused, Timeout: 5s"),
                                 "h2:2": {"ok": 1, "secondary": True}, "h3:3": {"ok": 1, "isWritablePrimary": True}})
    cli = mgr.client("m")
    assert [t for t, _ in opened] == ["set", "h1:1", "h2:2"]  # h3 never tried
    assert cli is opened[2][1] and opened[0][1].closed and opened[1][1].closed
    info = mgr.fallback_info("m")
    assert info["address"] == "h2:2" and info["role"] == "secondary" and "internal" in info["reason"]
    assert mgr.describe()[0]["direct_fallback"]["address"] == "h2:2"
    mgr.reset("m")
    assert mgr.fallback_info("m") is None and mgr.existing_client("m") is None and cli.closed


def test_fallback_not_used_when_set_is_reachable():
    mgr, opened = _manager_with({"set": {"ok": 1}})
    mgr.client("m")
    assert [t for t, _ in opened] == ["set"] and mgr.fallback_info("m") is None


def test_fallback_all_hosts_fail_and_auth_errors_are_not_masked():
    from pymongo.errors import OperationFailure, ServerSelectionTimeoutError
    sst = ServerSelectionTimeoutError("no set, Timeout: 5s")
    mgr, _ = _manager_with({"set": sst, "h1:1": ServerSelectionTimeoutError("h1 down, Timeout: 1s"),
                            "h2:2": ServerSelectionTimeoutError("h2 down, Timeout: 1s"),
                            "h3:3": ServerSelectionTimeoutError("h3 down, Timeout: 1s")})
    with pytest.raises(ServerSelectionTimeoutError, match="h1 down.*h2 down.*h3 down"):
        mgr.client("m")
    assert mgr.existing_client("m") is None
    mgr, _ = _manager_with({"set": sst, "h1:1": OperationFailure("Authentication failed", 18), "h2:2": {"ok": 1},
                            "h3:3": {"ok": 1}})
    with pytest.raises(OperationFailure, match="Authentication failed"):
        mgr.client("m")


# ---------------------------------------------------------------- RCA 2026-10-27 follow-ups (1.0.3)

import asyncio as _asyncio  # noqa: E402
import datetime as _dt  # noqa: E402

from pymongo import errors as _pe  # noqa: E402
from pymongo import monitoring as _mon  # noqa: E402

from mongo_mcp import server as _server  # noqa: E402
from mongo_mcp.events import EventRecorder  # noqa: E402


def test_event_recorder_keeps_evidence_without_payloads():
    rec = EventRecorder()
    rec.failed(_mon.ServerHeartbeatFailedEvent(0.25, _pe.NetworkTimeout("db01:27012: timed out"), ("db01", 27012)))
    rec.pool_cleared(_mon.PoolClearedEvent(("db01", 27012), None, True))
    rec.failed(_mon.CommandFailedEvent(_dt.timedelta(milliseconds=12), {"errmsg": "operation cancelled", "ok": 0},
                                       "listCollections", 1, ("db01", 27012), 1))
    rec.started(object())
    rec.succeeded(object())
    events = rec.recent()
    assert [e["event"] for e in events] == ["heartbeat_failed", "pool_cleared", "command_failed"]
    assert events[0]["address"] == "db01:27012" and events[0]["duration_ms"] == 250.0 and "timed out" in events[0]["error"]
    assert events[1]["interrupted_in_use_connections"] is True
    assert events[2]["command"] == "listCollections" and events[2]["error"] == "operation cancelled"
    assert all("at" in e and "_mono" not in e for e in events)
    assert rec.recent(seconds=-1) == []
    for _ in range(300):
        rec.pool_cleared(_mon.PoolClearedEvent(("h", 1)))
    assert len(rec.recent(limit=1000)) == 200  # bounded ring buffer


class _SeqAdmin:
    """admin.command() returning / raising the next item of a sequence (last one repeats)."""

    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0

    def command(self, name, **kw):
        item = self.seq[min(self.calls, len(self.seq) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


class _SeqClient:
    read_preference = None

    def __init__(self, seq):
        self.admin = _SeqAdmin(seq)
        self.closed = False

    def close(self):
        self.closed = True


def _seq_manager(set_seq, hosts):
    opened = []

    def opener(p):
        direct = p.direct_connection is True
        cli = _SeqClient(hosts[p.host] if direct else set_seq)
        opened.append((p.host if direct else "set", cli))
        return cli
    prof = MongoProfile(name="m", host="h1:1,h2:2", replica_set="rs")
    return ClientManager({"m": prof}, "m", opener=opener, probe=True), opened


def test_probe_retries_operation_cancelled_once_before_falling_back():
    cancelled = _pe._OperationCancelled("operation cancelled")
    mgr, opened = _seq_manager([cancelled, {"ok": 1}], {})
    mgr.client("m")
    assert [t for t, _ in opened] == ["set"] and opened[0][1].admin.calls == 2 and mgr.fallback_info("m") is None


def test_probe_falls_back_after_repeated_operation_cancelled():
    cancelled = _pe._OperationCancelled("operation cancelled")
    mgr, opened = _seq_manager([cancelled], {"h1:1": [{"ok": 1, "isWritablePrimary": True}], "h2:2": [{"ok": 1}]})
    mgr.client("m")
    assert [t for t, _ in opened] == ["set", "h1:1"] and opened[0][1].closed
    assert mgr.fallback_info("m")["role"] == "primary" and "operation cancelled" in mgr.fallback_info("m")["reason"]


def test_connect_failures_are_marked_as_connect_stage():
    sst = _pe.ServerSelectionTimeoutError("no set, Timeout: 1s")
    mgr, _ = _seq_manager([sst], {"h1:1": [_pe.ServerSelectionTimeoutError("down, Timeout: 1s")],
                                  "h2:2": [_pe.ServerSelectionTimeoutError("down, Timeout: 1s")]})
    with pytest.raises(_pe.ServerSelectionTimeoutError) as info:
        mgr.client("m")
    assert info.value.mcp_stage == "connect"


class _Closable:
    def close(self):
        pass


@pytest.fixture
def fake_mgr():
    opened = []
    mgr = ClientManager({"m": MongoProfile(name="m", host="h")}, "m",
                        opener=lambda p: opened.append(_Closable()) or opened[-1])
    previous = _server._manager
    _server.set_manager(mgr)
    yield mgr, opened
    _server.set_manager(previous)


def _run(fn, kind):
    return json.loads(_asyncio.run(_server._call(fn, "m", kind=kind)))


def test_read_is_retried_once_on_a_fresh_client(fake_mgr):
    mgr, opened = fake_mgr
    calls = []

    def fn():
        cli = mgr.client("m")
        calls.append(cli)
        if len(calls) == 1:
            raise _pe._OperationCancelled("operation cancelled")
        return {"value": 1}
    res = _run(fn, "read")
    assert res["success"] and res["value"] == 1 and res["retried_after"]["type"] == "_OperationCancelled"
    assert len(calls) == 2 and calls[0] is not calls[1] and len(opened) == 2  # second attempt used a new client


def test_read_failing_twice_resets_and_reports(fake_mgr):
    mgr, opened = fake_mgr

    def fn():
        mgr.client("m")
        raise _pe.AutoReconnect("db01:27012: connection closed")
    res = _run(fn, "read")
    err = res["error"]
    assert res["success"] is False and err["retried"] is True and err["stage"] == "operation"
    assert "write_outcome" not in err and mgr.existing_client("m") is None and len(opened) == 2


def test_write_is_never_retried_and_outcome_is_unknown(fake_mgr):
    mgr, _ = fake_mgr
    calls = []

    def fn():
        mgr.client("m")
        calls.append(1)
        raise _pe._OperationCancelled("operation cancelled")
    err = _run(fn, "write")["error"]
    assert len(calls) == 1 and err["write_outcome"].startswith("unknown") and "retried" not in err
    assert mgr.existing_client("m") is None  # next call reconnects


def test_server_selection_timeout_and_other_errors_are_not_retried(fake_mgr):
    mgr, _ = fake_mgr
    for exc in (_pe.ServerSelectionTimeoutError("no servers"), ValueError("bad input"),
                _pe.OperationFailure("Unauthorized", 13)):
        calls = []

        def fn(exc=exc):
            calls.append(1)
            raise exc
        res = _run(fn, "read")
        assert res["success"] is False and len(calls) == 1, exc


def test_explore_commands_use_profile_read_preference():
    seen = {}

    class _Db:
        class client:
            read_preference = "secondaryPreferred-sentinel"

        def command(self, cmd, **kw):
            seen.update(cmd=cmd, **kw)
            return {"cursor": {"firstBatch": [{"name": "a"}], "id": 0}}
    items, more = _server._first_batch(_Db(), {"listCollections": 1})
    assert items == [{"name": "a"}] and more is False
    assert seen["read_preference"] == "secondaryPreferred-sentinel" and seen["cmd"]["cursor"]["batchSize"] == 100000


def test_runtime_info():
    info = _server.runtime_info()
    import os as _os
    assert info["pid"] == _os.getpid() and info["mongo_mcp"] and info["python"].endswith("-bit)")
    assert info["module"].endswith("mongo_mcp")
