"""Paso 4 — Certificado TLS (§4.4).

El SAN es obligatorio y se verifica **leyendo el certificado de vuelta**, no
dando por hecho lo que se acaba de construir: sin `subjectAltName` el login
desde el app móvil de Mattermost falla con `x509: certificate relies on
legacy Common Name field` aunque el resto esté perfecto, y es un fallo que
solo aparece en el cliente, nunca en los logs del servidor.
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

#: IP del servidor dentro del túnel. En Linux con `network_mode: host` es un
#: IP real del host y los clientes de la VPN entran por ahí (§6.1), así que
#: el certificado debe cubrirlo o el navegador lo rechaza desde el túnel.
WIREGUARD_SERVER_IP = "10.8.0.1"


class CertificateStep(Step):
    name = "certificate"
    title = "Certificado TLS"

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
                f"[axion.info][dry-run][/] generaría {self.cert_path} y {self.key_path} "
                f"con SAN para {config.host!r}"
                + (f" y {', '.join(extra_hosts)}" if extra_hosts else "")
            )
            self.context.cert_path = self.cert_path
            return StepResult(name=self.name, ok=True, message="omitido por --dry-run")

        result = certs.generate_certificate(
            config.host, self.cert_path, self.key_path, extra_hosts=extra_hosts
        )
        # Releer del propio archivo: es la verificación que exige §4.4.
        san_entries = certs.verify_certificate_has_san(result.cert_path)
        self.context.cert_path = result.cert_path

        console.print(f"[axion.ok]Certificado generado:[/] {result.cert_path}")
        console.print(f"[axion.ok]Clave privada:[/] {result.key_path} (permisos restringidos)")
        console.print(f"[axion.info]SAN verificado:[/] {', '.join(san_entries)}")

        return StepResult(
            name=self.name, ok=True, data={"san": san_entries}, message=", ".join(san_entries)
        )

    def verify(self) -> StepResult:
        if self.state.dry_run:
            return StepResult(name=self.name, ok=True, message="omitido por --dry-run")
        if not self.cert_path.exists():
            return StepResult(name=self.name, ok=False, message=f"{self.cert_path} no existe")
        san_entries = certs.verify_certificate_has_san(self.cert_path)
        return StepResult(name=self.name, ok=True, message=", ".join(san_entries))

    def restore(self) -> None:
        if self.cert_path.exists():
            self.context.cert_path = self.cert_path

    def _extra_san_hosts(self) -> list[str]:
        """`10.8.0.1` solo entra en el SAN en la variante `host`.

        En la variante `ports` (Windows/Docker Desktop) esa IP existe
        únicamente dentro de la VPN y el acceso a Mattermost va por el IP de
        la LAN, así que añadirla no aportaría nada.
        """
        config = self.context.require_config()
        if config.wireguard_variant is WireguardVariant.HOST:
            return [WIREGUARD_SERVER_IP]
        return []
