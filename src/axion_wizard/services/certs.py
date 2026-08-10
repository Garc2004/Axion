"""Generación de certificados TLS con SAN, sin depender de `openssl` (§4.4).

Por qué importa: los clientes Go y las bibliotecas modernas de Android
rechazan certificados que solo declaran el host en el CN. Sin `subjectAltName`,
el login desde el app móvil de Mattermost falla con
`x509: certificate relies on legacy Common Name field` aunque todo lo demás
esté correcto — por eso el SAN es obligatorio y se verifica leyendo el propio
certificado de vuelta tras generarlo.
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

#: Nombres con los que el propio equipo que hospeda el stack se llama a sí
#: mismo. Van siempre en el SAN porque esa máquina es el sitio más probable
#: desde donde se abre Mattermost, y sin ellos `https://localhost` da un aviso
#: de certificado inválido aunque todo lo demás esté bien.
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
    """`IP:<ip>` o `DNS:<dominio>` para `host`, más los DNS names fijos que
    exige la spec (`axion.local`, `ia.local`) y los `extra_hosts` que pida
    quien llama.

    `extra_hosts` existe por §6.1: en Linux con `network_mode: host`,
    `10.8.0.1` es un IP real del host y los clientes de la VPN entran por ahí,
    así que el certificado tiene que cubrirlo además del host de acceso — o el
    navegador rechaza la conexión desde dentro del túnel.
    """
    general_names: list[x509.GeneralName] = [_general_name_for(host)]
    general_names.extend(x509.DNSName(name) for name in EXTRA_SAN_DNS_NAMES)

    seen = {host, *EXTRA_SAN_DNS_NAMES}
    # `localhost`/`127.0.0.1` van junto a los demás y con el mismo control de
    # duplicados, para no repetirlos si el host de acceso ya es uno de ellos.
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
    """Valida el host antes de construir el certificado.

    Sin esto, `cryptography` falla más abajo con un mensaje sobre longitudes
    de atributos X.509 que no le dice nada al usuario que escribió mal el
    `gen-cert`.
    """
    host = host.strip()
    if not host:
        raise ConfigError(
            what="No se indicó un host para el certificado",
            why="El certificado necesita una IP o un dominio para su CN y su SAN.",
            steps=["Ejecutar `axion-wizard gen-cert <IP|DOMINIO>` con un valor concreto."],
        )
    if len(host) > MAX_COMMON_NAME_LENGTH:
        raise ConfigError(
            what=f"El host del certificado excede {MAX_COMMON_NAME_LENGTH} caracteres",
            why=(
                f"El Common Name de X.509 está limitado a {MAX_COMMON_NAME_LENGTH} "
                f"caracteres y se recibieron {len(host)}."
            ),
            steps=["Usar un nombre de host más corto, o la IP de acceso."],
        )
    return host


def generate_certificate(
    host: str, cert_path: Path, key_path: Path, extra_hosts: Sequence[str] = ()
) -> GeneratedCert:
    """Genera un par cert/key autofirmado para `host` (IP o dominio) y lo
    escribe en disco con la clave privada restringida al usuario actual.

    `extra_hosts` añade nombres al SAN sin tocar el CN — ver
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
    """Escribe la clave privada sin exponerla ni un instante.

    `write_bytes` la crea con los permisos por defecto (0644 en POSIX) y
    `restrict_to_owner` la cierra *después*: entre las dos hay una ventana en
    la que cualquier usuario del sistema puede leerla. Es corta, pero es
    exactamente el escenario contra el que existe §6.2, y en POSIX se evita
    del todo creando el archivo ya en 0600.

    En Windows no hay equivalente atómico —el modo de `os.open` se ignora, ahí
    manda la ACL— así que ese camino sigue siendo escribir y luego `icacls`.
    """
    if _platform.system() == "Windows":
        key_path.write_bytes(pem)
        return

    # O_TRUNC y no O_EXCL: regenerar el certificado sobre uno existente es un
    # caso normal, no un error. El modo solo se aplica al crear, así que un
    # archivo preexistente se re-restringe con `restrict_to_owner` después.
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem)


def restrict_key_permissions(key_path: Path, timeout: float = 15.0) -> None:
    """`chmod 600` no tiene efecto real en Windows (§6.2): ahí se aplica una
    ACL restringida al usuario actual vía `icacls`."""
    restrict_to_owner(key_path, timeout=timeout)


def load_certificate(cert_path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(cert_path.read_bytes())


def describe_san(certificate: x509.Certificate) -> list[str]:
    """Lee la extensión SAN de vuelta del certificado y la formatea como
    `IP:...` / `DNS:...` — la verificación que exige §4.4."""
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
    """Lanza `ConfigError` si el certificado en `cert_path` no tiene SAN."""
    certificate = load_certificate(cert_path)
    san_entries = describe_san(certificate)
    if not san_entries:
        raise ConfigError(
            what=f"El certificado {cert_path} no tiene subjectAltName",
            why=(
                "Los clientes Go y las bibliotecas modernas de Android rechazan "
                "certificados que solo declaran el host en el CN. El login desde el "
                "app móvil de Mattermost fallaría con "
                "'x509: certificate relies on legacy Common Name field'."
            ),
            steps=["Regenerar el certificado con `axion-wizard gen-cert <IP|DOMINIO>`."],
        )
    return san_entries
