import datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from axion_wizard.errors import ConfigError
from axion_wizard.services import certs


def test_build_san_for_ip_address() -> None:
    names = certs.build_san_general_names("192.168.1.50")
    assert isinstance(names[0], x509.IPAddress)
    dns_values = [n.value for n in names if isinstance(n, x509.DNSName)]
    assert dns_values == ["axion.local", "ia.local", "localhost"]


def test_san_covers_the_loopback_of_the_hosting_machine() -> None:
    """The machine hosting the stack is the likeliest place Mattermost gets
    opened from, and without these names `https://localhost` throws an
    invalid-certificate warning even when everything else is right."""
    import ipaddress

    names = certs.build_san_general_names("192.168.1.50")
    dns_values = [n.value for n in names if isinstance(n, x509.DNSName)]
    ip_values = [n.value for n in names if isinstance(n, x509.IPAddress)]

    assert "localhost" in dns_values
    assert ipaddress.ip_address("127.0.0.1") in ip_values


def test_loopback_names_are_not_duplicated_when_they_are_the_host() -> None:
    names = certs.build_san_general_names("localhost")
    dns_values = [n.value for n in names if isinstance(n, x509.DNSName)]
    assert dns_values.count("localhost") == 1


def test_build_san_for_domain() -> None:
    names = certs.build_san_general_names("axion.example.com")
    assert isinstance(names[0], x509.DNSName)
    assert names[0].value == "axion.example.com"


def test_generate_certificate_writes_files_with_expected_properties(tmp_path: Path) -> None:
    cert_path = tmp_path / "cert.crt"
    key_path = tmp_path / "cert.key"

    result = certs.generate_certificate("192.168.1.50", cert_path, key_path)

    assert cert_path.exists()
    assert key_path.exists()
    assert result.san_entries

    certificate = certs.load_certificate(cert_path)
    assert certificate.signature_hash_algorithm.name == "sha256"

    public_key = certificate.public_key()
    assert isinstance(public_key, rsa.RSAPublicKey)
    assert public_key.key_size == 4096

    validity_days = (certificate.not_valid_after_utc - certificate.not_valid_before_utc).days
    assert validity_days == certs.CERT_VALIDITY_DAYS + 1  # +1 for the one-day backdating

    basic_constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic_constraints.critical is True
    assert basic_constraints.value.ca is False

    key_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage)
    assert key_usage.critical is True
    assert key_usage.value.digital_signature is True
    assert key_usage.value.key_encipherment is True

    eku = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku.value


def test_generate_certificate_for_domain_includes_all_san_entries(tmp_path: Path) -> None:
    cert_path = tmp_path / "cert.crt"
    key_path = tmp_path / "cert.key"
    result = certs.generate_certificate("axion.example.com", cert_path, key_path)
    assert "DNS:axion.example.com" in result.san_entries
    assert "DNS:axion.local" in result.san_entries
    assert "DNS:ia.local" in result.san_entries


def test_generate_certificate_for_ip_includes_ip_san(tmp_path: Path) -> None:
    cert_path = tmp_path / "cert.crt"
    key_path = tmp_path / "cert.key"
    result = certs.generate_certificate("192.168.1.50", cert_path, key_path)
    assert "IP:192.168.1.50" in result.san_entries


def test_verify_certificate_has_san_ok(tmp_path: Path) -> None:
    cert_path = tmp_path / "cert.crt"
    key_path = tmp_path / "cert.key"
    certs.generate_certificate("192.168.1.50", cert_path, key_path)
    entries = certs.verify_certificate_has_san(cert_path)
    assert entries


def _write_cert_without_san(cert_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "no-san.example.com")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def test_verify_certificate_has_san_raises_when_missing(tmp_path: Path) -> None:
    cert_path = tmp_path / "no-san.crt"
    _write_cert_without_san(cert_path)

    with pytest.raises(ConfigError, match="subjectAltName"):
        certs.verify_certificate_has_san(cert_path)


def test_describe_san_empty_when_extension_absent(tmp_path: Path) -> None:
    cert_path = tmp_path / "no-san.crt"
    _write_cert_without_san(cert_path)
    certificate = certs.load_certificate(cert_path)
    assert certs.describe_san(certificate) == []


def test_generate_certificate_rejects_empty_host(tmp_path: Path) -> None:
    """Without this guard, `cryptography` failed with a message about X.509
    attribute lengths that means nothing to the user."""
    with pytest.raises(ConfigError, match="No host was given"):
        certs.generate_certificate("", tmp_path / "c.crt", tmp_path / "c.key")


def test_generate_certificate_rejects_whitespace_only_host(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        certs.generate_certificate("   ", tmp_path / "c.crt", tmp_path / "c.key")


def test_generate_certificate_rejects_overlong_host(tmp_path: Path) -> None:
    too_long = "a" * 65 + ".example.com"
    with pytest.raises(ConfigError, match="exceeds"):
        certs.generate_certificate(too_long, tmp_path / "c.crt", tmp_path / "c.key")


def test_validate_cert_host_trims_whitespace() -> None:
    assert certs.validate_cert_host("  192.168.1.50  ") == "192.168.1.50"


def test_generate_certificate_trims_host_before_use(tmp_path: Path) -> None:
    result = certs.generate_certificate(
        "  192.168.1.50  ", tmp_path / "c.crt", tmp_path / "c.key"
    )
    assert "IP:192.168.1.50" in result.san_entries


def test_restrict_key_permissions_delegates_to_fsperms(mocker, tmp_path: Path) -> None:
    key_path = tmp_path / "cert.key"
    delegate_mock = mocker.patch("axion_wizard.services.certs.restrict_to_owner")
    certs.restrict_key_permissions(key_path, timeout=7.0)
    delegate_mock.assert_called_once_with(key_path, timeout=7.0)


def test_restrict_key_permissions_propagates_fsperms_errors(mocker, tmp_path: Path) -> None:
    key_path = tmp_path / "cert.key"
    mocker.patch(
        "axion_wizard.services.certs.restrict_to_owner",
        side_effect=ConfigError(what="boom", why="boom", steps=[]),
    )
    with pytest.raises(ConfigError):
        certs.restrict_key_permissions(key_path)
