"""Turn driver exceptions into short, model-friendly error dictionaries."""

from __future__ import annotations

from typing import Any


def is_network_error(exc: BaseException) -> bool:
    from pymongo import errors as pe

    return isinstance(exc, (pe.AutoReconnect, pe.ServerSelectionTimeoutError, pe.NetworkTimeout))


def describe_error(exc: BaseException) -> dict[str, Any]:
    from pymongo import errors as pe

    info: dict[str, Any] = {"type": type(exc).__name__}
    if isinstance(exc, pe.BulkWriteError):
        details = exc.details or {}
        write_errors = details.get("writeErrors") or []
        info["message"] = write_errors[0].get("errmsg", str(exc)) if write_errors else str(exc)
        if write_errors:
            info["code"] = write_errors[0].get("code")
            info["failed_index"] = write_errors[0].get("index")
        info["write_errors"] = len(write_errors)
        info["inserted"] = details.get("nInserted")
        return info
    if isinstance(exc, pe.OperationFailure):
        details = exc.details or {}
        info["message"] = details.get("errmsg") or str(exc)
        if exc.code is not None:
            info["code"] = exc.code
        if details.get("codeName"):
            info["code_name"] = details["codeName"]
        if exc.code == 18 or details.get("codeName") == "AuthenticationFailed":
            info["hint"] = "Check user / password / auth_source (users created in 'admin' need auth_source=admin)."
        if exc.code == 13 or details.get("codeName") == "Unauthorized":
            info["hint"] = "The login lacks the privilege for this action on this database."
        return info
    if isinstance(exc, pe.ServerSelectionTimeoutError):
        info["message"] = ("Could not reach the MongoDB server within the timeout. Check host/port, firewall, "
                           "replica_set / direct_connection and TLS settings. Driver detail: " + str(exc)[:500])
        return info
    if type(exc).__name__ == "_OperationCancelled" or (isinstance(exc, pe.AutoReconnect)
                                                        and not str(exc).strip(" .").replace("operation cancelled", "")):
        info["message"] = (
            "The driver cancelled the operation because its connection to the server was reset (a network error or "
            "timeout seen by the server monitor - typical with firewalls/VPNs that cut long-lived connections, or "
            "an unreachable replica set member). See 'servers' for each member's state and last error.")
        return info
    if isinstance(exc, pe.AutoReconnect):
        info["message"] = "Lost the connection to the server: " + (str(exc)[:500] or type(exc).__name__)
        return info
    if isinstance(exc, pe.ConfigurationError):
        info["message"] = str(exc)
        return info
    info["message"] = str(exc) or type(exc).__name__
    return info
