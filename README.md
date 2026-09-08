# Thelio fan monitor and controller

Everything about cooling on this System76 Thelio Mira: a read-only dashboard, an
unprivileged controller for the one fan that needed it, the kernel driver that makes
the motherboard's fan header visible, and the notes from the hardware diagnosis that
started it all.

| Part | What it is | Language | Docs |
|---|---|---|---|
| **fanwatch** | Terminal dashboard: every fan, every temperature, CPU and GPU throttling, the Io board's USB state, and charts over the last ten minutes. Never writes to hardware. | Python (`src/fanwatch/`) | [docs/fanwatch.md](docs/fanwatch.md) |
| **fanctl** | Systemd service that drives the chassis intake fan on a smooth curve with a never-stop floor. Runs as its own user; the only file it can write on the whole system is that fan's PWM attribute. | Rust (`fanctl/`) | [fanctl/README.md](fanctl/README.md), [packaging/README.md](packaging/README.md) |
| **drivers** | The out-of-tree `it87` module for the board's IT8689E chip, the host config that loads it, and what was learned about the Thelio Io board and its driver. | C (vendored), config | [drivers/README.md](drivers/README.md) |

## Why this exists

On 2026-09-07 the Thelio Io fan board kept dropping off USB. The diagnosis
([write-up](docs/2026-09-07-thelio-io-diagnosis.md))
found a loose USB0 cable after the board's connectors had been re-plugged in a
different order: the Io microcontroller is powered only through that cable and has no
firmware watchdog, so a marginal plug resets it. The same re-plug had swapped the
board's CPUOUT0 and CPUIN0 connectors and the CPU fans had stopped. They were moved
onto the motherboard's CPU_FAN header, where the BIOS drives them, and left there.

Three things followed, and this repository holds all three:

1. **See everything in one place.** The existing tools showed fragments: `sensors`
   for hwmon, `nvidia-smi` for the GPU, nothing for the Io board's USB health, and
   nothing at all for the fan header the CPU fans now hang off, because the in-kernel
   `it87` driver does not know this board's chip. Result: `fanwatch` plus the
   vendored `it87` driver.
2. **Stop the intake fan cycling.** Once the dashboard existed it showed the chassis
   intake fan switching on and off every few seconds at idle. That was
   system76-power's fan curve, 0 % below 45 °C and 30 % above it with no hysteresis,
   meeting a CPU that idles right at that line. Result: `fanctl`, which replaces that
   one loop with a curve borrowed from the better open-source controllers (smoothing,
   deadband, spin-down delay, slew limit, a 25 % floor so the fan never stops).
3. **Do it with the least privilege that works.** A fan controller normally runs as
   root. This one runs as the user `fanctl` with an empty capability set inside a
   locked-down systemd sandbox, and a udev rule makes exactly one sysfs attribute,
   the intake channel's `pwm2`, writable by its group. `sudo -u fanctl find /sys
   -writable` lists one file. Everything is reversible: `packaging/uninstall.sh`
   restores system76-power and the file's original permissions.

## Who drives which fan

| Fan | Driven by | Appears in fanwatch as |
|---|---|---|
| CPU fans (splitter on the motherboard CPU_FAN header) | BIOS fan curve | `CPU FAN (motherboard header)`, it8689 fan1 |
| Chassis intake fan (Io board INTAKE0) | **fanctl** via Io `pwm2` | `INTAKE FAN`, Io INTF, plus an `Intake control` row |
| GPU fans | NVIDIA driver | `GPU FAN 1/2`, nvml GPU0/GPU1 |
| Io board CPUOUT0 channel | nothing attached since 2026-09-07 | `CPU FAN via Io`, shown as `empty` and never an alert |

## Quick start

```sh
# Dashboard (no root, no writes)
uv sync
uv run fanwatch                 # live; --once, --json, --log for other modes

# Controller: build and watch it decide before it touches anything
packaging/build.sh
fanctl/target/release/fanctl --config /nonexistent check
fanctl/target/release/fanctl --config /nonexistent --state /tmp/fanctl-state.json dry-run --ticks 60

# Controller: install as a service (root, once), verify, or roll back
sudo packaging/install.sh
systemctl status fanctl && cat /run/fanctl/state.json
sudo packaging/uninstall.sh
```

Reading the motherboard header needs the `it87` module; see
[drivers/README.md](drivers/README.md) for the DKMS install. Without it the dashboard
still runs and simply lacks that row.

## Layout

```
src/fanwatch/     dashboard package: probe (sysfs), gpu (NVML), chart, dashboard, cli, state, text
tests/            pytest suite against fake sysfs trees; no hardware needed
fanctl/           Rust crate: config, controller, sensors, pwm, service, state, notify, main
fanctl/tests/     cargo integration tests, one file per module
packaging/        udev rule, systemd unit, default config, build/install/uninstall scripts
drivers/          it87 submodule and the /etc files installed on this host
docs/             fanwatch.md, the Io board diagnosis, and design specs and plans under superpowers/
```

Design documents: [fan dashboard](docs/superpowers/specs/2026-09-07-fan-dashboard-design.md)
and [fanctl](docs/superpowers/specs/2026-09-08-fanctl-design.md). The
fanctl spec lists the fail-safes and credits each borrowed control behaviour.

## Development

```sh
uv run pytest && uv run ruff check . && uv run ruff format . && uv run mypy   # Python
(cd fanctl && cargo test && cargo clippy --all-targets -- -D warnings && cargo doc --no-deps)  # Rust
uv export --no-hashes --no-emit-project | uvx pip-audit -r /dev/stdin   # dependency CVEs
(cd fanctl && cargo audit)
```

Public items are documented in both languages: `fanctl/src/lib.rs` sets
`#![warn(missing_docs)]`, and every public class and function in `fanwatch` carries a
docstring. The two programs share one interface, the state file `fanctl` writes and
`fanwatch` reads; its keys are listed in [fanctl/README.md](fanctl/README.md#state-file).

## License

MIT, see [LICENSE](LICENSE). The vendored `it87` driver under `drivers/it87` is a
separate GPL-2.0 project; nothing here links against it.

## Status

As of 2026-09-08 on this host: `it87` is installed through DKMS, `fanctl` is enabled
and running, and `com.system76.PowerDaemon.service` is masked. The intake fan holds a
steady 25 % at idle instead of cycling.
