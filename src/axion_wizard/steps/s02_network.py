"""Step 2 — Network checks (§4.2).

Four checks in the order the spec requires: LAN IP, CGNAT, free ports and
outbound connectivity. None aborts the flow on its own — unless the user
decides to stop: they are diagnostics the user needs *before* anything is
written to disk, not absolute requirements.

CGNAT cannot be detected unaided: it needs the WAN address the router shows
in its admin panel, and there is no reliable way to read it. The user is
asked and the two are compared (§4.2.2).
"""

from __future__ import annotations

import asyncio

from rich.table import Table

from axion_wizard.detect import network as detect_network
from axion_wizard.errors import NetworkError
from axion_wizard.render import ui
from axion_wizard.render.console import console
from axion_wizard.steps.base import Step, StepResult
from axion_wizard.steps.context import NetworkFacts
from axion_wizard.steps.prompts import interactive_input_available

#: Ports the stack needs to publish. Busy is not fatal here — it may be the
#: stack itself from a previous run — but it does warrant a warning.
CHECKED_PORTS = detect_network.REQUIRED_PORTS


class NetworkStep(Step):
    name = "network"
    title = "Network checks"

    def run(self) -> StepResult:
        facts = NetworkFacts()

        self._check_primary_interface(facts)
        self._check_ports(facts)
        asyncio.run(self._check_internet(facts))
        self._check_cgnat(facts)

        self.context.network = facts

        if not self.state.quiet:
            console.print(self._render_table(facts))

        if facts.unreachable_targets:
            self._confirm_or_abort(
                "No connectivity to "
                f"{', '.join(facts.unreachable_targets)}: pulling the images and the "
                "model will fail later on.",
                question="Continue anyway?",
            )

        return StepResult(
            name=self.name,
            ok=True,
            data={"lan_ip": facts.lan_ip or "", "cgnat": facts.cgnat},
            message=(
                f"LAN IP {facts.lan_ip or 'unknown'}, "
                f"CGNAT: {'yes' if facts.cgnat else 'no'}"
            ),
        )

    def verify(self) -> StepResult:
        """Ports the stack itself already uses do not count as a conflict, so
        verifying here only confirms we still have a LAN address."""
        iface = detect_network.get_primary_interface()
        if iface is None or not iface.ip:
            return StepResult(name=self.name, ok=False, message="no interface with a LAN IP")
        return StepResult(name=self.name, ok=True, message=f"LAN IP {iface.ip}")

    # --- individual checks ----------------------------------------------------------

    def _check_primary_interface(self, facts: NetworkFacts) -> None:
        iface = detect_network.get_primary_interface()
        if iface is None:
            self.context.warn("No interface with a LAN address was found.")
            return
        facts.lan_ip = iface.ip
        facts.interface_name = iface.name

    def _check_ports(self, facts: NetworkFacts) -> None:
        """Critical note from §4.2: under Docker Desktop, containers publish
        their ports in another VM, invisible to `psutil`. It is supplemented
        with `docker ps` so a port that is genuinely taken is not called
        free."""
        statuses = detect_network.check_ports_psutil(CHECKED_PORTS)
        statuses = detect_network.merge_docker_published_ports(statuses, _docker_ps_json())
        facts.busy_ports = [
            f"{s.port}/{s.protocol}" + (f" ({s.used_by})" if s.used_by else "")
            for s in statuses
            if s.in_use
        ]

    async def _check_internet(self, facts: NetworkFacts) -> None:
        facts.public_ip = await detect_network.get_public_ipv4()
        reachable = await detect_network.check_connectivity()
        facts.unreachable_targets = [name for name, ok in reachable.items() if not ok]

    def _check_cgnat(self, facts: NetworkFacts) -> None:
        """§4.2.2: compare the outbound public IP with the router's WAN address.

        If they differ, the ISP is doing Carrier-Grade NAT and no port
        forwarding will reach this host from the internet. Not a reason to
        abort: LAN and VPN access still work.
        """
        if facts.public_ip is None:
            self.context.warn(
                "The public IP could not be determined; CGNAT was not checked."
            )
            return
        if self.state.yes or self.state.unattended or not interactive_input_available():
            # With no interaction there is nothing to compare against: it is
            # left undetermined rather than inventing a verdict.
            return

        router_wan_ip = self._ask_router_wan_ip(facts.public_ip)
        if not router_wan_ip:
            return

        facts.cgnat = detect_network.is_cgnat(facts.public_ip, router_wan_ip)
        if not facts.cgnat:
            return

        console.print(
            f"[axion.warn]CGNAT detected.[/] Your public IP ({facts.public_ip}) does not "
            f"match the router's WAN address ({router_wan_ip}): the ISP uses "
            "Carrier-Grade NAT, so port forwarding will never reach this host from the "
            "internet."
        )
        self.context.warn(
            f"CGNAT detected ({facts.public_ip} != {router_wan_ip}): LAN/VPN access only."
        )
        self._confirm_or_abort(
            "Access from outside the LAN will not work without a WireGuard relay on a VPS.",
            question="Continue with LAN access only?",
        )

    def _ask_router_wan_ip(self, public_ip: str) -> str | None:
        import questionary

        console.print(
            f"[axion.info]Public IP detected:[/] {public_ip}\n"
            "[axion.dim]Ruling out CGNAT needs the WAN address shown in the router's "
            "admin panel. Leave it empty to skip this check.[/]"
        )
        answer = questionary.text("Router WAN address (optional):").ask()
        return (answer or "").strip() or None

    def _confirm_or_abort(self, explanation: str, question: str) -> None:
        console.print(f"[axion.warn]{explanation}[/]")
        if self.state.yes or self.state.unattended or not interactive_input_available():
            # With nobody to ask, carry on: the warning is already on screen,
            # and aborting an unattended install over a busy port would be
            # worse than continuing and reporting at the end.
            return

        import questionary

        if not questionary.confirm(question, default=True).ask():
            raise NetworkError(
                what="Install cancelled at the network checks",
                why=explanation,
                steps=[
                    "Fix the network problem and run `axion-wizard install` again.",
                    "The progress made so far is saved and resumes on its own.",
                ],
            )

    # --- presentation -------------------------------------------------------------------

    @staticmethod
    def _render_table(facts: NetworkFacts) -> Table:
        table = ui.make_table("Network checks")
        table.add_column("Check", style="axion.label")
        table.add_column("Result")
        table.add_column("Detail", overflow="fold")

        if facts.lan_ip:
            table.add_row(
                "Primary interface", ui.ok(), f"{facts.interface_name} — {facts.lan_ip}"
            )
        else:
            table.add_row("Primary interface", ui.fail(), "no LAN address")

        table.add_row(
            "Public IP (IPv4)",
            ui.status(bool(facts.public_ip), fail_label="UNKNOWN"),
            facts.public_ip or "could not be determined",
        )
        # Inverted semantics on purpose: here "YES" is the worrying result and
        # "NO" the good one — the other way round from every other row.
        table.add_row(
            "CGNAT",
            ui.warn("YES") if facts.cgnat else ui.ok("NO"),
            "LAN/VPN access only" if facts.cgnat else "",
        )
        table.add_row(
            "Required ports",
            ui.warn("IN USE") if facts.busy_ports else ui.ok("FREE"),
            ", ".join(facts.busy_ports),
        )
        table.add_row(
            "Outbound connectivity",
            ui.status(not facts.unreachable_targets),
            ", ".join(facts.unreachable_targets),
        )
        return table


def _docker_ps_json() -> list[dict]:
    """`docker ps --format json`, or an empty list if Docker does not answer.

    The ports containers publish are a supplement to the `psutil` check, not a
    requirement: if Docker is not ready yet, the step is still useful with
    what the host can see.

    Parsing goes through `utils.jsonio` rather than by hand: this copy only
    understood the "one object per line" format, so against a CLI that emitted
    a JSON array — which some versions do — the busy-port check under Docker
    Desktop silently returned "no containers".
    """
    from axion_wizard.utils.jsonio import parse_json_lines_or_array
    from axion_wizard.utils.shell import CommandNotFoundError, CommandTimeoutError, run

    try:
        result = run(["docker", "ps", "--format", "json"], timeout=15.0)
    except (CommandNotFoundError, CommandTimeoutError):
        return []
    if not result.ok:
        return []
    return parse_json_lines_or_array(result.stdout)
