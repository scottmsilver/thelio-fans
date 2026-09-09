# Kernel drivers and host configuration

Two drivers feed the dashboard, and one of them is also what fanctl writes through. One
ships with Pop!_OS, one is vendored here. The NVIDIA section at the end records two
host-side changes that are not part of this repository but that fanwatch depends on.

## system76-io (ships with Pop!_OS)

Package `system76-io-dkms`, module `system76_io`. Drives the Thelio Io v1 board
(USB `1209:1776`, ATmega32U4) over a CDC-ACM style protocol and exposes hwmon
`fan1` (CPUF) and `fan2` (INTF) with `pwm1`/`pwm2`. Until 2026‑09‑08 the
`system76-power` daemon wrote both PWM values once a second from a stepped curve; it is
now masked and [fanctl](../fanctl/README.md) writes `pwm2` alone. The driver's
`pwm*_enable` is a stub with no automatic mode, so whatever was last written is what the
fan runs at.

Facts that matter when it misbehaves, established on 2026‑09‑07:

- The Io microcontroller is powered **only** by the 5 V line of its USB0 cable. A
  loose USB0 plug resets the chip; the host sees enumerate, write timeouts (`110`),
  USB disconnect, then `connect-debounce failed` on the hub port.
- The firmware has no watchdog, and internal USB headers on this board stay powered
  while the machine is soft-off, so a normal shutdown does not reset a wedged chip.
  Reseat the cable or switch the PSU off for 30 s.
- The driver cannot cause a bus disconnect; it does synchronous bulk transfers only.
- Board connectors, top to bottom through the front drive cutout: POWER0, INTAKE0,
  CPUOUT0 (to the CPU fan splitter), CPUIN0 (to the motherboard CPU_FAN header,
  **no 12 V** on pin 2), four SATA data sockets, then PFP0, USB0, PMB0 with the red
  wire on top. Since 2026‑09‑07 the CPU fans are on the motherboard header and
  CPUOUT0/CPUIN0 are unused.

Full write-up: [docs/2026-09-07-thelio-io-diagnosis.md](../docs/2026-09-07-thelio-io-diagnosis.md)

## it87 (vendored, out of tree)

`it87/` is a git checkout of https://github.com/frankcrawford/it87, the maintained
fork of the hwmon driver for ITE Super I/O chips. The in-kernel `it87` stops at
IT8628E/IT8792E; this board's chip is an **IT8689E** (found by `sensors-detect`,
"Found unknown chip with ID 0x8689"), which only the out-of-tree driver supports.
It exposes hwmon `it8689` with `fan1`–`fan6`, `pwm1`–`pwm6` and six temperatures.
License GPL-2.0.

Installed on this host on 2026‑09‑08 with:

```sh
cd drivers/it87
make                       # sanity build against the running kernel
sudo insmod ./it87.ko ignore_resource_conflict=1   # trial load; sudo rmmod it87 undoes it
sudo make dkms             # register with DKMS so it rebuilds on kernel updates
sudo cp ../etc/modprobe.d/it87.conf /etc/modprobe.d/
sudo cp ../etc/modules-load.d/it87.conf /etc/modules-load.d/
```

`etc/` holds copies of the two config files as installed:

- `modprobe.d/it87.conf`: `options it87 ignore_resource_conflict=1`. The BIOS's
  ACPI tables claim the chip's I/O range; this tells the driver to read it anyway.
  The driver's README says it is required on Gigabyte boards. It means Linux and the
  firmware both touch the chip, which is why the kernel makes you opt in.
- `modules-load.d/it87.conf`: loads the module at boot.

Update: `git -C drivers/it87 pull`, then `sudo make dkms` again. Remove:
`sudo dkms remove it87/<version> --all` and delete the two files under `/etc`.

The it87 driver is read-only as used here. Neither fanwatch nor fanctl writes any
`it8689` attribute, and its `pwm*_enable` values stay at 2 (automatic, BIOS curve). The
CPU fans on the motherboard header are the BIOS's to drive.

## NVIDIA (host configuration, outside this repository)

Two changes were made on this host on 2026‑09‑08. Neither lives in the repository, so
they are recorded here for the next time the driver package or the kernel changes.

**Persistence mode.** Pop!_OS runs `nvidia-persistenced` with `--no-persistence-mode`,
a laptop default that lets a discrete GPU power off. On a desktop that cannot do that,
it means every NVML or `nvidia-smi` client tears the GPU down when it exits and the next
client rebuilds it: about 1.5 s of kernel time per poll, and the card stuck in P0. That
is what made the old `system76-power` fan poll so expensive. The dashboard, `--log` and
fanstress hold one NVML session for their whole run (`Collector` in
`src/fanwatch/probe.py`), so they are immune to the setting; `fanwatch --once`,
`--json` and any other one-shot client are not. Keep persistence on with a drop-in:

```ini
# /etc/systemd/system/nvidia-persistenced.service.d/persistence-mode.conf
[Service]
ExecStart=
ExecStart=/usr/bin/nvidia-persistenced --user nvidia-persistenced --persistence-mode --verbose
```

Apply with `sudo systemctl daemon-reload && sudo systemctl restart nvidia-persistenced`;
check with `nvidia-smi --query-gpu=persistence_mode --format=csv`. Revert by deleting
the file and repeating the two commands.

**Module rebuild on kernel 7.0.** Pop's `nvidia-dkms` applies a `thunk-Kbuild.patch`
that strips objtool from the build on kernel 7.0, leaving `nvidia.ko` without its
`.return_sites` section and unable to load. `~/scripts/nvidia-rebuild-no-thunk-patch.sh`
(not in this repository) comments the patch out of `dkms.conf`, rebuilds and reinstalls
the module for the running kernel, and verifies the section is back. A driver package
update restores `dkms.conf`, so **re-run the script after any NVIDIA package update or
new kernel**, then reboot. Neither fanwatch nor fanctl depends on this beyond needing a
loadable NVIDIA module for the GPU rows.
