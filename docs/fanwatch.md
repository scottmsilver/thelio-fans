# fanwatch: the dashboard

A read-only terminal dashboard for the fans, temperatures and the Thelio Io board on
this System76 Thelio Mira. It reads Linux hwmon and USB state from `/sys`, the GPU
through NVML, and the controller's state file from `/run/fanctl`, and never writes to
hardware.

```sh
uv sync            # creates .venv; one runtime dependency (nvidia-ml-py) plus dev tools
uv run fanwatch    # live dashboard
```

| Command | What it does |
|---|---|
| `uv run fanwatch` | live curses dashboard, refreshes every second |
| `uv run fanwatch --interval 2` | slower refresh (0.2 to 3600 s) |
| `uv run fanwatch --once` | one plain-text report |
| `uv run fanwatch --json` | one JSON snapshot; check `alerts`, each fan's `status`, `throttle`, `gpu` and `controller` |
| `uv run fanwatch --log \| tee fan.log` | one line whenever the Io board's USB presence, a driver binding, a fan's status, the CPU throttle counter, or a GPU throttle reason changes, or an RPM moves by 5 % or more |

Dashboard keys: **↑/↓** or **j/k** scroll, **PgUp/PgDn** page, **g/G** top/bottom,
**Space** pause/resume, **r** refresh, **q** quit. Resize freely.

## What it shows

On a terminal at least 24 rows tall, two braille charts first: TEMPERATURES (CPU package,
GPU, NVMe and each motherboard chip's first sensor, with a red dashed rule at the CPU's
reported limit) and FANS (every fan's RPM), each with a coloured legend showing current
values and a time axis covering the last ten minutes at the default interval. Under the
temperatures chart a `cores` strip shows one block per CPU core, height and colour by
heat, on a 16-step blue-to-red gradient when the terminal has 256 colours; the CPU
package line in the chart uses the same gradient. Then a row per fan, the controller
row, and a row per temperature. The header line summarises alerts and the Io board's
USB state. A DIAGNOSTICS block appears only when there are alerts.

```
  FAN                          SOURCE             RPM  DUTY
  CPU FAN via Io               Io CPUF          empty · no fan; CPU fans run from the motherboard header
  INTAKE FAN                   Io INTF            600   33% ━━━━┄┄┄┄┄┄┄┄
  CPU FAN (motherboard header) it8689 fan1      1,161   26% ━━━┄┄┄┄┄┄┄┄┄
  GPU FAN 1                    nvml GPU0            0    0% ┄┄┄┄┄┄┄┄┄┄┄┄  idle
  GPU FAN 2                    nvml GPU1            ?    0% ┄┄┄┄┄┄┄┄┄┄┄┄  duty only
  5 empty headers              it8689 fan2-fan6
  Intake control               fanctl · ema 60.4 °C · 33% · steady

  TEMPERATURE                     NOW    MAX   CRIT
  CPU package                    58.0   80.0  100.0
  CPU cores (12)                46-58
  GPU RTX 3080 Ti                34.0   95.0   98.0
  it8689 temp1                   35.0  127.0      —
  nvme Composite                 46.9   89.8   94.8
  CPU throttling               none since boot · 4.7/4.9 GHz
  GPU state                    P8 · 0% load · 14/350 W · 210/2100 MHz · 377/12288 MiB
  GPU throttling               none
```

### Fans

Fans come from every hwmon chip that exposes `fan*_input`. On this machine that is:

| Chip | Channel | Shown as | Notes |
|---|---|---|---|
| `system76_io` | CPUF | CPU FAN via Io | permanently empty: the CPU fans were moved to the motherboard header on 2026‑09‑07, so 0 RPM here is expected and not an alert |
| `system76_io` | INTF | INTAKE FAN | bottom case fan, driven by fanctl (by system76-power before 2026‑09‑08) |
| `it8689` | fan1 | CPU FAN (motherboard header) | driven by the BIOS fan curve. fan1 is the CPU_FAN channel on Gigabyte boards with this chip; the silkscreen cannot be verified from software |
| `it8689` | fan2–fan6 | collapsed into one "empty headers" row | unlabelled channels reading 0 RPM |
| `nvml` | GPU0, GPU1 | GPU FAN 1, GPU FAN 2 | the card's two fans via NVML. They stop at idle by design, shown dim as "idle", never an alert. NVML reports RPM for the first fan only; the second shows its duty percent tagged "duty only". NVML fan values are targets, not measured rotation |

PWM duty is the same-numbered `pwm*` channel as a percentage of 255. It is the
controller's setting, not measured fan speed.

Fan status: `ROTATING` means positive RPM. `IDLE` is zero RPM while the controller
commands 0 % duty, shown dim: normal for the GPU fans, which stop at idle by design.
`STOPPED` is zero RPM while duty is commanded above 0 %: shown as "starting" in yellow
for the first seconds, and as `STALLED` in red with a header alert once it has read zero
for eight consecutive samples while commanded on. `EMPTY` is zero RPM on an unlabelled
channel or a known-empty channel. `UNKNOWN` means the reading was missing or malformed.
`FAULT` and `ALARM` are hardware flags. Rotation alone does not prove adequate cooling.

If the intake fan is seen cycling on and off at idle, fanctl is not running and
system76-power's curve (0 % below 45 °C, 30 % above, no hysteresis) has it: check the
`Intake control` row and `systemctl status fanctl`.

### Intake control

The `Intake control` row is fanctl's state file, `/run/fanctl/state.json`, rewritten
every tick: the smoothed input temperature, the duty applied, and the reason for the
last decision (`steady`, `increase`, `decrease`, `spin-down delay`, `kick`,
`reassert`, `readback mismatch`, or a sensor-loss panic). It turns yellow with STALE
when the file is more than ten seconds old, red with PANIC when fanctl has lost the CPU
sensor and forced full speed, red with the message when it reports a write or device
error, and is absent when the file is absent. The file is written by a different,
unprivileged user, so it is read defensively (regular file, bounded size, no symlinks).

### Temperatures

Temperatures are read from every hwmon chip, converted from millidegrees; MAX and CRIT
are driver-reported limits. The 12 per-core coretemp readings fold into one "CPU cores"
row showing the min–max span. The `gigabyte_wmi` temperatures are hidden because they
mirror the `it8689` ones reading for reading; they reappear if the it87 driver is not
loaded.

The GPU rows come from NVML through the `nvidia-ml-py` package, NVIDIA's own Python
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
means the CPU is throttling right now. The current and maximum frequency follow.

### The Io board

The header finds the System76 Io board by its USB id `1209:1776` and reports the
device number and which driver is bound to each interface. The Io microcontroller is
powered only through its USB cable, so a changing device number or a missing board
means the cable, not the software. See [drivers/README.md](../drivers/README.md).

Known channel names live in one place, `KNOWN_CHANNELS` in `src/fanwatch/probe.py`.

## Development

```sh
uv run pytest          # fake-sysfs fixture tests, no hardware needed
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy            # strict typing over src and tests
```

Layout, all under `src/fanwatch/`:

| Module | Responsibility |
|---|---|
| `probe.py` | acquire a typed `Snapshot` from a sysfs root (any directory, which is how the tests work) |
| `gpu.py` | the NVML reader, one session kept open across refreshes |
| `state.py` | hardened reader for fanctl's state file |
| `text.py` | sanitising device-supplied text before it reaches a terminal |
| `chart.py` | pure braille chart, legend, time axis and core heat strip, returning coloured spans |
| `dashboard.py` | builds the screen from a `Snapshot` and history; `build_lines` and the chart groups are pure and tested without a terminal; only `run()` touches curses |
| `cli.py` | the `fanwatch` entry point and its four modes |

Python 3.12+. Every public class and function has a docstring.
