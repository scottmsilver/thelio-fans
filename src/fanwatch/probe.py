"""Read-only acquisition of fan, temperature and Io-board state from Linux sysfs."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from fanwatch.gpu import Gpu, NvmlSession, read_gpu
from fanwatch.state import read_state
from fanwatch.text import sanitize

IO_USB_ID: Final = ("1209", "1776")
IO_DRIVER: Final = "system76-io"

# Display names and notes for channels whose role is known on this machine.
# Keyed by (hwmon chip name, channel label or channel name).
KNOWN_CHANNELS: Final[dict[tuple[str, str], tuple[str, str | None]]] = {
    ("system76_io", "CPUF"): (
        "CPU FAN via Io",
        "no fan; CPU fans run from the motherboard header",
    ),
    ("system76_io", "INTF"): ("INTAKE FAN", None),
    # IT8689E fan1 is the CPU_FAN header on Gigabyte boards; not verified from software.
    ("it8689", "fan1"): ("CPU FAN (motherboard header)", None),
}
# Channels that are expected to read zero and should not raise an alert.
KNOWN_EMPTY: Final = frozenset({("system76_io", "CPUF")})
# Fans that stop at idle by design; zero RPM is STOPPED but not an alert.
IDLE_STOP_CHIPS: Final = frozenset({"nvml"})
GPU_CHIP: Final = "gpu"
CONTROLLER_STATE: Final = Path("/run/fanctl/state.json")  # written by the fanctl service

Status = str  # ROTATING, IDLE, STOPPED, EMPTY, UNKNOWN, FAULT, ALARM
STALL_SAMPLES = 8  # consecutive zero-RPM samples while commanded on before calling it a stall


@dataclass(frozen=True, slots=True)
class Fan:
    """One fan channel. `rpm` and `duty` are None when unreadable; `status` is one of the
    module constants (ROTATING, IDLE, STOPPED, EMPTY, UNKNOWN, FAULT, ALARM)."""

    key: str
    chip: str
    channel: str
    label: str | None
    name: str
    rpm: int | None
    duty: float | None
    status: Status
    error: str | None
    note: str | None
    path: str


@dataclass(frozen=True, slots=True)
class Temperature:
    """One temperature sensor with its driver-reported limits, in °C."""

    chip: str
    label: str
    celsius: float | None
    critical: float | None
    maximum: float | None
    error: str | None
    key: str


@dataclass(frozen=True, slots=True)
class Controller:
    """The Thelio Io board as seen on USB: port, device number and bound drivers."""

    port: str
    product: str
    usb_id: str
    devnum: int | None
    interfaces: dict[str, str | None]


@dataclass(frozen=True, slots=True)
class Throttle:
    """CPU thermal-throttle counters since boot, from the kernel's thermal_throttle sysfs."""

    package_events: int
    core_events: int
    total_ms: int
    longest_ms: int
    cur_khz: int | None
    max_khz: int | None


@dataclass(slots=True)
class Snapshot:
    """Everything read in one pass over sysfs plus the GPU and fanctl state. Pure data; the
    dashboard, CLI and log modes all render from it."""

    timestamp: float
    machine: str
    fans: list[Fan] = field(default_factory=list)
    temperatures: list[Temperature] = field(default_factory=list)
    controllers: list[Controller] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    throttle: Throttle | None = None
    gpu: Gpu | None = None
    controller: dict[str, object] | None = None


MAX_INT = 10**9  # sysfs integers are kernel-formatted; anything beyond this is garbage


def read_text(path: Path) -> str | None:
    """A sysfs attribute as sanitised text, or None if unreadable."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return sanitize(raw.decode("utf-8", errors="replace").strip())


def read_int(path: Path, *, nonnegative: bool = False) -> tuple[int | None, str | None]:
    """A sysfs integer attribute as (value, None) or (None, error text)."""
    try:
        value = int(path.read_text().strip())
    except (OSError, ValueError) as exc:
        return None, f"{path.name}: {exc}"
    if nonnegative and value < 0:
        return None, f"{path.name}: negative reading"
    if abs(value) > MAX_INT:
        return None, f"{path.name}: out of range"
    return value, None


def describe(chip: str, channel: str, label: str | None) -> tuple[str, str | None]:
    """Return a display name and optional note for a fan channel."""
    for key in ((chip, label or ""), (chip, channel)):
        if key in KNOWN_CHANNELS:
            return KNOWN_CHANNELS[key]
    return (label or channel, None)


def fan_status(
    *,
    rpm: int | None,
    fault: int | None,
    alarm: int | None,
    known_empty: bool,
    labelled: bool,
    duty: float | None = None,
) -> Status:
    """Classify a channel. Zero RPM means IDLE when the controller commands 0 % duty,
    STOPPED when it commands more than that, EMPTY when nothing is expected there."""
    if fault:
        return "FAULT"
    if alarm:
        return "ALARM"
    if rpm is None:
        return "UNKNOWN"
    if rpm > 0:
        return "ROTATING"
    if known_empty or not labelled:
        return "EMPTY"
    return "IDLE" if duty == 0 else "STOPPED"


def _fan_indices(hw: Path) -> list[str]:
    try:
        names = [p.name for p in hw.iterdir()]
    except OSError:
        return []
    found = {m[1] for n in names if (m := re.fullmatch(r"fan(\d+)_(input|label)", n))}
    return sorted(found, key=int)


def _collect_fans(hw: Path, chip: str, snapshot: Snapshot) -> None:
    device = (hw / "device").resolve() if (hw / "device").exists() else hw.resolve().parent
    for i in _fan_indices(hw):
        channel = f"fan{i}"
        rpm, error = read_int(hw / f"{channel}_input", nonnegative=True)
        pwm, _ = read_int(hw / f"pwm{i}", nonnegative=True)
        fault, _ = read_int(hw / f"{channel}_fault")
        alarm, _ = read_int(hw / f"{channel}_alarm")
        label = read_text(hw / f"{channel}_label")
        name, note = describe(chip, channel, label)
        known_empty = (chip, label or channel) in KNOWN_EMPTY
        # A channel with a label, or one whose role is known, is expected to carry a fan.
        expected = label is not None or (chip, channel) in KNOWN_CHANNELS
        duty = pwm / 255 * 100 if pwm is not None and pwm <= 255 else None
        status = fan_status(
            rpm=rpm, fault=fault, alarm=alarm, known_empty=known_empty, labelled=expected, duty=duty
        )
        snapshot.fans.append(
            Fan(
                key=f"{device}:{chip}:{i}",
                chip=chip,
                channel=channel,
                label=label,
                name=name,
                rpm=rpm,
                duty=duty,
                status=status,
                error=error,
                note=note,
                path=str(hw / f"{channel}_input"),
            )
        )
        # STOPPED is not alerted here: a fan takes seconds to spin up after being commanded on,
        # so the dashboard judges stalls over several samples (see dashboard.alerts_for).
        if status in ("UNKNOWN", "FAULT", "ALARM") and chip not in IDLE_STOP_CHIPS:
            detail = {"UNKNOWN": error or "reading unavailable"}.get(
                status, "hardware flag asserted"
            )
            snapshot.alerts.append(f"{label or channel} on {chip}: {status.lower()} — {detail}")


def _collect_temperatures(hw: Path, chip: str, snapshot: Snapshot) -> None:
    for p in sorted(hw.glob("temp*_input")):
        prefix = p.name.removesuffix("_input")
        value, error = read_int(p)
        critical, _ = read_int(hw / f"{prefix}_crit")
        maximum, _ = read_int(hw / f"{prefix}_max")
        label = read_text(hw / f"{prefix}_label") or prefix
        snapshot.temperatures.append(
            Temperature(
                chip=chip,
                label=label,
                celsius=value / 1000 if value is not None else None,
                critical=critical / 1000 if critical is not None else None,
                maximum=maximum / 1000 if maximum is not None else None,
                error=error,
                key=f"{chip}:{label}",
            )
        )
        if value is None:
            continue
        if critical is not None and value >= critical:
            snapshot.alerts.append(f"{chip} / {label}: at or above critical temperature")
        elif maximum is not None and value >= maximum:
            snapshot.alerts.append(f"{chip} / {label}: at or above reported maximum")


def _collect_controllers(root: Path, snapshot: Snapshot) -> None:
    for usb in sorted((root / "bus/usb/devices").glob("*")):
        vid, pid = read_text(usb / "idVendor"), read_text(usb / "idProduct")
        if (vid, pid) != IO_USB_ID:
            continue
        interfaces: dict[str, str | None] = {}
        for interface in sorted(usb.glob(f"{usb.name}:*")):
            driver = interface / "driver"
            bound = driver.is_symlink() and driver.exists()
            interfaces[interface.name] = driver.resolve().name if bound else None
        devnum, _ = read_int(usb / "devnum")
        snapshot.controllers.append(
            Controller(
                port=usb.name,
                product=read_text(usb / "product") or "System76 Io",
                usb_id=f"{vid}:{pid}",
                devnum=devnum,
                interfaces=interfaces,
            )
        )
        if not interfaces or any(d != IO_DRIVER for d in interfaces.values()):
            snapshot.alerts.append(f"USB {usb.name}: expected {IO_DRIVER} driver missing")


def _collect_throttle(root: Path) -> Throttle | None:
    cpus = sorted((root / "devices/system/cpu").glob("cpu[0-9]*"))
    dirs = [c / "thermal_throttle" for c in cpus if (c / "thermal_throttle").is_dir()]
    if not dirs:
        return None

    def counts(name: str) -> list[int]:
        return [v for d in dirs if (v := read_int(d / name)[0]) is not None]

    # Package counters repeat on every CPU of the package; core counters are per core.
    freq = root / "devices/system/cpu/cpu0/cpufreq"
    return Throttle(
        package_events=max(counts("package_throttle_count"), default=0),
        core_events=sum(counts("core_throttle_count")),
        total_ms=max(counts("package_throttle_total_time_ms"), default=0),
        longest_ms=max(counts("package_throttle_max_time_ms"), default=0),
        cur_khz=read_int(freq / "scaling_cur_freq")[0],
        max_khz=read_int(freq / "cpuinfo_max_freq")[0],
    )


def _add_gpu(gpu: Gpu, snapshot: Snapshot) -> None:
    snapshot.gpu = gpu
    for index, (percent, rpm) in enumerate(gpu.fans):
        channel = f"fan{index}"
        # NVML fan values are targets, not measured rotation; without an RPM we do not guess.
        status = fan_status(rpm=rpm, fault=None, alarm=None, known_empty=False, labelled=True)
        snapshot.fans.append(
            Fan(
                key=f"nvml:{gpu.name}:{index}",
                chip="nvml",
                channel=channel,
                label=f"GPU{index}",
                name="GPU FAN" if len(gpu.fans) == 1 else f"GPU FAN {index + 1}",
                rpm=rpm,
                duty=None if percent is None else float(percent),
                status=status,
                error=None,
                note=None,
                path=f"nvml:{channel}",
            )
        )
    snapshot.temperatures.append(
        Temperature(
            chip=GPU_CHIP,
            label=gpu.name,
            celsius=gpu.temp_c,
            critical=gpu.shutdown_c,
            maximum=gpu.slowdown_c,
            error=None,
            key=f"{GPU_CHIP}:{gpu.name}",
        )
    )
    if gpu.temp_c is not None and gpu.slowdown_c is not None and gpu.temp_c >= gpu.slowdown_c:
        snapshot.alerts.append(f"GPU {gpu.name}: at or above slowdown temperature")
    if gpu.reasons:
        snapshot.alerts.append(f"GPU {gpu.name}: throttling ({', '.join(gpu.reasons)})")


GpuReader = Callable[[], Gpu | None]


def collect(root: Path | str = "/sys", gpu_reader: GpuReader | None = None) -> Snapshot:
    """Take one read-only snapshot of every hwmon fan and temperature, the Io board, and GPU."""
    root = Path(root)
    machine = read_text(root / "class/dmi/id/product_name") or "Linux device"
    snapshot = Snapshot(time.time(), machine)
    for hw in sorted((root / "class/hwmon").glob("hwmon*")):
        chip = read_text(hw / "name") or hw.name
        _collect_fans(hw, chip, snapshot)
        _collect_temperatures(hw, chip, snapshot)
    _collect_controllers(root, snapshot)
    snapshot.throttle = _collect_throttle(root)
    gpu = (gpu_reader or read_gpu)()
    if gpu is not None:
        _add_gpu(gpu, snapshot)
    snapshot.controller = read_state(CONTROLLER_STATE)
    if not snapshot.controllers:
        snapshot.alerts.append("System76 Io USB controller (1209:1776) not detected")
    if not snapshot.fans:
        snapshot.alerts.append("No fan sensors available; rotation cannot be verified")
    elif snapshot.controllers and not any(f.chip == "system76_io" for f in snapshot.fans):
        snapshot.alerts.append("System76 controller detected but its fan readings are unavailable")
    return snapshot


class Collector:
    """Repeated snapshots that share one NVML session.

    `collect()` on its own initialises and shuts NVML down on every call, which is fine
    for a one-shot read. Every polling loop (dashboard, log mode, fanstress) goes through
    this instead, so the GPU is opened once and closed when the loop ends.
    """

    def __init__(self, root: Path | str = "/sys", session: NvmlSession | None = None) -> None:
        self.root = Path(root)
        self.session = NvmlSession() if session is None else session

    def __enter__(self) -> Collector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __call__(self) -> Snapshot:
        """One snapshot through the shared session."""
        return collect(self.root, gpu_reader=self.session.read)

    def close(self) -> None:
        """Shut the NVML session down; safe to call repeatedly."""
        self.session.close()
