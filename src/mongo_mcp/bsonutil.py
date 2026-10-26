"""Extended JSON in/out, result shaping and read-only guards."""

from __future__ import annotations

import json
from typing import Any, Iterable

from bson import json_util
from bson.json_util import JSONOptions, JSONMode

OUTPUT_OPTIONS = JSONOptions(json_mode=JSONMode.RELAXED, tz_aware=True, uuid_representation=4)
INPUT_OPTIONS = JSONOptions(tz_aware=True, uuid_representation=4)
MAX_RESPONSE_CHARS = 1_500_000


class ReadOnlyViolation(PermissionError):
    pass


def parse_ejson(value: Any, what: str, *, expect: type | tuple[type, ...] = dict, allow_none: bool = True) -> Any:
    """Accept a JSON object/array (already decoded) or a JSON string, honouring Extended JSON
    ({"$oid": ...}, {"$date": ...}, {"$numberDecimal": ...}, {"$regex": ...}, ...)."""
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"'{what}' is required")
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            if allow_none:
                return None
            raise ValueError(f"'{what}' is required")
        try:
            parsed = json_util.loads(text, json_options=INPUT_OPTIONS)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"'{what}' is not valid (Extended) JSON: {exc}") from None
    else:
        parsed = json_util.loads(json.dumps(value), json_options=INPUT_OPTIONS)
    if not isinstance(parsed, expect):
        names = expect.__name__ if isinstance(expect, type) else " or ".join(t.__name__ for t in expect)
        kind = {"dict": "a JSON object", "list": "a JSON array"}.get(names, names)
        raise ValueError(f"'{what}' must be {kind}")
    return parsed


def to_jsonable(value: Any) -> Any:
    """BSON value/document -> plain JSON-compatible structure (relaxed Extended JSON)."""
    return json.loads(json_util.dumps(value, json_options=OUTPUT_OPTIONS))


def collect_documents(cursor: Iterable[Any], max_docs: int, budget: int = MAX_RESPONSE_CHARS) -> dict[str, Any]:
    docs: list[Any] = []
    used = 0
    truncated = False
    reason = None
    for doc in cursor:
        if len(docs) >= max_docs:
            truncated, reason = True, f"limit of {max_docs} documents"
            break
        item = to_jsonable(doc)
        size = len(json.dumps(item, ensure_ascii=False))
        if used + size > budget:
            truncated, reason = True, "response size limit"
            break
        used += size
        docs.append(item)
    out: dict[str, Any] = {"count": len(docs), "documents": docs}
    if truncated:
        out["truncated"] = True
        out["note"] = (f"Result truncated at {len(docs)} documents ({reason}). Narrow the filter, use a projection, "
                       "or raise 'limit'.")
    return out


# ---------------------------------------------------------------- read-only guards

_WRITE_STAGES = {"$out", "$merge"}
READ_COMMANDS = {
    "ping", "buildinfo", "serverstatus", "dbstats", "collstats", "datasize", "listcollections", "listindexes",
    "listdatabases", "hello", "ismaster", "connectionstatus", "hostinfo", "currentop", "explain", "count",
    "distinct", "find", "aggregate", "getparameter", "replsetgetstatus", "replsetgetconfig", "usersinfo",
    "rolesinfo", "top", "validate", "getlog", "listcommands", "features", "getcmdlineopts", "dbhash",
    "getdefaultrwconcern", "shardingstate", "listshards", "balancerstatus", "whatsmyuri", "lockinfo",
}


def check_pipeline_readonly(pipeline: list[Any]) -> None:
    for stage in pipeline:
        if isinstance(stage, dict) and _WRITE_STAGES & set(stage):
            raise ReadOnlyViolation("Read-only connection: $out / $merge stages are not allowed")


def check_command_readonly(command: dict[str, Any]) -> None:
    if not command:
        raise ValueError("command must not be empty")
    name = str(next(iter(command))).lower()
    if name not in READ_COMMANDS:
        raise ReadOnlyViolation(f"Read-only connection: command '{next(iter(command))}' is not allowed")
    if name == "aggregate":
        check_pipeline_readonly(command.get("pipeline") or [])
    if name == "explain" and isinstance(command.get("explain"), dict):
        check_command_readonly(command["explain"])
