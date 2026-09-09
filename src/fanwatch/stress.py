"""Exercise the fans: load every CPU core, then the GPU, then both, sampling the sensors
once a second, and report how each fan answered. Stops the load at once if anything gets
too hot or the CPU starts throttling. `fanstress --help` for the phases and limits."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from fanwatch.probe import Collector, Fan, Snapshot
from fanwatch.text import sanitize

if TYPE_CHECKING:
    from fanwatch.load import Load

PHASES: dict[str, str] = {
    "idle": "baseline, no load",
    "cpu": "every core busy",
    "gpu": "GPU compute kernel",
    "both": "CPU and GPU together",
    "recovery": "no load, watch the fans wind down",
}
DEFAULT_SECONDS = 60
MAX_SECONDS = 1800
DEFAULT_MAX_CPU_C = 90.0
DEFAULT_MAX_GPU_C = 85.0
MIN_LIMIT_C, MAX_LIMIT_C = 40.0, 110.0
STALL_SAMPLES = 3  # consecutive zero-RPM readings under positive duty that count as a stall
RPM_RISE = 0.10  # a fan "responded" if its RPM rose this fraction, or its duty by 2 points
DUTY_RISE = 2.0


@dataclass(frozen=True, slots=True)
class FanSample:
    """One fan at one instant."""

    label: str
    rpm: int | None
    duty: float | None


@dataclass(frozen=True, slots=True)
class Sample:
    """The readings that matter for the stress test, taken from one dashboard snapshot."""

    time: float
    phase: str
    cpu_c: float | None
    gpu_c: float | None
    gpu_w: float | None
    throttle_events: int | None
    intake_duty: float | None
    fans: dict[str, FanSample] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Limits:
    """Abort thresholds. The load stops the moment either is reached."""

    max_cpu_c: float
    max_gpu_c: float


class FanVerdict(StrEnum):
    """How a fan behaved during a phase."""

    RESPONDED = "responded"
    FLAT = "flat"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class FanSummary:
    """One fan over one phase."""

    label: str
    rpm_start: int | None
    rpm_peak: int | None
    duty_start: float | None
    duty_peak: float | None
    responded_after_s: float | None
    verdict: FanVerdict


@dataclass(slots=True)
class PhaseSummary:
    """One phase: temperatures, GPU power and every fan."""

    phase: str
    seconds: float
    cpu_start_c: float | None
    cpu_peak_c: float | None
    cpu_end_c: float | None
    gpu_start_c: float | None
    gpu_peak_c: float | None
    gpu_end_c: float | None
    gpu_peak_w: float | None
    fans: dict[str, FanSummary] = field(default_factory=dict)


def _package_c(snapshot: Snapshot) -> float | None:
    """The hottest CPU package, or the hottest core if there is no package sensor."""
    packages: list[float] = []
    cores: list[float] = []
    for t in snapshot.temperatures:
        if t.chip != "coretemp" or t.celsius is None or not math.isfinite(t.celsius):
            continue
        (packages if t.label.startswith("Package") else cores).append(t.celsius)
    if packages:
        return max(packages)
    return max(cores) if cores else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        as_float = float(value)
    except OverflowError:
        return None
    return as_float if math.isfinite(as_float) else None


def _fan_label(fan: Fan) -> str:
    """The dashboard name, or chip and channel for an unlabelled header."""
    return f"{fan.chip} {fan.channel}" if fan.name == fan.channel else fan.name


def sample_from(snapshot: Snapshot, phase: str, *, now: float | None = None) -> Sample:
    """Reduce a snapshot to the numbers the report needs. `now` is the sample's time on a
    monotonic clock; the snapshot's wall-clock stamp is used when it is not given."""
    gpu = snapshot.gpu
    return Sample(
        time=snapshot.timestamp if now is None else now,
        phase=phase,
        cpu_c=_package_c(snapshot),
        gpu_c=_number(None if gpu is None else gpu.temp_c),
        gpu_w=None if gpu is None else gpu.power_w,
        throttle_events=None if snapshot.throttle is None else snapshot.throttle.package_events,
        intake_duty=None
        if snapshot.controller is None
        else _number(snapshot.controller.get("duty_pct")),
        fans={
            f.key: FanSample(_fan_label(f), f.rpm, f.duty)
            for f in snapshot.fans
            if f.status != "EMPTY"
        },
    )


def guard(sample: Sample, limits: Limits, *, throttle_baseline: int | None) -> str | None:
    """The reason to stop the load now, or None. A missing reading counts as a reason
    whenever the load it guards is running; `throttle_baseline` is the counter at start,
    or None if the kernel does not expose one, in which case throttling is not guarded."""
    if sample.cpu_c is None or not math.isfinite(sample.cpu_c):
        return "CPU temperature unreadable"
    if sample.cpu_c >= limits.max_cpu_c:
        return f"CPU package {sample.cpu_c:.1f} °C reached the {limits.max_cpu_c:.0f} °C limit"
    gpu_loaded = sample.phase in ("gpu", "both")
    if sample.gpu_c is None or not math.isfinite(sample.gpu_c):
        if gpu_loaded:
            return "GPU temperature unreadable while the GPU is loaded"
    elif sample.gpu_c >= limits.max_gpu_c:
        return f"GPU {sample.gpu_c:.0f} °C reached the {limits.max_gpu_c:.0f} °C limit"
    if throttle_baseline is not None:
        if sample.throttle_events is None:
            return "CPU throttle counter unreadable"
        if sample.throttle_events > throttle_baseline:
            return "CPU started thermal throttling"
    return None


def _first(values: Sequence[float | None]) -> float | None:
    return next((v for v in values if v is not None), None)


def _last(values: Sequence[float | None]) -> float | None:
    return _first(list(reversed(values)))


def _peak(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _fan_summary(samples: Sequence[Sample], key: str) -> FanSummary:
    readings = [s.fans[key] for s in samples if key in s.fans]
    label = readings[0].label
    rpms = [r.rpm for r in readings]
    duties = [r.duty for r in readings]
    rpm_start, rpm_peak = _first(rpms), _peak(rpms)
    duty_start, duty_peak = _first(duties), _peak(duties)
    responded_after: float | None = None
    start_time = samples[0].time
    for s in samples:
        r = s.fans.get(key)
        if r is None:
            continue
        rose_rpm = (
            rpm_start is not None and r.rpm is not None and r.rpm >= rpm_start * (1 + RPM_RISE) + 1
        )
        rose_duty = (
            duty_start is not None and r.duty is not None and r.duty >= duty_start + DUTY_RISE
        )
        if rose_rpm or rose_duty:
            responded_after = s.time - start_time
            break
    if rpm_peak is None and duty_peak is None:
        verdict = FanVerdict.UNKNOWN
    elif _stalled(readings):
        verdict = FanVerdict.STOPPED
    elif responded_after is not None:
        verdict = FanVerdict.RESPONDED
    else:
        verdict = FanVerdict.FLAT
    return FanSummary(
        label,
        int(rpm_start) if rpm_start is not None else None,
        int(rpm_peak) if rpm_peak is not None else None,
        duty_start,
        duty_peak,
        responded_after,
        verdict,
    )


def _stalled(readings: Sequence[FanSample]) -> bool:
    """Zero RPM for the last STALL_SAMPLES readings (or all of them) while commanded on."""
    tail = list(readings)[-STALL_SAMPLES:]
    return bool(tail) and all(r.rpm == 0 and r.duty is not None and r.duty > 0 for r in tail)


def summarise(samples: Sequence[Sample]) -> list[PhaseSummary]:
    """One summary per phase, in the order the phases ran."""
    out: list[PhaseSummary] = []
    i = 0
    while i < len(samples):
        phase = samples[i].phase
        j = i
        while j < len(samples) and samples[j].phase == phase:
            j += 1
        chunk = samples[i:j]
        keys: list[str] = []
        for s in chunk:
            keys.extend(k for k in s.fans if k not in keys)
        cpus = [s.cpu_c for s in chunk]
        gpus = [s.gpu_c for s in chunk]
        out.append(
            PhaseSummary(
                phase=phase,
                seconds=len(chunk) if len(chunk) < 2 else chunk[-1].time - chunk[0].time + 1,
                cpu_start_c=_first(cpus),
                cpu_peak_c=_peak(cpus),
                cpu_end_c=_last(cpus),
                gpu_start_c=_first(gpus),
                gpu_peak_c=_peak(gpus),
                gpu_end_c=_last(gpus),
                gpu_peak_w=_peak([s.gpu_w for s in chunk]),
                fans={k: _fan_summary(chunk, k) for k in keys},
            )
        )
        i = j
    return out


def _temp(start: float | None, peak: float | None, end: float | None = None) -> str:
    if start is None or peak is None:
        return "—"
    text = f"{start:.0f}→{peak:.0f} °C"
    if end is not None and round(end) != round(peak):
        text += f", ends {end:.0f}"
    return text


def render(summaries: Sequence[PhaseSummary], *, aborted: str | None, gpu_load: str | None) -> str:
    """The plain-text report."""
    lines = ["FAN STRESS REPORT"]
    lines.append(f"GPU load: {gpu_load}" if gpu_load else "GPU load: not used")
    width = max((len(f.label) for p in summaries for f in p.fans.values()), default=10)
    for p in summaries:
        gpu = _temp(p.gpu_start_c, p.gpu_peak_c, p.gpu_end_c)
        if p.gpu_peak_w is not None:
            gpu += f" (peak {p.gpu_peak_w:.0f} W)"
        lines.append("")
        cpu = _temp(p.cpu_start_c, p.cpu_peak_c, p.cpu_end_c)
        lines.append(f"{p.phase:<9}{p.seconds:>5.0f} s   CPU {cpu}   GPU {gpu}")
        for f in p.fans.values():
            rpm = (
                "—"
                if f.rpm_start is None or f.rpm_peak is None
                else f"{f.rpm_start}→{f.rpm_peak} rpm"
            )
            duty = (
                "—"
                if f.duty_start is None or f.duty_peak is None
                else f"{f.duty_start:.0f}→{f.duty_peak:.0f} %"
            )
            verdict = f.verdict.value
            if f.verdict is FanVerdict.RESPONDED and f.responded_after_s is not None:
                verdict += f" after {f.responded_after_s:.0f} s"
            lines.append(f"  {f.label:<{width}}  {rpm:>16}  {duty:>10}  {verdict}")
    if aborted:
        lines.append("")
        lines.append(f"stopped early: {aborted}")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The `fanstress` command line."""
    parser = argparse.ArgumentParser(prog="fanstress", description=__doc__)
    parser.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_SECONDS,
        metavar="N",
        help=f"length of each phase (default {DEFAULT_SECONDS}, max {MAX_SECONDS})",
    )
    parser.add_argument(
        "--phases",
        default=",".join(PHASES),
        help="comma-separated subset of " + ", ".join(f"{k} ({v})" for k, v in PHASES.items()),
    )
    parser.add_argument(
        "--max-cpu-c",
        type=float,
        default=DEFAULT_MAX_CPU_C,
        metavar="C",
        help=f"stop when the CPU package reaches this (default {DEFAULT_MAX_CPU_C:.0f})",
    )
    parser.add_argument(
        "--max-gpu-c",
        type=float,
        default=None,
        metavar="C",
        help=f"default: the GPU's slowdown threshold minus 5, else {DEFAULT_MAX_GPU_C:.0f}",
    )
    parser.add_argument("--workers", type=int, default=os.cpu_count(), help="CPU load processes")
    parser.add_argument("--json", action="store_true", help="print the summaries as JSON")
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 1 <= args.seconds <= MAX_SECONDS:
        parser.error(f"--seconds must be between 1 and {MAX_SECONDS}")
    for name in ("max_cpu_c", "max_gpu_c"):
        value = getattr(args, name)
        if value is not None and not (math.isfinite(value) and MIN_LIMIT_C <= value <= MAX_LIMIT_C):
            parser.error(
                f"--{name.replace('_', '-')} must be between {MIN_LIMIT_C} and {MAX_LIMIT_C}"
            )
    args.phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    unknown = [sanitize(p)[:20] for p in args.phases if p not in PHASES]
    if unknown or not args.phases:
        parser.error("unknown phase(s): " + ", ".join(unknown or ["none given"]))
    if args.workers is None or args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


def _live_line(s: Sample) -> str:
    cpu = "?" if s.cpu_c is None else f"{s.cpu_c:.0f}"
    gpu = "?" if s.gpu_c is None else f"{s.gpu_c:.0f}"
    fans = "  ".join(
        f"{f.label.split(' (')[0]} {'?' if f.rpm is None else f.rpm}rpm"
        + ("" if f.duty is None else f"/{f.duty:.0f}%")
        for f in s.fans.values()
    )
    stamp = time.strftime("%H:%M:%S", time.localtime(s.time))
    return f"{stamp} {s.phase:<9}cpu {cpu}°C  gpu {gpu}°C  {fans}"


def run(args: argparse.Namespace) -> int:
    """Run the phases, printing one line per second, then the report."""
    from fanwatch.load import CpuLoad, OpenClLoad, gpu_load

    with Collector() as fresh:

        def sample(phase: str) -> Sample:
            return sample_from(fresh(), phase, now=time.monotonic())

        first = sample("idle")
        snapshot = fresh()
        max_gpu = args.max_gpu_c
        if max_gpu is None:
            slowdown = None if snapshot.gpu is None else snapshot.gpu.slowdown_c
            max_gpu = DEFAULT_MAX_GPU_C if slowdown is None else max(MIN_LIMIT_C, slowdown - 5)
        limits = Limits(args.max_cpu_c, max_gpu)
        throttle_baseline = first.throttle_events
        if throttle_baseline is None:
            print("no CPU throttle counter in sysfs: throttling is not guarded", file=sys.stderr)
        print(
            f"limits: CPU {limits.max_cpu_c:.0f} °C, GPU {limits.max_gpu_c:.0f} °C, "
            f"throttle counter at {throttle_baseline}; Ctrl-C stops the load",
            file=sys.stderr,
        )
        preflight = guard(first, limits, throttle_baseline=throttle_baseline)
        if preflight:
            print(f"not starting: {preflight}", file=sys.stderr)
            return 1
        # The GPU load is created only for the phases that use it and released right after:
        # an open OpenCL context keeps the card in its high-power state (about 100 W on this
        # one), which would ruin the idle and recovery phases.
        gpu: OpenClLoad | None = None
        gpu_desc: str | None = None
        gpu_missing: str | None = None
        cpu = CpuLoad(args.workers)
        samples: list[Sample] = []
        aborted: str | None = None
        active: list[Load] = []
        try:
            for phase in args.phases:
                if phase in ("gpu", "both") and gpu_missing:
                    continue
                aborted = guard(sample(phase), limits, throttle_baseline=throttle_baseline)
                if aborted:
                    break
                active = []
                if phase in ("cpu", "both"):
                    active.append(cpu)
                if phase in ("gpu", "both"):
                    gpu, reason = gpu_load()  # runs one short calibration kernel
                    if gpu is None:
                        gpu_missing = reason
                        gpu_desc = gpu_desc or f"unavailable ({reason}), GPU phases skipped"
                        print(
                            f"GPU load unavailable: {reason}; skipping GPU phases", file=sys.stderr
                        )
                        continue
                    gpu_desc = gpu_desc or reason
                    active.append(gpu)
                for load in active:
                    load.start()
                end = time.monotonic() + args.seconds
                while time.monotonic() < end:
                    tick = time.monotonic()
                    s = sample(phase)
                    samples.append(s)
                    # Safety first: decide and stop before anything that could block on output.
                    aborted = guard(s, limits, throttle_baseline=throttle_baseline)
                    if aborted is None:
                        aborted = next((f for f in (load.failure() for load in active) if f), None)
                    if aborted:
                        for load in active:
                            load.stop()
                        active = []
                    print(_live_line(s), flush=True)
                    if aborted:
                        break
                    time.sleep(max(0.0, 1.0 - (time.monotonic() - tick)))
                for load in active:
                    load.stop()
                active = []
                if gpu is not None:
                    gpu.release()
                    gpu = None
                if aborted:
                    break
        except KeyboardInterrupt:
            aborted = "interrupted"
        finally:
            for load in active:
                load.stop()
            if gpu is not None:
                gpu.release()
    summaries = summarise(samples)
    if args.json:
        payload = {
            "aborted": aborted,
            "gpu_load": gpu_desc,
            "phases": [asdict(p) for p in summaries],
        }
        print(json.dumps(payload, indent=2))
    else:
        print()
        print(render(summaries, aborted=aborted, gpu_load=gpu_desc))
    return 1 if aborted and aborted != "interrupted" else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
