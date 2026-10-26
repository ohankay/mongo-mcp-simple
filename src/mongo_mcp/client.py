"""MongoClient construction and the per-profile client cache."""

from __future__ import annotations

import os
import threading
from typing import Any, Callable, Mapping

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


def open_client(profile: MongoProfile) -> Any:
    import pymongo

    host, kw = client_kwargs(profile)
    return pymongo.MongoClient(host, **kw)


class ClientManager:
    """Holds one pooled MongoClient per profile (MongoClient is thread-safe)."""

    def __init__(self, profiles: dict[str, MongoProfile], default: str | None, broken: dict[str, str] | None = None,
                 opener: Callable[[MongoProfile], Any] = open_client):
        self._profiles = dict(profiles)
        self._broken = dict(broken or {})
        self._default = default
        self._clients: dict[str, Any] = {}
        self._opener = opener
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
                cli = self._opener(self._profiles[key])
                self._clients[key] = cli
            return cli

    def database_name(self, name: str | None, database: str | None) -> str:
        db = database or self.profile(name).database
        if not db:
            raise ConfigError("No database given: pass 'database' or set 'database' in the connection profile")
        return db

    def add(self, profile: MongoProfile, make_default: bool = False) -> None:
        with self._lock:
            old = self._clients.pop(profile.name, None)
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
            info["connected"] = name in self._clients
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
