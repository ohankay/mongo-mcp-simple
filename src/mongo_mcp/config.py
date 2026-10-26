"""Connection profile configuration.

Profiles can come from three places (later ones win on name clashes):

1. Environment variables ``MONGO_MCP_*`` -> a single profile (named ``MONGO_MCP_NAME`` or "default").
2. A JSON file pointed to by ``MONGO_MCP_CONFIG`` holding several named profiles.
3. Runtime registration through the ``add_connection`` tool.

A profile that cannot be loaded (for example its ``password_env`` variable is missing) does not stop
the others from working: it is reported as broken with a message telling how to fix it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Mapping

AUTH_NONE = "none"
AUTH_SCRAM = "scram"
AUTH_LDAP = "ldap"
AUTH_WINDOWS = "windows"
AUTH_X509 = "x509"
AUTH_URI = "uri"
_AUTH_ALIASES = {
    "none": AUTH_NONE, "noauth": AUTH_NONE, "anonymous": AUTH_NONE,
    "scram": AUTH_SCRAM, "password": AUTH_SCRAM, "user": AUTH_SCRAM, "sql": AUTH_SCRAM,
    "scram-sha-1": AUTH_SCRAM, "scram-sha-256": AUTH_SCRAM,
    "ldap": AUTH_LDAP, "plain": AUTH_LDAP,
    "windows": AUTH_WINDOWS, "kerberos": AUTH_WINDOWS, "gssapi": AUTH_WINDOWS, "integrated": AUTH_WINDOWS,
    "x509": AUTH_X509, "mongodb-x509": AUTH_X509, "certificate": AUTH_X509,
    "uri": AUTH_URI,
}
READ_PREFERENCES = ("primary", "primaryPreferred", "secondary", "secondaryPreferred", "nearest")


class ConfigError(ValueError):
    """Raised for invalid or incomplete connection configuration."""


def _to_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off", ""):
        return False
    raise ConfigError(f"'{name}' must be a boolean, got {value!r}")


def _to_int(value: Any, name: str, minimum: int = 0) -> int:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"'{name}' must be an integer, got {value!r}") from None
    if result < minimum:
        raise ConfigError(f"'{name}' must be >= {minimum}, got {result}")
    return result


@dataclass
class MongoProfile:
    name: str
    host: str | None = None            # "srv01", "srv01:27017" or "h1:27017,h2:27017"
    port: int | None = None
    uri: str | None = field(default=None, repr=False)  # full mongodb:// or mongodb+srv:// URI
    auth: str | None = None
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    auth_source: str | None = None
    auth_mechanism: str | None = None  # override, e.g. SCRAM-SHA-1
    database: str | None = None        # default database for tools
    replica_set: str | None = None
    direct_connection: bool | None = None
    tls: bool = False
    tls_allow_invalid_certificates: bool = False
    tls_ca_file: str | None = None
    tls_certificate_key_file: str | None = None
    read_preference: str | None = None
    readonly: bool = False
    max_docs: int = 100
    max_time_ms: int = 60000
    server_selection_timeout_ms: int = 10000
    app_name: str = "mongo-mcp"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.name or not str(self.name).strip():
            raise ConfigError("Connection name must not be empty")
        if not self.uri and not self.host:
            raise ConfigError(f"[{self.name}] 'host' or 'uri' is required")
        if self.uri and not str(self.uri).startswith(("mongodb://", "mongodb+srv://")):
            raise ConfigError(f"[{self.name}] 'uri' must start with mongodb:// or mongodb+srv://")
        if self.auth is None:
            self.auth = AUTH_URI if self.uri else (AUTH_SCRAM if self.user else AUTH_NONE)
        auth = _AUTH_ALIASES.get(str(self.auth).strip().lower())
        if auth is None:
            raise ConfigError(f"[{self.name}] 'auth' must be one of none, scram, ldap, windows, x509, uri "
                              f"- got {self.auth!r}")
        self.auth = auth
        if auth in (AUTH_SCRAM, AUTH_LDAP):
            if not self.user:
                raise ConfigError(f"[{self.name}] '{auth}' authentication requires 'user'")
            if self.password is None:
                raise ConfigError(f"[{self.name}] '{auth}' authentication requires a password "
                                  "('password_env' recommended)")
        if auth == AUTH_X509 and not self.tls_certificate_key_file:
            raise ConfigError(f"[{self.name}] x509 authentication requires 'tls_certificate_key_file'")
        if auth == AUTH_URI and not self.uri:
            raise ConfigError(f"[{self.name}] auth 'uri' requires 'uri' (or 'uri_env')")
        if self.port is not None:
            self.port = _to_int(self.port, "port", 1)
            if self.port > 65535:
                raise ConfigError(f"[{self.name}] 'port' must be <= 65535")
        if self.direct_connection is not None:
            self.direct_connection = _to_bool(self.direct_connection, "direct_connection")
        for flag in ("tls", "tls_allow_invalid_certificates", "readonly"):
            setattr(self, flag, _to_bool(getattr(self, flag), flag))
        if self.tls_ca_file or self.tls_certificate_key_file:
            self.tls = True
        if self.read_preference is not None:
            match = {p.lower(): p for p in READ_PREFERENCES}.get(str(self.read_preference).strip().lower())
            if match is None:
                raise ConfigError(f"[{self.name}] 'read_preference' must be one of {', '.join(READ_PREFERENCES)}")
            self.read_preference = match
        self.max_docs = _to_int(self.max_docs, "max_docs", 1)
        self.max_time_ms = _to_int(self.max_time_ms, "max_time_ms", 0)
        self.server_selection_timeout_ms = _to_int(self.server_selection_timeout_ms, "server_selection_timeout_ms", 1)

    def public_dict(self) -> dict[str, Any]:
        """Profile description safe to show to the model (no secrets)."""
        data = asdict(self)
        data.pop("password", None)
        data["has_password"] = self.password is not None
        if self.uri:
            data["uri"] = redact_uri(self.uri)
        return data


def redact_uri(uri: str) -> str:
    """mongodb://user:secret@host -> mongodb://user:***@host"""
    scheme, sep, rest = uri.partition("://")
    if not sep or "@" not in rest.split("/", 1)[0]:
        return uri
    creds, _, hostpart = rest.partition("@")
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{hostpart}"


_FIELD_NAMES = {f.name for f in fields(MongoProfile)}


def profile_from_mapping(name: str, data: Mapping[str, Any]) -> MongoProfile:
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        key = str(key).strip().lower()
        if key in ("password_env", "uri_env"):
            env_name = str(value)
            target = "password" if key == "password_env" else "uri"
            if env_name not in os.environ or os.environ[env_name] == "" and target == "uri":
                raise ConfigError(
                    f"[{name}] environment variable '{env_name}' ({key}) is not set. In VS Code add it to the "
                    f"MCP server's \"env\" in mcp.json, e.g. \"{env_name}\": \"${{input:{env_name.lower()}}}\" "
                    f"plus a matching promptString entry under \"inputs\"."
                )
            kwargs[target] = os.environ[env_name]
            continue
        key = {"username": "user", "server": "host", "hosts": "host", "db": "database",
               "authsource": "auth_source", "replicaset": "replica_set"}.get(key, key)
        if key not in _FIELD_NAMES or key == "name":
            raise ConfigError(f"[{name}] unknown setting '{key}'")
        kwargs[key] = value
    return MongoProfile(name=name, **kwargs)


_ENV_MAP = {
    "URI": "uri", "HOST": "host", "SERVER": "host", "PORT": "port", "AUTH": "auth", "USER": "user",
    "USERNAME": "user", "PASSWORD": "password", "AUTH_SOURCE": "auth_source",
    "AUTH_MECHANISM": "auth_mechanism", "DATABASE": "database", "REPLICA_SET": "replica_set",
    "DIRECT_CONNECTION": "direct_connection", "TLS": "tls",
    "TLS_ALLOW_INVALID_CERTIFICATES": "tls_allow_invalid_certificates", "TLS_CA_FILE": "tls_ca_file",
    "TLS_CERTIFICATE_KEY_FILE": "tls_certificate_key_file", "READ_PREFERENCE": "read_preference",
    "READONLY": "readonly", "MAX_DOCS": "max_docs", "MAX_TIME_MS": "max_time_ms",
    "SERVER_SELECTION_TIMEOUT_MS": "server_selection_timeout_ms", "APP_NAME": "app_name",
}


def _is_placeholder(value: str) -> bool:
    return value.startswith("${") and value.endswith("}")


def profile_from_env(env: Mapping[str, str], prefix: str = "MONGO_MCP_") -> MongoProfile | None:
    data: dict[str, Any] = {}
    for suffix, key in _ENV_MAP.items():
        value = env.get(prefix + suffix)
        if value is None or value == "" or _is_placeholder(value):
            continue
        data[key] = value
    if "host" not in data and "uri" not in data:
        return None
    name = env.get(prefix + "NAME") or "default"
    return MongoProfile(name=name, **data)


def load_config_file(path: str) -> tuple[dict[str, MongoProfile], str | None, dict[str, str]]:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file {path} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError("Config file root must be a JSON object")
    conns = raw.get("connections")
    if not isinstance(conns, dict) or not conns:
        raise ConfigError("Config file must contain a non-empty 'connections' object")
    profiles: dict[str, MongoProfile] = {}
    broken: dict[str, str] = {}
    for name, data in conns.items():
        if not isinstance(data, dict):
            broken[name] = f"[{name}] connection must be an object"
            continue
        try:
            profiles[name] = profile_from_mapping(name, data)
        except ConfigError as exc:
            broken[name] = str(exc)
    default = raw.get("default")
    if default is not None and default not in profiles and default not in broken:
        raise ConfigError(f"Default connection '{default}' is not defined in 'connections'")
    return profiles, default, broken


def load_profiles(env: Mapping[str, str] | None = None) -> tuple[dict[str, MongoProfile], str | None, dict[str, str]]:
    """Returns (profiles, default name, broken profiles {name: error message})."""
    env = os.environ if env is None else env
    profiles: dict[str, MongoProfile] = {}
    broken: dict[str, str] = {}
    default: str | None = None
    path = env.get("MONGO_MCP_CONFIG")
    if path and not _is_placeholder(path):
        profiles, default, broken = load_config_file(os.path.expanduser(os.path.expandvars(path)))
    env_profile = profile_from_env(env)
    if env_profile is not None:
        profiles[env_profile.name] = env_profile
        broken.pop(env_profile.name, None)
        default = default or env_profile.name
    if default is None and profiles:
        default = next(iter(profiles))
    return profiles, default, broken
