"""TLS/SSL problem diagnosis: which server, what exactly failed, the evidence, and how to fix it.

The driver reports TLS problems as one-line OpenSSL messages inside a server selection timeout, and two different
problems (server requires TLS / server requires a client certificate) both look like "connection closed". This module
classifies the message and, where the message is ambiguous or the certificate itself is the problem, makes one short
TLS handshake to the server (no credentials are sent) to collect evidence: whether the server speaks TLS, the protocol,
and the certificate it presents (subject, issuer, names, validity, fingerprint).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
import socket
import ssl
from typing import Any
from urllib.parse import parse_qsl

TEST_ONLY = "tls_insecure: true (test environments only - the server's identity is not verified)"

# (problem, lower-case fragments) - first match wins, most specific first
_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("hostname_mismatch", ("hostname mismatch", "ip address mismatch")),
    ("certificate_expired", ("certificate has expired",)),
    ("certificate_not_yet_valid", ("certificate is not yet valid",)),
    ("certificate_revoked", ("certificate revoked", "certificate_revoked")),
    ("untrusted_certificate", ("self-signed certificate", "self signed certificate", "unable to get local issuer",
                               "unable to verify the first certificate", "unable to get issuer certificate")),
    ("weak_certificate", ("ca_md_too_weak", "ca md too weak", "ee_key_too_small", "ee key too small",
                          "key too small", "md too weak")),
    ("certificate_verify_failed", ("certificate_verify_failed", "certificate verify failed")),
    ("client_certificate_required", ("certificate required", "certificate_required",
                                     "peer did not return a certificate")),
    ("client_certificate_rejected", ("tlsv1 alert unknown ca", "alert_unknown_ca", "alert bad certificate",
                                     "bad_certificate", "alert certificate unknown")),
    ("client_certificate_file", ("private key doesn't match", "pem lib", "key values mismatch")),
    ("protocol_mismatch", ("unsupported protocol", "unsupported_protocol", "protocol version",
                           "no protocols available", "version too low", "tlsv1 alert protocol version")),
    ("server_not_tls", ("wrong version number", "wrong_version_number")),
    ("handshake_failed", ("ssl handshake failed", "unexpected_eof_while_reading", "eof occurred in violation",
                          "handshake failure", "handshake_failure", "no shared cipher", "[ssl")),
]
_CERT_PROBLEMS = {"hostname_mismatch", "certificate_expired", "certificate_not_yet_valid", "certificate_revoked",
                  "untrusted_certificate", "weak_certificate", "certificate_verify_failed"}
_CLOSED = ("connection closed", "connection reset", "reset by peer", "forcibly closed", "10054")
_ADDRESS = re.compile(r"(?:^|SSL handshake failed: )\[?([A-Za-z0-9_.\-]+|[0-9a-fA-F:]+)\]?:(\d{1,5}): ")


def uri_options(profile: Any) -> dict[str, str]:
    """Lower-cased query options of the profile's URI (empty for host-style profiles)."""
    if not getattr(profile, "uri", None):
        return {}
    rest = str(profile.uri).split("://", 1)[1]
    query = rest.split("?", 1)[1] if "?" in rest else ""
    return {k.lower(): v for k, v in parse_qsl(query, keep_blank_values=True)}


def _true(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def settings(profile: Any) -> dict[str, Any]:
    """Effective TLS settings of a profile (profile fields and URI options combined)."""
    opts = uri_options(profile)
    srv = str(getattr(profile, "uri", "") or "").startswith("mongodb+srv://")
    enabled = bool(profile.tls) or _true(opts.get("tls", opts.get("ssl", "false"))) or (
        srv and opts.get("tls", opts.get("ssl")) is None)
    insecure = bool(profile.tls_insecure) or _true(opts.get("tlsinsecure", "false"))
    no_certs = insecure or bool(profile.tls_allow_invalid_certificates) or _true(
        opts.get("tlsallowinvalidcertificates", "false"))
    no_hosts = insecure or bool(profile.tls_allow_invalid_hostnames) or _true(
        opts.get("tlsallowinvalidhostnames", "false"))
    out: dict[str, Any] = {"enabled": enabled}
    if not enabled:
        return out
    if no_certs and no_hosts:
        out["verification"] = "disabled"
        out["warning"] = ("TLS certificate verification is disabled (tls_insecure): traffic is encrypted but the "
                          "server's identity is not checked. Use only in test environments.")
    elif no_certs:
        out["verification"] = "certificate chain not verified (tls_allow_invalid_certificates)"
        out["warning"] = "The server certificate is not verified. Use only in test environments."
    elif no_hosts:
        out["verification"] = "host name not verified (tls_allow_invalid_hostnames)"
        out["warning"] = "The certificate's host name is not checked. Use only in test environments."
    else:
        out["verification"] = "full"
    ca = profile.tls_ca_file or opts.get("tlscafile")
    out["ca_file"] = ca or "system certificate store"
    cert = profile.tls_certificate_key_file or opts.get("tlscertificatekeyfile")
    if cert:
        out["client_certificate"] = cert
    return out


def classify(text: str) -> str | None:
    low = text.lower()
    for problem, fragments in _PATTERNS:
        if any(f in low for f in fragments):
            return problem
    return None


def is_tls_error(text: str) -> bool:
    return classify(text) is not None


def _split_address(address: str) -> tuple[str, int] | None:
    host, sep, port = address.rpartition(":")
    if not sep or not port.isdigit():
        return None
    return host.strip("[]"), int(port)


# ---------------------------------------------------------------------------------------------- evidence (probe)

def _describe_certificate(der: bytes) -> dict[str, Any]:
    info: dict[str, Any] = {"sha256": hashlib.sha256(der).hexdigest()}
    try:
        from cryptography import x509
        from cryptography.x509.oid import ExtensionOID
    except ImportError:  # pragma: no cover - cryptography ships with the bundles (via mcp)
        return info
    try:
        cert = x509.load_der_x509_certificate(der)
    except ValueError as exc:  # pragma: no cover - a server sending garbage
        info["parse_error"] = str(exc)[:200]
        return info
    start = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before.replace(tzinfo=_dt.timezone.utc)
    end = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=_dt.timezone.utc)
    now = _dt.datetime.now(_dt.timezone.utc)
    info.update(subject=cert.subject.rfc4514_string(), issuer=cert.issuer.rfc4514_string(),
                self_signed=cert.subject == cert.issuer,
                valid_from=start.isoformat(), valid_until=end.isoformat(),
                expired=now > end, not_yet_valid=now < start)
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        info["names"] = [str(n.value) for n in san]
    except x509.ExtensionNotFound:
        info["names"] = []
    return info


def probe(address: str, client_cert: str | None = None, timeout: float = 3.0) -> dict[str, Any]:
    """One TLS handshake without verification (nothing but the handshake is sent): does the server speak TLS, which
    protocol, which certificate, and does it keep the session open (it closes it at once when it requires or rejects a
    client certificate)."""
    parsed = _split_address(address)
    if parsed is None:
        return {"error": f"cannot parse address {address!r}"}
    host, port = parsed
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if client_cert:
        try:
            ctx.load_cert_chain(client_cert)
        except (OSError, ssl.SSLError) as exc:
            return {"client_certificate_error": str(exc)[:300]}
    out: dict[str, Any] = {}
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return {"tcp_connect": f"failed: {exc}"[:300]}
    try:
        try:
            tls = ctx.wrap_socket(raw, server_hostname=host)
        except (ssl.SSLError, OSError) as exc:
            out["server_speaks_tls"] = False
            out["handshake_error"] = str(exc)[:300]
            return out
        out["server_speaks_tls"] = True
        out["protocol"] = tls.version()
        cipher = tls.cipher()
        if cipher:
            out["cipher"] = cipher[0]
        der = tls.getpeercert(binary_form=True)
        if der:
            out["certificate"] = _describe_certificate(der)
        tls.settimeout(min(timeout, 1.5))
        try:
            data = tls.recv(1)
            out["session"] = "closed by the server right after the handshake" if not data else "open"
        except (socket.timeout, TimeoutError):
            out["session"] = "open"  # the server waits for a request: the TLS session was accepted
        except (ssl.SSLError, OSError) as exc:
            out["session"] = f"closed by the server right after the handshake: {exc}"[:300]
        return out
    finally:
        raw.close()


def _plain_hello(address: str, timeout_ms: int = 3000) -> bool:
    """Does the server answer 'hello' without TLS? (hello needs no authentication)."""
    import pymongo

    try:
        with pymongo.MongoClient(f"mongodb://{address}/", directConnection=True, tls=False,
                                 serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms) as cli:
            cli.admin.command("hello")
            return True
    except Exception:  # noqa: BLE001 - only the yes/no matters
        return False


# ---------------------------------------------------------------------------------------------- diagnosis

def _hostname_fix(cert: dict[str, Any] | None, host: str | None) -> list[str]:
    names = (cert or {}).get("names") or []
    fixes = []
    if names:
        fixes.append(f"Connect with a name the certificate is issued for: {', '.join(names[:10])} (you used "
                     f"{host!r}). Use that name in 'host' / the URI.")
    fixes.append("Or ask the DBA for a certificate that includes the name you connect with.")
    fixes.append("Test only: tls_allow_invalid_hostnames: true (keeps the certificate check) or " + TEST_ONLY)
    return fixes


def _explain(problem: str, tls: dict[str, Any], host: str | None, evidence: dict[str, Any]) -> tuple[str, list[str]]:
    cert = evidence.get("certificate")
    ca = tls.get("ca_file", "system certificate store")
    if problem == "untrusted_certificate":
        issuer = (cert or {}).get("issuer")
        return (f"The server's certificate is not trusted: it is self-signed or signed by a CA that is not in "
                f"{ca!r}{f' (issuer: {issuer})' if issuer else ''}.",
                ["Get the issuing CA certificate (PEM, from the DBA / IT) and set tls_ca_file to its path "
                 "(in JSON: \"tls_ca_file\": \"C:\\\\certs\\\\company-ca.pem\").",
                 "If tls_ca_file is already set, it is not the CA that signed this server's certificate.",
                 "Test only: " + TEST_ONLY])
    if problem == "hostname_mismatch":
        return (f"The certificate is valid but not for the name used to connect ({host!r}).",
                _hostname_fix(cert, host))
    if problem == "certificate_expired":
        until = (cert or {}).get("valid_until")
        return (f"The server's certificate has expired{f' (valid until {until})' if until else ''}.",
                ["Ask the DBA to renew the server certificate.", "Also check this computer's date and time.",
                 "Test only: " + TEST_ONLY])
    if problem == "certificate_not_yet_valid":
        since = (cert or {}).get("valid_from")
        return (f"The server's certificate is not valid yet{f' (valid from {since})' if since else ''}.",
                ["Check this computer's date and time.", "Test only: " + TEST_ONLY])
    if problem == "certificate_revoked":
        return ("The certificate was reported as revoked or its revocation status could not be checked.",
                ["Ask the DBA to replace the server certificate.", "Test only: " + TEST_ONLY])
    if problem == "weak_certificate":
        return ("OpenSSL on this computer rejects the certificate as too weak (short key or old signature "
                "algorithm such as SHA-1).",
                ["Ask the DBA for a certificate with an RSA key of 2048+ bits and a SHA-256 signature.",
                 "Test only: " + TEST_ONLY])
    if problem == "certificate_verify_failed":
        return ("The server's certificate could not be verified.",
                ["Check tls_ca_file and the certificate details below with the DBA.", "Test only: " + TEST_ONLY])
    if problem in ("client_certificate_required", "client_certificate_rejected"):
        if tls.get("client_certificate"):
            return ("The server closed the TLS session right after the handshake although a client certificate "
                    f"({tls['client_certificate']}) was sent: the server does not accept it (not signed by a CA it "
                    "trusts, expired, or not allowed).",
                    ["Ask the DBA which CA the server trusts for client certificates and get a matching one.",
                     "tls_insecure does not help here: it only affects checking the server's certificate."])
        return ("The server requires a client certificate (mutual TLS) and none was configured: it closes the TLS "
                "session right after the handshake.",
                ["Get a client certificate + key in one PEM file from the DBA and set tls_certificate_key_file.",
                 "tls_insecure does not help here: it only affects checking the server's certificate."])
    if problem == "client_certificate_file":
        return ("The client certificate file (tls_certificate_key_file) could not be used: it must contain the "
                "private key and the matching certificate in PEM format, without a password.",
                ["Check the file with the DBA; combine key and certificate into one .pem file."])
    if problem == "protocol_mismatch":
        return ("Client and server could not agree on a TLS version (one side only allows old or only new "
                "versions).",
                ["Ask the DBA which TLS versions the server allows (TLS 1.2+ is needed by current Python)."])
    if problem == "server_not_tls":
        return ("TLS is enabled for this profile but the server does not use TLS"
                f"{' (it answers without TLS)' if evidence.get('server_answers_without_tls') else ''}.",
                ["Remove \"tls\": true (and tls=true / ssl=true from the URI) for this connection."])
    if problem == "server_requires_tls":
        return ("The server requires TLS but this profile connects without it: the server closes the connection.",
                ["Add \"tls\": true to the profile (or tls=true to the URI).",
                 "If the server certificate is not trusted, set tls_ca_file; test only: " + TEST_ONLY])
    return ("The TLS handshake with the server failed.",
            ["Check the details below with the DBA (TLS enabled on the server? which versions / certificates?)."])


def diagnose(exc: BaseException, profile: Any, servers: list[dict[str, Any]] | None = None,
             do_probe: bool = True) -> dict[str, Any] | None:
    """A detailed TLS diagnosis if the error is TLS related, else None."""
    tls = settings(profile)
    candidates: list[tuple[str | None, str]] = []
    for entry in servers or []:
        if entry.get("error"):
            candidates.append((entry.get("address"), str(entry["error"])))
    message = str(exc)
    for part in re.split(r",\s*(?=[A-Za-z0-9_.\-]+:\d+: )", message.split(", Timeout:")[0]):
        match = _ADDRESS.search(part)
        candidates.append((match.group(1) + ":" + match.group(2) if match else None, part))
    if isinstance(exc, FileNotFoundError):
        path = getattr(exc, "filename", None)
        if path and path in (profile.tls_ca_file, profile.tls_certificate_key_file):
            which = "tls_ca_file" if path == profile.tls_ca_file else "tls_certificate_key_file"
            return {"problem": f"{which}_not_found", "summary": f"{which} {path!r} does not exist.",
                    "how_to_fix": ["Fix the path (in JSON escape backslashes: C:\\\\certs\\\\file.pem)."],
                    "settings": tls}
    problem = address = detail = None
    for addr, text in candidates:
        found = classify(text)
        if found:
            problem, address, detail = found, addr, text
            break
    if problem is None:
        closed = next(((a, t) for a, t in candidates if any(c in t.lower() for c in _CLOSED)), None)
        if closed is None:
            return None
        address, detail = closed
        if not do_probe or not address:
            return None
        evidence = probe(address, tls.get("client_certificate") if tls["enabled"] else None)
        if not tls["enabled"] and evidence.get("server_speaks_tls"):
            problem = "server_requires_tls"
        elif tls["enabled"] and evidence.get("server_speaks_tls") and str(evidence.get("session", "")).startswith(
                "closed"):
            problem = "client_certificate_rejected" if tls.get("client_certificate") else \
                "client_certificate_required"
        else:
            return None  # a plain connection loss, not TLS
    else:
        evidence = {}
        if do_probe and address and problem not in ("client_certificate_file",):
            evidence = probe(address, tls.get("client_certificate") if problem.startswith("client_") else None)
            if problem in ("handshake_failed", "server_not_tls") and evidence.get("server_speaks_tls") is False:
                if _plain_hello(address):
                    problem = "server_not_tls"
                    evidence["server_answers_without_tls"] = True
    host = _split_address(address)[0] if address and _split_address(address) else None
    summary, fixes = _explain(problem, tls, host, evidence)
    out: dict[str, Any] = {"problem": problem, "summary": summary}
    if address:
        out["server"] = address
    out["driver_detail"] = re.sub(r"\s*\(configured timeouts:[^)]*\)", "", detail or "")[:400]
    out["how_to_fix"] = fixes
    out["settings"] = tls
    if evidence:
        out["evidence"] = evidence
    if problem in _CERT_PROBLEMS:
        out["test_only_workaround"] = {"connections.json": {"tls_insecure": True},
                                       "env": {"MONGO_MCP_TLS_INSECURE": "true"},
                                       "uri": "&tlsInsecure=true"}
    return out
