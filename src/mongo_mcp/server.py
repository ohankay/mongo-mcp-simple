"""MCP server exposing MongoDB tools over stdio."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import struct
import sys
from typing import Any, Callable

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .bsonutil import (MAX_RESPONSE_CHARS, ReadOnlyViolation, check_command_readonly, check_pipeline_readonly,
                       collect_documents, parse_ejson, to_jsonable)
from .client import ClientManager
from .config import ConfigError, load_profiles, profile_from_mapping
from . import __version__
from .errors import describe_error, is_network_error

log = logging.getLogger("mongo_mcp")

INSTRUCTIONS = """\
MongoDB tools (server 4.4+). Every tool takes an optional `connection` (profile name, see list_connections) and most
take `database` (defaults to the profile's database). Filters, documents, updates and pipelines are JSON; use MongoDB
Extended JSON for special types: {"$oid": "..."}, {"$date": "2024-01-31T00:00:00Z"}, {"$numberDecimal": "1.5"},
{"$numberLong": "1"}. Results come back in relaxed Extended JSON, so values can be reused in later filters.
- Explore first: list_databases, list_collections, infer_schema, list_indexes, collection_stats.
- Read: find, aggregate, count_documents, distinct.
- Write: insert_documents, update_documents, replace_document, delete_documents; several writes that must succeed or
  fail together: run_transaction (needs a replica set / sharded cluster).
- An empty filter on update/delete requires confirm_all=true. Drops require confirm_name equal to the target.
- Anything else (users, roles, validation, compact, replication, ...): run_command.
"""

mcp = FastMCP("mongo-mcp", instructions=INSTRUCTIONS)
_manager: ClientManager | None = None
_EXPECTED_ERRORS = (ConfigError, ReadOnlyViolation, ValueError)
HARD_DOC_LIMIT = 10000
_STARTED_AT = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
WRITE_OUTCOME_UNKNOWN = ("unknown - the connection failed while the write was in flight, so it may or may not have "
                         "been applied; check with a read before retrying")


def manager() -> ClientManager:
    global _manager
    if _manager is None:
        profiles, default, broken = load_profiles()
        _manager = ClientManager(profiles, default, broken)
    return _manager


def set_manager(mgr: ClientManager | None) -> None:
    global _manager
    _manager = mgr


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, _EXPECTED_ERRORS):
        return {"success": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
    log.debug("tool error", exc_info=exc)
    return {"success": False, "error": describe_error(exc)}


def _topology(connection: str | None) -> list[dict[str, Any]] | None:
    """Per-server state as the driver sees it - the real reason behind network-type errors."""
    try:
        cli = manager().existing_client(connection)
        if cli is None:
            return manager().last_failure(connection)
        servers = []
        for sd in cli.topology_description.server_descriptions().values():
            entry: dict[str, Any] = {"address": f"{sd.address[0]}:{sd.address[1]}", "type": sd.server_type_name}
            if sd.round_trip_time is not None:
                entry["round_trip_ms"] = round(sd.round_trip_time * 1000, 1)
            if sd.error is not None:
                entry["error"] = str(sd.error)[:500]
            servers.append(entry)
        return servers
    except Exception:  # noqa: BLE001 - diagnostics are best effort
        return None


def _retryable(exc: BaseException) -> bool:
    """Transient connection loss (incl. _OperationCancelled) - not a full server-selection timeout."""
    from pymongo import errors as pe

    return isinstance(exc, (pe.AutoReconnect, pe.NetworkTimeout)) and not isinstance(
        exc, pe.ServerSelectionTimeoutError) and getattr(exc, "mcp_stage", None) != "connect"


async def _call(fn: Callable[[], Any], connection: str | None = None, kind: str = "read") -> str:
    """Run a tool body in a worker thread and turn exceptions into structured errors.

    kind="read": a transient network error is retried once on a fresh client (reads are idempotent). kind="write":
    never retried,
    a network error reports that the outcome is unknown. kind="meta": no retry. After a final network error the
    cached client is dropped, so the next call reconnects and re-probes the replica set (incl. direct fallback).
    """
    def guarded() -> Any:
        retried: dict[str, Any] | None = None
        attempts = 2 if kind == "read" else 1
        for attempt in range(1, attempts + 1):
            try:
                result = fn()
                if isinstance(result, dict) and "success" not in result:
                    result = {"success": True, **result}
                if retried and isinstance(result, dict):
                    result["retried_after"] = retried
                return result
            except Exception as exc:  # noqa: BLE001 - reported to the model as a structured error
                if attempt < attempts and _retryable(exc):
                    retried = {"type": type(exc).__name__, "message": describe_error(exc)["message"][:300],
                               "events": manager().events(connection, seconds=30)}
                    # After a pool clear the driver pauses that pool until the next good heartbeat, so a retry on
                    # the same client usually fails again: retry on a fresh client (new pools, new probe/fallback).
                    manager().reset(connection)
                    continue
                out = _error(exc)
                if is_network_error(exc):
                    err = out["error"]
                    err["stage"] = getattr(exc, "mcp_stage", "operation")
                    topo = _topology(connection)
                    if topo:
                        err["servers"] = topo
                    events = manager().events(connection)
                    if events:
                        err["events"] = events
                    if retried:
                        err["retried"] = True
                    if kind == "write" and err["stage"] == "operation":
                        err["write_outcome"] = WRITE_OUTCOME_UNKNOWN
                    manager().reset(connection)  # next call reconnects and re-probes the replica set
                return out
        raise AssertionError("unreachable")  # pragma: no cover

    result = await anyio.to_thread.run_sync(guarded)
    return json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":"))


def _db(connection: str | None, database: str | None):
    mgr = manager()
    return mgr.client(connection)[mgr.database_name(connection, database)]


def _coll(connection: str | None, database: str | None, collection: str):
    if not collection or not str(collection).strip():
        raise ValueError("'collection' is required")
    return _db(connection, database)[collection]


def _read_command(db: Any, cmd: dict[str, Any]) -> dict[str, Any]:
    """Run a read-only command with the profile's read preference (pymongo's list_collections / list_indexes
    helpers always select the primary, so they could fail while the members the profile reads from are fine)."""
    return db.command(cmd, read_preference=db.client.read_preference)


def _first_batch(db: Any, cmd: dict[str, Any]) -> tuple[list[Any], bool]:
    """All results of a small cursor command in one batch; (items, more_available)."""
    cmd = {**cmd, "cursor": {"batchSize": 100000}}
    res = _read_command(db, cmd)
    cursor = res.get("cursor") or {}
    return list(cursor.get("firstBatch", [])), bool(cursor.get("id"))


def _writable(connection: str | None) -> None:
    prof = manager().profile(connection)
    if prof.readonly:
        raise ReadOnlyViolation(f"Connection '{prof.name}' is read-only")


def _max_time(connection: str | None, override: int | None) -> int | None:
    value = override if override is not None else manager().profile(connection).max_time_ms
    return int(value) or None


def _limit(connection: str | None, limit: int | None) -> int:
    n = int(limit) if limit is not None else manager().profile(connection).max_docs
    if n < 1:
        raise ValueError("limit must be >= 1")
    return min(n, HARD_DOC_LIMIT)


def _sort_spec(sort: Any) -> list[tuple[str, int]] | None:
    sort = parse_ejson(sort, "sort", expect=(dict, list))
    if not sort:
        return None
    if isinstance(sort, dict):
        items = list(sort.items())
    else:
        items = [tuple(x) if isinstance(x, (list, tuple)) else (x, 1) for x in sort]
    spec = []
    for key, direction in items:
        if isinstance(direction, str):
            direction = {"asc": 1, "ascending": 1, "desc": -1, "descending": -1}.get(direction.lower(), direction)
        if direction not in (1, -1):
            raise ValueError(f"sort direction for '{key}' must be 1 or -1")
        spec.append((str(key), int(direction)))
    return spec


def _collation(value: Any) -> dict[str, Any] | None:
    """{"locale": "tr", "strength": 2} -> Turkish case-insensitive comparison (İ=i, I=ı)."""
    col = parse_ejson(value, "collation")
    if col is not None and "locale" not in col:
        raise ValueError("collation needs at least a 'locale', e.g. {\"locale\": \"tr\", \"strength\": 2}")
    return col


def _is_empty_filter(flt: Any) -> bool:
    return not flt


_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def tool(annotations: ToolAnnotations):
    def deco(fn):
        return mcp.tool(annotations=annotations, structured_output=False)(fn)
    return deco


# ====================================================================== connections

@tool(_READ)
async def list_connections() -> str:
    """List configured MongoDB connection profiles (passwords are never shown; broken ones show their error)."""
    return await _call(lambda: {"default": manager().default, "connections": manager().describe()}, kind="meta")


def _server_info(connection: str | None) -> dict[str, Any]:
    import pymongo

    mgr = manager()
    prof = mgr.profile(connection)
    cli = mgr.client(connection)
    admin = cli.admin
    build = admin.command("buildInfo")
    try:
        hello = admin.command("hello")
    except Exception:  # noqa: BLE001 - servers before 4.4.2 only know isMaster
        hello = admin.command("isMaster")
    status = admin.command("connectionStatus")
    users = status.get("authInfo", {}).get("authenticatedUsers", [])
    topology = "sharded (mongos)" if hello.get("msg") == "isdbgrid" else (
        f"replica set '{hello['setName']}'" if hello.get("setName") else "standalone")
    fallback = mgr.fallback_info(connection)
    if fallback:
        topology = f"direct connection to {fallback['address']} ({fallback['role']}) - {topology} not reachable as a set"
    info = {"server": {
        "connection": prof.name,
        "version": build.get("version"),
        "edition": "enterprise" if "enterprise" in build.get("modules", []) else "community",
        "topology": topology,
        "is_writable_primary": hello.get("isWritablePrimary", hello.get("ismaster")),
        "authenticated_users": [f"{u.get('user')}@{u.get('db')}" for u in users],
        "auth": prof.auth,
        "default_database": prof.database,
        "readonly_profile": prof.readonly,
        "driver": f"pymongo {pymongo.version}",
        "transactions_supported": topology.startswith(("replica set", "sharded")) or bool(
            fallback and fallback["role"] == "primary" and hello.get("setName")),
    }}
    types = [sd.server_type_name for sd in cli.topology_description.server_descriptions().values()]
    info["server"]["members_seen"] = types
    info["server"]["primary_reachable"] = any(t in ("RSPrimary", "Standalone", "Mongos") for t in types)
    info["runtime"] = runtime_info()
    if fallback:
        info["server"]["direct_fallback"] = fallback
        if fallback["role"] != "primary":
            info["server"]["warning"] = (
                f"The replica set could not be used as a whole ({fallback['reason']}); connected directly to "
                f"{fallback['address']}, which is a {fallback['role']}: reads work, writes will be refused.")
    return info


def runtime_info() -> dict[str, Any]:
    """Which process / version actually answers - an old MCP process can keep running after an update."""
    from . import __file__ as pkg_file

    return {"mongo_mcp": __version__, "pid": os.getpid(), "started_at": _STARTED_AT,
            "python": f"{sys.version.split()[0]} ({struct.calcsize('P') * 8}-bit)",
            "module": os.path.dirname(os.path.abspath(pkg_file))}


@tool(_READ)
async def test_connection(connection: str | None = None) -> str:
    """Connect and return server version, edition, topology (standalone / replica set / sharded), the authenticated
    user and whether transactions are supported."""
    return await _call(lambda: _server_info(connection), connection, kind="read")


@tool(_WRITE)
async def add_connection(name: str, host: str | None = None, uri: str | None = None, auth: str | None = None,
                         user: str | None = None, password_env: str | None = None, auth_source: str | None = None,
                         database: str | None = None, replica_set: str | None = None,
                         direct_connection: bool | None = None, tls: bool = False,
                         tls_allow_invalid_certificates: bool = False, readonly: bool = False,
                         make_default: bool = False) -> str:
    """Register (or replace) a connection profile at runtime and test it.

    host: "srv01", "srv01:27017" or "h1:27017,h2:27017"; or uri: "mongodb://..." / "mongodb+srv://...".
    auth: none | scram (user+password) | ldap | windows (Kerberos with the Windows logon) | x509 | uri.
    Passwords are never passed here: use password_env (name of an environment variable set in mcp.json).
    """
    def run():
        data: dict[str, Any] = {k: v for k, v in {
            "host": host, "uri": uri, "auth": auth, "user": user, "password_env": password_env,
            "auth_source": auth_source, "database": database, "replica_set": replica_set,
            "direct_connection": direct_connection}.items() if v is not None}
        data.update(tls=tls, tls_allow_invalid_certificates=tls_allow_invalid_certificates, readonly=readonly)
        manager().add(profile_from_mapping(name, data), make_default=make_default)
        info = _server_info(name)
        info["added"] = name
        return info
    return await _call(run, name, kind="meta")


@tool(_WRITE)
async def remove_connection(name: str) -> str:
    """Close and forget a connection profile."""
    def run():
        manager().remove(name)
        return {"removed": name, "default": manager().default}
    return await _call(run, kind="meta")


# ====================================================================== exploration

@tool(_READ)
async def list_databases(connection: str | None = None) -> str:
    """List databases with size on disk (only the ones the login may see)."""
    def run():
        res = _read_command(manager().client(connection).admin, {"listDatabases": 1, "authorizedDatabases": True})
        dbs = [{"name": d["name"], "size_mb": round(d.get("sizeOnDisk", 0) / 1048576, 2), "empty": d.get("empty")}
               for d in res.get("databases", [])]
        return {"databases": sorted(dbs, key=lambda d: d["name"]),
                "total_size_mb": round(res.get("totalSize", 0) / 1048576, 2)}
    return await _call(run, connection, kind="read")


@tool(_READ)
async def list_collections(connection: str | None = None, database: str | None = None,
                           name_filter: str | None = None, include_counts: bool = True) -> str:
    """Collections and views of a database with type and estimated document count.
    name_filter: regex on the name, e.g. "^order"."""
    def run():
        db = _db(connection, database)
        flt = {"name": {"$regex": name_filter}} if name_filter else {}
        items = []
        infos, more = _first_batch(db, {"listCollections": 1, "filter": flt, "nameOnly": False,
                                        "authorizedCollections": True})
        for info in infos:
            entry: dict[str, Any] = {"name": info["name"], "type": info.get("type", "collection")}
            opts = info.get("options") or {}
            if opts.get("validator"):
                entry["has_validator"] = True
            if opts.get("capped"):
                entry["capped"] = True
            if opts.get("viewOn"):
                entry["view_on"] = opts["viewOn"]
            if include_counts and entry["type"] == "collection" and len(items) < 500:
                try:
                    entry["estimated_count"] = db[info["name"]].estimated_document_count()
                except Exception:  # noqa: BLE001 - counts are best effort
                    pass
            items.append(entry)
        out = {"database": db.name, "collections": sorted(items, key=lambda c: c["name"])}
        if more:
            out["truncated"] = True
        return out
    return await _call(run, connection, kind="read")


@tool(_READ)
async def collection_stats(collection: str, connection: str | None = None, database: str | None = None) -> str:
    """Document count, data / storage / index sizes (MB), average document size, index sizes, capped flag."""
    def run():
        db = _db(connection, database)
        s = _read_command(db, {"collStats": collection})
        mb = lambda v: round((v or 0) / 1048576, 3)  # noqa: E731
        return {"collection": collection, "count": s.get("count"), "size_mb": mb(s.get("size")),
                "storage_size_mb": mb(s.get("storageSize")), "avg_obj_size_bytes": s.get("avgObjSize"),
                "total_index_size_mb": mb(s.get("totalIndexSize")), "index_count": s.get("nindexes"),
                "index_sizes_mb": {k: mb(v) for k, v in (s.get("indexSizes") or {}).items()},
                "capped": s.get("capped", False)}
    return await _call(run, connection, kind="read")


@tool(_READ)
async def list_indexes(collection: str, connection: str | None = None, database: str | None = None) -> str:
    """Indexes of a collection with keys and options (unique, sparse, TTL, partial filter)."""
    def run():
        coll = _coll(connection, database, collection)
        items, _ = _first_batch(coll.database, {"listIndexes": coll.name})
        return {"collection": collection, "indexes": [to_jsonable(i) for i in items]}
    return await _call(run, connection, kind="read")


_TYPE_NAMES = {
    "str": "string", "int": "int", "Int64": "long", "float": "double", "bool": "bool", "datetime": "date",
    "ObjectId": "objectId", "Decimal128": "decimal", "list": "array", "dict": "object", "NoneType": "null",
    "bytes": "binData", "Binary": "binData", "UUID": "uuid", "Regex": "regex", "Timestamp": "timestamp",
    "SON": "object", "Code": "javascript", "MinKey": "minKey", "MaxKey": "maxKey", "DBRef": "dbPointer",
}


def _walk(value: Any, path: str, stats: dict[str, dict[str, int]], depth: int) -> None:
    tname = _TYPE_NAMES.get(type(value).__name__, type(value).__name__)
    stats.setdefault(path, {})[tname] = stats.setdefault(path, {}).get(tname, 0) + 1
    if depth >= 5:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _walk(v, f"{path}.{k}", stats, depth + 1)
    elif isinstance(value, list):
        for v in value[:20]:
            if isinstance(v, dict):
                for k, vv in v.items():
                    _walk(vv, f"{path}[].{k}", stats, depth + 1)


@tool(_READ)
async def infer_schema(collection: str, connection: str | None = None, database: str | None = None,
                       sample_size: int = 200) -> str:
    """Infer the document structure from a random sample: every field path with its BSON types and how often it
    appears (present_pct). Nested objects use dots, array elements use "[]"."""
    def run():
        n = max(1, min(int(sample_size), 5000))
        coll = _coll(connection, database, collection)
        docs = list(coll.aggregate([{"$sample": {"size": n}}], maxTimeMS=_max_time(connection, None)))
        stats: dict[str, dict[str, int]] = {}
        for doc in docs:
            for k, v in doc.items():
                _walk(v, k, stats, 1)
        fields = [{"path": p, "types": t, "present_pct": round(100 * sum(t.values()) / len(docs), 1) if docs else 0}
                  for p, t in sorted(stats.items())]
        return {"collection": collection, "sampled": len(docs), "fields": fields}
    return await _call(run, connection, kind="read")


# ====================================================================== reads

@tool(_READ)
async def find(collection: str, filter: Any = None, projection: Any = None, sort: Any = None, limit: int | None = None,
               skip: int = 0, connection: str | None = None, database: str | None = None,
               max_time_ms: int | None = None, collation: Any = None) -> str:
    """Query documents. filter / projection / sort are JSON (Extended JSON for ObjectId, dates...).
    sort: {"createdAt": -1} or [["a", 1], ["b", -1]]. limit defaults to the profile's max_docs (100).
    collation: {"locale": "tr", "strength": 2} for Turkish case-insensitive matching: "istanbul" finds "İstanbul"
    (İ=i, I=ı; regex $options "i" does not fold İ/ı)."""
    def run():
        n = _limit(connection, limit)
        cursor = _coll(connection, database, collection).find(
            parse_ejson(filter, "filter") or {}, projection=parse_ejson(projection, "projection", expect=(dict, list)),
            sort=_sort_spec(sort), skip=max(0, int(skip)), limit=n + 1, max_time_ms=_max_time(connection, max_time_ms),
            collation=_collation(collation))
        return collect_documents(cursor, n)
    return await _call(run, connection, kind="read")


@tool(_READ)
async def aggregate(collection: str, pipeline: Any, connection: str | None = None, database: str | None = None,
                    limit: int | None = None, allow_disk_use: bool = False, max_time_ms: int | None = None,
                    collation: Any = None) -> str:
    """Run an aggregation pipeline (JSON array of stages). $out / $merge are allowed unless the profile is read-only.
    limit caps the returned documents (default the profile's max_docs). collation: {"locale": "tr", "strength": 2} for Turkish case-insensitive matching (İ=i, I=ı)."""
    def run():
        stages = parse_ejson(pipeline, "pipeline", expect=list, allow_none=False)
        if manager().profile(connection).readonly:
            check_pipeline_readonly(stages)
        n = _limit(connection, limit)
        kwargs: dict[str, Any] = {"allowDiskUse": bool(allow_disk_use)}
        col = _collation(collation)
        if col:
            kwargs["collation"] = col
        mt = _max_time(connection, max_time_ms)
        if mt:
            kwargs["maxTimeMS"] = mt
        return collect_documents(_coll(connection, database, collection).aggregate(stages, **kwargs), n)
    return await _call(run, connection, kind="read")


@tool(_READ)
async def count_documents(collection: str, filter: Any = None, estimated: bool = False,
                          connection: str | None = None, database: str | None = None, collation: Any = None) -> str:
    """Count documents matching a filter. estimated=true uses collection metadata (fast, ignores the filter). collation: {"locale": "tr", "strength": 2} for Turkish case-insensitive matching (İ=i, I=ı)."""
    def run():
        coll = _coll(connection, database, collection)
        if estimated:
            return {"count": coll.estimated_document_count(), "estimated": True}
        mt = _max_time(connection, None)
        kwargs: dict[str, Any] = {"maxTimeMS": mt} if mt else {}
        col = _collation(collation)
        if col:
            kwargs["collation"] = col
        return {"count": coll.count_documents(parse_ejson(filter, "filter") or {}, **kwargs)}
    return await _call(run, connection, kind="read")


@tool(_READ)
async def distinct(collection: str, field: str, filter: Any = None, connection: str | None = None,
                   database: str | None = None, collation: Any = None) -> str:
    """Distinct values of a field (optionally within a filter). Returns at most 5000 values. collation: {"locale": "tr", "strength": 2} for Turkish case-insensitive matching (İ=i, I=ı)."""
    def run():
        values = _coll(connection, database, collection).distinct(field, parse_ejson(filter, "filter") or {},
                                                                  collation=_collation(collation))
        out: dict[str, Any] = {"field": field, "count": len(values), "values": to_jsonable(values[:5000])}
        if len(values) > 5000:
            out["truncated"] = True
        return out
    return await _call(run, connection, kind="read")


# ====================================================================== writes

@tool(_WRITE)
async def insert_documents(collection: str, documents: Any, connection: str | None = None,
                           database: str | None = None, ordered: bool = True) -> str:
    """Insert one document (JSON object) or many (JSON array). Returns the inserted _ids.
    ordered=false keeps inserting after a failing document."""
    def run():
        _writable(connection)
        docs = parse_ejson(documents, "documents", expect=(dict, list), allow_none=False)
        coll = _coll(connection, database, collection)
        if isinstance(docs, dict):
            res = coll.insert_one(docs)
            return {"inserted_count": 1, "inserted_ids": to_jsonable([res.inserted_id])}
        if not docs or not all(isinstance(d, dict) for d in docs):
            raise ValueError("'documents' must be a non-empty array of objects")
        res = coll.insert_many(docs, ordered=ordered)
        return {"inserted_count": len(res.inserted_ids), "inserted_ids": to_jsonable(res.inserted_ids[:1000])}
    return await _call(run, connection, kind="write")


@tool(_WRITE)
async def update_documents(collection: str, filter: Any, update: Any, many: bool = False, upsert: bool = False,
                           array_filters: Any = None, connection: str | None = None, database: str | None = None,
                           confirm_all: bool = False, collation: Any = None) -> str:
    """Update with operators ({"$set": {...}, "$inc": {...}}) or an aggregation pipeline (JSON array).
    many=false updates the first match. An empty filter requires confirm_all=true."""
    def run():
        _writable(connection)
        flt = parse_ejson(filter, "filter") or {}
        if _is_empty_filter(flt) and not confirm_all:
            raise ValueError("Empty filter would update every document; pass confirm_all=true if that is intended")
        upd = parse_ejson(update, "update", expect=(dict, list), allow_none=False)
        if isinstance(upd, dict) and upd and not all(str(k).startswith("$") for k in upd):
            raise ValueError("'update' must use update operators such as $set / $inc (use replace_document to "
                             "replace a whole document)")
        coll = _coll(connection, database, collection)
        kwargs: dict[str, Any] = {"upsert": upsert, "collation": _collation(collation)}
        if array_filters is not None:
            kwargs["array_filters"] = parse_ejson(array_filters, "array_filters", expect=list)
        res = (coll.update_many if many else coll.update_one)(flt, upd, **kwargs)
        out = {"matched_count": res.matched_count, "modified_count": res.modified_count}
        if res.upserted_id is not None:
            out["upserted_id"] = to_jsonable(res.upserted_id)
        return out
    return await _call(run, connection, kind="write")


@tool(_WRITE)
async def replace_document(collection: str, filter: Any, replacement: Any, upsert: bool = False,
                           connection: str | None = None, database: str | None = None) -> str:
    """Replace the first document matching the filter with a whole new document (no update operators)."""
    def run():
        _writable(connection)
        flt = parse_ejson(filter, "filter") or {}
        if _is_empty_filter(flt):
            raise ValueError("replace_document needs a non-empty filter")
        doc = parse_ejson(replacement, "replacement", allow_none=False)
        res = _coll(connection, database, collection).replace_one(flt, doc, upsert=upsert)
        out = {"matched_count": res.matched_count, "modified_count": res.modified_count}
        if res.upserted_id is not None:
            out["upserted_id"] = to_jsonable(res.upserted_id)
        return out
    return await _call(run, connection, kind="write")


@tool(_DESTRUCTIVE)
async def delete_documents(collection: str, filter: Any, many: bool = False, connection: str | None = None,
                           database: str | None = None, confirm_all: bool = False, collation: Any = None) -> str:
    """Delete the first matching document (many=false) or all matching documents (many=true).
    An empty filter requires confirm_all=true."""
    def run():
        _writable(connection)
        flt = parse_ejson(filter, "filter") or {}
        if _is_empty_filter(flt) and not confirm_all:
            raise ValueError("Empty filter would delete documents without any condition; pass confirm_all=true "
                             "if that is intended")
        coll = _coll(connection, database, collection)
        res = (coll.delete_many if many else coll.delete_one)(flt, collation=_collation(collation))
        return {"deleted_count": res.deleted_count}
    return await _call(run, connection, kind="write")


_TX_OPS = {"insert", "insert_many", "update", "update_many", "replace", "delete", "delete_many"}


def _run_tx_op(db: Any, op: dict[str, Any], session: Any) -> dict[str, Any]:
    kind = str(op.get("op", "")).lower()
    if kind not in _TX_OPS:
        raise ValueError(f"transaction op must be one of {', '.join(sorted(_TX_OPS))}, got {op.get('op')!r}")
    coll_name = op.get("collection")
    if not coll_name:
        raise ValueError("each transaction op needs 'collection'")
    coll = db[coll_name]
    if kind == "insert":
        res = coll.insert_one(parse_ejson(op.get("document"), "document", allow_none=False), session=session)
        return {"op": kind, "inserted_id": to_jsonable(res.inserted_id)}
    if kind == "insert_many":
        res = coll.insert_many(parse_ejson(op.get("documents"), "documents", expect=list, allow_none=False),
                               session=session)
        return {"op": kind, "inserted_count": len(res.inserted_ids)}
    flt = parse_ejson(op.get("filter"), "filter") or {}
    if not flt and not op.get("confirm_all"):
        raise ValueError(f"{kind} with an empty filter needs \"confirm_all\": true")
    if kind in ("update", "update_many"):
        upd = parse_ejson(op.get("update"), "update", expect=(dict, list), allow_none=False)
        res = (coll.update_many if kind == "update_many" else coll.update_one)(
            flt, upd, upsert=bool(op.get("upsert")), session=session)
        return {"op": kind, "matched_count": res.matched_count, "modified_count": res.modified_count}
    if kind == "replace":
        res = coll.replace_one(flt, parse_ejson(op.get("replacement"), "replacement", allow_none=False),
                               upsert=bool(op.get("upsert")), session=session)
        return {"op": kind, "matched_count": res.matched_count, "modified_count": res.modified_count}
    res = (coll.delete_many if kind == "delete_many" else coll.delete_one)(flt, session=session)
    return {"op": kind, "deleted_count": res.deleted_count}


@tool(_WRITE)
async def run_transaction(operations: Any, connection: str | None = None, database: str | None = None) -> str:
    """Execute several writes atomically (all or nothing). Requires a replica set or sharded cluster.

    operations: [{"op": "insert", "collection": "orders", "document": {...}},
                 {"op": "update", "collection": "stock", "filter": {...}, "update": {"$inc": {"qty": -1}}},
                 {"op": "insert_many" | "update_many" | "replace" | "delete" | "delete_many", ...}]
    If any operation fails, nothing is written and the error names the failing step.
    """
    def run():
        _writable(connection)
        ops = parse_ejson(operations, "operations", expect=list, allow_none=False)
        if not ops or not all(isinstance(o, dict) for o in ops):
            raise ValueError("'operations' must be a non-empty array of objects")
        db = _db(connection, database)
        results: list[dict[str, Any]] = []
        step: dict[str, Any] = {}

        def body(session: Any) -> None:
            results.clear()
            for i, op in enumerate(ops, start=1):
                step.update(index=i, op=op.get("op"), collection=op.get("collection"))
                results.append(_run_tx_op(db, op, session))

        try:
            with db.client.start_session() as session:
                session.with_transaction(body)
        except Exception as exc:  # noqa: BLE001 - report which step failed; nothing was committed
            out = _error(exc)
            out["error"]["failed_step"] = dict(step)
            out["committed"] = False
            return out
        return {"committed": True, "results": results}
    return await _call(run, connection, kind="write")


# ====================================================================== indexes & collections

@tool(_WRITE)
async def create_index(collection: str, keys: Any, name: str | None = None, unique: bool = False,
                       sparse: bool = False, expire_after_seconds: int | None = None, partial_filter: Any = None,
                       connection: str | None = None, database: str | None = None) -> str:
    """Create an index. keys: {"customerId": 1, "createdAt": -1} or {"title": "text"} / {"loc": "2dsphere"}.
    expire_after_seconds makes a TTL index; partial_filter is a filter expression."""
    def run():
        _writable(connection)
        spec = parse_ejson(keys, "keys", expect=(dict, list), allow_none=False)
        items = list(spec.items()) if isinstance(spec, dict) else [tuple(x) for x in spec]
        if not items:
            raise ValueError("'keys' must not be empty")
        kwargs: dict[str, Any] = {"unique": unique, "sparse": sparse}
        if name:
            kwargs["name"] = name
        if expire_after_seconds is not None:
            kwargs["expireAfterSeconds"] = int(expire_after_seconds)
        if partial_filter is not None:
            kwargs["partialFilterExpression"] = parse_ejson(partial_filter, "partial_filter")
        created = _coll(connection, database, collection).create_index(items, **kwargs)
        return {"index": created}
    return await _call(run, connection, kind="write")


@tool(_DESTRUCTIVE)
async def drop_index(collection: str, name: str, connection: str | None = None, database: str | None = None) -> str:
    """Drop an index by name (see list_indexes). The _id index cannot be dropped."""
    def run():
        _writable(connection)
        if name == "_id_":
            raise ValueError("The _id index cannot be dropped")
        _coll(connection, database, collection).drop_index(name)
        return {"dropped": name}
    return await _call(run, connection, kind="write")


@tool(_WRITE)
async def create_collection(name: str, options: Any = None, connection: str | None = None,
                            database: str | None = None) -> str:
    """Create a collection or view. options are passed to the 'create' command, e.g.
    {"validator": {"$jsonSchema": {...}}}, {"capped": true, "size": 1048576},
    {"timeseries": {"timeField": "ts"}}, {"viewOn": "orders", "pipeline": [...]}."""
    def run():
        _writable(connection)
        opts = parse_ejson(options, "options") or {}
        db = _db(connection, database)
        cmd: dict[str, Any] = {"create": name}
        cmd.update(opts)
        db.command(cmd)
        return {"created": name, "database": db.name}
    return await _call(run, connection, kind="write")


@tool(_DESTRUCTIVE)
async def drop_collection(collection: str, confirm_name: str, connection: str | None = None,
                          database: str | None = None) -> str:
    """Drop a collection with all its documents and indexes. confirm_name must repeat the collection name."""
    def run():
        _writable(connection)
        if confirm_name != collection:
            raise ValueError("confirm_name must be exactly the collection name")
        db = _db(connection, database)
        if collection not in db.list_collection_names(filter={"name": collection}):
            raise ValueError(f"Collection '{collection}' does not exist in database '{db.name}'")
        db.drop_collection(collection)
        return {"dropped": collection, "database": db.name}
    return await _call(run, connection, kind="write")


@tool(_WRITE)
async def rename_collection(collection: str, new_name: str, drop_target: bool = False,
                            connection: str | None = None, database: str | None = None) -> str:
    """Rename a collection within the same database."""
    def run():
        _writable(connection)
        _coll(connection, database, collection).rename(new_name, dropTarget=drop_target)
        return {"renamed": collection, "to": new_name}
    return await _call(run, connection, kind="write")


@tool(_DESTRUCTIVE)
async def drop_database(database: str, confirm_name: str, connection: str | None = None) -> str:
    """Drop a whole database. confirm_name must repeat the database name. admin/config/local are refused."""
    def run():
        _writable(connection)
        if confirm_name != database:
            raise ValueError("confirm_name must be exactly the database name")
        if database in ("admin", "config", "local"):
            raise ValueError(f"Refusing to drop system database '{database}'")
        manager().client(connection).drop_database(database)
        return {"dropped": database}
    return await _call(run, connection, kind="write")


# ====================================================================== commands & administration

@tool(_DESTRUCTIVE)
async def run_command(command: Any, connection: str | None = None, database: str | None = None) -> str:
    """Run any database command (like db.runCommand), e.g. {"serverStatus": 1}, {"usersInfo": 1},
    {"createUser": "app", "pwd": "...", "roles": [...]}, {"collMod": "orders", "validator": {...}},
    {"explain": {"find": "orders", "filter": {...}}, "verbosity": "executionStats"}.
    database defaults to the profile's database (use "admin" for server-level commands)."""
    def run():
        cmd = parse_ejson(command, "command", allow_none=False)
        if manager().profile(connection).readonly:
            check_command_readonly(cmd)
        db_name = database or manager().profile(connection).database or "admin"
        res = manager().client(connection)[db_name].command(cmd)
        res.pop("$clusterTime", None)
        res.pop("operationTime", None)
        text = json.dumps(to_jsonable(res), ensure_ascii=False)
        if len(text) > MAX_RESPONSE_CHARS:
            return {"truncated": True, "result_text": text[:MAX_RESPONSE_CHARS]}
        return {"result": json.loads(text)}
    return await _call(run, connection, kind="write")


@tool(_READ)
async def server_status(connection: str | None = None) -> str:
    """Server health summary: version, uptime, connections, operation counters, memory, replication role."""
    def run():
        s = manager().client(connection).admin.command("serverStatus")
        repl = s.get("repl") or {}
        return {"host": s.get("host"), "version": s.get("version"), "uptime_hours": round(s.get("uptime", 0) / 3600, 1),
                "connections": to_jsonable(s.get("connections")), "opcounters": to_jsonable(s.get("opcounters")),
                "memory_mb": to_jsonable(s.get("mem")), "replica_set": repl.get("setName"),
                "is_primary": repl.get("isWritablePrimary", repl.get("ismaster"))}
    return await _call(run, connection, kind="read")


@tool(_READ)
async def current_ops(connection: str | None = None, active_only: bool = True, min_seconds: int = 0) -> str:
    """Running operations (opid, op, namespace, seconds running, client, command summary)."""
    def run():
        cmd: dict[str, Any] = {"currentOp": 1}
        if active_only:
            cmd["active"] = True   # a query predicate on the operations
        else:
            cmd["$all"] = True     # include idle connections and system operations
        res = manager().client(connection).admin.command(cmd)
        ops = []
        for op in res.get("inprog", []):
            secs = op.get("secs_running", 0) or 0
            if secs < min_seconds:
                continue
            command = json.dumps(to_jsonable(op.get("command", {})), ensure_ascii=False)
            ops.append({"opid": to_jsonable(op.get("opid")), "op": op.get("op"), "ns": op.get("ns"),
                        "secs_running": secs, "client": op.get("client"), "desc": op.get("desc"),
                        "app_name": op.get("appName"), "command": command[:2000]})
        ops.sort(key=lambda o: -o["secs_running"])
        return {"count": len(ops), "operations": ops[:500]}
    return await _call(run, connection, kind="read")


@tool(_DESTRUCTIVE)
async def kill_op(opid: Any, connection: str | None = None) -> str:
    """Kill a running operation by opid (from current_ops)."""
    def run():
        _writable(connection)
        manager().client(connection).admin.command("killOp", op=opid)
        return {"killed": opid}
    return await _call(run, connection, kind="write")


def run_stdio() -> None:
    try:
        mcp.run("stdio")
    finally:
        if _manager is not None:
            _manager.close_all()
