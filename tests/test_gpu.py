from types import SimpleNamespace

from fanwatch.gpu import Gpu, read_gpu


class FakeError(Exception):
    pass


def fake_nvml(
    *, fan_rpm: bool = True, reasons: int = 0x1, fail_init: bool = False
) -> SimpleNamespace:
    calls: list[str] = []

    def init() -> None:
        if fail_init:
            raise FakeError("driver not loaded")

    def rpm(handle: object) -> int:  # the binding reads fan 0 only
        if not fan_rpm:
            raise FakeError("unsupported")
        return 1200

    return SimpleNamespace(
        NVMLError=FakeError,
        NVML_TEMPERATURE_GPU=0,
        NVML_TEMPERATURE_THRESHOLD_SLOWDOWN=1,
        NVML_TEMPERATURE_THRESHOLD_SHUTDOWN=2,
        NVML_CLOCK_GRAPHICS=0,
        NVML_CLOCK_MEM=2,
        nvmlClocksEventReasonGpuIdle=0x1,
        nvmlClocksEventReasonSwPowerCap=0x4,
        nvmlClocksEventReasonHwSlowdown=0x8,
        nvmlClocksEventReasonSwThermalSlowdown=0x20,
        nvmlClocksEventReasonHwThermalSlowdown=0x40,
        nvmlClocksEventReasonHwPowerBrakeSlowdown=0x80,
        nvmlInit=init,
        nvmlShutdown=lambda: calls.append("shutdown"),
        nvmlDeviceGetHandleByIndex=lambda i: "h",
        nvmlDeviceGetName=lambda h: "NVIDIA GeForce RTX 3080 Ti",
        nvmlDeviceGetTemperature=lambda h, k: 46,
        nvmlDeviceGetTemperatureThreshold=lambda h, k: {1: 95, 2: 98}[k],
        nvmlDeviceGetNumFans=lambda h: 2,
        nvmlDeviceGetFanSpeed_v2=lambda h, i: 30 + i,
        nvmlDeviceGetFanSpeedRPM=rpm,
        nvmlDeviceGetUtilizationRates=lambda h: SimpleNamespace(gpu=4, memory=1),
        nvmlDeviceGetPowerUsage=lambda h: 71160,
        nvmlDeviceGetEnforcedPowerLimit=lambda h: 350000,
        nvmlDeviceGetClockInfo=lambda h, k: {0: 1665, 2: 9501}[k],
        nvmlDeviceGetMaxClockInfo=lambda h, k: 2100,
        nvmlDeviceGetMemoryInfo=lambda h: SimpleNamespace(used=395706368, total=12884901888),
        nvmlDeviceGetPerformanceState=lambda h: 0,
        nvmlDeviceGetCurrentClocksEventReasons=lambda h: reasons,
        calls=calls,
    )


def test_read_gpu_maps_nvml_values() -> None:
    nvml = fake_nvml()
    gpu = read_gpu(nvml)
    assert gpu == Gpu(
        name="RTX 3080 Ti",
        temp_c=46,
        slowdown_c=95,
        shutdown_c=98,
        fans=((30, 1200), (31, None)),
        util_pct=4,
        power_w=71.16,
        power_limit_w=350.0,
        clock_mhz=1665,
        clock_max_mhz=2100,
        mem_clock_mhz=9501,
        mem_used_mb=377,
        mem_total_mb=12288,
        pstate=0,
        reasons=(),
    )
    assert nvml.calls == ["shutdown"]


def test_read_gpu_without_rpm_support_and_with_throttle_reasons() -> None:
    gpu = read_gpu(fake_nvml(fan_rpm=False, reasons=0x4 | 0x20))
    assert gpu is not None
    assert gpu.fans == ((30, None), (31, None))
    assert gpu.reasons == ("thermal", "power cap")


def test_read_gpu_is_none_when_driver_unavailable() -> None:
    assert read_gpu(fake_nvml(fail_init=True)) is None
    assert read_gpu(None) is None


def test_shutdown_failure_does_not_lose_the_reading() -> None:
    nvml = fake_nvml()

    def bad_shutdown() -> None:
        raise FakeError("shutdown failed")

    nvml.nvmlShutdown = bad_shutdown
    gpu = read_gpu(nvml)
    assert gpu is not None and gpu.temp_c == 46


def test_missing_binding_functions_and_bad_name_are_tolerated() -> None:
    nvml = fake_nvml()
    del nvml.nvmlDeviceGetFanSpeedRPM
    del nvml.nvmlDeviceGetCurrentClocksEventReasons

    def bad_name(h: object) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")

    nvml.nvmlDeviceGetName = bad_name
    gpu = read_gpu(nvml)
    assert gpu is not None
    assert gpu.name == "GPU"
    assert gpu.fans == ((30, None), (31, None))
    assert gpu.reasons is None  # unavailable, not "none"


def test_gpu_name_is_sanitised() -> None:
    nvml = fake_nvml()
    nvml.nvmlDeviceGetName = lambda h: "NVIDIA GeForce RTX\x1b[2J 3080\n"
    gpu = read_gpu(nvml)
    assert gpu is not None and "\x1b" not in gpu.name and "\n" not in gpu.name


def test_session_initialises_once_and_recovers_from_errors() -> None:
    from fanwatch.gpu import NvmlSession

    nvml = fake_nvml()
    inits: list[str] = []
    real_init = nvml.nvmlInit

    def counting_init() -> None:
        inits.append("init")
        real_init()

    def lost(h: object, k: int) -> int:
        raise FakeError("device lost")

    nvml.nvmlInit = counting_init
    with NvmlSession(nvml) as session:
        assert session.read() is not None
        assert session.read() is not None
        assert inits == ["init"] and nvml.calls == []  # no shutdown between reads
        good_temperature = nvml.nvmlDeviceGetTemperature
        nvml.nvmlDeviceGetTemperature = lost
        stale = session.read()  # reading kept, but the session is closed as stale
        assert stale is not None and stale.temp_c is None
        assert nvml.calls == ["shutdown"]
        nvml.nvmlDeviceGetTemperature = good_temperature
        assert session.read() is not None  # re-initialised on the next read
        assert inits == ["init", "init"]
    assert nvml.calls == ["shutdown", "shutdown"]


def test_session_without_nvml_reads_none() -> None:
    from fanwatch.gpu import NvmlSession

    with NvmlSession(None) as session:
        assert session.read() is None


def test_read_gpu_keeps_a_reading_without_a_temperature() -> None:
    nvml = fake_nvml()

    def unsupported(h: object, k: int) -> int:
        raise FakeError("unsupported")

    nvml.nvmlDeviceGetTemperature = unsupported
    gpu = read_gpu(nvml)
    assert gpu is not None and gpu.temp_c is None
    assert nvml.calls == ["shutdown"]


def test_session_shuts_down_when_the_handle_lookup_fails() -> None:
    from fanwatch.gpu import NvmlSession

    nvml = fake_nvml()

    def no_device(index: int) -> object:
        raise FakeError("no device")

    nvml.nvmlDeviceGetHandleByIndex = no_device
    with NvmlSession(nvml) as session:
        assert session.read() is None
    assert nvml.calls == ["shutdown"]
