from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_real_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the host's GPU from the fixture tests."""
    monkeypatch.setattr("fanwatch.probe.read_gpu", lambda: None)


Writer = Callable[[str, object], Path]
IoFan = Callable[..., None]


@pytest.fixture
def sysfs(tmp_path: Path) -> Path:
    """A fake sysfs root."""
    return tmp_path


@pytest.fixture
def write(sysfs: Path) -> Writer:
    def _write(relative: str, value: object) -> Path:
        path = sysfs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value))
        return path

    return _write


@pytest.fixture
def io_fan(write: Writer) -> IoFan:
    """Write a System76 Io hwmon chip with one labelled fan."""

    def _fan(rpm: object = "150", label: str = "INTF", index: int = 1, pwm: int = 81) -> None:
        base = "class/hwmon/hwmon9/"
        write(base + "name", "system76_io")
        write(base + f"fan{index}_label", label)
        write(base + f"fan{index}_input", rpm)
        write(base + f"pwm{index}", pwm)

    return _fan
