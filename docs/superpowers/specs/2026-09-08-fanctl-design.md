# fanctl: an unprivileged intake-fan controller for the Thelio Mira

Status: approved 2026-09-08; implemented in Rust (see Code layout).

## Goal

Replace system76-power's control of the chassis intake fan with a small controller
that (a) never cycles the fan on and off at idle, (b) runs without root, and (c) can
write exactly one file on the whole system: the intake channel's PWM attribute.

Non-goals: the CPU fan on the motherboard header (BIOS controls it), the GPU fans
(NVIDIA driver controls them), the Io board's unused CPUOUT0 channel, more than one
fan, a GUI, and any write to the it87 driver.

## Facts the design rests on

- The intake fan is `pwm2` (0-255) on the hwmon device of the `system76_io` USB driver,
  currently `/sys/class/hwmon/hwmon2`, permissions `0644 root:root`. The driver's write
  path has no capability check, so a group-writable bit is sufficient (verified in
  `system76-io_hwmon.c`).
- The driver's `pwm2_enable` is a stub that always reads 1 and accepts only 1. There is
  no firmware "auto" mode to hand back to. Whatever value is last written is what the
  fan runs at, indefinitely. Fail-safe therefore means "write a safe speed", never
  "release control".
- The Io board re-enumerates on USB disturbances, which renumbers the hwmon device.
  Nothing may cache the path across an error.
- system76-power writes `pwm1` and `pwm2` once a second from a compiled-in curve with a
  0 % to 30 % step at 45 °C and no hysteresis. It cannot be told to stop only the fan
  loop. Nothing on this system depends on its service. It will be masked.
- CPU package temperature is world-readable in sysfs. GPU temperature is readable
  unprivileged through NVML because the `/dev/nvidia*` nodes are `0666`.
- Measured: this fan reads 660 rpm at 36 % duty and does not reliably start at 30 %.

## Privilege model

No process runs as root at runtime. Root acts once, at install, to place static files.

1. A system user and group `fanctl` (no home, no shell).
2. A udev rule, `/etc/udev/rules.d/60-fanctl.rules`:

   ```
   ACTION=="add|change", SUBSYSTEM=="hwmon", ATTR{name}=="system76_io", \
     RUN+="/bin/chgrp fanctl /sys$devpath/pwm2", RUN+="/bin/chmod 0664 /sys$devpath/pwm2"
   ```

   It changes the group and mode of one file. `add|change` covers re-enumeration.
   This is the mechanism brightnessctl uses for backlight files; it works because
   kernfs honours chown/chmod on attribute inodes until the device is removed.
3. A systemd unit `fanctl.service` running as `User=fanctl`, `Type=notify`,
   `WatchdogSec=30`, `Restart=always`, with: `CapabilityBoundingSet=` (empty),
   `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
   `PrivateNetwork=yes`, `RestrictAddressFamilies=AF_UNIX` (for sd_notify only),
   `ProtectKernelModules=yes`, `ProtectKernelLogs=yes`, `ProtectControlGroups=yes`,
   `ProtectClock=yes`, `ProtectHostname=yes`, `RestrictNamespaces=yes`,
   `RestrictRealtime=yes`, `RestrictSUIDSGID=yes`, `LockPersonality=yes`,
   `SystemCallFilter=@system-service`, `SystemCallErrorNumber=EPERM`, `UMask=0077`,
   `DevicePolicy=closed` with `DeviceAllow=/dev/nvidiactl rw` and `/dev/nvidia0 rw`,
   `RuntimeDirectory=fanctl` (for a state file the dashboard reads), `MemoryMax=128M`.
   `ProtectKernelTunables` is deliberately not set: it would mount `/sys` read-only.
   The permission bit on `pwm2` is the real guarantee; the sandbox is a second fence.
   Precisely: the service can write that one hardware attribute, its `RuntimeDirectory`
   (`/run/fanctl`, for the state file) and its private `/tmp`, and nothing else.
4. `ExecStopPost=fanctl safe` runs as the service user after any exit, including a
   watchdog kill or a crash, and writes the exit duty. A missing board or a wedged
   kernel cannot be covered by software.
5. Verification, all as ordinary commands: `ls -l .../pwm2` shows `root fanctl 0664`;
   `id fanctl` shows only group `fanctl`; `sudo -u fanctl find /sys -writable` lists
   `pwm2` and nothing else; `systemd-analyze security fanctl.service` for the sandbox.

Considered and rejected: a root "writer" helper behind a socket (more code and more
surface than a permission bit, for no added guarantee); Landlock (Python has no clean
binding, and the permission bit already bounds writes to one file).

## Control algorithm

One tick per second. Every element below is borrowed as behaviour from a surveyed
project, credited in the source; no code is copied. LACT's loop (MIT) is the skeleton.

1. **Input**: `max(CPU package, GPU)` in °C. CPU from `coretemp` `Package id 0` via
   `fanwatch.probe`; GPU via `fanwatch.gpu.read_gpu`. If the GPU is unavailable, CPU alone.
2. **Smoothing**: exponential moving average with an 8 s time constant
   (fan2go, CoolerControl). Removes the second-to-second jitter that drives the
   current hunting.
3. **Deadband**: the curve is re-evaluated only when the smoothed temperature has
   moved 2 °C or more from the value last applied (LACT `change_threshold`,
   CoolerControl `deviance`).
4. **Curve**: linear interpolation over points, duty in percent, clamped at the ends
   (LACT, CoolerControl, and system76-power's own shape minus the step):

   | °C | 50 | 58 | 65 | 75 | 82 | 88 |
   |---|---|---|---|---|---|---|
   | duty | 25 | 30 | 40 | 55 | 75 | 100 |

   Below 50 °C the duty is the floor, 25 %. The ramp starts at 50 rather than at
   system76-power's 45 because this machine's smoothed idle temperature sits at 45 to 49 °C. The fan never stops, so there is no on/off
   transition and no spin-up kick to get wrong. The floor is configurable; if it is
   ever set to 0 the on/off hysteresis from nbfc-linux would be needed and is out of
   scope for this version.
   **Start kick**: at startup and whenever the board reappears, the duty is held at
   `start_pct` (45 %) for `start_seconds` (3 s) before the curve applies, because a
   stopped fan may not start at the floor (fancontrol MINSTART, CoolerControl kick).
   The kick is not a thermal increase, so the spin-down delay does not follow it.
5. **Spin-down delay**: after any increase, no decrease for 30 s (LACT
   `spindown_delay`). Increases are immediate.
6. **Slew limit on decreases**: at most 3 percentage points per second, so a ramp from
   100 % to the floor takes 25 s and is inaudible as a step (CoolerControl
   `step_size_max_decreasing`).
7. **Write policy**: write only when the PWM byte changes, plus a re-assert every 60 s
   or whenever the read-back value differs from what was written, so external writers
   or a re-enumerated board cannot leave the fan at a stale value (CoolerControl safety
   latch, fan2go third-party check).
8. **Fail-safe**:
   - CPU sensor unreadable 5 ticks in a row, regardless of the GPU reading: force the
     duty to 100 % directly (CoolerControl `EMERGENCY_MISSING_TEMP`).
   - PWM file missing (board re-enumerating): re-scan `/sys/class/hwmon` for the
     `system76_io` device each tick; hold the last duty while absent.
   - PWM write failing 10 ticks in a row: exit non-zero. systemd restarts it in 2 s.
   - On any exit (SIGTERM, SIGINT, unhandled exception): write 50 % first.
   - `sd_notify WATCHDOG=1` after each completed tick; a wedged loop is killed and
     restarted by systemd after 30 s.
9. **State file**: `/run/fanctl/state.json` rewritten each tick with the raw and
   smoothed temperatures, target and applied duty, and the reason for the last write.
   `fanwatch` reads it and shows one row: `Intake control  fanctl · ema 47.2 °C · 28 %`.

Default numbers live in code; `/etc/fanctl.toml` may override any of them and is
validated on start (type, range, monotonic curve). A bad config refuses to start
rather than running with surprises.

## Code layout

A Rust crate at `fanctl/` in this repository, producing one static-ish binary (links
only libc; NVML is loaded at runtime through `nvml-wrapper`, so the binary runs on a
machine without the NVIDIA driver and simply reports no GPU). The dashboard,
`fanwatch`, stays Python and read-only; it reads the controller's state file through
`fanwatch/state.py`.

- `config.rs`: `Config` with defaults, TOML loading via serde with unknown keys
  rejected, validation: finite numbers, monotonic curve, `floor_pct` at least 10,
  `exit_duty_pct` at least 25, `start_pct` between the floor and 100, `interval_s` at
  most 5 (the watchdog is 30 s).
- `controller.rs`: the pure algorithm. `Controller::step(temp_c, now, readback) ->
  Decision`; `kick(now)` for the start hold; `write_failed()` to force a retry. Panic
  sets the target to 100 directly, not through the curve.
- `pwm.rs`: locate the `system76_io` hwmon device under a sysfs root each time the
  cached path stops existing; write and read back `pwm2`; a read error also drops the
  cached path.
- `sensors.rs`: CPU package from coretemp; GPU through a `GpuReader` trait with an NVML
  implementation that keeps the session open across ticks and re-initialises after an
  error. Tests use a fake reader.
- `service.rs`: one tick: sensors, decision, write, state file. The CPU reading is the
  safety input: if it is missing the controller panics even when the GPU reads fine. An
  absent board holds the last duty without counting write failures; its return triggers
  a kick.
- `notify.rs`, `state.rs`: `sd_notify` over `NOTIFY_SOCKET`; atomic JSON state file.
- `main.rs`: `fanctl run|dry-run|check|safe`; `--config`, `--sysfs`, `--state`,
  `--ticks`. `READY=1` is sent only after the first successful write. Every exit path
  writes the exit duty; a crash aborts instead, and the unit's `ExecStopPost=fanctl safe`
  covers that.
- `packaging/`: `60-fanctl.rules`, `fanctl.service`, `fanctl.toml`, `build.sh` (cargo
  test and release build as the ordinary user), `install.sh` (root: copies the binary to
  `/opt/fanctl/bin`, installs rule, unit and config, verifies the rule applied, masks
  system76-power, starts fanctl, and rolls back to system76-power if it does not come up
  healthy), `uninstall.sh` (also revokes the group on `pwm2` immediately).

Measured on this machine: 663 KB binary, 21 MB peak RSS (mostly the NVIDIA library),
0.03 s CPU per 30 ticks.

## Installation and rollback

Build as the ordinary user with `packaging/build.sh`. Install (root, once) with
`packaging/install.sh`: it creates the user and group, copies the binary, rule, unit
and config, validates the config, triggers udev and checks that `pwm2` now belongs to
`fanctl`, masks system76-power, starts fanctl, waits for a healthy state file, and
restores system76-power if that does not happen. Then the verification commands above.

Rollback: `systemctl disable --now fanctl`; `systemctl unmask
com.system76.PowerDaemon.service && systemctl start` it; remove the rule and re-trigger
udev (or reboot) to restore `0644 root:root` on `pwm2`.

## Testing

Unit tests for the algorithm and the fake-sysfs writer, as with `fanwatch`. Then
`fanctl dry-run` for a few minutes against the real sensors while the cycling is
happening, to confirm the decisions look right before anything is written. Then
install, and a soak with `fanwatch --log` to confirm the intake holds a steady duty at
idle and the header alerts stay quiet. Finally the audit: pip-audit (no new
dependencies expected) and a codex pass on the new package and the unit file.
