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

def test_client_kwargs_per_auth():
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
    _, kw = client_kwargs(MongoProfile(name="a", host="h", auth="x509", tls_certificate_key_file="c.pem"))
    assert kw["authMechanism"] == "MONGODB-X509" and kw["tls"] is True and kw["tlsCertificateKeyFile"] == "c.pem"
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
