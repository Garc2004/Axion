from axion_wizard.detect import hardware as hw
from axion_wizard.utils.shell import CommandNotFoundError, CommandResult


def test_parse_nvidia_smi_csv() -> None:
    output = "NVIDIA GeForce RTX 4090, 24564\nNVIDIA GeForce RTX 3060, 12288\n"
    gpus = hw.parse_nvidia_smi_csv(output)
    assert len(gpus) == 2
    assert gpus[0].vendor == "nvidia"
    assert gpus[0].name == "NVIDIA GeForce RTX 4090"
    assert gpus[0].vram_mb == 24564


def test_parse_nvidia_smi_csv_ignores_malformed_lines() -> None:
    assert hw.parse_nvidia_smi_csv("garbage-line-without-comma\n") == []


def test_parse_rocm_smi_output() -> None:
    output = "GPU[0]\t\t: Card Series: Radeon RX 7900 XTX\n"
    gpus = hw.parse_rocm_smi_output(output)
    assert len(gpus) == 1
    assert gpus[0].vendor == "amd"


def test_classify_gpu_vendor() -> None:
    assert hw.classify_gpu_vendor("NVIDIA GeForce RTX 4090") == "nvidia"
    assert hw.classify_gpu_vendor("AMD Radeon RX 7900") == "amd"
    assert hw.classify_gpu_vendor("Intel(R) UHD Graphics 770") == "intel"
    assert hw.classify_gpu_vendor("Some Unknown Chip") == "unknown"


def test_parse_wmi_video_controllers() -> None:
    output = "NVIDIA GeForce RTX 4090\nIntel(R) UHD Graphics 770\n"
    gpus = hw.parse_wmi_video_controllers(output)
    assert [g.vendor for g in gpus] == ["nvidia", "intel"]


def test_detect_nvidia_gpus_not_installed(mocker) -> None:
    mocker.patch("axion_wizard.detect.hardware.run", side_effect=CommandNotFoundError("nvidia-smi"))
    assert hw.detect_nvidia_gpus() == []


def test_detect_nvidia_gpus_ok(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.hardware.run",
        return_value=CommandResult(
            args=[], returncode=0, stdout="NVIDIA GeForce RTX 4090, 24564\n", stderr=""
        ),
    )
    gpus = hw.detect_nvidia_gpus()
    assert len(gpus) == 1
    assert gpus[0].vram_mb == 24564


def test_detect_ram_bytes_positive() -> None:
    assert hw.detect_ram_bytes() > 0


def test_detect_cpu_counts_positive() -> None:
    logical, physical = hw.detect_cpu_counts()
    assert logical >= 1
    assert physical is None or physical >= 1


def test_hardware_info_ram_gb_and_has_gpu() -> None:
    info = hw.HardwareInfo(
        ram_total_bytes=16 * 1024**3,
        cpu_logical=8,
        cpu_physical=4,
        gpus=[hw.GpuInfo(vendor="nvidia", name="RTX 4090")],
    )
    assert info.ram_total_gb == 16.0
    assert info.has_gpu is True


def test_hardware_info_no_gpu() -> None:
    info = hw.HardwareInfo(ram_total_bytes=8 * 1024**3, cpu_logical=4, cpu_physical=2)
    assert info.has_gpu is False


def test_parse_nvidia_smi_csv_unparseable_memory_yields_none_vram() -> None:
    gpus = hw.parse_nvidia_smi_csv("NVIDIA GeForce RTX 4090, N/A\n")
    assert gpus[0].vram_mb is None


def test_detect_nvidia_gpus_nonzero_exit(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.hardware.run",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr="not found"),
    )
    assert hw.detect_nvidia_gpus() == []


def test_detect_rocm_gpus_not_installed(mocker) -> None:
    mocker.patch("axion_wizard.detect.hardware.run", side_effect=CommandNotFoundError("rocm-smi"))
    assert hw.detect_rocm_gpus() == []


def test_detect_rocm_gpus_nonzero_exit(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.hardware.run",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr=""),
    )
    assert hw.detect_rocm_gpus() == []


def test_detect_rocm_gpus_ok(mocker) -> None:
    mocker.patch(
        "axion_wizard.detect.hardware.run",
        return_value=CommandResult(
            args=[], returncode=0, stdout="GPU[0]\t\t: Card Series: Radeon RX 7900 XTX\n", stderr=""
        ),
    )
    gpus = hw.detect_rocm_gpus()
    assert gpus[0].vendor == "amd"


def test_detect_windows_gpus_skips_on_non_windows(mocker) -> None:
    mocker.patch("axion_wizard.detect.hardware._platform.system", return_value="Linux")
    assert hw.detect_windows_gpus() == []


def test_detect_windows_gpus_ok(mocker) -> None:
    mocker.patch("axion_wizard.detect.hardware._platform.system", return_value="Windows")
    mocker.patch(
        "axion_wizard.detect.hardware.run",
        return_value=CommandResult(
            args=[], returncode=0, stdout="NVIDIA GeForce RTX 4090\n", stderr=""
        ),
    )
    gpus = hw.detect_windows_gpus()
    assert gpus[0].vendor == "nvidia"


def test_detect_windows_gpus_command_missing(mocker) -> None:
    mocker.patch("axion_wizard.detect.hardware._platform.system", return_value="Windows")
    mocker.patch("axion_wizard.detect.hardware.run", side_effect=CommandNotFoundError("powershell"))
    assert hw.detect_windows_gpus() == []


def test_detect_windows_gpus_nonzero_exit(mocker) -> None:
    mocker.patch("axion_wizard.detect.hardware._platform.system", return_value="Windows")
    mocker.patch(
        "axion_wizard.detect.hardware.run",
        return_value=CommandResult(args=[], returncode=1, stdout="", stderr=""),
    )
    assert hw.detect_windows_gpus() == []


def test_detect_gpus_falls_back_to_windows_when_no_vendor_tools(mocker) -> None:
    mocker.patch("axion_wizard.detect.hardware.detect_nvidia_gpus", return_value=[])
    mocker.patch("axion_wizard.detect.hardware.detect_rocm_gpus", return_value=[])
    mocker.patch(
        "axion_wizard.detect.hardware.detect_windows_gpus",
        return_value=[hw.GpuInfo(vendor="intel", name="UHD 770")],
    )
    gpus = hw.detect_gpus()
    assert gpus[0].vendor == "intel"


def test_detect_hardware_end_to_end(mocker) -> None:
    mocker.patch("axion_wizard.detect.hardware.detect_gpus", return_value=[])
    info = hw.detect_hardware()
    assert info.ram_total_bytes > 0
    assert info.cpu_logical >= 1
