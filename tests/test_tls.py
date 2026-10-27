"""TLS / SSL: verification switch (tls_insecure) and detailed diagnosis of TLS connection problems.

Unit tests always run. Integration tests need TLS servers created from tests/make_tls_certs.py::

    MONGO_MCP_TEST_TLS="dir=/path/to/certs;good=localhost:27060;wronghost=localhost:27061;mtls=localhost:27063"

good:      --tlsMode requireTLS, server.pem, --tlsCAFile ca.pem --tlsAllowConnectionsWithoutCertificates
wronghost: same with wronghost.pem
mtls:      --tlsMode requireTLS, server.pem, --tlsCAFile ca.pem (client certificate required)
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time

import pytest

from mongo_mcp import server as T
from mongo_mcp import tls as tls_mod
from mongo_mcp.client import ClientManager, client_kwargs
from mongo_mcp.config import ConfigError, MongoProfile, profile_from_env, profile_from_mapping

from .conftest import TARGETS, call, err, ok

# ---------------------------------------------------------------------------------------------- unit

REAL_MESSAGES = {
    "untrusted_certificate": "localhost:27060: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                             "self-signed certificate in certificate chain (_ssl.c:1032)",
    "hostname_mismatch": "localhost:27061: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname "
                         "mismatch, certificate is not valid for 'localhost'. (_ssl.c:1032)",
    "certificate_expired": "h:1: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired",
    "handshake_failed": "SSL handshake failed: localhost:27082: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in "
                        "violation of protocol (_ssl.c:1032)",
    "client_certificate_file": "Private key doesn't match certificate: [SSL] PEM lib (_ssl.c:4129)",
    "protocol_mismatch": "h:1: [SSL: UNSUPPORTED_PROTOCOL] unsupported protocol (_ssl.c:1000)",
    "server_not_tls": "h:1: [SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1000)",
    "weak_certificate": "h:1: [SSL: CA_MD_TOO_WEAK] ca md too weak (_ssl.c:1000)",
    "client_certificate_required": "h:1: [SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED] tlsv13 alert certificate required",
}


@pytest.mark.parametrize("problem,text", sorted(REAL_MESSAGES.items()))
def test_classify_real_driver_messages(problem, text):
    assert tls_mod.classify(text) == problem


@pytest.mark.parametrize("text", ["localhost:1: [Errno 111] Connection refused", "connection closed",
                                  "Authentication failed.", "timed out", "Private key file is fine",
                                  "operation cancelled"])
def test_non_tls_messages_are_not_classified(text):
    assert tls_mod.classify(text) is None


def test_tls_insecure_disables_both_checks():
    p = MongoProfile(name="p", host="h", tls_insecure="true")
    assert p.tls is True  # implies TLS
    kw = client_kwargs(p)[1]
    assert kw["tls"] and kw["tlsAllowInvalidCertificates"] and kw["tlsAllowInvalidHostnames"]
    assert "tlsInsecure" not in kw
    s = tls_mod.settings(p)
    assert s["verification"] == "disabled" and "test environments" in s["warning"]


def test_tls_verification_is_on_by_default():
    p = MongoProfile(name="p", host="h", tls=True)
    kw = client_kwargs(p)[1]
    assert "tlsAllowInvalidCertificates" not in kw and "tlsAllowInvalidHostnames" not in kw
    assert tls_mod.settings(p) == {"enabled": True, "verification": "full", "ca_file": "system certificate store"}
    assert tls_mod.settings(MongoProfile(name="p", host="h")) == {"enabled": False}


def test_tls_insecure_with_uri_options_never_conflicts():
    import pymongo

    # the driver refuses tlsInsecure together with tlsAllowInvalid*; the profile switch must work with either in the URI
    for uri in ("mongodb://h/?tlsInsecure=false", "mongodb://h/?tlsAllowInvalidCertificates=false",
                "mongodb://h/?tls=true&tlsAllowInvalidHostnames=true", "mongodb://h/"):
        prof = MongoProfile(name="p", uri=uri, tls_insecure=True)
        host, kw = client_kwargs(prof)
        cli = pymongo.MongoClient(host, connect=False, **kw)
        try:
            opts = cli.options.pool_options._ssl_context  # noqa: SLF001 - verify the effective SSL context
            assert opts is not None
            assert opts.verify_mode == ssl.CERT_NONE
        finally:
            cli.close()
        assert tls_mod.settings(prof)["verification"] == "disabled"


def test_tls_settings_from_uri_options():
    assert tls_mod.settings(MongoProfile(name="p", uri="mongodb://h/?tls=true&tlsInsecure=true"))[
        "verification"] == "disabled"
    assert tls_mod.settings(MongoProfile(name="p", uri="mongodb://h/?ssl=true&tlsAllowInvalidHostnames=true"))[
        "verification"].startswith("host name not verified")
    assert tls_mod.settings(MongoProfile(name="p", uri="mongodb+srv://c.example.net/"))["enabled"] is True
    assert tls_mod.settings(MongoProfile(name="p", uri="mongodb+srv://c.example.net/?tls=false"))["enabled"] is False
    assert tls_mod.settings(MongoProfile(name="p", host="h", tls_allow_invalid_hostnames=True))[
        "verification"].startswith("host name not verified")


def test_tls_insecure_from_env_and_json():
    prof = profile_from_env({"MONGO_MCP_HOST": "h", "MONGO_MCP_TLS_INSECURE": "true"})
    assert prof.tls_insecure is True and prof.tls is True
    prof = profile_from_mapping("p", {"host": "h", "tls_insecure": True, "tls_allow_invalid_hostnames": "yes"})
    assert prof.tls_insecure and prof.tls_allow_invalid_hostnames
    with pytest.raises(ConfigError):
        MongoProfile(name="p", host="h", tls_insecure="maybe")


def test_missing_certificate_files_are_reported_clearly(tmp_path):
    missing = str(tmp_path / "nope.pem")
    with pytest.raises(ConfigError, match="tls_ca_file .*does not exist"):
        client_kwargs(MongoProfile(name="p", host="h", tls_ca_file=missing))
    with pytest.raises(ConfigError, match="tls_certificate_key_file .*does not exist"):
        client_kwargs(MongoProfile(name="p", host="h", tls_certificate_key_file=missing))


def test_diagnose_without_probe_explains_and_strips_timeouts():
    from pymongo import errors as pe

    exc = pe.ServerSelectionTimeoutError(
        REAL_MESSAGES["untrusted_certificate"] + " (configured timeouts: socketTimeoutMS: 20000.0ms, "
        "connectTimeoutMS: 20000.0ms), Timeout: 2.5s, Topology Description: <...>")
    d = tls_mod.diagnose(exc, MongoProfile(name="p", host="localhost:27060", tls=True), None, do_probe=False)
    assert d["problem"] == "untrusted_certificate" and d["server"] == "localhost:27060"
    assert "configured timeouts" not in d["driver_detail"] and "self-signed" in d["driver_detail"]
    assert any("tls_ca_file" in f for f in d["how_to_fix"]) and d["test_only_workaround"]["connections.json"] == {
        "tls_insecure": True}


def test_diagnose_prefers_per_server_errors_and_ignores_other_errors():
    from pymongo import errors as pe

    prof = MongoProfile(name="p", host="a:1,b:2", tls=True)
    servers = [{"address": "a:1", "type": "Unknown", "error": "a:1: [Errno 111] Connection refused"},
               {"address": "b:2", "type": "Unknown", "error": REAL_MESSAGES["hostname_mismatch"]}]
    d = tls_mod.diagnose(pe.ServerSelectionTimeoutError("x"), prof, servers, do_probe=False)
    assert d["problem"] == "hostname_mismatch" and d["server"] == "b:2"
    assert tls_mod.diagnose(pe.ServerSelectionTimeoutError("a:1: [Errno 111] Connection refused"), prof,
                            None, do_probe=False) is None
    # 'connection closed' is ambiguous: without the probe's evidence nothing is claimed
    assert tls_mod.diagnose(pe.AutoReconnect("a:1: connection closed"), prof, None, do_probe=False) is None


def test_list_connections_warns_about_disabled_verification():
    mgr = ClientManager({"t": MongoProfile(name="t", host="h", tls_insecure=True),
                         "s": MongoProfile(name="s", host="h", tls=True)}, "t", opener=lambda p: None)
    by_name = {c["name"]: c for c in mgr.describe()}
    assert "test environments" in by_name["t"]["tls_warning"] and "tls_warning" not in by_name["s"]
    assert by_name["t"]["tls_insecure"] is True


# ---------------------------------------------------------------------------------------------- integration

def _tls_env() -> dict[str, str] | None:
    raw = os.environ.get("MONGO_MCP_TEST_TLS")
    if not raw:
        return None
    return dict(item.split("=", 1) for item in raw.split(";") if "=" in item)


TLS = _tls_env()
needs_tls = pytest.mark.skipif(TLS is None, reason="MONGO_MCP_TEST_TLS not set - TLS integration tests skipped")


def _cert(name: str) -> str:
    return os.path.join(TLS["dir"], name)


@pytest.fixture
def use():
    """use(name, **profile) -> name; installs a manager with that one profile."""
    previous = T._manager
    managers = []

    def make(name: str, **kw):
        mgr = ClientManager({name: MongoProfile(name=name, server_selection_timeout_ms=2500, **kw)}, name)
        managers.append(mgr)
        T.set_manager(mgr)
        return name
    yield make
    for mgr in managers:
        mgr.close_all()
    T.set_manager(previous)


def _tls_error(name: str) -> dict:
    e = err(call(T.test_connection, connection=name))
    assert e["message"].startswith("TLS/SSL problem"), e
    return e


@pytest.mark.integration
@needs_tls
def test_untrusted_server_certificate_is_explained_with_the_certificate(use):
    started = time.monotonic()
    e = _tls_error(use("u", host=TLS["good"], tls=True))
    t = e["tls"]
    assert t["problem"] == "untrusted_certificate" and t["server"] == TLS["good"]
    cert = t["evidence"]["certificate"]
    assert "mongo-mcp test CA" in cert["issuer"] and "localhost" in cert["names"] and not cert["expired"]
    assert len(cert["sha256"]) == 64 and t["evidence"]["protocol"].startswith("TLS")
    assert t["settings"]["verification"] == "full" and t["test_only_workaround"]
    assert e["stage"] == "connect" and any("CERTIFICATE_VERIFY_FAILED" in s.get("error", "") for s in e["servers"])
    assert time.monotonic() - started < 15  # no pointless direct fallback for a TLS failure
    assert T.manager().fallback_info("u") is None


@pytest.mark.integration
@needs_tls
def test_wrong_ca_file_is_explained(use):
    t = _tls_error(use("w", host=TLS["good"], tls_ca_file=_cert("other-ca.pem")))["tls"]
    assert t["problem"] == "untrusted_certificate" and t["settings"]["ca_file"].endswith("other-ca.pem")
    assert "other-ca.pem" in t["summary"]


@pytest.mark.integration
@needs_tls
def test_tls_insecure_connects_and_warns(use):
    info = ok(call(T.test_connection, connection=use("i", host=TLS["good"], tls_insecure=True)))["server"]
    assert info["tls"]["verification"] == "disabled" and "test environments" in info["tls"]["warning"]
    assert ok(call(T.list_connections))["connections"][0]["tls_warning"]
    # also through the URI, and with the switch combined with conflicting-looking URI options
    for uri in (f"mongodb://{TLS['good']}/?tls=true&tlsInsecure=true",
                f"mongodb://{TLS['good']}/?tls=true&tlsAllowInvalidCertificates=false"):
        name = use("iu", uri=uri, tls_insecure=True)
        assert ok(call(T.test_connection, connection=name))["server"]["tls"]["verification"] == "disabled"


@pytest.mark.integration
@needs_tls
def test_tls_with_the_right_ca_verifies_fully(use):
    info = ok(call(T.test_connection, connection=use("c", host=TLS["good"], tls_ca_file=_cert("ca.pem"))))["server"]
    assert info["tls"]["verification"] == "full" and "warning" not in info["tls"]


@pytest.mark.integration
@needs_tls
def test_hostname_mismatch_lists_the_certificate_names(use):
    t = _tls_error(use("h", host=TLS["wronghost"], tls_ca_file=_cert("ca.pem")))["tls"]
    assert t["problem"] == "hostname_mismatch" and t["evidence"]["certificate"]["names"] == ["wronghost.example"]
    assert "wronghost.example" in t["how_to_fix"][0]
    port = TLS["wronghost"].rsplit(":", 1)[1]
    t = _tls_error(use("ip", host=f"127.0.0.1:{port}", tls_ca_file=_cert("ca.pem")))["tls"]
    assert t["problem"] == "hostname_mismatch" and "127.0.0.1" in t["summary"]
    info = ok(call(T.test_connection, connection=use("hok", host=TLS["wronghost"], tls_ca_file=_cert("ca.pem"),
                                                     tls_allow_invalid_hostnames=True)))["server"]
    assert info["tls"]["verification"].startswith("host name not verified")


@pytest.fixture
def expired_server():
    """A bare TLS endpoint presenting an expired certificate (mongod refuses to start with one)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(_cert("expired.pem"))
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            try:
                with ctx.wrap_socket(conn, server_side=True) as tls_conn:
                    tls_conn.settimeout(1)
                    tls_conn.recv(1)
            except OSError:
                pass
            finally:
                conn.close()
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    yield f"localhost:{srv.getsockname()[1]}"
    stop.set()
    thread.join(2)
    srv.close()


@pytest.mark.integration
@needs_tls
def test_expired_certificate_shows_its_validity(use, expired_server):
    t = _tls_error(use("x", host=expired_server, tls_ca_file=_cert("ca.pem")))["tls"]
    cert = t["evidence"]["certificate"]
    assert t["problem"] == "certificate_expired" and cert["expired"] is True
    assert cert["valid_until"].startswith("2021-01-01") and "2021-01-01" in t["summary"]


@pytest.mark.integration
@needs_tls
def test_server_requiring_tls_is_detected(use):
    t = _tls_error(use("p", host=TLS["good"], fallback_direct=False))["tls"]
    assert t["problem"] == "server_requires_tls" and t["evidence"]["server_speaks_tls"] is True
    assert t["driver_detail"].endswith("connection closed") and '"tls": true' in t["how_to_fix"][0]


@pytest.mark.integration
@needs_tls
@pytest.mark.skipif(not TARGETS, reason="needs a plain (non-TLS) server from MONGO_MCP_TEST_TARGETS")
def test_tls_against_a_server_without_tls(use):
    host = TARGETS[0][1].split("@")[-1].split("/")[0]
    t = _tls_error(use("n", host=host, tls=True))["tls"]
    assert t["problem"] == "server_not_tls" and t["evidence"]["server_speaks_tls"] is False
    assert t["evidence"]["server_answers_without_tls"] is True


@pytest.mark.integration
@needs_tls
def test_mutual_tls_client_certificate(use):
    t = _tls_error(use("m0", host=TLS["mtls"], tls_ca_file=_cert("ca.pem")))["tls"]
    assert t["problem"] == "client_certificate_required" and "tls_certificate_key_file" in t["how_to_fix"][0]
    assert t["evidence"]["session"].startswith("closed")
    t = _tls_error(use("m1", host=TLS["mtls"], tls_ca_file=_cert("ca.pem"),
                       tls_certificate_key_file=_cert("foreign-client.pem")))["tls"]
    assert t["problem"] == "client_certificate_rejected" and "foreign-client.pem" in t["summary"]
    ok(call(T.test_connection, connection=use("m2", host=TLS["mtls"], tls_ca_file=_cert("ca.pem"),
                                              tls_certificate_key_file=_cert("client.pem"))))


@pytest.mark.integration
@needs_tls
def test_invalid_client_certificate_file(use):
    e = err(call(T.test_connection, connection=use("k", host=TLS["mtls"], tls_ca_file=_cert("ca.pem"),
                                                   tls_certificate_key_file=_cert("ca.pem"))))
    assert e["tls"]["problem"] == "client_certificate_file" and "PEM" in e["tls"]["summary"]


@pytest.mark.integration
@needs_tls
def test_tool_errors_carry_the_tls_diagnosis_and_are_not_retried(use):
    name = use("f", host=TLS["good"], tls=True, database="x")
    e = err(call(T.find, collection="c", connection=name))
    assert e["tls"]["problem"] == "untrusted_certificate" and "retried" not in e


@pytest.mark.integration
@needs_tls
def test_several_hosts_all_failing_tls_skip_the_direct_fallback(use):
    started = time.monotonic()
    e = _tls_error(use("multi", host=f"{TLS['good']},{TLS['wronghost']}", tls_ca_file=_cert("other-ca.pem")))
    assert time.monotonic() - started < 15 and T.manager().fallback_info("multi") is None
    assert {s["address"] for s in e["servers"]} == {TLS["good"], TLS["wronghost"]}
    assert e["tls"]["problem"] == "untrusted_certificate"


@pytest.mark.integration
@needs_tls
def test_check_command_reports_tls_problems():
    env = {**os.environ, "MONGO_MCP_HOST": TLS["good"], "MONGO_MCP_TLS": "true",
           "MONGO_MCP_SERVER_SELECTION_TIMEOUT_MS": "2500"}
    env.pop("MONGO_MCP_CONFIG", None)
    out = subprocess.run([sys.executable, "-m", "mongo_mcp", "--check"], env=env, capture_output=True, text=True,
                         timeout=120)
    report = json.loads(out.stdout)
    assert out.returncode == 1
    (conn,) = report["connections"].values()
    assert conn["error"]["tls"]["problem"] == "untrusted_certificate"
    env["MONGO_MCP_TLS_INSECURE"] = "true"
    out = subprocess.run([sys.executable, "-m", "mongo_mcp", "--check"], env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stdout
    (conn,) = json.loads(out.stdout)["connections"].values()
    assert conn["server"]["tls"]["verification"] == "disabled"
