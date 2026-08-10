"""TLS certificate generation with SAN, without depending on `openssl` (§4.4).

Why it matters: Go clients and modern Android libraries reject certificates
that only declare the host in the CN. Without `subjectAltName`, logging in
from the Mattermost mobile app fails with
`x509: certificate relies on legacy Common Name field` even when everything
else is correct — hence the SAN being mandatory and verified by reading the
certificate itself back after generating it.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import platform as _platform
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from axion_wizard.errors import ConfigError
from axion_wizard.utils.fsperms import restrict_to_owner

RSA_KEY_SIZE = 4096
CERT_VALIDITY_DAYS = 825
EXTRA_SAN_DNS_NAMES = ("axion.local", "ia.local")

#: The names the machine hosting the stack uses for itself. They always go in
#: the SAN because that machine is the likeliest place Mattermost gets opened
#: from, and without them `https://localhost` throws an invalid-certificate
#: warning even when everything else is right.
LOOPBACK_SAN_NAMES = ("localhost", "127.0.0.1")


@dataclass
class GeneratedCert:
    cert_path: Path
    key_path: Path
    san_entries: list[str]


def _general_name_for(host: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def build_san_general_names(
    host: str, extra_hosts: Sequence[str] = ()
) -> list[x509.GeneralName]:
    """`IP:<ip>` or `DNS:<domain>` for `host`, plus the fixed DNS names the
    spec requires (`axion.local`, `ia.local`) and whatever `extra_hosts` the
    caller asks for.

    `extra_hosts` exists because of §6.1: on Linux with `network_mode: host`,
    `10.8.0.1` is a real host IP and VPN clients arrive through it, so the
    certificate has to cover it as well as the access host — otherwise the
    browser refuses the connection from inside the tunnel.
    """
    general_names: list[x509.GeneralName] = [_general_name_for(host)]
    general_names.extend(x509.DNSName(name) for name in EXTRA_SAN_DNS_NAMES)

    seen = {host, *EXTRA_SAN_DNS_NAMES}
    # `localhost`/`127.0.0.1` go in with the rest and through the same
    # duplicate check, so they are not repeated when the access host is
    # already one of them.
    for extra in (*LOOPBACK_SAN_NAMES, *extra_hosts):
        extra = extra.strip()
        if extra and extra not in seen:
            seen.add(extra)
            general_names.append(_general_name_for(extra))
    return general_names


def _build_certificate(
    host: str, private_key: rsa.RSAPrivateKey, extra_hosts: Sequence[str] = ()
) -> x509.Certificate:
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.UTC)

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(build_san_general_names(host, extra_hosts)),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    return builder.sign(private_key, hashes.SHA256())


MAX_COMMON_NAME_LENGTH = 64


def validate_cert_host(host: str) -> str:
    """Validate the host before building the certificate.

    Without this, `cryptography` fails further down with a message about X.509
    attribute lengths that means nothing to a user who simply mistyped their
    `gen-cert` argument.
    """
    host = host.strip()
    if not host:
        raise ConfigError(
            what="No host was given for the certificate",
            why="The certificate needs an IP or a domain for its CN and its SAN.",
            steps=["Run `axion-wizard gen-cert <IP|DOMAIN>` with a concrete value."],
        )
    if len(host) > MAX_COMMON_NAME_LENGTH:
        raise ConfigError(
            what=f"The certificate host exceeds {MAX_COMMON_NAME_LENGTH} characters",
            why=(
                f"X.509's Common Name is limited to {MAX_COMMON_NAME_LENGTH} "
                f"characters and {len(host)} were given."
            ),
            steps=["Use a shorter host name, or the access IP."],
        )
    return host


def generate_certificate(
    host: str, cert_path: Path, key_path: Path, extra_hosts: Sequence[str] = ()
) -> GeneratedCert:
    """Generate a self-signed cert/key pair for `host` (IP or domain) and
    write it to disk with the private key restricted to the current user.

    `extra_hosts` adds names to the SAN without touching the CN — see
    `build_san_general_names`.
    """
    host = validate_cert_host(host)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_SIZE)
    certificate = _build_certificate(host, private_key, extra_hosts)

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    _write_private_key(
        key_path,
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    restrict_key_permissions(key_path)

    return GeneratedCert(
        cert_path=cert_path, key_path=key_path, san_entries=describe_san(certificate)
    )


def _write_private_key(key_path: Path, pem: bytes) -> None:
    """Write the private key without exposing it for even an instant.

    `write_bytes` creates it with default permissions (0644 on POSIX) and
    `restrict_to_owner` locks it down *afterwards*: between the two there is a
    window in which any user on the system can read it. It is short, but it is
    exactly the scenario §6.2 exists to prevent, and on POSIX it is avoided
    entirely by creating the file at 0600 in the first place.

    Windows has no atomic equivalent — `os.open`'s mode is ignored there, the
    ACL is what counts — so that path remains write-then-`icacls`.
    """
    if _platform.system() == "Windows":
        key_path.write_bytes(pem)
        return

    # O_TRUNC and not O_EXCL: regenerating the certificate over an existing
    # one is normal, not an error. The mode only applies on creation, so a
    # pre-existing file is re-locked by `restrict_to_owner` afterwards.
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem)


def restrict_key_permissions(key_path: Path, timeout: float = 15.0) -> None:
    """`chmod 600` has no real effect on Windows (§6.2): there an ACL
    restricted to the current user is applied via `icacls` instead."""
    restrict_to_owner(key_path, timeout=timeout)


def load_certificate(cert_path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(cert_path.read_bytes())


def describe_san(certificate: x509.Certificate) -> list[str]:
    """Read the SAN extension back off the certificate and format it as
    `IP:...` / `DNS:...` — the verification §4.4 requires."""
    try:
        ext = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return []

    entries = []
    for name in ext.value:
        if isinstance(name, x509.DNSName):
            entries.append(f"DNS:{name.value}")
        elif isinstance(name, x509.IPAddress):
            entries.append(f"IP:{name.value}")
    return entries


def verify_certificate_has_san(cert_path: Path) -> list[str]:
    """Raise `ConfigError` if the certificate at `cert_path` has no SAN."""
    certificate = load_certificate(cert_path)
    san_entries = describe_san(certificate)
    if not san_entries:
        raise ConfigError(
            what=f"The certificate at {cert_path} has no subjectAltName",
            why=(
                "Go clients and modern Android libraries reject certificates that "
                "only declare the host in the CN. Logging in from the Mattermost "
                "mobile app would fail with "
                "'x509: certificate relies on legacy Common Name field'."
            ),
            steps=["Regenerate the certificate with `axion-wizard gen-cert <IP|DOMAIN>`."],
        )
    return san_entries
