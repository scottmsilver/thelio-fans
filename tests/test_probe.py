from pathlib import Path

import pytest

from fanwatch.gpu import Gpu
from fanwatch.probe import collect
from tests.conftest import IoFan, Writer


def test_rotation_and_duty(sysfs: Path, io_fan: IoFan) -> None:
    io_fan()
    fan = collect(sysfs).fans[0]
    assert (fan.chip, fan.label, fan.rpm, fan.status) == ("system76_io", "INTF", 150, "ROTATING")
    assert fan.duty is not None
    assert abs(fan.duty - 81 / 255 * 100) < 1e-6


def test_zero_on_commanded_channel_is_stopped_without_probe_alert(
    sysfs: Path, io_fan: IoFan
) -> None:
    io_fan("0")  # pwm 81: commanded on. Stalls are judged over time by the dashboard.
    snapshot = collect(sysfs)
    assert snapshot.fans[0].status == "STOPPED"
    assert not any("INTF" in alert for alert in snapshot.alerts)


def test_invalid_reading_is_unknown(sysfs: Path, io_fan: IoFan) -> None:
    for value in ("bad", "-1", ""):
        io_fan(value)
        fan = collect(sysfs).fans[0]
        assert fan.rpm is None
        assert fan.status == "UNKNOWN"
        assert fan.error


def test_hardware_fault_overrides_rotation(sysfs: Path, io_fan: IoFan, write: Writer) -> None:
    io_fan()
    write("class/hwmon/hwmon9/fan1_fault", 1)
    assert collect(sysfs).fans[0].status == "FAULT"


def test_missing_input_with_label_is_unknown(sysfs: Path, io_fan: IoFan) -> None:
    io_fan()
    (sysfs / "class/hwmon/hwmon9/fan1_input").unlink()
    assert collect(sysfs).fans[0].status == "UNKNOWN"


def test_io_cpu_channel_is_known_empty_not_an_alert(sysfs: Path, io_fan: IoFan) -> None:
    io_fan("0", label="CPUF")
    snapshot = collect(sysfs)
    fan = snapshot.fans[0]
    assert fan.status == "EMPTY"
    assert fan.name == "CPU FAN via Io"
    assert "motherboard" in (fan.note or "")
    assert not any("CPUF" in alert for alert in snapshot.alerts)


def test_io_cpu_channel_rotating_is_still_rotating(sysfs: Path, io_fan: IoFan) -> None:
    io_fan("900", label="CPUF")
    assert collect(sysfs).fans[0].status == "ROTATING"


def test_motherboard_chip_names_cpu_fan_and_marks_empty_headers(sysfs: Path, write: Writer) -> None:
    base = "class/hwmon/hwmon6/"
    write(base + "name", "it8689")
    for index, rpm in enumerate(("976", "0", "0"), 1):
        write(base + f"fan{index}_input", rpm)
        write(base + f"pwm{index}", 66)
    fans = collect(sysfs).fans
    assert [f.status for f in fans] == ["ROTATING", "EMPTY", "EMPTY"]
    assert fans[0].name == "CPU FAN (motherboard header)"
    assert fans[1].name == "fan2"
    assert not [a for a in collect(sysfs).alerts if "fan" in a]


def test_display_names_for_io_channels(sysfs: Path, io_fan: IoFan) -> None:
    io_fan(label="INTF")
    assert collect(sysfs).fans[0].name == "INTAKE FAN"


def test_temperature_units_and_limit(sysfs: Path, write: Writer) -> None:
    for name, value in {
        "name": "coretemp",
        "temp1_input": 56000,
        "temp1_crit": 100000,
        "temp1_label": "Package id 0",
    }.items():
        write("class/hwmon/hwmon4/" + name, value)
    t = collect(sysfs).temperatures[0]
    assert (t.celsius, t.critical) == (56, 100)


def test_usb_binding_devnum_and_disconnect(sysfs: Path, write: Writer) -> None:
    base = "bus/usb/devices/1-5.1/"
    write(base + "idVendor", "1209")
    product = write(base + "idProduct", "1776")
    write(base + "devnum", "9")
    interface = sysfs / (base + "1-5.1:1.0")
    interface.mkdir()
    driver = sysfs / "bus/usb/drivers/system76-io"
    driver.mkdir(parents=True)
    (interface / "driver").symlink_to(driver)
    usb = collect(sysfs).controllers[0]
    assert usb.interfaces == {"1-5.1:1.0": "system76-io"}
    assert usb.devnum == 9
    (interface / "driver").unlink()
    assert collect(sysfs).controllers[0].interfaces["1-5.1:1.0"] is None
    product.unlink()
    assert collect(sysfs).controllers == []


def test_empty_system_does_not_report_success(sysfs: Path) -> None:
    snapshot = collect(sysfs)
    assert snapshot.fans == []
    assert snapshot.alerts


def test_history_identity_survives_hwmon_renumbering(sysfs: Path, io_fan: IoFan) -> None:
    io_fan()
    device = sysfs / "devices/controller"
    device.mkdir(parents=True)
    hw = sysfs / "class/hwmon/hwmon9"
    (hw / "device").symlink_to(device)
    key = collect(sysfs).fans[0].key
    hw.rename(hw.with_name("hwmon20"))
    assert collect(sysfs).fans[0].key == key


def test_device_strings_are_sanitised_to_printable_text(sysfs: Path, io_fan: IoFan) -> None:
    io_fan(label="IN\x1b[2JTF\nX")
    fan = collect(sysfs).fans[0]
    assert fan.label is not None
    assert "\x1b" not in fan.label and "\n" not in fan.label
    assert fan.label.startswith("IN")


def test_invalid_utf8_in_sysfs_does_not_crash(sysfs: Path, io_fan: IoFan) -> None:
    io_fan()
    (sysfs / "class/dmi/id").mkdir(parents=True)
    (sysfs / "class/dmi/id/product_name").write_bytes(b"Thel\xff\xfeio")
    snapshot = collect(sysfs)
    assert snapshot.machine.startswith("Thel")
    assert snapshot.fans


def test_absurd_integer_is_unknown_not_overflow(sysfs: Path, io_fan: IoFan, write: Writer) -> None:
    io_fan("9" * 400)
    assert collect(sysfs).fans[0].status == "UNKNOWN"
    write("class/hwmon/hwmon4/name", "coretemp")
    write("class/hwmon/hwmon4/temp1_input", "1" * 400)
    assert collect(sysfs).temperatures[0].celsius is None


def test_known_motherboard_cpu_header_at_zero_is_stopped_not_empty(
    sysfs: Path, write: Writer
) -> None:
    write("class/hwmon/hwmon6/name", "it8689")
    write("class/hwmon/hwmon6/fan1_input", "0")
    snapshot = collect(sysfs)
    assert snapshot.fans[0].status == "STOPPED"  # duty unknown: assume commanded; not EMPTY
    assert not any("fan1" in a for a in snapshot.alerts)  # stalls are judged over time


def test_temperature_has_stable_history_key(sysfs: Path, write: Writer) -> None:
    write("class/hwmon/hwmon4/name", "coretemp")
    write("class/hwmon/hwmon4/temp1_input", 56000)
    write("class/hwmon/hwmon4/temp1_label", "Package id 0")
    assert collect(sysfs).temperatures[0].key == "coretemp:Package id 0"


def test_throttle_counters_package_max_core_sum_and_frequency(sysfs: Path, write: Writer) -> None:
    for cpu, (pkg, core) in enumerate(((12, 3), (12, 5))):
        base = f"devices/system/cpu/cpu{cpu}/thermal_throttle/"
        write(base + "package_throttle_count", pkg)
        write(base + "package_throttle_total_time_ms", 3400)
        write(base + "package_throttle_max_time_ms", 800)
        write(base + "core_throttle_count", core)
    write("devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", 4700000)
    write("devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", 4900000)
    t = collect(sysfs).throttle
    assert t is not None
    assert (t.package_events, t.core_events) == (12, 8)
    assert (t.total_ms, t.longest_ms) == (3400, 800)
    assert (t.cur_khz, t.max_khz) == (4700000, 4900000)


def test_no_throttle_sysfs_gives_none(sysfs: Path) -> None:
    assert collect(sysfs).throttle is None


def _gpu(temp: int = 46, reasons: tuple[str, ...] = (), rpm: int | None = 0) -> Gpu:
    return Gpu(
        "RTX 3080 Ti",
        temp,
        95,
        98,
        ((0, rpm),),
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


def test_gpu_contributes_fan_and_temperature_rows(sysfs: Path) -> None:
    snapshot = collect(sysfs, gpu_reader=lambda: _gpu())
    assert snapshot.gpu is not None and snapshot.gpu.name == "RTX 3080 Ti"
    fan = next(f for f in snapshot.fans if f.chip == "nvml")
    assert (fan.name, fan.rpm, fan.duty, fan.status) == ("GPU FAN", 0, 0.0, "STOPPED")
    assert not any("GPU" in a for a in snapshot.alerts)  # idle fan stop is normal on this card
    t = next(t for t in snapshot.temperatures if t.chip == "gpu")
    assert (t.label, t.celsius, t.maximum, t.critical) == ("RTX 3080 Ti", 46, 95, 98)


def test_gpu_throttle_reasons_and_hot_temperature_alert(sysfs: Path) -> None:
    hot = collect(sysfs, gpu_reader=lambda: _gpu(temp=96, reasons=("thermal",)))
    assert any("GPU" in a and "thermal" in a for a in hot.alerts)
    assert any("critical" in a or "slowdown" in a for a in hot.alerts)


def test_no_gpu_reader_result_means_no_gpu_rows(sysfs: Path) -> None:
    snapshot = collect(sysfs, gpu_reader=lambda: None)
    assert snapshot.gpu is None
    assert not [f for f in snapshot.fans if f.chip == "nvml"]


def test_gpu_fan_without_rpm_is_unknown_not_stopped(sysfs: Path) -> None:
    snapshot = collect(sysfs, gpu_reader=lambda: _gpu(rpm=None))
    fan = next(f for f in snapshot.fans if f.chip == "nvml")
    assert (fan.rpm, fan.duty, fan.status) == (None, 0.0, "UNKNOWN")
    assert not any("GPU" in a for a in snapshot.alerts)


def test_zero_rpm_with_zero_duty_is_idle_not_an_alert(sysfs: Path, io_fan: IoFan) -> None:
    io_fan("0", pwm=0)
    snapshot = collect(sysfs)
    assert snapshot.fans[0].status == "IDLE"
    assert not any("INTF" in a for a in snapshot.alerts)


def test_controller_state_is_attached_when_present(
    sysfs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fanwatch import probe

    state = sysfs / "state.json"
    state.write_text('{"duty_pct": 28.0, "smoothed_c": 47.2, "reason": "steady", "dry_run": false}')
    monkeypatch.setattr(probe, "CONTROLLER_STATE", state)
    assert collect(sysfs).controller == {
        "duty_pct": 28.0,
        "smoothed_c": 47.2,
        "reason": "steady",
        "dry_run": False,
    }
    monkeypatch.setattr(probe, "CONTROLLER_STATE", sysfs / "absent.json")
    assert collect(sysfs).controller is None


def test_collector_shares_one_nvml_session_across_snapshots(sysfs: Path) -> None:
    from fanwatch.gpu import NvmlSession
    from fanwatch.probe import Collector
    from tests.test_gpu import fake_nvml

    nvml = fake_nvml()
    inits: list[str] = []
    real_init = nvml.nvmlInit

    def counting_init() -> None:
        inits.append("init")
        real_init()

    nvml.nvmlInit = counting_init
    with Collector(sysfs, NvmlSession(nvml)) as fresh:
        assert fresh().gpu is not None
        assert fresh().gpu is not None
        assert inits == ["init"] and nvml.calls == []
    assert nvml.calls == ["shutdown"]
