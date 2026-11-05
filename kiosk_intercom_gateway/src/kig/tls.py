"""TLS for kiosks with **Encrypt communications** on.

Encrypted kiosks talk intercom over HTTPS and WSS only, with the kiosks'
own self-signed certificates: they don't check each other's certificates
(kiosk_http_client.dart: "kiosks do not check each other's certificates"),
they insist only that the connection is encrypted. The gateway does the
same: a self-signed certificate of its own, made once and kept in /data,
and no certificate checks towards the kiosks.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import os
import socket
import ssl
from pathlib import Path

_LOG = logging.getLogger(__name__)


def client_context() -> ssl.SSLContext:
    """Encrypted, unchecked: kiosks' certificates are self-signed."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class Certificate:
    """The gateway's own certificate and key, made on first use."""

    def __init__(self, directory: Path) -> None:
        self.cert = directory / "tls" / "cert.pem"
        self.key = directory / "tls" / "key.pem"
        self._server: ssl.SSLContext | None = None

    def server_context(self) -> ssl.SSLContext:
        if self._server is None:
            if not (self.cert.exists() and self.key.exists()):
                self._make()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                ctx.load_cert_chain(self.cert, self.key)
            except (ssl.SSLError, OSError) as err:  # damaged: make a new one
                _LOG.warning("certificate unusable (%s); making a new one", err)
                self._make()
                ctx.load_cert_chain(self.cert, self.key)
            self._server = ctx
        return self._server

    def _make(self) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Kiosk Intercom Gateway")])
        now = datetime.datetime.now(datetime.UTC)
        alt = [x509.DNSName(socket.gethostname() or "localhost"), x509.DNSName("localhost"),
               x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .sign(key, hashes.SHA256())
        )
        self.cert.parent.mkdir(parents=True, exist_ok=True)
        key_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        fd = os.open(self.key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key_pem)
        self.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        _LOG.info("made a certificate for encrypted kiosks")
