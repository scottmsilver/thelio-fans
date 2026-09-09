"""The stress tool's pure parts: sampling, the safety guard, phase summaries and verdicts."""

from __future__ import annotations

from fanwatch.gpu import Gpu
from fanwatch.probe import Fan, Snapshot, Temperature, Throttle
from fanwatch.stress import (
    PHASES,
    FanVerdict,
    Limits,
    Sample,
    guard,
    render,
    sample_from,
    summarise,
)


def fan(key: str, rpm: int | None, duty: float | None, name: str = "INTAKE FAN") -> Fan:
    return Fan(key, "system76_io", "fan2", None, name, rpm, duty, "ROTATING", None, None, "")


def snapshot(
    *,
    cpu: float | None = 50.0,
    gpu: int | None = 40,
    intake: int = 600,
    duty: float = 25.0,
    events: int = 0,
    t: float = 0.0,
) -> Snapshot:
    temps = [
        Temperature("coretemp", "Package id 0", cpu, 100.0, 80.0, None, "coretemp:Package id 0")
    ]
    g = (
        None
        if gpu is None
        else Gpu("RTX", gpu, 95, 98, (), None, 120.0, 350.0, None, None, None, None, None, None, ())
    )
    return Snapshot(
        t,
        "Box",
        fans=[fan("io:fan2", intake, duty), fan("it8689:fan1", 1000, 30.0, "CPU FAN")],
        temperatures=temps,
        throttle=Throttle(events, 0, 0, 0, None, None),
        gpu=g,
        controller={"duty_pct": duty, "reason": "steady"},
    )


def test_sample_from_snapshot_picks_package_gpu_and_every_fan() -> None:
    s = sample_from(snapshot(cpu=61.5, gpu=52, intake=800, duty=40.0, events=3, t=12.0), "cpu")
    assert s.phase == "cpu" and s.time == 12.0
    assert s.cpu_c == 61.5 and s.gpu_c == 52.0 and s.throttle_events == 3
    assert s.fans["io:fan2"].rpm == 800 and s.fans["io:fan2"].duty == 40.0
    assert s.fans["it8689:fan1"].label == "CPU FAN"
    assert s.intake_duty == 40.0


def test_guard_trips_on_cpu_gpu_or_throttle_and_not_otherwise() -> None:
    limits = Limits(max_cpu_c=90.0, max_gpu_c=85.0)
    ok = sample_from(snapshot(cpu=80.0, gpu=70), "cpu")
    assert guard(ok, limits, throttle_baseline=0) is None
    hot_cpu = sample_from(snapshot(cpu=90.0), "cpu")
    assert "CPU" in (guard(hot_cpu, limits, throttle_baseline=0) or "")
    hot_gpu = sample_from(snapshot(gpu=86), "gpu")
    assert "GPU" in (guard(hot_gpu, limits, throttle_baseline=0) or "")
    throttled = sample_from(snapshot(events=1), "cpu")
    assert "throttl" in (guard(throttled, limits, throttle_baseline=0) or "")
    assert guard(sample_from(snapshot(events=1), "cpu"), limits, throttle_baseline=1) is None


def test_guard_trips_when_the_cpu_reading_is_missing() -> None:
    lost = sample_from(snapshot(cpu=None), "cpu")
    assert guard(lost, Limits(90.0, 85.0), throttle_baseline=0) is not None


def test_summarise_reports_peaks_response_time_and_verdicts() -> None:
    samples = [
        sample_from(snapshot(cpu=45, gpu=40, intake=450, duty=25, t=t), "idle") for t in range(3)
    ]
    ramp = [
        (3, 50, 450, 25),
        (4, 62, 450, 25),
        (5, 70, 700, 40),
        (6, 78, 1000, 55),
        (7, 79, 1000, 55),
    ]
    samples += [
        sample_from(snapshot(cpu=c, gpu=41, intake=r, duty=d, t=t), "cpu") for t, c, r, d in ramp
    ]
    idle, cpu = summarise(samples)
    assert idle.phase == "idle" and idle.seconds == 3 and idle.cpu_peak_c == 45
    assert cpu.cpu_start_c == 50 and cpu.cpu_peak_c == 79 and cpu.gpu_peak_c == 41
    assert cpu.cpu_end_c == 79 and idle.cpu_end_c == 45
    intake = cpu.fans["io:fan2"]
    assert intake.rpm_start == 450 and intake.rpm_peak == 1000
    assert intake.duty_start == 25 and intake.duty_peak == 55
    assert intake.responded_after_s == 2  # first rise at t=5, phase began at t=3
    assert intake.verdict is FanVerdict.RESPONDED
    assert cpu.fans["it8689:fan1"].verdict is FanVerdict.FLAT


def test_summarise_marks_a_fan_that_never_spins() -> None:
    samples = [sample_from(snapshot(intake=0, duty=60, t=t), "cpu") for t in range(4)]
    (cpu,) = summarise(samples)
    assert cpu.fans["io:fan2"].verdict is FanVerdict.STOPPED


def test_render_is_plain_text_with_one_block_per_phase() -> None:
    samples = [sample_from(snapshot(t=t), "idle") for t in range(2)]
    ramp = [(2, 55, 450, 25), (3, 65, 900, 50), (4, 70, 900, 50)]
    samples += [sample_from(snapshot(cpu=c, intake=r, duty=d, t=t), "cpu") for t, c, r, d in ramp]
    text = render(summarise(samples), aborted=None, gpu_load="OpenCL on RTX")
    assert "idle" in text and "cpu" in text and "INTAKE FAN" in text and "responded" in text
    assert "\x1b" not in text
    assert "stopped early" not in text
    assert "stopped early: CPU" in render(
        summarise(samples), aborted="CPU package 90.0 °C", gpu_load=None
    )


def test_sample_without_gpu_or_controller() -> None:
    snap = snapshot(gpu=None)
    snap.controller = None
    s = sample_from(snap, "idle")
    assert s.gpu_c is None and s.intake_duty is None
    assert isinstance(s, Sample)


def test_duty_only_fan_is_judged_by_its_duty() -> None:
    def snap(duty: float, t: float) -> Snapshot:
        s = snapshot(t=t)
        s.fans = [
            Fan(
                "nvml:GPU1",
                "nvml",
                "GPU1",
                None,
                "GPU FAN 2",
                None,
                duty,
                "UNKNOWN",
                None,
                None,
                "",
            )
        ]
        return s

    (phase,) = summarise([sample_from(snap(40, 0), "gpu"), sample_from(snap(61, 1), "gpu")])
    assert phase.fans["nvml:GPU1"].verdict is FanVerdict.RESPONDED
    (flat,) = summarise([sample_from(snap(40, 0), "gpu"), sample_from(snap(40, 1), "gpu")])
    assert flat.fans["nvml:GPU1"].verdict is FanVerdict.FLAT


def test_unlabelled_channel_is_named_by_chip() -> None:
    s = snapshot()
    s.fans = [
        Fan("it8689:2", "it8689", "fan2", None, "fan2", 527, 25.0, "ROTATING", None, None, "")
    ]
    assert sample_from(s, "cpu").fans["it8689:2"].label == "it8689 fan2"


def test_guard_rejects_nonfinite_and_missing_gpu_during_gpu_load() -> None:
    limits = Limits(90.0, 85.0)
    assert guard(sample_from(snapshot(cpu=float("nan")), "cpu"), limits, throttle_baseline=0)
    assert guard(sample_from(snapshot(gpu=None), "gpu"), limits, throttle_baseline=0)
    assert guard(sample_from(snapshot(gpu=None), "both"), limits, throttle_baseline=0)
    assert guard(sample_from(snapshot(gpu=None), "cpu"), limits, throttle_baseline=0) is None


def test_guard_treats_a_lost_throttle_counter_as_a_stop() -> None:
    s = sample_from(snapshot(), "cpu")
    s = Sample(s.time, s.phase, s.cpu_c, s.gpu_c, s.gpu_w, None, s.intake_duty, s.fans)
    assert "throttle" in (guard(s, Limits(90.0, 85.0), throttle_baseline=0) or "")


def test_a_fan_that_stops_mid_phase_is_reported_stopped() -> None:
    rpms = [500, 600, 0, 0, 0]
    samples = [sample_from(snapshot(intake=r, duty=60, t=t), "cpu") for t, r in enumerate(rpms)]
    (cpu,) = summarise(samples)
    assert cpu.fans["io:fan2"].verdict is FanVerdict.STOPPED


def test_sample_time_can_be_monotonic() -> None:
    s = sample_from(snapshot(t=1e9), "cpu", now=12.5)
    assert s.time == 12.5


def test_parse_args_rejects_nonfinite_limits_and_escapes_in_phase_names() -> None:
    import pytest

    from fanwatch.stress import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--max-cpu-c", "nan"])
    with pytest.raises(SystemExit):
        parse_args(["--max-gpu-c", "inf"])
    with pytest.raises(SystemExit) as exc:
        parse_args(["--phases", "cpu,\x1b[2Jbad"])
    assert exc.value.code == 2
    assert parse_args(["--seconds", "5"]).phases == list(PHASES)


def test_render_shows_the_end_temperature_when_it_differs_from_the_peak() -> None:
    cool = [(0, 80), (1, 70), (2, 60)]
    samples = [sample_from(snapshot(cpu=c, t=t), "recovery") for t, c in cool]
    text = render(summarise(samples), aborted=None, gpu_load=None)
    assert "80→80 °C, ends 60" in text
    assert "GPU load: not used" in text
