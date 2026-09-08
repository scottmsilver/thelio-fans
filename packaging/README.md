# Installing fanctl

fanctl replaces system76-power's control of the intake fan. It is a Rust binary that runs
as the unprivileged user `fanctl`, whose only writable hardware attribute is the Io
board's `pwm2`, granted by the udev rule in this directory. What it does and how it
fails safe: [`../fanctl/README.md`](../fanctl/README.md). Design:
`../docs/superpowers/specs/2026-09-08-fanctl-design.md`.

## 1. Build and try it, as yourself

```sh
packaging/build.sh                                        # cargo test + release build
fanctl/target/release/fanctl --config /nonexistent check  # device, permissions, sensors
fanctl/target/release/fanctl --config /nonexistent --state /tmp/fanctl-state.json dry-run --ticks 120
```

The dry run prints one decision per second and writes nothing. Expect the 45 % start
kick for three seconds, a ramp down to 25 %, then steadiness at idle.

## 2. Install, as root, once

```sh
sudo packaging/install.sh
```

In order: creates the `fanctl` system user and group; copies the binary to
`/opt/fanctl/bin/fanctl`; installs `60-fanctl.rules`, `fanctl.service` and, if absent,
`/etc/fanctl.toml`; validates the config; reloads and triggers udev and checks that
`pwm2` is now `root:fanctl 0664`; masks `com.system76.PowerDaemon`; starts `fanctl` and
waits for a healthy state file. If fanctl does not come up healthy it restores
system76-power and exits non-zero. Nothing is downloaded or built as root.

## 3. Verify

```sh
ls -l /sys/class/hwmon/hwmon*/pwm2               # the system76_io one: root fanctl 0664
sudo -u fanctl find /sys -writable 2>/dev/null   # exactly that one file
systemctl status fanctl                          # active, "Watchdog" in the status line
systemd-analyze security fanctl.service          # exposure level
cat /run/fanctl/state.json
uv run fanwatch --once | grep -E 'INTAKE|Intake control'
```

## Rollback

```sh
sudo packaging/uninstall.sh
```

Stops fanctl, removes the unit, rule and `/opt/fanctl`, puts `pwm2` back to
`root:root 0644` at once, and restarts system76-power.

## Tuning

Edit `/etc/fanctl.toml` (keys, bounds and defaults are listed in it), then
`sudo systemctl restart fanctl`. A bad file makes the service refuse to start; see
`journalctl -u fanctl`.
