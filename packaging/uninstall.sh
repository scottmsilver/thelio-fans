#!/usr/bin/env bash
# Remove the fanctl service and give the fan back to system76-power. Run with sudo.
set -euo pipefail
run() { printf '+ %s\n' "$*"; "$@"; }
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
run systemctl disable --now fanctl.service || echo "fanctl was not running"
run rm -f /etc/systemd/system/fanctl.service /etc/udev/rules.d/60-fanctl.rules
run systemctl daemon-reload
run udevadm control --reload-rules
for name in /sys/class/hwmon/hwmon*/name; do
    if grep -q '^system76_io$' "$name" 2>/dev/null; then
        pwm="$(dirname "$name")/pwm2"
        run chgrp root "$pwm"
        run chmod 0644 "$pwm"
    fi
done
run systemctl unmask com.system76.PowerDaemon.service
run systemctl start com.system76.PowerDaemon.service
run rm -rf /opt/fanctl
echo "/etc/fanctl.toml and the fanctl user were left in place; remove with: userdel fanctl"
