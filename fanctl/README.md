# fanctl: the intake fan controller

A systemd service that holds the Thelio Mira's chassis intake fan on a smooth curve
over the hotter of the CPU package and the GPU, with a floor so the fan never stops.
It replaces system76-power's fan loop, which stepped the fan between 0 % and 30 % at
45 °C with no hysteresis and cycled it on and off at idle.

It runs as the unprivileged user `fanctl`. The only file on the system it can write is
the Io board's `pwm2` attribute. Design and credits:
[`docs/superpowers/specs/2026-09-08-fanctl-design.md`](../docs/superpowers/specs/2026-09-08-fanctl-design.md).
Install, verify and roll back: [`packaging/README.md`](../packaging/README.md).

```sh
packaging/build.sh                                        # cargo test + release build
target/release/fanctl --config /nonexistent check         # device, permissions, sensors
target/release/fanctl --config /nonexistent --state /tmp/s.json dry-run --ticks 60
```

## Commands

| Command | What it does |
|---|---|
| `fanctl run` | the service loop: read, decide, write, record state, ping the watchdog |
| `fanctl dry-run` | the same loop printing one decision per tick and writing nothing to the device |
| `fanctl check` | print the resolved config, the device path and its permissions, and the sensor readings |
| `fanctl safe` | write the exit duty and quit; the unit's `ExecStopPost` runs it after any exit |

Options before the command: `--config PATH` (default `/etc/fanctl.toml`; a missing file
means defaults), `--state PATH` (the JSON status file), `--sysfs PATH` (a fake tree for
tests). `run` and `dry-run` take `--ticks N` to stop after N ticks.

## The control law, once a second

| Step | Behaviour | Default | Borrowed from |
|---|---|---|---|
| Input | hotter of CPU package (coretemp) and GPU (NVML); GPU absent is fine, CPU absent is not | | |
| Smoothing | exponential moving average | 8 s | fan2go, CoolerControl |
| Deadband | re-evaluate the curve only when the smoothed input has moved this far | 2 °C | LACT, CoolerControl |
| Curve | linear interpolation over (°C, %) points, clamped at the ends | 50→25, 58→30, 65→40, 75→55, 82→75, 88→100 | LACT, CoolerControl |
| Floor | the curve result is never below this; the fan never stops | 25 % | |
| Start kick | hold at least this duty at startup and whenever the board reappears, so a stopped fan starts | 45 % for 3 s | fancontrol, CoolerControl |
| Spin-down delay | after any thermal increase, no decrease for this long | 30 s | LACT |
| Slew limit | decreases are rate-limited; increases are immediate | 3 %/s | CoolerControl |
| Write policy | write when the byte changes, when the read-back differs by more than one, or after this long unchanged | 60 s | CoolerControl, fan2go |

Only behaviour was borrowed; no code was copied. The law lives in `src/controller.rs`
and is pure: no clock, no files, everything comes in through `Controller::step`.

## Fail-safes

| Condition | Behaviour |
|---|---|
| CPU sensor missing for 5 ticks in a row, whatever the GPU says | duty forced to 100 % directly, bypassing the curve, until it returns |
| Board absent (USB re-enumeration) | hold the last duty, re-scan `/sys/class/hwmon` each tick, kick when it returns |
| Board replaced between two ticks | detected by a generation counter on the resolved path; kick |
| PWM write failing 10 ticks in a row with the board present | exit non-zero; systemd restarts the service |
| Any exit: SIGTERM, SIGINT, error, `--ticks` reached | write the exit duty (50 %) first |
| Crash or watchdog kill | `panic = abort`; the unit's `ExecStopPost=fanctl safe` writes the exit duty |
| Loop wedged | `WATCHDOG=1` is sent after each tick; systemd kills and restarts after 30 s |
| Bad config | refuse to start with a message naming the key; `fanctl safe` still works with defaults |

The driver has no automatic mode to hand back to, so "safe" always means "write a
speed", never "release control".

## Privilege model

Three fences, each sufficient on its own for what it covers:

1. **A permission bit.** `packaging/60-fanctl.rules` runs `chgrp fanctl` and
   `chmod 0664` on the `system76_io` device's `pwm2` whenever it appears. Nothing else
   on the system is writable by the `fanctl` user or group.
2. **A sandbox.** `packaging/fanctl.service` runs as `User=fanctl` with an empty
   `CapabilityBoundingSet`, `NoNewPrivileges`, `ProtectSystem=strict`, private
   network and tmp, `DevicePolicy=closed` with only the two NVIDIA nodes allowed,
   `MemoryDenyWriteExecute`, a system-service syscall filter and a 64 MB memory cap.
   `systemd-analyze security` rates it 1.1. `ProtectKernelTunables` is deliberately
   off: it would mount `/sys` read-only and defeat the whole point.
3. **A small binary.** Five runtime dependencies (`serde`, `serde_json`, `toml`,
   `libc`, `nvml-wrapper`), 664 KB stripped, 21 MB peak RSS of which almost all is the
   NVIDIA library, 0.03 s of CPU per 30 ticks.

Verification commands are in [`packaging/README.md`](../packaging/README.md).

## State file

`/run/fanctl/state.json` is rewritten atomically each tick. It is the one interface
shared with the dashboard, which shows it as the `Intake control` row.

| Key | Meaning |
|---|---|
| `time` | Unix seconds of the tick |
| `cpu_c`, `gpu_c` | raw inputs, `null` when unreadable |
| `smoothed_c` | the EMA the curve saw, `null` before the first reading |
| `duty_pct`, `pwm` | duty applied, in percent and as the 0..255 byte |
| `reason` | what the tick did: `steady`, or a list such as `kick, increase, changed`, `decrease`, `spin-down delay`, `reassert`, `readback mismatch`, `sensor missing: full speed` |
| `panic` | `true` while the CPU sensor is considered lost |
| `dry_run` | `true` when nothing is being written to the device |
| `device` | where `pwm2` was found, or `not present` |
| `error` | the last write or device error, `null` when healthy |

`packaging/install.sh` waits for a state file with `"error": null` and `"panic": false`
before it considers the install healthy.

## Configuration

`/etc/fanctl.toml`, every key optional. Defaults live in `src/config.rs` and are
mirrored, commented out, in `packaging/fanctl.toml`. Validation on start rejects
anything non-finite, a non-monotonic curve, unknown keys, `floor_pct` below 10,
`exit_duty_pct` below 25, `start_pct` outside floor..100, `start_seconds` over 30 and
`interval_s` outside 0.2..5 (the watchdog is 30 s).

## Code layout

| File | Responsibility |
|---|---|
| `src/config.rs` | `Config` with defaults, TOML loading, validation |
| `src/controller.rs` | the pure control law: `Controller::step(temp, now, readback) -> Decision` |
| `src/sensors.rs` | CPU package from coretemp; `GpuReader` trait with the NVML implementation and a `NoGpu` stub |
| `src/pwm.rs` | find the `system76_io` hwmon device, write and read back `pwm2`, re-resolve when it vanishes |
| `src/service.rs` | one tick: sensors, decision, write, failure counting, kick on reconnect, state file |
| `src/state.rs`, `src/notify.rs` | atomic JSON write; `sd_notify` without libsystemd |
| `src/main.rs` | argument parsing, signals, sleeping, the four commands, exit-duty on every path |
| `tests/` | one integration test file per module, against fake sysfs trees in temp dirs |

`src/lib.rs` sets `#![warn(missing_docs)]`, so every public item is documented and
`cargo doc --no-deps` is warning-free.

```sh
cargo test
cargo clippy --all-targets -- -D warnings
cargo doc --no-deps --open
cargo audit
```
