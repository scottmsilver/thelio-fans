from fanwatch.chart import (
    BRAILLE_BASE,
    Series,
    core_strip,
    heat_level,
    legend,
    render_chart,
    x_labels,
)
from fanwatch.dashboard import Color


def text_of(rows: list[list[tuple[str, int]]]) -> list[str]:
    return ["".join(t for t, _ in row) for row in rows]


def test_flat_series_lands_on_the_right_row_and_keeps_its_colour() -> None:
    s = Series("CPU", [50.0] * 20, Color.YELLOW, "°C")
    rows = render_chart([s], width=40, height=8, y_min=0, y_max=100)
    assert len(rows) == 8
    lines = text_of(rows)
    # Left gutter carries the axis; 50 of 100 on 8 rows is row index 4 (rows count down).
    plotted = [
        i
        for i, line in enumerate(lines)
        if any(BRAILLE_BASE < ord(c) <= BRAILLE_BASE + 255 for c in line)
    ]
    assert plotted == [4]
    colours = {c for row in rows for t, c in row if any(ord(ch) > BRAILLE_BASE for ch in t)}
    assert colours == {Color.YELLOW}


def test_rising_series_climbs_and_series_are_right_aligned() -> None:
    s = Series("GPU", [float(v) for v in range(0, 100, 5)], Color.CYAN, "°C")
    rows = render_chart([s], width=30, height=10, y_min=0, y_max=100)
    lines = text_of(rows)
    first_col = min(i for line in lines for i, ch in enumerate(line) if ord(ch) > BRAILLE_BASE)
    last_col = max(i for line in lines for i, ch in enumerate(line) if ord(ch) > BRAILLE_BASE)
    assert last_col == len(lines[0]) - 1  # newest sample at the right edge
    assert last_col - first_col + 1 == 10  # 20 samples, two per braille cell
    bottom_row = max(i for i, line in enumerate(lines) if any(ord(c) > BRAILLE_BASE for c in line))
    top_row = min(i for i, line in enumerate(lines) if any(ord(c) > BRAILLE_BASE for c in line))
    assert top_row < bottom_row


def test_limit_rule_missing_samples_time_axis_and_legend() -> None:
    s = Series("CPU", [None, 60.0, None, 60.0], Color.YELLOW, "°C")
    rows = render_chart([s], width=30, height=6, y_min=0, y_max=100, limit=80.0)
    lines = text_of(rows)
    assert all(len(line) == 30 for line in lines)  # no axis gutter: colours carry it
    assert any(c == Color.RED for row in rows for _, c in row)  # the limit rule
    assert x_labels(width=40, samples=600, interval_s=1.0).strip().endswith("now")
    assert "-10:00" in x_labels(width=40, samples=600, interval_s=1.0)
    spans = legend([s, Series("intake", [25.0], Color.GREEN, "%")], limit=80.0)
    text = "".join(t for t, _ in spans)
    assert "CPU 60°C" in text and "intake 25%" in text and "80°C" in text
    assert {c for t, c in spans if "CPU" in t} == {Color.YELLOW}
    assert {c for t, c in spans if "intake" in t} == {Color.GREEN}


def test_heat_level_and_core_strip() -> None:
    assert heat_level(30.0) == 0 and heat_level(100.0) == 15 and 0 < heat_level(65.0) < 15
    strip = core_strip([("Core 0", 41.0), ("Core 4", 58.0), ("Core 8", None)])
    text = "".join(t for t, _ in strip)
    assert text.count("?") == 1 and "41" in text and "58" in text
    blocks = [t for t, _ in strip if t and t in "▁▂▃▄▅▆▇█"]
    assert len(blocks) == 2 and blocks[0] < blocks[1]


def test_heat_coloured_series_matches_the_strip_mapping() -> None:
    values = [30.0] * 10 + [100.0] * 10
    s = Series("CPU", values, Color.YELLOW, "°C", heat=True)
    rows = render_chart([s], width=10, height=4, y_min=0, y_max=100, heat_base=20)
    colours = {c for row in rows for t, c in row if any(ord(ch) > BRAILLE_BASE for ch in t)}
    assert 20 + heat_level(30.0) in colours and 20 + heat_level(100.0) in colours
    assert Color.YELLOW not in colours
    spans = legend([s], heat_base=20)
    assert [c for t, c in spans if "CPU" in t] == [20 + heat_level(100.0)]
