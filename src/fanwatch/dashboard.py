"""Compact curses rendering of a fan snapshot. Rendering is pure; terminal I/O lives in run()."""

from __future__ import annotations

import contextlib
import curses
import math
import textwrap
import time
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import IntEnum

from fanwatch.gpu import Gpu
from fanwatch.probe import (
    GPU_CHIP,
    IDLE_STOP_CHIPS,
    IO_DRIVER,
    STALL_SAMPLES,
    Fan,
    Snapshot,
    Temperature,
    Throttle,
    collect,
)
from fanwatch.text import sanitize

HISTORY = 120
MIN_HEIGHT, MIN_WIDTH = 8, 38
TREND_MIN_WIDTH = 100  # sparkline column appears at this terminal width
HEADER_ROWS, FOOTER_ROWS = 3, 1
NAME_W, SOURCE_W, RPM_W, DUTY_W, BAR_W = 28, 14, 7, 5, 12
THROTTLE_KEY = "throttle"  # history key for the package throttle counter
# Chips whose temperatures mirror another chip's; hidden while that chip is present.
DUPLICATE_TEMP_CHIPS = {"gigabyte_wmi": "it8689"}


class Color(IntEnum):
    NORMAL = 0
    CYAN = 1
    GREEN = 2
    YELLOW = 3
    RED = 4
    MUTED = 5


Line = tuple[str, Color]
Histories = Mapping[str, Sequence[int | None]]


def sparkline(values: Iterable[int | None], width: int) -> str:
    samples = list(values)[-max(1, width) :]
    maximum = max((v for v in samples if v is not None), default=1) or 1
    bars = "▁▂▃▄▅▆▇█"
    return "".join(
        "·" if v is None else bars[min(7, max(0, int(v / maximum * 7)))] for v in samples
    )


def _status_color(status: str) -> Color:
    if status == "ROTATING":
        return Color.GREEN
    if status in ("FAULT", "ALARM"):
        return Color.RED
    if status in ("EMPTY", "IDLE"):
        return Color.MUTED
    return Color.YELLOW


def _source(fan: Fan) -> str:
    chip = "Io" if fan.chip == "system76_io" else fan.chip
    return f"{chip} {fan.label or fan.channel}"


def _trend(history: Sequence[int | None], width: int) -> str:
    if width < TREND_MIN_WIDTH or not history:
        return ""
    return "  " + sparkline(history, min(24, width - TREND_MIN_WIDTH + 16))


def _stalled(fan: Fan, history: Sequence[int | None]) -> bool:
    """Commanded on but at zero RPM for STALL_SAMPLES consecutive samples."""
    if fan.status != "STOPPED" or fan.chip in IDLE_STOP_CHIPS:
        return False
    recent = list(history)[-STALL_SAMPLES:]
    return len(recent) >= STALL_SAMPLES and all(v == 0 for v in recent)


def alerts_for(snapshot: Snapshot, histories: Histories) -> list[str]:
    """Snapshot alerts plus stall alerts, which need history to be judged."""
    alerts = list(snapshot.alerts)
    for fan in snapshot.fans:
        if _stalled(fan, histories.get(fan.key, [])):
            duty = f"commanded {fan.duty:.0f}%" if fan.duty is not None else "commanded on"
            tag = fan.label or fan.channel
            alerts.append(f"{tag} on {fan.chip}: STALLED — {duty} but not turning")
    return alerts


def _fan_row(fan: Fan, history: Sequence[int | None], width: int) -> Line:
    name = f"{fan.name[:NAME_W]:<{NAME_W}} {_source(fan)[:SOURCE_W]:<{SOURCE_W}}"
    if fan.status == "EMPTY":
        note = f" · {fan.note}" if fan.note else ""
        return (f"  {name} {'empty':>{RPM_W}}{note}", Color.MUTED)
    rpm = f"{fan.rpm:,}" if fan.rpm is not None else "?"
    duty = f"{fan.duty:.0f}%" if fan.duty is not None else "?"
    bar = ""
    if fan.duty is not None:
        filled = round(fan.duty / 100 * BAR_W)
        bar = " " + "━" * filled + "┄" * (BAR_W - filled)
    color = _status_color(fan.status)
    tag = "" if fan.status == "ROTATING" else f"  {fan.status}"
    if fan.status == "IDLE" or (fan.chip in IDLE_STOP_CHIPS and fan.status == "STOPPED"):
        tag, color = "  idle", Color.MUTED
    elif fan.chip in IDLE_STOP_CHIPS and fan.status == "UNKNOWN" and fan.duty is not None:
        tag, color = "  duty only", Color.MUTED
    elif fan.status == "STOPPED":
        if _stalled(fan, history):
            tag, color = "  STALLED", Color.RED
        else:
            tag, color = "  starting", Color.YELLOW
    return (f"  {name} {rpm:>{RPM_W}} {duty:>{DUTY_W}}{bar}{tag}{_trend(history, width)}", color)


def _fan_lines(snapshot: Snapshot, histories: Histories, width: int) -> list[Line]:
    if not snapshot.fans:
        return [("  No fan sensors available.", Color.YELLOW)]
    lines: list[Line] = []
    unlabelled_empty = [f for f in snapshot.fans if f.status == "EMPTY" and f.label is None]
    for fan in snapshot.fans:
        if fan not in unlabelled_empty:
            lines.append(_fan_row(fan, histories.get(fan.key, []), width))
    by_chip: dict[str, list[str]] = defaultdict(list)
    for fan in unlabelled_empty:
        by_chip[fan.chip].append(fan.channel)
    for chip, channels in by_chip.items():
        plural = "s" if len(channels) != 1 else ""
        span = f"{channels[0]}-{channels[-1]}" if len(channels) > 2 else ", ".join(channels)
        label = f"{len(channels)} empty header{plural}"
        lines.append((f"  {label:<{NAME_W}} {chip} {span}", Color.MUTED))
    if snapshot.controller:
        lines.append(_controller_row(snapshot.controller))
    return lines


def _controller_row(state: dict[str, object]) -> Line:
    """What the fanctl service says it is doing, from its state file."""

    def number(key: str) -> float | None:
        value = state.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        try:
            as_float = float(value)  # a huge JSON integer overflows here, not later
        except OverflowError:
            return None
        return as_float if math.isfinite(as_float) else None

    def num(key: str, fmt: str) -> str:
        value = number(key)
        return "?" if value is None else fmt.format(value)

    written = number("time")
    age = math.inf if written is None else time.time() - written
    stale = " · STALE" if age > 10 else ""
    mode = " (dry run)" if state.get("dry_run") else ""
    reason = sanitize(str(state.get("reason", "")))[:80]
    text = (
        f"  {'Intake control':<{NAME_W}} fanctl{mode} · ema {num('smoothed_c', '{:.1f}')} °C"
        f" · {num('duty_pct', '{:.0f}')}% · {reason}{stale}"
    )
    return (text, Color.YELLOW if stale else Color.MUTED)


def _fmt(v: float | None) -> str:
    return f"{v:.1f}" if v is not None else "—"


def _temp_color(t: Temperature) -> Color:
    if t.celsius is None:
        return Color.YELLOW
    if t.critical is not None and t.celsius >= t.critical:
        return Color.RED
    if t.maximum is not None and t.celsius >= t.maximum:
        return Color.YELLOW
    return Color.NORMAL


def _temp_name(t: Temperature) -> str:
    if t.chip == "coretemp" and t.label.startswith("Package"):
        return "CPU package"
    if t.chip == GPU_CHIP:
        return f"GPU {t.label}"
    return f"{t.chip} {t.label}"


def _temperature_lines(snapshot: Snapshot, histories: Histories, width: int) -> list[Line]:
    chips = {t.chip for t in snapshot.temperatures}
    hidden = {dup for dup, primary in DUPLICATE_TEMP_CHIPS.items() if primary in chips}
    temps = sorted(
        (t for t in snapshot.temperatures if t.chip not in hidden),
        key=lambda t: (
            t.chip != "coretemp",
            "Package" not in t.label,
            t.chip != GPU_CHIP,
            t.chip,
            t.label,
        ),
    )
    if not temps:
        return [("  No temperature sensors available.", Color.YELLOW)]
    lines: list[Line] = []
    cores = [t for t in temps if t.chip == "coretemp" and t.label.startswith("Core")]
    for t in temps:
        if t in cores:
            continue
        name = _temp_name(t)[:NAME_W]
        now, mx, crit = _fmt(t.celsius), _fmt(t.maximum), _fmt(t.critical)
        lines.append(
            (
                f"  {name:<{NAME_W}} {now:>6} {mx:>6} {crit:>6}"
                + _trend(histories.get(t.key, []), width),
                _temp_color(t),
            )
        )
    if cores:
        readings = [t.celsius for t in cores if t.celsius is not None]
        span = f"{min(readings):.0f}-{max(readings):.0f}" if readings else "—"
        name = f"CPU cores ({len(cores)})"
        color = max((_temp_color(t) for t in cores), default=Color.NORMAL)
        lines.insert(1 if lines else 0, (f"  {name:<{NAME_W}} {span:>6}", color))
    return lines


def throttling_now(snapshot: Snapshot, histories: Histories) -> bool:
    """True when the package throttle counter moved between the last two samples."""
    history = histories.get(THROTTLE_KEY, [])
    return snapshot.throttle is not None and len(history) >= 2 and history[-1] != history[-2]


def _throttle_row(t: Throttle, live: bool) -> Line:
    freq = ""
    if t.cur_khz is not None and t.max_khz is not None:
        freq = f" · {t.cur_khz / 1e6:.1f}/{t.max_khz / 1e6:.1f} GHz"
    name = f"  {'CPU throttling':<{NAME_W}} "
    if t.package_events == 0 and t.core_events == 0:
        return (f"{name}none since boot{freq}", Color.MUTED)
    events = f"{t.package_events} events · {t.total_ms / 1000:.1f} s total"
    longest = f" · longest {t.longest_ms / 1000:.1f} s"
    if live:
        return (f"{name}THROTTLING NOW · {events}{longest}{freq}", Color.RED)
    return (f"{name}{events}{longest}{freq}", Color.YELLOW)


def _gpu_rows(gpu: Gpu) -> list[Line]:
    def num(v: int | float | None, fmt: str = "{:.0f}") -> str:
        return "?" if v is None else fmt.format(v)

    pstate = "?" if gpu.pstate is None else f"P{gpu.pstate}"
    state = (
        f"{pstate} · {num(gpu.util_pct)}% load · {num(gpu.power_w)}/{num(gpu.power_limit_w)} W"
        f" · {num(gpu.clock_mhz)}/{num(gpu.clock_max_mhz)} MHz"
        f" · {num(gpu.mem_used_mb)}/{num(gpu.mem_total_mb)} MiB"
    )
    rows = [(f"  {'GPU state':<{NAME_W}} {state}", Color.NORMAL)]
    if gpu.reasons is None:
        rows.append((f"  {'GPU throttling':<{NAME_W}} unavailable", Color.MUTED))
    elif gpu.reasons:
        reasons = " · ".join(r.upper() for r in gpu.reasons)
        rows.append((f"  {'GPU throttling':<{NAME_W}} {reasons}", Color.RED))
    else:
        rows.append((f"  {'GPU throttling':<{NAME_W}} none", Color.MUTED))
    return rows


def status_line(
    snapshot: Snapshot, *, live_throttle: bool = False, alerts: list[str] | None = None
) -> Line:
    """One-line summary of alerts and the Io board for the header."""
    alerts = snapshot.alerts if alerts is None else alerts
    if live_throttle:
        return ("  ! CPU THROTTLING NOW · thermal limit reached", Color.RED)
    if snapshot.gpu is not None and snapshot.gpu.reasons:
        return (f"  ! GPU THROTTLING · {', '.join(snapshot.gpu.reasons)}", Color.RED)
    if snapshot.controllers:
        usb = snapshot.controllers[0]
        bound = sum(1 for d in usb.interfaces.values() if d == IO_DRIVER)
        io = f"Io {usb.port} devnum {usb.devnum} · {IO_DRIVER} bound on {bound} interfaces"
    else:
        io = "Io absent"
    if alerts:
        n = len(alerts)
        plural = "s" if n != 1 else ""
        return (f"  ! {n} alert{plural} · {io} · {alerts[0]}", Color.YELLOW)
    return (f"  ● no alerts · {io}", Color.GREEN)


def build_lines(snapshot: Snapshot, histories: Histories, width: int) -> list[Line]:
    """Build the dashboard body, independent of terminal I/O."""
    width = max(1, width)
    lines: list[Line] = []

    def add(text: str = "", color: Color = Color.NORMAL) -> None:
        lines.append((text[:width], color))

    trend = "TREND" if width >= TREND_MIN_WIDTH else ""
    add(
        f"  {'FAN':<{NAME_W}} {'SOURCE':<{SOURCE_W}} {'RPM':>{RPM_W}} {'DUTY':>{DUTY_W}}"
        f" {'':<{BAR_W}}  {trend}",
        Color.CYAN,
    )
    for text, color in _fan_lines(snapshot, histories, width):
        add(text, color)
    add()
    add(f"  {'TEMPERATURE':<{NAME_W}} {'NOW':>6} {'MAX':>6} {'CRIT':>6}  {trend}", Color.CYAN)
    for text, color in _temperature_lines(snapshot, histories, width):
        add(text, color)
    if snapshot.throttle is not None:
        text, color = _throttle_row(snapshot.throttle, throttling_now(snapshot, histories))
        add(text, color)
    if snapshot.gpu is not None:
        for text, color in _gpu_rows(snapshot.gpu):
            add(text, color)
    alerts = alerts_for(snapshot, histories)
    if alerts:
        add()
        add("  DIAGNOSTICS", Color.CYAN)
        for alert in alerts:
            for part in textwrap.wrap(alert, max(1, width - 4)):
                add("  " + part, Color.YELLOW)
    return lines


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    background = curses.COLOR_BLACK
    with contextlib.suppress(curses.error):
        curses.use_default_colors()
        background = -1
    palette = (
        curses.COLOR_CYAN,
        curses.COLOR_GREEN,
        curses.COLOR_YELLOW,
        curses.COLOR_RED,
        curses.COLOR_WHITE,
    )
    for index, color in enumerate(palette, 1):
        curses.init_pair(index, color, background)


def _record(histories: defaultdict[str, deque[int | None]], snapshot: Snapshot) -> None:
    live = {f.key for f in snapshot.fans} | {t.key for t in snapshot.temperatures}
    live.add(THROTTLE_KEY)
    for stale in [k for k in histories if k not in live]:
        del histories[stale]
    for fan in snapshot.fans:
        histories[fan.key].append(fan.rpm)
    for t in snapshot.temperatures:
        histories[t.key].append(None if t.celsius is None else round(t.celsius))
    if snapshot.throttle is not None:
        histories[THROTTLE_KEY].append(snapshot.throttle.package_events)


def run(screen: curses.window, interval: float) -> None:
    """Interactive loop: sample, render, handle keys. Read-only throughout."""
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    screen.keypad(True)
    screen.timeout(100)
    _init_colors()

    def put(row: int, text: str, color: Color = Color.NORMAL, bold: bool = False) -> None:
        height, width = screen.getmaxyx()
        if not (0 <= row < height and width > 1):
            return
        # Sensor labels are untrusted terminal text; drop control characters.
        text = "".join(c if c.isprintable() else "?" for c in text)
        attr = curses.color_pair(color) if curses.has_colors() else 0
        if bold:
            attr |= curses.A_BOLD
        if color == Color.MUTED:
            attr |= curses.A_DIM
        # A concurrent resize can invalidate the coordinates.
        with contextlib.suppress(curses.error):
            screen.addnstr(row, 0, text, width - 1, attr)

    histories: defaultdict[str, deque[int | None]] = defaultdict(lambda: deque(maxlen=HISTORY))
    snapshot: Snapshot | None = None
    paused = False
    offset = 0
    due = 0.0
    lines: list[Line] = []
    while True:
        now = time.monotonic()
        if snapshot is None or (not paused and now >= due):
            snapshot = collect()
            _record(histories, snapshot)
            due = time.monotonic() + interval
        height, width = screen.getmaxyx()
        screen.erase()
        stamp = datetime.fromtimestamp(snapshot.timestamp).strftime("%H:%M:%S")
        state = "PAUSED" if paused else "LIVE"
        title = f"  ◉ FAN WATCH  /  {snapshot.machine}"
        put(0, f"{title}    {state} · {stamp} · {interval:g}s · READ ONLY", Color.CYAN, True)
        live_throttle = throttling_now(snapshot, histories)
        alerts = alerts_for(snapshot, histories)
        text, color = status_line(snapshot, live_throttle=live_throttle, alerts=alerts)
        put(1, text, color, bool(alerts) or live_throttle or color == Color.RED)
        page = max(1, height - HEADER_ROWS - FOOTER_ROWS)
        if height < MIN_HEIGHT or width < MIN_WIDTH:
            put(3, f"  Enlarge terminal to at least {MIN_WIDTH} x {MIN_HEIGHT}.", Color.YELLOW)
        else:
            lines = build_lines(snapshot, histories, width - 1)
            offset = max(0, min(offset, max(0, len(lines) - page)))
            for row, (text, color) in enumerate(lines[offset : offset + page], HEADER_ROWS):
                put(row, text, color)
        more = f"  {offset + 1}-{min(len(lines), offset + page)}/{len(lines)}" if offset else ""
        put(height - 1, f"  q quit · space pause · r refresh · ↑↓ scroll{more}", Color.MUTED)
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        if key == ord(" "):
            paused = not paused
            due = 0
        elif key in (ord("r"), ord("R")):
            paused = False
            due = 0
        elif key in (curses.KEY_DOWN, ord("j")):
            offset += 1
        elif key in (curses.KEY_UP, ord("k")):
            offset -= 1
        elif key == curses.KEY_NPAGE:
            offset += page
        elif key == curses.KEY_PPAGE:
            offset -= page
        elif key in (curses.KEY_HOME, ord("g")):
            offset = 0
        elif key in (curses.KEY_END, ord("G")):
            offset = 10**9
