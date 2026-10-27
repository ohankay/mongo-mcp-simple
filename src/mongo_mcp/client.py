"""MongoClient construction and the per-profile client cache."""

from __future__ import annotations

import dataclasses
import os
import threading
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlencode

from .config import (AUTH_LDAP, AUTH_NONE, AUTH_SCRAM, AUTH_URI, AUTH_WINDOWS, AUTH_X509, ConfigError,
                     MongoProfile)


def windows_principal(env: Mapping[str, str] | None = None) -> str:
    """Kerberos principal of the logged-on Windows domain user, e.g. jdoe@CORP.EXAMPLE.COM."""
    env = os.environ if env is None else env
    user, realm = env.get("USERNAME"), env.get("USERDNSDOMAIN")
    if not user or not realm:
        raise ConfigError(
            "auth 'windows' (Kerberos) needs the Windows domain user, but USERNAME / USERDNSDOMAIN are not set "
            "(not a domain logon?). Set 'user' to your Kerberos principal, e.g. \"jdoe@CORP.EXAMPLE.COM\"."
        )
    return f"{user}@{realm.upper()}"


def client_kwargs(profile: MongoProfile, env: Mapping[str, str] | None = None) -> tuple[str | list[str], dict[str, Any]]:
    """Return (host argument, keyword arguments) for pymongo.MongoClient."""
    kw: dict[str, Any] = {
        "appname": profile.app_name,
        "serverSelectionTimeoutMS": profile.server_selection_timeout_ms,
        "connectTimeoutMS": min(profile.server_selection_timeout_ms, 20000),
        "tz_aware": True,
        "uuidRepresentation": "standard",
        # "poll" uses short request/response heartbeats instead of long-lived streaming ones that
        # firewalls/VPNs tend to cut (which makes the driver reset pools and cancel running operations).
        "serverMonitoringMode": profile.server_monitoring_mode,
    }
    if profile.uri:
        host: str | list[str] = profile.uri
    else:
        host = [h.strip() for h in str(profile.host).split(",") if h.strip()]
        if profile.port and len(host) == 1 and ":" not in host[0]:
            host = [f"{host[0]}:{profile.port}"]
        if len(host) == 1:
            host = host[0]
    if profile.auth == AUTH_SCRAM:
        kw["username"] = profile.user
        kw["password"] = profile.password
        kw["authSource"] = profile.auth_source or "admin"
        if profile.auth_mechanism:
            kw["authMechanism"] = profile.auth_mechanism
    elif profile.auth == AUTH_LDAP:
        kw.update(username=profile.user, password=profile.password, authMechanism="PLAIN",
                  authSource=profile.auth_source or "$external")
    elif profile.auth == AUTH_WINDOWS:
        # Kerberos with the logged-on Windows user (winkerberos/SSPI uses the current logon credentials when
        # no password is given); the principal defaults to USERNAME@USERDNSDOMAIN.
        kw.update(authMechanism="GSSAPI", authSource="$external",
                  username=profile.user or windows_principal(env))
        if profile.password is not None:
            kw["password"] = profile.password
    elif profile.auth == AUTH_X509:
        kw.update(authMechanism="MONGODB-X509", authSource="$external")
        if profile.user:
            kw["username"] = profile.user
    elif profile.auth == AUTH_URI:
        # credentials come from the URI; explicit user/password (e.g. password_env) override it
        if profile.user:
            kw["username"] = profile.user
        if profile.password is not None:
            kw["password"] = profile.password
        if profile.auth_source:
            kw["authSource"] = profile.auth_source
    elif profile.auth != AUTH_NONE:  # pragma: no cover - validated in config
        raise ConfigError(f"Unsupported auth {profile.auth!r}")
    if profile.replica_set:
        kw["replicaSet"] = profile.replica_set
    if profile.direct_connection is not None:
        kw["directConnection"] = profile.direct_connection
    if profile.tls:
        kw["tls"] = True
        if profile.tls_allow_invalid_certificates:
            kw["tlsAllowInvalidCertificates"] = True
        if profile.tls_ca_file:
            kw["tlsCAFile"] = profile.tls_ca_file
        if profile.tls_certificate_key_file:
            kw["tlsCertificateKeyFile"] = profile.tls_certificate_key_file
    if profile.read_preference:
        kw["readPreference"] = profile.read_preference
    return host, kw


def seed_hosts(profile: MongoProfile) -> list[str]:
    """Hosts the user listed (not the ones the replica set advertises). Empty for mongodb+srv."""
    if profile.uri:
        if profile.uri.startswith("mongodb+srv://"):
            return []
        authority = profile.uri.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0]
        hosts = authority.rpartition("@")[2]
    else:
        hosts = str(profile.host)
    out = [h.strip() for h in hosts.split(",") if h.strip()]
    if profile.port and len(out) == 1 and ":" not in out[0]:
        out = [f"{out[0]}:{profile.port}"]
    return out


def direct_profile(profile: MongoProfile, host: str) -> MongoProfile:
    """Same credentials/options, but a direct connection to one host (no replica set discovery)."""
    timeout = min(profile.server_selection_timeout_ms, 5000)
    if profile.uri:
        scheme, rest = profile.uri.split("://", 1)
        authority, _, tail = rest.partition("/")
        creds = authority.rpartition("@")[0]
        path, _, query = tail.partition("?")
        if "?" in authority:  # mongodb://host?opts without a slash
            authority, _, query = authority.partition("?")
            creds = authority.rpartition("@")[0]
        opts = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
                if k.lower() not in ("replicaset", "directconnection", "loadbalanced")]
        opts.append(("directConnection", "true"))
        uri = f"{scheme}://{creds + '@' if creds else ''}{host}/{path}?{urlencode(opts)}"
        return dataclasses.replace(profile, uri=uri, direct_connection=None, server_selection_timeout_ms=timeout)
    return dataclasses.replace(profile, host=host, port=None, replica_set=None, direct_connection=True,
                               server_selection_timeout_ms=timeout)


def _wants_fallback(profile: MongoProfile) -> bool:
    if not profile.fallback_direct or profile.direct_connection is True:
        return False
    if profile.uri and "directconnection=true" in profile.uri.lower():
        return False
    return bool(seed_hosts(profile))


def open_client(profile: MongoProfile) -> Any:
    import pymongo

    host, kw = client_kwargs(profile)
    return pymongo.MongoClient(host, **kw)


class ClientManager:
    """Holds one pooled MongoClient per profile (MongoClient is thread-safe)."""

    def __init__(self, profiles: dict[str, MongoProfile], default: str | None, broken: dict[str, str] | None = None,
                 opener: Callable[[MongoProfile], Any] = open_client, probe: bool | None = None):
        self._profiles = dict(profiles)
        self._broken = dict(broken or {})
        self._default = default
        self._clients: dict[str, Any] = {}
        self._fallback: dict[str, dict[str, Any]] = {}
        self._last_failure: dict[str, list[dict[str, Any]]] = {}
        self._opener = opener
        # Probing needs real clients; injected test openers skip it unless asked.
        self._probe = (opener is open_client) if probe is None else probe
        self._lock = threading.Lock()

    @property
    def default(self) -> str | None:
        return self._default

    def _resolve(self, name: str | None) -> str:
        if not name:
            if self._default is None:
                if self._broken:
                    raise ConfigError("No usable connection. Broken profiles: " + "; ".join(self._broken.values()))
                raise ConfigError(
                    "No MongoDB connection configured. Set MONGO_MCP_HOST or MONGO_MCP_URI (+ MONGO_MCP_USER / "
                    "MONGO_MCP_PASSWORD) in the MCP server env, point MONGO_MCP_CONFIG at a JSON file, or call "
                    "add_connection."
                )
            name = self._default
        if name in self._profiles:
            return name
        if name in self._broken:
            raise ConfigError(self._broken[name])
        lowered = {k.lower(): k for k in list(self._profiles) + list(self._broken)}
        if name.lower() in lowered:
            return self._resolve(lowered[name.lower()])
        names = ", ".join(list(self._profiles) + list(self._broken)) or "(none)"
        raise ConfigError(f"Unknown connection '{name}'. Available: {names}")

    def profile(self, name: str | None) -> MongoProfile:
        return self._profiles[self._resolve(name)]

    def client(self, name: str | None) -> Any:
        key = self._resolve(name)
        with self._lock:
            cli = self._clients.get(key)
            if cli is None:
                cli = self._connect(key, self._profiles[key])
                self._clients[key] = cli
            return cli

    def _connect(self, key: str, prof: MongoProfile) -> Any:
        """Open the normal (replica-set aware) client. If no member suitable for the profile's read preference
        can be reached - typically because the replica set advertises internal host names or ports - try the
        hosts listed by the user one by one with a direct connection and keep the first that answers."""
        cli = self._opener(prof)
        if not (self._probe and _wants_fallback(prof)):
            return cli
        from pymongo import errors as pe

        try:
            cli.admin.command("ping", read_preference=cli.read_preference)
            return cli
        except pe.ServerSelectionTimeoutError as exc:
            first_error = exc
        cli.close()
        tried: list[str] = []
        failures: list[dict[str, Any]] = []
        for host in seed_hosts(prof):
            direct = self._opener(direct_profile(prof, host))
            try:
                hello = direct.admin.command("hello")
            except pe.OperationFailure:
                direct.close()
                raise  # wrong credentials etc. - not a reachability problem
            except pe.PyMongoError as exc:
                direct.close()
                detail = str(exc).split(", Timeout")[0][:300]
                tried.append(f"{host}: {detail}")
                failures.append({"address": host, "type": "Unknown", "error": detail, "tried": "direct"})
                continue
            self._fallback[key] = {
                "address": host,
                "role": "primary" if hello.get("isWritablePrimary") else (
                    "secondary" if hello.get("secondary") else "other"),
                "reason": str(first_error).split(", Timeout")[0][:300],
            }
            self._last_failure.pop(key, None)
            return direct
        self._last_failure[key] = failures
        raise pe.ServerSelectionTimeoutError(
            f"{str(first_error).split(', Timeout')[0]}. Direct connection to each listed host also failed: "
            + "; ".join(tried))

    def last_failure(self, name: str | None) -> list[dict[str, Any]] | None:
        """Per-host results of the last connection attempt that failed completely."""
        try:
            return self._last_failure.get(self._resolve(name))
        except ConfigError:
            return None

    def fallback_info(self, name: str | None) -> dict[str, Any] | None:
        try:
            return self._fallback.get(self._resolve(name))
        except ConfigError:
            return None

    def reset(self, name: str | None) -> None:
        """Drop the cached client so the next call reconnects (and re-probes the replica set)."""
        try:
            key = self._resolve(name)
        except ConfigError:
            return
        with self._lock:
            cli = self._clients.pop(key, None)
            self._fallback.pop(key, None)
        if cli is not None:
            cli.close()

    def existing_client(self, name: str | None) -> Any:
        """The already-open client for a profile, or None (never opens a new one)."""
        try:
            key = self._resolve(name)
        except ConfigError:
            return None
        return self._clients.get(key)

    def database_name(self, name: str | None, database: str | None) -> str:
        db = database or self.profile(name).database
        if not db:
            raise ConfigError("No database given: pass 'database' or set 'database' in the connection profile")
        return db

    def add(self, profile: MongoProfile, make_default: bool = False) -> None:
        with self._lock:
            old = self._clients.pop(profile.name, None)
            self._fallback.pop(profile.name, None)
            self._profiles[profile.name] = profile
            self._broken.pop(profile.name, None)
            if make_default or self._default is None:
                self._default = profile.name
        if old is not None:
            old.close()

    def remove(self, name: str) -> None:
        key = self._resolve(name)
        with self._lock:
            cli = self._clients.pop(key, None)
            del self._profiles[key]
            if self._default == key:
                self._default = next(iter(self._profiles), None)
        if cli is not None:
            cli.close()

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for name, prof in self._profiles.items():
            info = prof.public_dict()
            info["is_default"] = name == self._default
            info["client_open"] = name in self._clients  # opened on first use; not a health check
            if name in self._fallback:
                info["direct_fallback"] = self._fallback[name]
            out.append(info)
        for name, err in self._broken.items():
            out.append({"name": name, "broken": True, "error": err})
        return out

    def close_all(self) -> None:
        for cli in list(self._clients.values()):
            try:
                cli.close()
            except Exception:
                pass
        self._clients.clear()
