"""GPU telemetry through NVML, the library that ships with the NVIDIA driver."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from fanwatch.text import sanitize

try:
    import pynvml as _pynvml
except ImportError:  # pragma: no cover - the package is a declared dependency
    _pynvml = None

FanReading = tuple[int | None, int | None]  # (duty percent, rpm)

# Clock-event reasons that mean the GPU is being held back, in display order.
THROTTLE_REASONS = (
    ("nvmlClocksEventReasonHwThermalSlowdown", "hw thermal"),
    ("nvmlClocksEventReasonSwThermalSlowdown", "thermal"),
    ("nvmlClocksEventReasonSwPowerCap", "power cap"),
    ("nvmlClocksEventReasonHwPowerBrakeSlowdown", "power brake"),
    ("nvmlClocksEventReasonHwSlowdown", "hw slowdown"),
)


@dataclass(frozen=True, slots=True)
class Gpu:
    """One reading of the first NVIDIA GPU: temperature, thresholds, fans, load, power,
    clocks, memory and any active throttle reasons. Missing values are None."""

    name: str
    temp_c: int | None
    slowdown_c: int | None
    shutdown_c: int | None
    fans: tuple[FanReading, ...]
    util_pct: int | None
    power_w: float | None
    power_limit_w: float | None
    clock_mhz: int | None
    clock_max_mhz: int | None
    mem_clock_mhz: int | None
    mem_used_mb: int | None
    mem_total_mb: int | None
    pstate: int | None
    reasons: tuple[str, ...] | None  # None when the clock-event query is unavailable


def _short_name(name: str) -> str:
    for prefix in ("NVIDIA GeForce ", "NVIDIA "):
        name = name.removeprefix(prefix)
    return name


def read_gpu(nvml: Any = _pynvml) -> Gpu | None:
    """Read the first GPU. Returns None when NVML or the driver is unavailable.

    Every call crosses into a native library, so any exception at that boundary is
    treated as "unavailable" rather than allowed to take the dashboard down.
    """
    if nvml is None:
        return None
    try:
        nvml.nvmlInit()
    except Exception:
        return None
    try:
        return _read(nvml, nvml.nvmlDeviceGetHandleByIndex(0))
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            nvml.nvmlShutdown()


class NvmlSession:
    """Keep NVML initialised across reads. Initialising it costs tens of milliseconds of
    system time, which matters for a loop that runs every second. Any error closes the
    session and the next read re-initialises, so a driver restart is survivable."""

    def __init__(self, nvml: Any = _pynvml) -> None:
        self.nvml = nvml
        self._handle: Any = None

    def __enter__(self) -> NvmlSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _open(self) -> bool:
        if self._handle is not None:
            return True
        if self.nvml is None:
            return False
        try:
            self.nvml.nvmlInit()
            self._handle = self.nvml.nvmlDeviceGetHandleByIndex(0)
            return True
        except Exception:
            self.close()
            return False

    def read(self) -> Gpu | None:
        """One reading, or None if NVML is unavailable; a failed reading closes the session
        so the next call re-initialises."""
        if not self._open():
            return None
        try:
            gpu = _read(self.nvml, self._handle)
        except Exception:
            self.close()
            return None
        if gpu.temp_c is None:
            # The one reading we exist for failed: assume the handle is stale (driver
            # restart, device reset) and start over next time.
            self.close()
            return None
        return gpu

    def close(self) -> None:
        """Shut NVML down; safe to call repeatedly."""
        if self.nvml is None:
            return
        had_handle = self._handle is not None
        self._handle = None
        if had_handle:
            with contextlib.suppress(Exception):
                self.nvml.nvmlShutdown()


def _read(nvml: Any, handle: Any) -> Gpu:
    def opt(name: str, *args: Any) -> Any:
        """Call an NVML getter by name; None if the binding lacks it or the call fails."""
        call = getattr(nvml, name, None)
        if call is None:
            return None
        try:
            return call(handle, *args)
        except Exception:
            return None

    def const(name: str) -> int:
        value = getattr(nvml, name, 0)
        return value if isinstance(value, int) else 0

    fans: list[FanReading] = []
    for index in range(opt("nvmlDeviceGetNumFans") or 0):
        rpm = opt("nvmlDeviceGetFanSpeedRPM") if index == 0 else None  # binding reads fan 0
        fans.append((opt("nvmlDeviceGetFanSpeed_v2", index), rpm))
    util = opt("nvmlDeviceGetUtilizationRates")
    memory = opt("nvmlDeviceGetMemoryInfo")
    power = opt("nvmlDeviceGetPowerUsage")
    limit = opt("nvmlDeviceGetEnforcedPowerLimit")
    mask = opt("nvmlDeviceGetCurrentClocksEventReasons")
    reasons = (
        None
        if mask is None
        else tuple(label for attr, label in THROTTLE_REASONS if mask & const(attr))
    )
    name = opt("nvmlDeviceGetName")
    return Gpu(
        name=sanitize(_short_name(str(name))) if name else "GPU",
        temp_c=opt("nvmlDeviceGetTemperature", const("NVML_TEMPERATURE_GPU")),
        slowdown_c=opt(
            "nvmlDeviceGetTemperatureThreshold", const("NVML_TEMPERATURE_THRESHOLD_SLOWDOWN")
        ),
        shutdown_c=opt(
            "nvmlDeviceGetTemperatureThreshold", const("NVML_TEMPERATURE_THRESHOLD_SHUTDOWN")
        ),
        fans=tuple(fans),
        util_pct=None if util is None else util.gpu,
        power_w=None if power is None else power / 1000,
        power_limit_w=None if limit is None else limit / 1000,
        clock_mhz=opt("nvmlDeviceGetClockInfo", const("NVML_CLOCK_GRAPHICS")),
        clock_max_mhz=opt("nvmlDeviceGetMaxClockInfo", const("NVML_CLOCK_GRAPHICS")),
        mem_clock_mhz=opt("nvmlDeviceGetClockInfo", const("NVML_CLOCK_MEM")),
        mem_used_mb=None if memory is None else memory.used // (1024 * 1024),
        mem_total_mb=None if memory is None else memory.total // (1024 * 1024),
        pstate=opt("nvmlDeviceGetPerformanceState"),
        reasons=reasons,
    )
