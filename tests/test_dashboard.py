from fanwatch.dashboard import build_lines, chart_groups, core_readings
from fanwatch.gpu import Gpu
from fanwatch.probe import Fan, Snapshot, Temperature


def fan(**overrides: object) -> Fan:
    fields: dict[str, object] = {
        "key": "k",
        "chip": "system76_io",
        "channel": "fan2",
        "label": "INTF",
        "name": "INTAKE FAN",
        "rpm": 660,
        "duty": 36.1,
        "status": "ROTATING",
        "error": None,
        "note": None,
        "path": "/sys/example",
    }
    fields.update(overrides)
    return Fan(**fields)  # type: ignore[arg-type]


def temp(chip: str, label: str, celsius: float | None, **overrides: object) -> Temperature:
    fields: dict[str, object] = {
        "chip": chip,
        "label": label,
        "celsius": celsius,
        "critical": None,
        "maximum": None,
        "error": None,
        "key": f"{chip}:{label}",
    }
    fields.update(overrides)
    return Temperature(**fields)  # type: ignore[arg-type]


def lines(
    snapshot: Snapshot, histories: dict[str, list[int | None]] | None = None, width: int = 120
) -> list[str]:
    return [line for line, _ in build_lines(snapshot, histories or {}, width)]


def row(items: list[str], needle: str) -> str:
    return next(x for x in items if needle in x)


def test_fan_is_one_row_with_source_rpm_and_duty() -> None:
    out = lines(Snapshot(0, "Box", fans=[fan()]), {"k": [600, 660]})
    r = row(out, "INTAKE FAN")
    for value in ("Io INTF", "660", "36%"):
        assert value in r
    assert "ROTATING" not in r  # rotating is the quiet default; only other states are tagged
    stopped = lines(Snapshot(0, "Box", fans=[fan(rpm=0, status="STOPPED")]))
    assert "starting" in row(stopped, "INTAKE FAN")  # commanded on, no history yet
    assert sum("INTAKE FAN" in x for x in out) == 1
    assert "TREND" not in "\n".join(out)  # the chart replaced per-row sparklines


def test_rows_fit_a_narrow_terminal() -> None:
    out = lines(Snapshot(0, "Box", fans=[fan()]), {"k": [600, 660]}, width=80)
    assert "660" in row(out, "INTAKE FAN")
    assert all(len(x) <= 80 for x in out)


def test_empty_headers_collapse_to_one_row() -> None:
    empties = [
        fan(
            key=f"e{i}",
            chip="it8689",
            channel=f"fan{i}",
            label=None,
            name=f"fan{i}",
            rpm=0,
            duty=25.0,
            status="EMPTY",
        )
        for i in range(2, 7)
    ]
    out = lines(Snapshot(0, "Box", fans=[fan(), *empties]))
    r = row(out, "5 empty headers")
    assert "it8689" in r and "fan2" in r and "fan6" in r
    assert not any("EMPTY" in x for x in out)


def test_known_empty_channel_is_one_row_with_note() -> None:
    cpuf = fan(
        key="c",
        channel="fan1",
        label="CPUF",
        name="CPU FAN via Io",
        rpm=0,
        status="EMPTY",
        note="no fan connected; CPU fans run from the motherboard header",
    )
    out = lines(Snapshot(0, "Box", fans=[cpuf]))
    r = row(out, "CPU FAN via Io")
    assert "empty" in r and "motherboard header" in r
    assert "STOPPED" not in "\n".join(out)


def test_temperatures_fold_cores_and_show_package_with_trend() -> None:
    temps = [
        temp("coretemp", "Package id 0", 56.0, maximum=80.0, critical=100.0),
        temp("coretemp", "Core 0", 41.0),
        temp("coretemp", "Core 4", 58.0),
        temp("nvme", "Composite", 53.9, critical=94.8),
    ]
    hist: dict[str, list[int | None]] = {"coretemp:Package id 0": [50, 56]}
    out = lines(Snapshot(0, "Box", temperatures=temps), hist)
    pkg = row(out, "CPU package")
    assert "56.0" in pkg and "80.0" in pkg and "100.0" in pkg
    cores = row(out, "CPU cores (2)")
    assert "41" in cores and "58" in cores
    assert not any("Core 0" in x for x in out)
    assert "94.8" in row(out, "nvme Composite")


def test_header_summarises_io_and_alerts() -> None:
    from fanwatch.dashboard import status_line
    from fanwatch.probe import Controller

    ok = Snapshot(
        0,
        "Box",
        controllers=[
            Controller("1-5.1", "Io", "1209:1776", 4, {"a": "system76-io", "b": "system76-io"})
        ],
    )
    text, _ = status_line(ok)
    assert "no alerts" in text and "1-5.1" in text and "devnum 4" in text and "2 interfaces" in text
    bad = Snapshot(0, "Box", alerts=["x stopped", "y"])
    text, _ = status_line(bad)
    assert "2 alerts" in text and "Io absent" in text


def test_diagnostics_only_when_alerts() -> None:
    assert not any("DIAGNOSTICS" in x for x in lines(Snapshot(0, "Box", fans=[fan()])))
    out = lines(Snapshot(0, "Box", alerts=["INTF stopped"]))
    assert any("DIAGNOSTICS" in x for x in out) and any("INTF stopped" in x for x in out)


def test_empty_and_narrow_dashboard() -> None:
    out = lines(Snapshot(0, "Device"), width=40)
    assert out and all(len(x) <= 40 for x in out)
    assert "No fan" in "\n".join(out)


def test_chart_groups_cover_every_temperature_and_fan_line() -> None:
    from fanwatch.dashboard import Color

    temps = [
        temp("coretemp", "Package id 0", 56.0, maximum=80.0),
        temp("coretemp", "Core 0", 41.0),
        temp("coretemp", "Core 4", 58.0),
        temp("gpu", "RTX 3080 Ti", 36.0),
        temp("nvme", "Composite", 50.0),
        temp("it8689", "temp1", 41.0),
        temp("gigabyte_wmi", "temp1", 41.0),  # duplicate chip: not charted
        temp("acpitz", "temp1", 17.0),  # chassis ACPI zones: noise, not charted
        temp("iwlwifi_1", "temp1", None),  # never reads: not charted
    ]
    cpu_fan = fan(
        key="c",
        chip="it8689",
        channel="fan1",
        label=None,
        name="CPU FAN (motherboard header)",
        rpm=1000,
    )
    snap = Snapshot(0, "Box", fans=[fan(), cpu_fan], temperatures=temps)
    hist: dict[str, list[int | None]] = {
        "coretemp:Package id 0": [50, 56],
        "gpu:RTX 3080 Ti": [35, 36],
        "nvme:Composite": [50, 50],
        "it8689:temp1": [41, 41],
        "k": [600, 660],
        "c": [980, 1000],
    }
    temps_group, fans_group = chart_groups(snap, hist)
    assert temps_group.title == "TEMPERATURES" and temps_group.limit == 80.0
    assert [s.name for s in temps_group.series] == [
        "CPU",
        "GPU RTX 3080 Ti",
        "nvme Composite",
        "it8689 temp1",
    ]
    assert temps_group.series[0].color == Color.YELLOW
    assert temps_group.series[1].color == Color.CYAN
    assert list(temps_group.series[0].values) == [50, 56]
    assert fans_group.title == "FANS" and fans_group.y_max == 1000
    assert [s.name for s in fans_group.series] == ["INTAKE FAN", "CPU FAN (motherboard header)"]
    assert len({s.color for s in fans_group.series}) == 2
    assert core_readings(snap) == [("Core 0", 41.0), ("Core 4", 58.0)]


def test_duplicate_wmi_temperatures_hidden_when_superio_chip_present() -> None:
    temps = [temp("gigabyte_wmi", "temp1", 41.0), temp("it8689", "temp1", 41.0)]
    out = lines(Snapshot(0, "Box", temperatures=temps))
    assert any("it8689 temp1" in x for x in out)
    assert not any("gigabyte_wmi" in x for x in out)
    alone = lines(Snapshot(0, "Box", temperatures=[temp("gigabyte_wmi", "temp1", 41.0)]))
    assert any("gigabyte_wmi temp1" in x for x in alone)


def test_throttle_row_quiet_when_no_events() -> None:
    from fanwatch.probe import Throttle

    snap = Snapshot(0, "Box", throttle=Throttle(0, 0, 0, 0, 4700000, 4900000))
    r = row(lines(snap), "CPU throttling")
    assert "none since boot" in r and "4.7/4.9 GHz" in r


def test_throttle_row_reports_events_and_flags_live_throttling() -> None:
    from fanwatch.dashboard import Color, status_line, throttling_now
    from fanwatch.probe import Throttle

    snap = Snapshot(0, "Box", throttle=Throttle(12, 8, 3400, 800, 3000000, 4900000))
    body = build_lines(snap, {"throttle": [11, 12]}, 120)
    text, color = next(x for x in body if "CPU throttling" in x[0])
    assert "12 events" in text and "3.4 s total" in text and "longest 0.8 s" in text
    assert "NOW" in text and color == Color.RED
    assert throttling_now(snap, {"throttle": [11, 12]})
    assert not throttling_now(snap, {"throttle": [12, 12]})
    steady, color = next(
        x for x in build_lines(snap, {"throttle": [12, 12]}, 120) if "CPU throttling" in x[0]
    )
    assert "NOW" not in steady and color == Color.YELLOW
    header, hcolor = status_line(snap, live_throttle=True)
    assert "THROTTLING" in header and hcolor == Color.RED


def _gpu(reasons: tuple[str, ...] = ()) -> Gpu:
    return Gpu(
        "RTX 3080 Ti",
        46,
        95,
        98,
        ((0, 0),),
        4,
        71.2,
        350.0,
        1665,
        2100,
        9501,
        377,
        12288,
        0,
        reasons,
    )


def test_gpu_state_and_throttle_rows() -> None:
    out = lines(Snapshot(0, "Box", gpu=_gpu()))
    state = row(out, "GPU state")
    for value in ("P0", "4% load", "71/350 W", "1665/2100 MHz", "377/12288 MiB"):
        assert value in state
    assert "none" in row(out, "GPU throttling")


def test_gpu_throttling_is_red_and_in_header() -> None:
    from fanwatch.dashboard import Color, status_line

    snap = Snapshot(0, "Box", gpu=_gpu(("power cap", "thermal")))
    text, color = next(x for x in build_lines(snap, {}, 120) if "GPU throttling" in x[0])
    assert "POWER CAP" in text and "THERMAL" in text and color == Color.RED
    header, hcolor = status_line(snap)
    assert "GPU" in header and hcolor == Color.RED


def test_gpu_fan_idle_stop_is_dim_not_yellow() -> None:
    from fanwatch.dashboard import Color

    gpu_fan = fan(
        key="g",
        chip="nvml",
        channel="fan0",
        label="GPU0",
        name="GPU FAN",
        rpm=0,
        duty=0.0,
        status="STOPPED",
    )
    text, color = next(
        x for x in build_lines(Snapshot(0, "Box", fans=[gpu_fan]), {}, 120) if "GPU FAN" in x[0]
    )
    assert "idle" in text and color == Color.MUTED


def test_gpu_unavailable_reasons_and_duty_only_fan_are_shown_honestly() -> None:
    from dataclasses import replace

    from fanwatch.dashboard import Color

    out = lines(Snapshot(0, "Box", gpu=replace(_gpu(), reasons=None)))
    assert "unavailable" in row(out, "GPU throttling")
    duty_only = fan(
        key="g",
        chip="nvml",
        channel="fan1",
        label="GPU1",
        name="GPU FAN 2",
        rpm=None,
        duty=0.0,
        status="UNKNOWN",
    )
    text, color = next(
        x for x in build_lines(Snapshot(0, "Box", fans=[duty_only]), {}, 120) if "GPU FAN 2" in x[0]
    )
    assert "duty only" in text and color == Color.MUTED and "UNKNOWN" not in text


def test_idle_fan_is_dim_and_stalled_fan_is_red_after_persistent_zero() -> None:
    from fanwatch.dashboard import Color

    idle = fan(rpm=0, duty=0.0, status="IDLE")
    text, color = next(
        x for x in build_lines(Snapshot(0, "Box", fans=[idle]), {}, 120) if "INTAKE" in x[0]
    )
    assert "idle" in text and color == Color.MUTED
    stopped = fan(rpm=0, duty=31.0, status="STOPPED")
    starting, color = next(
        x
        for x in build_lines(Snapshot(0, "Box", fans=[stopped]), {"k": [660, 0, 0]}, 120)
        if "INTAKE" in x[0]
    )
    assert "starting" in starting and color == Color.YELLOW
    stalled, color = next(
        x
        for x in build_lines(Snapshot(0, "Box", fans=[stopped]), {"k": [0] * 8}, 120)
        if "INTAKE" in x[0]
    )
    assert "STALLED" in stalled and color == Color.RED


def test_stall_raises_alert_only_when_persistent() -> None:
    from fanwatch.dashboard import alerts_for, status_line

    stopped = fan(rpm=0, duty=31.0, status="STOPPED")
    snap = Snapshot(0, "Box", fans=[stopped])
    assert alerts_for(snap, {"k": [660, 0, 0]}) == []
    stalled = alerts_for(snap, {"k": [0] * 8})
    assert len(stalled) == 1 and "INTF" in stalled[0] and "commanded 31%" in stalled[0]
    text, _ = status_line(snap, alerts=stalled)
    assert "1 alert" in text
    body = build_lines(snap, {"k": [0] * 8}, 120)
    assert any("DIAGNOSTICS" in t for t, _ in body) and any("commanded 31%" in t for t, _ in body)
    assert not any("DIAGNOSTICS" in t for t, _ in build_lines(snap, {"k": [0, 0]}, 120))


def test_controller_row_is_shown_when_state_exists() -> None:
    import time

    state = {
        "duty_pct": 28.0,
        "smoothed_c": 47.2,
        "reason": "steady",
        "dry_run": False,
        "time": time.time(),
    }
    r = row(lines(Snapshot(0, "Box", fans=[fan()], controller=state)), "Intake control")
    assert "fanctl" in r and "47.2" in r and "28%" in r and "STALE" not in r
    stale = row(
        lines(Snapshot(0, "Box", fans=[fan()], controller={**state, "time": 0})), "Intake control"
    )
    assert "STALE" in stale
    assert not any("Intake control" in x for x in lines(Snapshot(0, "Box", fans=[fan()])))


def test_controller_row_sanitises_and_validates_state_fields() -> None:
    import time

    state = {"duty_pct": 1e400, "smoothed_c": "hot", "reason": "ok\x1b[2J\n", "time": time.time()}
    r = row(lines(Snapshot(0, "Box", fans=[fan()], controller=state)), "Intake control")
    assert "\x1b" not in r and "\n" not in r and "?" in r


def test_controller_row_survives_huge_integers() -> None:
    state = {"duty_pct": 10**400, "smoothed_c": 47.2, "reason": "ok", "time": 10**400}
    r = row(lines(Snapshot(0, "Box", fans=[fan()], controller=state)), "Intake control")
    assert "?" in r and "STALE" in r
