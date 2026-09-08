# fanwatch

A read-only terminal dashboard for the fans, temperatures and the Thelio Io board on
this System76 Thelio Mira. It reads Linux hwmon and USB state from `/sys` and never
writes to hardware.

```sh
uv sync            # creates .venv; one runtime dependency (nvidia-ml-py) plus dev tools
uv run fanwatch    # live dashboard
```

| Command | What it does |
|---|---|
| `uv run fanwatch` | live curses dashboard, refreshes every second |
| `uv run fanwatch --interval 2` | slower refresh (0.2 to 3600 s) |
| `uv run fanwatch --once` | one plain-text report |
| `uv run fanwatch --json` | one JSON snapshot; check `alerts`, each fan's `status`, `throttle` and `gpu` |
| `uv run fanwatch --log \| tee fan.log` | one line whenever the Io board's USB presence, a driver binding, a fan's status, the CPU throttle counter, or a GPU throttle reason changes, or an RPM moves by 5 % or more |

Dashboard keys: **↑/↓** or **j/k** scroll, **PgUp/PgDn** page, **g/G** top/bottom,
**Space** pause/resume, **r** refresh, **q** quit. Resize freely.

## What it shows

One compact screen: a row per fan, then a row per temperature, with a TREND sparkline
column on terminals 100 columns or wider. The header line summarises alerts and the Io
board's USB state. A DIAGNOSTICS block appears only when there are alerts.

```
  FAN                          SOURCE             RPM  DUTY               TREND
  CPU FAN via Io               Io CPUF          empty · no fan; CPU fans run from the motherboard header
  INTAKE FAN                   Io INTF            660   36% ━━━━┄┄┄┄┄┄┄┄  ▄▄▄▄▄▄▄▄▄▄
  CPU FAN (motherboard header) it8689 fan1      1,019   26% ━━━┄┄┄┄┄┄┄┄┄  ▅▅▅▆▆▆▆▆▅▅
  GPU FAN 1                    nvml GPU0            0    0% ┄┄┄┄┄┄┄┄┄┄┄┄  idle
  GPU FAN 2                    nvml GPU1            ?    0% ┄┄┄┄┄┄┄┄┄┄┄┄  duty only
  5 empty headers              it8689 fan2-fan6

  TEMPERATURE                     NOW    MAX   CRIT  TREND
  CPU package                    51.0   80.0  100.0  ▃▃▄▄▅▄▄▃▃▃
  CPU cores (12)                43-51
  CPU throttling               none since boot · 4.7/4.9 GHz
  GPU RTX 3080 Ti                40.0   95.0   98.0  ▃▃▃▃▃▃▃▃▃▃
  GPU state                    P8 · 0% load · 14/350 W · 210/2100 MHz · 377/12288 MiB
  GPU throttling               none
  it8689 temp1                   41.0  127.0      —
  nvme Composite                 53.9   89.8   94.8  ▃▃▃▃▃▃▃▃▃▃
```

Fans come from every hwmon chip that exposes `fan*_input`. On this machine that is:

| Chip | Channel | Shown as | Notes |
|---|---|---|---|
| `system76_io` | CPUF | CPU FAN via Io | permanently empty: the CPU fans were moved to the motherboard header on 2026‑09‑07, so 0 RPM here is expected and not an alert |
| `system76_io` | INTF | INTAKE FAN | bottom case fan, driven by system76-power |
| `it8689` | fan1 | CPU FAN (motherboard header) | driven by the BIOS fan curve. fan1 is the CPU_FAN channel on Gigabyte boards with this chip; the silkscreen cannot be verified from software |
| `it8689` | fan2–fan6 | collapsed into one "empty headers" row | unlabelled channels reading 0 RPM |
| `nvml` | GPU0, GPU1 | GPU FAN 1, GPU FAN 2 | the card's two fans via NVML. They stop at idle by design, shown dim as "idle", never an alert. NVML reports RPM for the first fan only; the second shows its duty percent tagged "duty only". NVML fan values are targets, not measured rotation |

The 12 per-core coretemp readings fold into one "CPU cores" row showing the min–max
span. The GPU rows come from NVML through the `nvidia-ml-py` package, NVIDIA's own Python
binding over the `libnvidia-ml.so` that ships with the driver. No subprocess, no
`nvidia-smi` parsing. The temperature row uses the card's slowdown and shutdown
thresholds as MAX and CRIT. "GPU state" shows performance state, load, power draw
against the enforced limit, graphics clock against its maximum, and memory in use.
"GPU throttling" lists the live clock-event reasons (thermal, power cap, hardware
slowdown, power brake) in red with a header warning when any is active, otherwise
"none". If the driver or NVML is missing, the GPU rows are simply absent and `--json`
reports `"gpu": null`.

The "CPU throttling" row reads the kernel's thermal-throttle counters under
`/sys/devices/system/cpu/cpu*/thermal_throttle/`: it stays dim while no throttle event
has happened since boot, turns yellow with the event count and total time once one has,
and turns red with a header warning when the counter moves between two refreshes, which
means the CPU is throttling right now. The current and maximum frequency follow. The `gigabyte_wmi` temperatures are hidden because they mirror the `it8689` ones
reading for reading; they reappear if the it87 driver is not loaded.

Fan status: `ROTATING` means positive RPM. `IDLE` is zero RPM while the controller
commands 0 % duty, shown dim; system76-power's curve switches the intake fan off below
45 °C, so at idle this is the normal state and it never alerts. `STOPPED` is zero RPM
while duty is commanded above 0 %: shown as "starting" in yellow for the first seconds,
and as `STALLED` in red with a header alert once it has read zero for eight consecutive
samples while commanded on. `EMPTY` is zero RPM on an unlabelled channel or a
known-empty channel. `UNKNOWN` means the reading was missing or malformed. `FAULT` and
`ALARM` are hardware flags. Rotation alone does not prove adequate cooling.

Note that the intake fan visibly cycles on and off at idle. That is system76-power's
fan curve (0 % below 45 °C, 30 % at 45 °C, no hysteresis) meeting a CPU that idles
right at that line, not a fault.

PWM duty is the same-numbered `pwm*` channel as a percentage of 255. It is the
controller's setting, not measured fan speed. Temperatures are read from every hwmon
chip, converted from millidegrees; MAX and CRIT are driver-reported limits.

The USB panel finds the System76 Io board by its USB id `1209:1776` and reports the
device number and which driver is bound to each interface. The Io microcontroller is
powered only through its USB cable, so a changing device number or a missing board
means the cable, not the software. See `docs/` and the diagnosis notes in
[`drivers/README.md`](drivers/README.md).

Known channel names live in one place, `KNOWN_CHANNELS` in `src/fanwatch/probe.py`.

## Development

```sh
uv run pytest          # fake-sysfs fixture tests, no hardware needed
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy            # strict typing over src and tests
```

Layout: `src/fanwatch/probe.py` acquires a typed `Snapshot` from a sysfs root (any
directory, which is how the tests work); `dashboard.py` renders it, with `build_lines`
kept pure so it can be tested without a terminal; `cli.py` is the `fanwatch` entry
point; `gpu.py` is the NVML reader. Python 3.12+.

## fanctl: the intake fan controller

`fanctl/` is a small Rust program that replaces system76-power's on/off cycling of the
intake fan with a smooth curve over the hotter of CPU package and GPU, with a 25 % floor
so the fan never stops. It runs as an unprivileged systemd service whose only writable
hardware attribute is the Io board's `pwm2` file. See
[`packaging/README.md`](packaging/README.md) for build, install, verify and rollback,
and `docs/superpowers/specs/2026-09-08-fanctl-design.md` for the design and the credits
for the borrowed control behaviours. While it runs, the dashboard shows an
`Intake control` row with its smoothed temperature, duty and last reason.

```sh
packaging/build.sh
fanctl/target/release/fanctl --config /nonexistent check      # what it sees
fanctl/target/release/fanctl --config /nonexistent dry-run    # decisions once a second, no writes
```

## Kernel drivers

Reading the motherboard's own fan header needs the out-of-tree `it87` module, because
the in-kernel driver does not know the board's IT8689E chip. The checkout and the
config files installed on this host are under `drivers/`; see
[`drivers/README.md`](drivers/README.md) for the install steps.
