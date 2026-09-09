"""Command-line entry point: live dashboard, one-shot text/JSON, or a change log."""

from __future__ import annotations

import argparse
import curses
import json
import math
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict

from fanwatch import dashboard
from fanwatch.probe import Collector, Snapshot, collect

RPM_JITTER = 5  # percent; smaller RPM changes are not logged
MIN_INTERVAL, MAX_INTERVAL = 0.2, 3600.0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The `fanwatch` command line; `--once`, `--json` and `--log` are mutually exclusive."""
    parser = argparse.ArgumentParser(prog="fanwatch", description=__doc__)
    parser.add_argument(
        "--interval",
        type=float,
        default=1,
        metavar="SECONDS",
        help="refresh interval in seconds, 0.2 to 3600 (default: 1)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="print one plain-text report")
    mode.add_argument("--json", action="store_true", help="print one JSON snapshot")
    mode.add_argument(
        "--log",
        action="store_true",
        help="print a line whenever the Io board or a fan changes state (for tee)",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or not MIN_INTERVAL <= args.interval <= MAX_INTERVAL:
        parser.error(f"--interval must be between {MIN_INTERVAL} and {MAX_INTERVAL:g} seconds")
    return args


def _log_state(snapshot: Snapshot) -> tuple[str, dict[str, tuple[str, int | None]]]:
    if snapshot.controllers:
        usb = snapshot.controllers[0]
        drivers = ",".join(d or "unbound" for d in usb.interfaces.values()) or "no-interfaces"
        io_state = f"IO {usb.port} devnum={usb.devnum} driver={drivers}"
    else:
        io_state = "IO ABSENT"
    if snapshot.throttle is not None:
        io_state += f"  throttle={snapshot.throttle.package_events}"
    if snapshot.gpu is not None:
        reasons = snapshot.gpu.reasons
        state = "unknown" if reasons is None else ",".join(reasons) or "none"
        io_state += f"  gpu_throttle={state}"
    fans = {
        f.key: (f.status, f.rpm)
        for f in snapshot.fans
        if f.status != "EMPTY" or f.label is not None
    }
    return io_state, fans


def _rpm_changed(before: int | None, after: int | None) -> bool:
    if before is None or after is None:
        return before is not after
    if before == 0 or after == 0:
        return before != after
    return abs(after - before) / before * 100 >= RPM_JITTER


def log_line(previous: Snapshot | None, current: Snapshot) -> str | None:
    """Describe the current state if it differs meaningfully from the previous one."""
    io_state, fans = _log_state(current)
    if previous is not None:
        prev_io, prev_fans = _log_state(previous)
        changed = io_state != prev_io or fans.keys() != prev_fans.keys()
        for key, (status, rpm) in fans.items():
            prev_status, prev_rpm = prev_fans.get(key, ("", None))
            if status != prev_status or _rpm_changed(prev_rpm, rpm):
                changed = True
        if not changed:
            return None
    stamp = time.strftime("%H:%M:%S", time.localtime(current.timestamp))
    parts = [io_state]
    if current.gpu is not None:
        temp = "?" if current.gpu.temp_c is None else f"{current.gpu.temp_c}C"
        power = "?" if current.gpu.power_w is None else f"{current.gpu.power_w:.0f}W"
        parts.append(f"gpu={temp}/{power}")
    for fan in current.fans:
        if fan.key not in fans:
            continue
        tag = fan.label or fan.channel
        shown = "?" if fan.rpm is None else str(fan.rpm)
        parts.append(f"{tag}={shown}" + ("" if fan.status == "ROTATING" else f"({fan.status})"))
    return f"{stamp} " + "  ".join(parts)


class LogTracker:
    """Emit a line when state moves relative to the last line emitted, not the last sample."""

    def __init__(self) -> None:
        self.baseline: Snapshot | None = None

    def observe(self, current: Snapshot) -> str | None:
        line = log_line(self.baseline, current)
        if line is not None:
            self.baseline = current
        return line


def _run_log(interval: float) -> int:
    tracker = LogTracker()
    try:
        with Collector() as fresh:
            while True:
                line = tracker.observe(fresh())
                if line:
                    print(line, flush=True)
                time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: dispatch to the log, one-shot or live modes. Returns the exit status."""
    args = parse_args(argv)
    if args.log:
        return _run_log(args.interval)
    if args.once or args.json:
        snapshot = collect()
        if args.json:
            print(json.dumps(asdict(snapshot), indent=2))
        else:
            print("FAN WATCH / " + snapshot.machine)
            print("\n".join(line for line, _ in dashboard.build_lines(snapshot, {}, 120)))
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("The dashboard needs a terminal. Use --once, --json or --log.", file=sys.stderr)
        return 2
    try:
        curses.wrapper(dashboard.run, args.interval)
    except KeyboardInterrupt:
        pass
    except curses.error as exc:
        print(f"Cannot initialize terminal: {exc}. Try --once.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
