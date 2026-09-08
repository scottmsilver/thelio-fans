"""Compact curses dashboard: braille charts for every temperature and fan line, a per-core
heat strip, then one row per fan and per temperature. Rendering functions are pure and
return coloured spans; terminal I/O lives in run()."""

from __future__ import annotations

import contextlib
import curses
import math
import textwrap
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import IntEnum

from fanwatch import chart
from fanwatch.chart import Series, Span
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

HISTORY = 600  # samples kept per series: ten minutes at the default interval
MIN_HEIGHT, MIN_WIDTH = 8, 38
CHART_MIN_HEIGHT, CHART_MIN_WIDTH = 24, 60  # terminal size below which charts are skipped
HEADER_ROWS, FOOTER_ROWS = 3, 1
NAME_W, SOURCE_W, RPM_W, DUTY_W, BAR_W = 28, 14, 7, 5, 12
THROTTLE_KEY = "throttle"  # history key for the package throttle counter
CPU_KEY = "coretemp:Package id 0"
HEAT_BASE = 20  # first colour pair of the 16-step heat gradient
# Chips whose temperatures mirror another chip's; hidden while that chip is present.
DUPLICATE_TEMP_CHIPS = {"gigabyte_wmi": "it8689"}
# Chips that never say anything useful on this machine: ACPI chassis zones, the wifi card.
NOISE_TEMP_CHIPS = {"acpitz", "iwlwifi_1"}


class Color(IntEnum):
    NORMAL = 0
    CYAN = 1
    GREEN = 2
    YELLOW = 3
    RED = 4
    MUTED = 5
    BLUE = 6
    MAGENTA = 7


PALETTE = (Color.YELLOW, Color.CYAN, Color.GREEN, Color.MAGENTA, Color.BLUE, Color.RED)
# 256-colour indices for the heat gradient, cool blue through green and yellow to red,
# and the 8-colour fallback.
HEAT_256 = (27, 33, 39, 45, 51, 49, 47, 82, 118, 154, 190, 226, 220, 214, 208, 196)
HEAT_8 = (4, 4, 4, 6, 6, 6, 2, 2, 2, 3, 3, 3, 1, 1, 1, 1)

Line = tuple[str, Color]
Histories = Mapping[str, Sequence[int | None]]


# ---------------------------------------------------------------- charts


@dataclass(frozen=True, slots=True)
class ChartGroup:
    title: str
    series: list[Series]
    y_min: float
    y_max: float
    limit: float | None = None


def _paint(series: list[Series]) -> list[Series]:
    """Assign palette colours in order. The first series is the most important one."""
    return [replace(s, color=PALETTE[i % len(PALETTE)]) for i, s in enumerate(series)]


def _temp_name(t: Temperature) -> str:
    if t.key == CPU_KEY:
        return "CPU package"
    if t.chip == GPU_CHIP:
        return f"GPU {t.label}"
    return f"{t.chip} {t.label}"


def temperature_group(snapshot: Snapshot, histories: Histories) -> ChartGroup:
    """CPU package, GPU, NVMe, then each other chip's first sensor. Cores are the strip."""
    chosen: list[Series] = []
    seen_chips: set[str] = set()
    ordered = sorted(
        snapshot.temperatures,
        key=lambda t: (t.key != CPU_KEY, t.chip != GPU_CHIP, t.chip != "nvme", t.chip, t.label),
    )
    limit = None
    for t in ordered:
        if t.chip == "coretemp" and t.key != CPU_KEY:
            continue
        if t.chip in DUPLICATE_TEMP_CHIPS or t.chip in NOISE_TEMP_CHIPS or t.chip in seen_chips:
            continue
        if not any(v is not None for v in histories.get(t.key, [])):
            continue  # never reads: nothing to draw
        seen_chips.add(t.chip)
        name = "CPU" if t.key == CPU_KEY else _temp_name(t)
        # The CPU line shares the core strip's heat colouring; other lines get the palette.
        chosen.append(Series(name, histories.get(t.key, []), 0, "°C", heat=t.key == CPU_KEY))
        if t.key == CPU_KEY:
            limit = t.maximum
    return ChartGroup("TEMPERATURES", _paint(chosen), 0, 100, limit)


def fan_group(snapshot: Snapshot, histories: Histories) -> ChartGroup:
    """Every fan that has ever reported RPM, on a shared RPM axis."""
    chosen: list[Series] = []
    top = 0.0
    for fan in snapshot.fans:
        history = histories.get(fan.key, [])
        readings = [v for v in history if v is not None]
        if not readings or max(readings) == 0:
            continue
        top = max(top, float(max(readings)))
        chosen.append(Series(fan.name, history, 0, " rpm"))
    y_max = max(500.0, math.ceil(top / 500) * 500)
    return ChartGroup("FANS", _paint(chosen), 0, y_max)


def chart_groups(snapshot: Snapshot, histories: Histories) -> list[ChartGroup]:
    """Same chart, stamped out per group of lines; drawn top to bottom."""
    return [temperature_group(snapshot, histories), fan_group(snapshot, histories)]


def core_readings(snapshot: Snapshot) -> list[tuple[str, float | None]]:
    cores = [
        t for t in snapshot.temperatures if t.chip == "coretemp" and t.label.startswith("Core")
    ]
    return [(t.label, t.celsius) for t in sorted(cores, key=lambda t: int(t.label.split()[-1]))]


# ---------------------------------------------------------------- rows


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


def _fan_row(fan: Fan, history: Sequence[int | None]) -> Line:
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
    return (f"  {name} {rpm:>{RPM_W}} {duty:>{DUTY_W}}{bar}{tag}", color)


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


def _fan_lines(snapshot: Snapshot, histories: Histories) -> list[Line]:
    if not snapshot.fans:
        return [("  No fan sensors available.", Color.YELLOW)]
    lines: list[Line] = []
    unlabelled_empty = [f for f in snapshot.fans if f.status == "EMPTY" and f.label is None]
    for fan in snapshot.fans:
        if fan not in unlabelled_empty:
            lines.append(_fan_row(fan, histories.get(fan.key, [])))
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


def _temperature_lines(snapshot: Snapshot) -> list[Line]:
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
        lines.append((f"  {name:<{NAME_W}} {now:>6} {mx:>6} {crit:>6}", _temp_color(t)))
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
    """The table body, independent of terminal I/O. Charts are drawn separately."""
    width = max(1, width)
    lines: list[Line] = []

    def add(text: str = "", color: Color = Color.NORMAL) -> None:
        lines.append((text[:width], color))

    add(
        f"  {'FAN':<{NAME_W}} {'SOURCE':<{SOURCE_W}} {'RPM':>{RPM_W}} {'DUTY':>{DUTY_W}}",
        Color.CYAN,
    )
    for text, color in _fan_lines(snapshot, histories):
        add(text, color)
    add()
    add(f"  {'TEMPERATURE':<{NAME_W}} {'NOW':>6} {'MAX':>6} {'CRIT':>6}", Color.CYAN)
    for text, color in _temperature_lines(snapshot):
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


# ---------------------------------------------------------------- terminal


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    background = curses.COLOR_BLACK
    with contextlib.suppress(curses.error):
        curses.use_default_colors()
        background = -1
    palette = {
        Color.CYAN: curses.COLOR_CYAN,
        Color.GREEN: curses.COLOR_GREEN,
        Color.YELLOW: curses.COLOR_YELLOW,
        Color.RED: curses.COLOR_RED,
        Color.MUTED: curses.COLOR_WHITE,
        Color.BLUE: curses.COLOR_BLUE,
        Color.MAGENTA: curses.COLOR_MAGENTA,
    }
    for pair, color in palette.items():
        curses.init_pair(pair, color, background)
    gradient = HEAT_256 if curses.COLORS >= 256 else HEAT_8
    for level, color in enumerate(gradient):
        with contextlib.suppress(curses.error):
            curses.init_pair(HEAT_BASE + level, color, background)


def _record(histories: defaultdict[str, deque[int | None]], snapshot: Snapshot) -> None:
    live = {THROTTLE_KEY}
    for fan in snapshot.fans:
        live.update((fan.key, f"{fan.key}:duty"))
        histories[fan.key].append(fan.rpm)
        histories[f"{fan.key}:duty"].append(None if fan.duty is None else round(fan.duty))
    for t in snapshot.temperatures:
        live.add(t.key)
        histories[t.key].append(None if t.celsius is None else round(t.celsius))
    if snapshot.throttle is not None:
        histories[THROTTLE_KEY].append(snapshot.throttle.package_events)
    for stale in [k for k in histories if k not in live]:
        del histories[stale]


def draw_chart(
    put: Callable[..., None],
    put_spans: Callable[..., None],
    top: int,
    group: ChartGroup,
    width: int,
    height: int,
    interval: float,
) -> int:
    """Title and legend, the braille rows, the time axis. Returns the next free row."""
    legend = chart.legend(
        group.series, limit=group.limit, limit_color=Color.RED, heat_base=HEAT_BASE
    )
    put_spans(top, [(f"{group.title}  ", Color.CYAN), *legend])
    rows = chart.render_chart(
        group.series,
        width=width,
        height=height,
        y_min=group.y_min,
        y_max=group.y_max,
        limit=group.limit,
        limit_color=Color.RED,
        heat_base=HEAT_BASE,
    )
    for i, row_spans in enumerate(rows):
        put_spans(top + 1 + i, row_spans)
    axis = chart.x_labels(width=width, samples=HISTORY, interval_s=interval)
    put(top + 1 + height, "  " + axis, Color.MUTED)
    return top + 2 + height


def run(screen: curses.window, interval: float) -> None:
    """Interactive loop: sample, render, handle keys. Read-only throughout."""
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    screen.keypad(True)
    screen.timeout(100)
    _init_colors()

    def attr_for(color: int, bold: bool = False) -> int:
        attr = curses.color_pair(color) if curses.has_colors() and color else 0
        if bold:
            attr |= curses.A_BOLD
        if color == Color.MUTED:
            attr |= curses.A_DIM
        return attr

    def put(row: int, text: str, color: Color = Color.NORMAL, bold: bool = False) -> None:
        height, width = screen.getmaxyx()
        if not (0 <= row < height and width > 1):
            return
        # Sensor labels are untrusted terminal text; drop control characters.
        text = "".join(c if c.isprintable() else "?" for c in text)
        # A concurrent resize can invalidate the coordinates.
        with contextlib.suppress(curses.error):
            screen.addnstr(row, 0, text, width - 1, attr_for(color, bold))

    def put_spans(row: int, spans: Sequence[Span], indent: int = 2) -> None:
        height, width = screen.getmaxyx()
        if not (0 <= row < height and width > indent + 1):
            return
        col = indent
        for text, color in spans:
            text = "".join(c if c.isprintable() else "?" for c in text)
            room = width - 1 - col
            if room <= 0:
                break
            with contextlib.suppress(curses.error):
                screen.addnstr(row, col, text, room, attr_for(color))
            col += min(len(text), room)

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
        top = HEADER_ROWS
        if height >= CHART_MIN_HEIGHT and width >= CHART_MIN_WIDTH:
            groups = [g for g in chart_groups(snapshot, histories) if g.series]
            # Both charts on a tall terminal, otherwise temperatures only.
            shown = groups if height >= 2 * CHART_MIN_HEIGHT else groups[:1]
            chart_h = min(10, max(5, (height - 18) // max(1, 2 * len(shown))))
            for group in shown:
                top = draw_chart(put, put_spans, top, group, width - 5, chart_h, interval)
                if group.title == "TEMPERATURES":
                    strip = chart.core_strip(core_readings(snapshot), heat_base=HEAT_BASE)
                    put_spans(top, [("cores  ", Color.MUTED), *strip])
                    top += 1
                top += 1
        page = max(1, height - top - FOOTER_ROWS)
        if height < MIN_HEIGHT or width < MIN_WIDTH:
            put(3, f"  Enlarge terminal to at least {MIN_WIDTH} x {MIN_HEIGHT}.", Color.YELLOW)
        else:
            lines = build_lines(snapshot, histories, width - 1)
            offset = max(0, min(offset, max(0, len(lines) - page)))
            for row, (text, color) in enumerate(lines[offset : offset + page], top):
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
