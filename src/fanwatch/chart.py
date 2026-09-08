"""Braille time-series chart, legend and per-core heat strip. Pure: returns coloured spans."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

BRAILLE_BASE = 0x2800
# Braille dot bit for (column 0..1, row 0..3) inside one cell, per the Unicode layout.
_DOT = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
BLOCKS = "▁▂▃▄▅▆▇█"
HEAT_LEVELS = 16  # colour steps for the core strip, mapped to colour pairs by the terminal layer
HEAT_MIN_C, HEAT_MAX_C = 30.0, 100.0

Span = tuple[str, int]  # text, colour pair index (see dashboard.Color and heat pairs)


@dataclass(frozen=True, slots=True)
class Series:
    name: str
    values: Sequence[float | None]
    color: int
    unit: str
    heat: bool = False  # colour each point by its value on the heat gradient instead


def _series_color(s: Series, value: float | None, heat_base: int) -> int:
    if s.heat and value is not None:
        return heat_base + heat_level(value)
    return s.color


def _scale(value: float, y_min: float, y_max: float, dots_high: int) -> int:
    """Map a value to a dot row, 0 at the top."""
    frac = (value - y_min) / (y_max - y_min)
    frac = min(1.0, max(0.0, frac))
    return round((1.0 - frac) * (dots_high - 1))


def render_chart(
    series: Sequence[Series],
    *,
    width: int,
    height: int,
    y_min: float,
    y_max: float,
    limit: float | None = None,
    limit_color: int = 4,
    heat_base: int = 20,
) -> list[list[Span]]:
    """`height` rows of `width` cells. Newest sample at the right edge; two samples per
    cell. Later series draw over earlier ones. `limit` draws a dashed reference rule."""
    width, height = max(1, width), max(1, height)
    dots_wide, dots_high = width * 2, height * 4
    bits = [[0] * width for _ in range(height)]
    owner = [[0] * width for _ in range(height)]  # colour pair of the last series on a cell
    for s in series:
        samples = list(s.values)[-dots_wide:]
        offset = dots_wide - len(samples)
        prev: int | None = None
        for i, value in enumerate(samples):
            if value is None:
                prev = None
                continue
            x = offset + i
            y = _scale(value, y_min, y_max, dots_high)
            lo, hi = (y, y) if prev is None else (min(prev, y), max(prev, y))
            color = _series_color(s, value, heat_base)
            for yy in range(lo, hi + 1):  # fill vertical gaps so the line is continuous
                bits[yy // 4][x // 2] |= _DOT[x % 2][yy % 4]
                owner[yy // 4][x // 2] = color
            prev = y
    rule_row = None if limit is None else _scale(limit, y_min, y_max, dots_high) // 4
    rows: list[list[Span]] = []
    for r in range(height):
        spans: list[Span] = []
        for c in range(width):
            if bits[r][c]:
                spans.append((chr(BRAILLE_BASE + bits[r][c]), owner[r][c]))
            elif r == rule_row and c % 2 == 0:
                spans.append(("╌", limit_color))
            else:
                spans.append((" ", 0))
        rows.append(_merge(spans))
    return rows


def _merge(spans: list[Span]) -> list[Span]:
    merged: list[Span] = []
    for text, color in spans:
        if merged and merged[-1][1] == color:
            merged[-1] = (merged[-1][0] + text, color)
        else:
            merged.append((text, color))
    return merged


def x_labels(*, width: int, samples: int, interval_s: float) -> str:
    """A time axis: how far back the left edge is, a midpoint, and 'now' at the right."""
    span_s = samples * interval_s

    def ago(seconds: float) -> str:
        m, s = divmod(round(seconds), 60)
        return f"-{m}:{s:02d}"

    left, mid, right = ago(span_s), ago(span_s / 2), "now"
    gap = max(1, width - len(left) - len(mid) - len(right))
    return left + " " * (gap // 2) + mid + " " * (gap - gap // 2) + right


def legend(
    series: Sequence[Series],
    *,
    limit: float | None = None,
    limit_color: int = 4,
    heat_base: int = 20,
) -> list[Span]:
    spans: list[Span] = []
    for s in series:
        current = next((v for v in reversed(list(s.values)) if v is not None), None)
        shown = "?" if current is None else f"{current:.0f}"
        spans.append(("● " + f"{s.name} {shown}{s.unit}", _series_color(s, current, heat_base)))
        spans.append(("   ", 0))
    if limit is not None:
        spans.append((f"╌╌ {limit:.0f}°C limit", limit_color))
    return spans


def heat_level(temp_c: float) -> int:
    """0 (cool) to HEAT_LEVELS-1 (hot), clamped."""
    frac = (temp_c - HEAT_MIN_C) / (HEAT_MAX_C - HEAT_MIN_C)
    return round(min(1.0, max(0.0, frac)) * (HEAT_LEVELS - 1))


def core_strip(cores: Sequence[tuple[str, float | None]], *, heat_base: int = 20) -> list[Span]:
    """One block per core, height and colour by temperature, then the min-max range."""
    spans: list[Span] = []
    readings = [t for _, t in cores if t is not None]
    for _, temp in cores:
        if temp is None:
            spans.append(("?", 0))
            continue
        level = heat_level(temp)
        block = BLOCKS[min(len(BLOCKS) - 1, level * len(BLOCKS) // HEAT_LEVELS)]
        spans.append((block, heat_base + level))
    if readings:
        spans.append((f"  {min(readings):.0f}-{max(readings):.0f}°C", 0))
    return spans
