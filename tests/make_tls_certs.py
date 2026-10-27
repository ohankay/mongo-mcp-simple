"""Create throw-away certificates for the TLS integration tests (test use only).

    python tests/make_tls_certs.py <dir>

Writes into <dir>:
    ca.pem                 test CA (the client trusts it via tls_ca_file)
    other-ca.pem           an unrelated CA (wrong tls_ca_file)
    server.pem             key + cert for localhost / 127.0.0.1, signed by ca.pem
    wronghost.pem          key + cert for "wronghost.example" only, signed by ca.pem
    expired.pem            key + cert for localhost that expired in 2021, signed by ca.pem
    client.pem             key + client certificate signed by ca.pem (mutual TLS)
    foreign-client.pem     key + client certificate signed by other-ca.pem (rejected by the server)
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "mongo-mcp tests"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _ca(cn: str):
    key = _key()
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn)).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def _leaf(ca_key, ca_cert, cn: str, dns: list[str], ips: list[str], client: bool = False,
          not_before: dt.datetime | None = None, not_after: dt.datetime | None = None):
    key = _key()
    now = dt.datetime.now(dt.timezone.utc)
    san = [x509.DNSName(d) for d in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    builder = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(ca_cert.subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(not_before or now - dt.timedelta(days=1))
               .not_valid_after(not_after or now + dt.timedelta(days=825))
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH] if client else
                                                    [ExtendedKeyUsageOID.SERVER_AUTH,
                                                     ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                              critical=False))
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    return key, builder.sign(ca_key, hashes.SHA256())


def _pem(key, cert) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()) + cert.public_bytes(serialization.Encoding.PEM)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    ca_key, ca_cert = _ca("mongo-mcp test CA")
    other_key, other_cert = _ca("unrelated CA")
    (out / "ca.pem").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (out / "other-ca.pem").write_bytes(other_cert.public_bytes(serialization.Encoding.PEM))
    (out / "server.pem").write_bytes(_pem(*_leaf(ca_key, ca_cert, "localhost", ["localhost"], ["127.0.0.1"])))
    (out / "wronghost.pem").write_bytes(_pem(*_leaf(ca_key, ca_cert, "wronghost.example", ["wronghost.example"], [])))
    (out / "expired.pem").write_bytes(_pem(*_leaf(
        ca_key, ca_cert, "localhost", ["localhost"], ["127.0.0.1"],
        not_before=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
        not_after=dt.datetime(2021, 1, 1, tzinfo=dt.timezone.utc))))
    (out / "client.pem").write_bytes(_pem(*_leaf(ca_key, ca_cert, "mcp-client", [], [], client=True)))
    (out / "foreign-client.pem").write_bytes(_pem(*_leaf(other_key, other_cert, "foreign", [], [], client=True)))
    for path in out.iterdir():
        path.chmod(0o644)  # mongod in the container runs as a different user


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "tls-certs"))
