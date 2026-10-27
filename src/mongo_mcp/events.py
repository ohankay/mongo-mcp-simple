"""Driver event recorder: evidence for network-type errors instead of guesses.

Records, per connection profile, the driver events that explain why an operation failed - failed heartbeats,
server state changes, pool clears (with whether in-progress operations were interrupted), connection check-out
failures and failed commands with the server they ran on. Only event type, address, timing and error summary are
kept: never command documents, replies or credentials.
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from collections import deque
from typing import Any

from pymongo import monitoring


def _addr(address: Any) -> str | None:
    if not address:
        return None
    try:
        return f"{address[0]}:{address[1]}"
    except (TypeError, IndexError):
        return str(address)


def _short(value: Any, limit: int = 300) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


class EventRecorder(monitoring.ServerHeartbeatListener, monitoring.ServerListener, monitoring.ConnectionPoolListener,
                    monitoring.CommandListener):
    def __init__(self, maxlen: int = 200):
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ storage
    def _add(self, kind: str, **fields: Any) -> None:
        entry = {"at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds"), "event": kind,
                 "_mono": time.monotonic()}
        entry.update({k: v for k, v in fields.items() if v is not None})
        with self._lock:
            self._events.append(entry)

    def recent(self, seconds: float = 120.0, limit: int = 30) -> list[dict[str, Any]]:
        cutoff = time.monotonic() - seconds
        with self._lock:
            items = [e for e in self._events if e["_mono"] >= cutoff]
        return [{k: v for k, v in e.items() if k != "_mono"} for e in items[-limit:]]

    # ------------------------------------------------------------------ heartbeats
    def started(self, event: Any) -> None:  # heartbeat started / command started: not recorded
        pass

    def succeeded(self, event: Any) -> None:  # heartbeat or command succeeded: not recorded
        pass

    def failed(self, event: Any) -> None:
        if isinstance(event, monitoring.ServerHeartbeatFailedEvent):
            self._add("heartbeat_failed", address=_addr(event.connection_id),
                      duration_ms=round(event.duration * 1000, 1), awaited=getattr(event, "awaited", None),
                      error=_short(event.reply))
        elif isinstance(event, monitoring.CommandFailedEvent):
            failure = event.failure or {}
            self._add("command_failed", command=event.command_name, address=_addr(event.connection_id),
                      duration_ms=round(event.duration_micros / 1000, 1),
                      error=_short(failure.get("errmsg") or failure.get("codeName") or failure))

    # ------------------------------------------------------------------ server state
    def opened(self, event: Any) -> None:
        pass

    def description_changed(self, event: Any) -> None:
        old, new = event.previous_description, event.new_description
        if old.server_type == new.server_type and new.error is None:
            return
        self._add("server_changed", address=_addr(event.server_address), previous=old.server_type_name,
                  now=new.server_type_name, error=_short(new.error) if new.error is not None else None)

    def closed(self, event: Any) -> None:
        pass

    # ------------------------------------------------------------------ pools
    def pool_created(self, event: Any) -> None:
        pass

    def pool_ready(self, event: Any) -> None:
        pass

    def pool_cleared(self, event: Any) -> None:
        self._add("pool_cleared", address=_addr(event.address),
                  interrupted_in_use_connections=bool(getattr(event, "interrupt_connections", False)))

    def pool_closed(self, event: Any) -> None:
        pass

    def connection_created(self, event: Any) -> None:
        pass

    def connection_ready(self, event: Any) -> None:
        pass

    def connection_closed(self, event: Any) -> None:
        if event.reason not in ("idle", "poolClosed"):  # stale/error closes are evidence
            self._add("connection_closed", address=_addr(event.address), reason=event.reason)

    def connection_check_out_started(self, event: Any) -> None:
        pass

    def connection_check_out_failed(self, event: Any) -> None:
        self._add("connection_check_out_failed", address=_addr(event.address), reason=event.reason)

    def connection_checked_out(self, event: Any) -> None:
        pass

    def connection_checked_in(self, event: Any) -> None:
        pass
