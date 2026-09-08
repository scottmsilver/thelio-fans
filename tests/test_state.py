import os
from pathlib import Path

from fanwatch.state import read_state


def test_read_state_round_trip_and_errors(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text('{"duty_pct": 25, "reason": "steady"}')
    assert read_state(p) == {"duty_pct": 25, "reason": "steady"}
    assert read_state(tmp_path / "absent.json") is None
    p.write_text("{not json")
    assert read_state(p) is None
    p.write_text("[1, 2]")
    assert read_state(p) is None


def test_read_state_rejects_oversized_deep_and_nonregular(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text("[" * 100000 + "]" * 100000)
    assert read_state(p) is None
    p.write_text('{"a": "' + "x" * 200000 + '"}')
    assert read_state(p) is None
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    assert read_state(fifo) is None
