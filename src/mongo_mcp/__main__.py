"""Entry point: ``python -m mongo_mcp`` (stdio MCP server) or ``python -m mongo_mcp --check``."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__


def _check(connection: str | None) -> int:
    from .client import ClientManager
    from .config import load_profiles
    from .errors import is_network_error
    from .server import _error, _server_info, _tls_file_error, _topology, add_tls_diagnosis, set_manager

    try:
        import pymongo
        driver = pymongo.version
    except ImportError:  # pragma: no cover
        driver = None
    report: dict = {"version": __version__, "python": sys.version.split()[0], "pymongo": driver}
    try:
        profiles, default, broken = load_profiles()
    except Exception as exc:  # noqa: BLE001
        report["config_error"] = str(exc)
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 2
    mgr = ClientManager(profiles, default, broken)
    set_manager(mgr)
    report["default_connection"] = default
    report["connections"] = {}
    names = [connection] if connection else list(profiles) + list(broken)
    ok = True
    for name in names:
        try:
            info = {"success": True, **_server_info(name)}
        except Exception as exc:  # noqa: BLE001
            info = _error(exc)
            if is_network_error(exc):
                info["error"]["stage"] = getattr(exc, "mcp_stage", "operation")
                info["error"]["servers"] = _topology(name)
                info["error"]["events"] = mgr.events(name)
                add_tls_diagnosis(info["error"], exc, name, info["error"]["servers"])
            elif _tls_file_error(exc):
                add_tls_diagnosis(info["error"], exc, name, None)
        ok = ok and bool(info.get("success"))
        report["connections"][name] = info
    mgr.close_all()
    if not names:
        report["note"] = "No connection configured (set MONGO_MCP_HOST / MONGO_MCP_URI or MONGO_MCP_CONFIG)."
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0 if ok and names else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mongo-mcp", description="MCP server for MongoDB")
    parser.add_argument("--version", action="version", version=f"mongo-mcp {__version__}")
    parser.add_argument("--check", action="store_true", help="test the configured connection(s) and exit")
    parser.add_argument("--connection", help="with --check: only this profile")
    parser.add_argument("--log-level", default="WARNING", help="stderr log level (default WARNING)")
    args = parser.parse_args(argv)
    # stdout is the MCP channel - logs must go to stderr only.
    logging.basicConfig(stream=sys.stderr, level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.check:
        return _check(args.connection)
    from .server import run_stdio

    run_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
