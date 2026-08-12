"""Step 1 — Environment detection (§4.1).

Produces `wireguard_variant`, the decisive output the whole rest of the flow
depends on: `host` on native Linux with Docker Engine, `ports` on Windows or
under any Docker Desktop context.

This step writes nothing; it only looks and decides. Hence it fails early and
with an actionable message on anything that would make deployment unviable —
Docker missing, Compose v1 — rather than letting it blow up eight steps later.
"""

from __future__ import annotations

from rich.table import Table

from axion_wizard.detect import docker as detect_docker
from axion_wizard.detect import platform as detect_platform
from axion_wizard.detect.hardware import HardwareInfo, detect_hardware
from axion_wizard.domain.config import WireguardVariant
from axion_wizard.errors import PlatformError
from axion_wizard.render import ui
from axion_wizard.render.console import console
from axion_wizard.steps.base import Step, StepResult
from axion_wizard.steps.context import EnvironmentFacts

#: Prefix of the Windows filesystem mounted inside WSL. A `project_dir` here
#: works, but with slow crossed I/O and no POSIX permissions (§6.2).
WINDOWS_MOUNT_PREFIX = "/mnt/"


class EnvironmentStep(Step):
    name = "environment"
    title = "Environment detection"

    def run(self) -> StepResult:
        os_info = detect_platform.get_os_info()
        wsl = detect_platform.gather_wsl_info()
        docker = detect_docker.gather_docker_info()
        hardware = detect_hardware()

        self._assert_docker_is_usable(docker)

        variant = detect_platform.decide_wireguard_variant(
            os_info.name, docker.context.is_desktop
        )
        gpu_acceleration = self._check_gpu_passthrough(hardware)
        ipv6_supported = self._check_ipv6_netfilter(variant)
        facts = EnvironmentFacts(
            os_info=os_info,
            wsl=wsl,
            docker=docker,
            hardware=hardware,
            wireguard_variant=variant,
            gpu_acceleration=gpu_acceleration,
            wireguard_ipv6_supported=ipv6_supported,
        )
        self.context.environment = facts

        self._warn_about_crossed_filesystem(wsl)
        self._warn_about_broken_mirrored(wsl)
        self._warn_about_windows_docker_desktop_lan_exposure(os_info, wsl, docker, variant)

        if not self.state.quiet:
            console.print(self._render_table(facts))

        return StepResult(
            name=self.name,
            ok=True,
            data={"wireguard_variant": variant},
            message=f"{os_info.name} {os_info.release}, WireGuard variant `{variant}`",
        )

    def verify(self) -> StepResult:
        """Re-detect and confirm the environment still works.

        Not paranoia: between one run and the next the user may have changed
        the Docker context (`docker context use`) — leaving the already
        rendered compose file no longer matching the platform — or uninstalled
        Docker entirely, in which case *nothing* that follows can work and
        carrying on only delays the error until a step that is not at fault.
        """
        docker = detect_docker.gather_docker_info()
        os_info = detect_platform.get_os_info()

        if not docker.installed:
            return StepResult(
                name=self.name, ok=False, message="Docker is no longer available on this system"
            )
        if not docker.compose_is_v2:
            return StepResult(
                name=self.name, ok=False, message="Docker Compose v2 is no longer available"
            )

        variant = detect_platform.decide_wireguard_variant(
            os_info.name, docker.context.is_desktop
        )
        expected = self.context.require_environment().wireguard_variant
        if variant != expected:
            return StepResult(
                name=self.name,
                ok=False,
                message=f"the variant changed from `{expected}` to `{variant}`",
            )
        return StepResult(name=self.name, ok=True, message=f"variant `{variant}`")

    def restore(self) -> None:
        """Resuming means detecting again: none of this is persisted, and
        re-detecting is cheap and touches nothing."""
        self.run()

    # --- viability checks -------------------------------------------------------

    def _assert_docker_is_usable(self, docker: detect_docker.DockerInfo) -> None:
        if not docker.installed:
            raise PlatformError(
                what="Docker was not found on this system",
                why=(
                    "The entire AXION stack runs in containers; without the Docker "
                    "engine there is nothing to deploy."
                ),
                steps=[
                    "Windows: install Docker Desktop and start it "
                    "(https://docs.docker.com/desktop/install/windows-install/).",
                    "Linux: install Docker Engine "
                    "(https://docs.docker.com/engine/install/).",
                    "Check it answers: docker --version",
                ],
            )
        if not docker.compose_is_v2:
            detected = docker.compose_version or "not detected"
            raise PlatformError(
                what=f"Docker Compose v2 is required and this was found: {detected}",
                why=(
                    "The compose file the wizard generates uses Compose v2 syntax "
                    "(`docker compose`, no hyphen). Under v1 (`docker-compose`) the "
                    "deployment fails with schema errors that are hard to read."
                ),
                steps=[
                    "Update Docker Desktop to a recent version (it ships Compose v2).",
                    "Linux: install your distribution's `docker-compose-plugin`.",
                    "Check: docker compose version",
                ],
            )

    def _check_gpu_passthrough(self, hardware: HardwareInfo) -> str:
        """Decide how the GPU is handed to Ollama, by actually testing it.

        Each vendor hands it over by a different mechanism and the matching
        one has to be tested: NVIDIA through the runtime (`--gpus`), AMD
        through the kernel devices (`/dev/kfd`, `/dev/dri`). Testing NVIDIA's
        on an AMD machine always comes back negative, and the GPU went unused
        with nothing to explain why.

        With no GPU detected there is nothing to test — and testing anyway
        would cost an unnecessary image pull on most installs.
        """
        if not hardware.has_gpu:
            return detect_docker.GPU_ACCELERATION_NONE

        vendors = {gpu.vendor for gpu in hardware.gpus}
        gpu_label = ", ".join(g.name or g.vendor for g in hardware.gpus)

        if "nvidia" in vendors and detect_docker.docker_gpu_passthrough_works():
            return detect_docker.GPU_ACCELERATION_NVIDIA
        if "amd" in vendors and detect_docker.docker_rocm_passthrough_works():
            return detect_docker.GPU_ACCELERATION_ROCM

        self._warn_gpu_unusable(gpu_label, vendors)
        return detect_docker.GPU_ACCELERATION_NONE

    def _warn_gpu_unusable(self, gpu_label: str, vendors: set[str]) -> None:
        """Explain *why* the GPU will not be used, which differs case by case.
        A generic warning sent people with an Intel GPU off to check their
        NVIDIA driver, where there is nothing to check."""
        if vendors == {"intel"}:
            why = (
                "Ollama publishes no image for Intel GPUs, so there is no way to make "
                "use of it from this stack."
            )
        elif "amd" in vendors:
            why = (
                "Docker could not open /dev/kfd and /dev/dri. Usual causes: the kernel "
                "does not carry the `amdgpu` module, ROCm is not installed, or the "
                "user is not in the `video` and `render` groups."
            )
        else:
            why = (
                "Usual causes: a GPU with no passthrough support under WSL2, an "
                "out-of-date NVIDIA driver, or a missing `nvidia-container-toolkit`."
            )

        message = (
            f"A GPU was detected ({gpu_label}) but it cannot be used for the AI on "
            f"this machine — the model will run on CPU. {why}"
        )
        self.context.warn(message)
        console.print(f"[axion.warn]{message}[/]")

    def _check_ipv6_netfilter(self, variant: str) -> bool:
        """Whether the runtime's kernel can run IPv6 netfilter rules, by
        actually testing it — see `detect_docker.docker_ipv6_netfilter_works`
        for the failure this exists to catch.

        Only the `ports` variant's compose section reacts to the result
        (`network_mode: host` has no IPv6 handling to switch off), so that is
        the only case worth paying for a probe — which pulls the wg-easy
        image early. Skipping it for `host` also means native Linux keeps its
        current behaviour by default rather than losing IPv6 in the tunnel to
        a probe it did not need.
        """
        if variant != WireguardVariant.PORTS.value:
            return True

        supported = detect_docker.docker_ipv6_netfilter_works()
        if not supported:
            console.print(
                "[axion.dim]This machine's Docker cannot run IPv6 netfilter rules "
                "(common under Docker Desktop): the WireGuard tunnel will carry IPv4 "
                "only.[/]"
            )
        return supported

    # --- non-fatal warnings --------------------------------------------------------

    def _warn_about_crossed_filesystem(self, wsl: detect_platform.WslInfo) -> None:
        """§6.2: a `project_dir` under `/mnt/c/...` from WSL works, but the
        crossed I/O is slow and POSIX permissions are not preserved — exactly
        the ones `.env` and the certificate key need."""
        if not wsl.inside_wsl:
            return
        if not str(self.context.project_dir).replace("\\", "/").startswith(WINDOWS_MOUNT_PREFIX):
            return
        message = (
            f"The project is at {self.context.project_dir}, inside the Windows "
            "filesystem mounted in WSL: I/O is slow and the POSIX permissions on "
            "`.env` and `cert.key` are not preserved. Better to move it onto the WSL "
            "filesystem (~/)."
        )
        self.context.warn(message)
        console.print(f"[axion.warn]{message}[/]")

    def _warn_about_broken_mirrored(self, wsl: detect_platform.WslInfo) -> None:
        """Mirrored configured but with `eth0` back in `172.16/12` means it was
        not applied: the stack will not be visible on the LAN."""
        if not (wsl.inside_wsl and wsl.mirrored_configured):
            return
        from axion_wizard.detect import network as detect_network

        iface = detect_network.get_primary_interface()
        if iface is None or not detect_platform.is_eth0_in_forbidden_range(iface.ip):
            return
        message = (
            f"`.wslconfig` asks for networkingMode=mirrored, but the interface reports "
            f"{iface.ip} (Docker Desktop's internal range): mirrored is not active. "
            "The stack will not be reachable from the LAN without `netsh portproxy`."
        )
        self.context.warn(message)
        console.print(f"[axion.warn]{message}[/]")

    def _warn_about_windows_docker_desktop_lan_exposure(
        self,
        os_info: detect_platform.OsInfo,
        wsl: detect_platform.WslInfo,
        docker: detect_docker.DockerInfo,
        variant: str,
    ) -> None:
        """LAN access under Docker Desktop on Windows is not automatic, and
        `axion-wizard.exe` almost always runs natively on Windows (not inside
        WSL) — so `_warn_about_broken_mirrored`, which requires
        `wsl.inside_wsl`, never gets evaluated in the commonest case.

        Discovered live: a deployment with Docker publishing the ports
        correctly and the firewall configured properly still did not answer
        from the LAN, because either (a) mirrored networking was not really
        active, or (b) the interface was categorised "Public" on Windows,
        which applies `BlockInbound` by default — neither of which appears in
        any log, Docker's or the app's own.
        """
        if wsl.inside_wsl or os_info.name != "Windows" or not docker.context.is_desktop:
            return
        if variant != WireguardVariant.PORTS.value:
            return

        wslconfig_path = detect_platform.locate_wslconfig_native()
        mirrored_configured = detect_platform.is_mirrored_networking_configured(wslconfig_path)

        if not mirrored_configured:
            message = (
                "Docker Desktop on Windows does not expose its ports to the LAN by "
                "default: only to `localhost`. Without `networkingMode=mirrored` in "
                r"%UserProfile%\.wslconfig, Mattermost and the WireGuard panel will "
                "probably not answer from other devices on the network, even though "
                "they work perfectly on this machine."
            )
            steps_hint = (
                "enable mirrored networking (recommended) or configure "
                "`netsh interface portproxy` plus a firewall rule pointing at the LAN "
                "address"
            )
        else:
            from axion_wizard.detect import network as detect_network

            iface = detect_network.get_primary_interface()
            category = detect_network.get_windows_network_category(
                iface.name if iface else None
            )
            if category != "Public":
                # Mirrored active and the network not Public: the Windows side
                # looks right. Client isolation on the router is another matter
                # entirely — it is warned about anyway, because from here there
                # is no way to check it.
                self.context.warn(
                    "LAN access also depends on the router not isolating clients from "
                    "each other (AP/client isolation). If Mattermost will not load "
                    "from another device despite the Windows configuration, check that "
                    "option in the router's admin panel."
                )
                self._warn_about_mirrored_tcp_stalls()
                return
            message = (
                f'This machine\'s network is categorised "Public" on Windows '
                f"({iface.name if iface else 'primary interface'}), which blocks "
                "inbound traffic by default — including mirrored networking's, even "
                "when that is active. Mattermost and the WireGuard panel will probably "
                "not answer from other devices on the LAN."
            )
            steps_hint = (
                'reclassify the network as "Private": '
                "Set-NetConnectionProfile -NetworkCategory Private"
            )

        self.context.warn(message)
        console.print(f"[axion.warn]{message}[/]")
        console.print(f"[axion.dim]Before deploying: {steps_hint}.[/]")

    def _warn_about_mirrored_tcp_stalls(self) -> None:
        """Mirrored networking fixes LAN access and breaks something else.

        `networkingMode=mirrored` is what makes the stack reachable from a
        phone, but it drags in a known and still-open Docker/WSL2 bug
        (moby/moby#48201): long-lived TCP connections stall. Mattermost's
        WebSocket is exactly that — an idle connection waiting for a message
        to arrive — so the symptom is not an error; it is that new messages
        (the AI's answer included) only appear on reloading the page.

        It is warned about here because from the outside it is
        indistinguishable from "the AI does not work", and because the correct
        diagnosis is one command away: `axion-wizard doctor` now performs the
        WebSocket handshake for real.
        """
        message = (
            "Mirrored networking is active (it is what gives LAN access), but it drags "
            "in an open WSL2/Docker bug affecting long TCP connections "
            "(moby/moby#48201). If messages — the AI's answer included — only appear "
            "after pressing F5, this is why: Mattermost's WebSocket stalls. The "
            "alternative is going back to NAT plus `netsh portproxy`."
        )
        self.context.warn(message)
        console.print(f"[axion.dim]{message}[/]")
        console.print(
            "[axion.dim]To confirm it without changing anything: `axion-wizard doctor` "
            "— the `Mattermost WebSocket` row tells it apart from a configuration "
            "problem.[/]"
        )

    # --- presentation -----------------------------------------------------------------

    @staticmethod
    def _render_table(facts: EnvironmentFacts) -> Table:
        table = ui.make_table("Detected environment")
        table.add_column("Item", style="axion.label")
        table.add_column("Value", overflow="fold")

        table.add_row("Operating system", f"{facts.os_info.name} {facts.os_info.release}")
        if facts.wsl.inside_wsl:
            distro = facts.wsl.distro_name or "?"
            version = facts.wsl.version or "?"
            table.add_row("WSL", f"{distro} (WSL{version})")
            mirrored = "[axion.ok]yes[/]" if facts.wsl.mirrored_configured else "[axion.dim]no[/]"
            table.add_row("Mirrored networking", mirrored)
        table.add_row("Docker", facts.docker.docker_version or "[axion.error]not detected[/]")
        table.add_row("Compose", facts.docker.compose_version or "[axion.error]not detected[/]")
        table.add_row("Docker context", facts.docker.context.active_context or "default")
        table.add_row("RAM", f"{facts.hardware.ram_total_gb:.1f} GB")
        table.add_row("CPU", f"{facts.hardware.cpu_logical} logical cores")
        gpus = ", ".join(g.name or g.vendor for g in facts.hardware.gpus)
        if not gpus:
            gpu_value = "[axion.dim]no dedicated GPU[/]"
        elif facts.gpu_acceleration == detect_docker.GPU_ACCELERATION_NVIDIA:
            gpu_value = f"{gpus} [axion.ok]({ui.GLYPH_OK} CUDA via Docker)[/]"
        elif facts.gpu_acceleration == detect_docker.GPU_ACCELERATION_ROCM:
            gpu_value = f"{gpus} [axion.ok]({ui.GLYPH_OK} ROCm via Docker)[/]"
        else:
            gpu_value = (
                f"{gpus} [axion.warn]({ui.GLYPH_WARN} no passthrough, Ollama will use CPU)[/]"
            )
        table.add_row("GPU", gpu_value)
        table.add_row("WireGuard variant", f"[axion.info]{facts.wireguard_variant}[/]")
        return table
