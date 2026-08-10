"""Step 4 — TLS certificate (§4.4).

The SAN is mandatory and is verified by **reading the certificate back**,
not by assuming what was just built: without `subjectAltName`, logging in
from the Mattermost mobile app fails with `x509: certificate relies on legacy
Common Name field` even when everything else is perfect — and it is a failure
that only ever appears on the client, never in the server's logs.
"""

from __future__ import annotations

from pathlib import Path

from axion_wizard.domain.config import WireguardVariant
from axion_wizard.render.console import console
from axion_wizard.services import certs
from axion_wizard.steps.base import Step, StepResult

CERT_RELATIVE_DIR = Path("nginx") / "certs"
CERT_FILENAME = "cert.crt"
KEY_FILENAME = "cert.key"

#: The server's address inside the tunnel. On Linux with `network_mode: host`
#: it is a real host IP and VPN clients arrive through it (§6.1), so the
#: certificate must cover it or the browser rejects it from inside the tunnel.
WIREGUARD_SERVER_IP = "10.8.0.1"


class CertificateStep(Step):
    name = "certificate"
    title = "TLS certificate"

    @property
    def cert_path(self) -> Path:
        return self.context.project_dir / CERT_RELATIVE_DIR / CERT_FILENAME

    @property
    def key_path(self) -> Path:
        return self.context.project_dir / CERT_RELATIVE_DIR / KEY_FILENAME

    def run(self) -> StepResult:
        config = self.context.require_config()
        extra_hosts = self._extra_san_hosts()

        if self.state.dry_run:
            console.print(
                f"[axion.info][dry-run][/] would generate {self.cert_path} and "
                f"{self.key_path} with a SAN for {config.host!r}"
                + (f" and {', '.join(extra_hosts)}" if extra_hosts else "")
            )
            self.context.cert_path = self.cert_path
            return StepResult(name=self.name, ok=True, message="skipped by --dry-run")

        result = certs.generate_certificate(
            config.host, self.cert_path, self.key_path, extra_hosts=extra_hosts
        )
        # Read it back from the file itself: the verification §4.4 requires.
        san_entries = certs.verify_certificate_has_san(result.cert_path)
        self.context.cert_path = result.cert_path

        console.print(f"[axion.ok]Certificate generated:[/] {result.cert_path}")
        console.print(f"[axion.ok]Private key:[/] {result.key_path} (permissions restricted)")
        console.print(f"[axion.info]SAN verified:[/] {', '.join(san_entries)}")

        return StepResult(
            name=self.name, ok=True, data={"san": san_entries}, message=", ".join(san_entries)
        )

    def verify(self) -> StepResult:
        if self.state.dry_run:
            return StepResult(name=self.name, ok=True, message="skipped by --dry-run")
        if not self.cert_path.exists():
            return StepResult(name=self.name, ok=False, message=f"{self.cert_path} does not exist")
        san_entries = certs.verify_certificate_has_san(self.cert_path)
        return StepResult(name=self.name, ok=True, message=", ".join(san_entries))

    def restore(self) -> None:
        if self.cert_path.exists():
            self.context.cert_path = self.cert_path

    def _extra_san_hosts(self) -> list[str]:
        """`10.8.0.1` only enters the SAN in the `host` variant.

        In the `ports` variant (Windows/Docker Desktop) that address exists
        only inside the VPN and Mattermost is reached through the LAN IP, so
        adding it would contribute nothing.
        """
        config = self.context.require_config()
        if config.wireguard_variant is WireguardVariant.HOST:
            return [WIREGUARD_SERVER_IP]
        return []
