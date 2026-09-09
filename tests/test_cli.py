import contextlib
import io
import json

import pytest

from fanwatch.cli import log_line, main, parse_args
from fanwatch.probe import Controller, Fan, Snapshot


def fan(rpm: int | None = 660, status: str = "ROTATING") -> Fan:
    return Fan(
        "k", "system76_io", "fan2", "INTF", "INTAKE FAN", rpm, 36.0, status, None, None, "/sys/x"
    )


def controller(devnum: int | None = 9, driver: str | None = "system76-io") -> Controller:
    interfaces = {"1-5.1:1.0": driver, "1-5.1:1.1": driver}
    return Controller("1-5.1", "Io", "1209:1776", devnum, interfaces)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "0.01"])
def test_invalid_intervals_rejected(value: str) -> None:
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        parse_args(["--interval", value])


def test_modes_are_exclusive() -> None:
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        parse_args(["--json", "--log"])
    assert parse_args(["--log"]).log


def test_log_line_reports_initial_state_and_changes() -> None:
    first = Snapshot(0, "m", fans=[fan()], controllers=[controller()])
    line = log_line(None, first)
    assert line is not None
    assert "1-5.1" in line and "devnum=9" in line and "INTF=660" in line
    assert log_line(first, first) is None
    re_enumerated = Snapshot(1, "m", fans=[fan()], controllers=[controller(devnum=12)])
    assert "devnum=12" in (log_line(first, re_enumerated) or "")
    gone = Snapshot(2, "m", fans=[], controllers=[])
    assert "ABSENT" in (log_line(re_enumerated, gone) or "")


def test_log_line_ignores_small_rpm_jitter_but_not_status() -> None:
    a = Snapshot(0, "m", fans=[fan(660)], controllers=[controller()])
    b = Snapshot(1, "m", fans=[fan(672)], controllers=[controller()])
    assert log_line(a, b) is None
    c = Snapshot(2, "m", fans=[fan(0, "STOPPED")], controllers=[controller()])
    assert "STOPPED" in (log_line(b, c) or "")


def test_json_mode_prints_a_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = Snapshot(0, "m", fans=[fan()], controllers=[controller()], alerts=[])
    monkeypatch.setattr("fanwatch.cli.collect", lambda: snap)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["--json"]) == 0
    data = json.loads(out.getvalue())
    assert data["fans"][0]["rpm"] == 660
    assert data["controllers"][0]["devnum"] == 9


def test_once_mode_prints_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fanwatch.cli.collect", lambda: Snapshot(0, "Box", fans=[fan()]))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["--once"]) == 0
    assert "Box" in out.getvalue() and "INTAKE FAN" in out.getvalue()


def test_interval_upper_bound() -> None:
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        parse_args(["--interval", "1e300"])
    assert parse_args(["--interval", "3600"]).interval == 3600


def test_log_uses_last_logged_reading_as_baseline() -> None:
    from fanwatch.cli import LogTracker

    tracker = LogTracker()
    assert tracker.observe(Snapshot(0, "m", fans=[fan(1000)], controllers=[controller()]))
    assert tracker.observe(Snapshot(1, "m", fans=[fan(1040)], controllers=[controller()])) is None
    assert (
        tracker.observe(Snapshot(2, "m", fans=[fan(1080)], controllers=[controller()])) is not None
    )


def test_log_line_reports_throttle_count_changes() -> None:
    from fanwatch.probe import Throttle

    a = Snapshot(
        0, "m", fans=[fan()], controllers=[controller()], throttle=Throttle(0, 0, 0, 0, 1, 2)
    )
    b = Snapshot(
        1, "m", fans=[fan()], controllers=[controller()], throttle=Throttle(1, 0, 50, 50, 1, 2)
    )
    line = log_line(a, b)
    assert line is not None and "throttle=1" in line
    assert log_line(b, b) is None


def test_log_line_includes_gpu_and_logs_reason_changes() -> None:
    from fanwatch.gpu import Gpu

    def gpu(reasons: tuple[str, ...]) -> Gpu:
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

    a = Snapshot(0, "m", fans=[fan()], controllers=[controller()], gpu=gpu(()))
    b = Snapshot(1, "m", fans=[fan()], controllers=[controller()], gpu=gpu(("thermal",)))
    first = log_line(None, a)
    assert first is not None and "gpu=46C/71W" in first
    assert "gpu_throttle=thermal" in (log_line(a, b) or "")
    assert log_line(b, b) is None


def test_log_marks_unavailable_gpu_reasons_as_unknown() -> None:
    from dataclasses import replace

    from fanwatch.gpu import Gpu

    g = Gpu("RTX", 46, 95, 98, ((0, 0),), 4, 71.2, 350.0, 1665, 2100, 9501, 377, 12288, 0, ())
    snap = Snapshot(0, "m", fans=[fan()], controllers=[controller()], gpu=replace(g, reasons=None))
    assert "gpu_throttle=unknown" in (log_line(None, snap) or "")


def test_log_mode_polls_through_one_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class FakeCollector:
        def __init__(self) -> None:
            self.reads = 0

        def __enter__(self) -> "FakeCollector":
            events.append("open")
            return self

        def __exit__(self, *exc: object) -> None:
            events.append("close")

        def __call__(self) -> Snapshot:
            self.reads += 1
            if self.reads > 2:
                raise KeyboardInterrupt
            return Snapshot(0, "Box", fans=[fan(rpm=100 * self.reads)])

    monkeypatch.setattr("fanwatch.cli.Collector", FakeCollector)
    monkeypatch.setattr("fanwatch.cli.time.sleep", lambda s: None)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["--log", "--interval", "0.5"]) == 0
    assert events == ["open", "close"]
    assert out.getvalue().count("\n") == 2
