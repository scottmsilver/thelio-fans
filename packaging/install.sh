#!/usr/bin/env bash
# Install fanctl as an unprivileged systemd service. Build first with packaging/build.sh, then:
#   sudo packaging/install.sh
# Prints every command. Nothing is built or downloaded as root; the binary is copied.
# If the new service does not come up healthy, system76-power is restored.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
bin="$here/../fanctl/target/release/fanctl"
run() { printf '+ %s\n' "$*"; "$@"; }
rollback() {
    echo "fanctl did not start cleanly; restoring system76-power" >&2
    systemctl disable --now fanctl.service || true
    systemctl unmask com.system76.PowerDaemon.service || true
    systemctl start com.system76.PowerDaemon.service || true
    exit 1
}

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -x "$bin" ] || { echo "no release binary at $bin; run packaging/build.sh first" >&2; exit 1; }

getent group fanctl >/dev/null || run groupadd --system fanctl
getent passwd fanctl >/dev/null || run useradd --system --gid fanctl --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin fanctl

run install -d -m 0755 /opt/fanctl /opt/fanctl/bin
run install -m 0755 "$bin" /opt/fanctl/bin/fanctl.new
run mv -f /opt/fanctl/bin/fanctl.new /opt/fanctl/bin/fanctl
run install -m 0644 "$here/README.md" /opt/fanctl/README.md
run install -m 0644 "$here/60-fanctl.rules" /etc/udev/rules.d/60-fanctl.rules
run install -m 0644 "$here/fanctl.service" /etc/systemd/system/fanctl.service
[ -e /etc/fanctl.toml ] || run install -m 0644 "$here/fanctl.toml" /etc/fanctl.toml

echo "Validating the config before touching any service:"
run /opt/fanctl/bin/fanctl --config /etc/fanctl.toml check >/dev/null

run udevadm control --reload-rules
run udevadm trigger --subsystem-match=hwmon --attr-match=name=system76_io --action=change
run udevadm settle
pwm=""
for name in /sys/class/hwmon/hwmon*/name; do
    [ -f "$name" ] || continue
    if [ "$(cat "$name")" = system76_io ]; then
        [ -z "$pwm" ] || { echo "more than one system76_io hwmon device; refusing to guess" >&2; exit 1; }
        pwm="$(dirname "$name")/pwm2"
    fi
done
[ -n "$pwm" ] || { echo "no system76_io hwmon device found; is the Io board connected?" >&2; exit 1; }
run ls -l "$pwm"
[ "$(stat -c '%G %a' "$pwm")" = "fanctl 664" ] || { echo "udev rule did not apply to $pwm" >&2; exit 1; }
run systemctl daemon-reload

# From here on any failure must restore system76-power, whatever set -e would do.
trap rollback ERR
echo "Masking system76-power: it writes the same PWM file once a second."
run systemctl mask --now com.system76.PowerDaemon.service
run systemctl enable fanctl.service
run systemctl restart fanctl.service
healthy=no
for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    if systemctl is-active --quiet fanctl.service && [ -s /run/fanctl/state.json ] \
        && grep -q '"error":null' /run/fanctl/state.json \
        && grep -q '"panic":false' /run/fanctl/state.json; then
        healthy=yes
        break
    fi
done
if [ "$healthy" != yes ]; then
    [ -s /run/fanctl/state.json ] && cat /run/fanctl/state.json
    journalctl -u fanctl.service --no-pager --lines=10 || true
    rollback
fi
trap - ERR
run systemctl --no-pager --lines=3 status fanctl.service
echo
echo "Verify:"
echo "  ls -l $pwm                                       # root fanctl 0664"
echo "  sudo -u fanctl find /sys -writable 2>/dev/null   # only that pwm2"
echo "  systemd-analyze security fanctl.service"
echo "  cat /run/fanctl/state.json"
