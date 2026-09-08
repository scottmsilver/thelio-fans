# Why the Thelio Io board kept dropping off USB

System76 Thelio Mira, 2026-09-07. Board: System76 Io v1 (ATmega32U4, USB `1209:1776`,
firmware 1.0.5). Kernel 7.0.11-76070011, driver system76-io-dkms 1.0.4, fan daemon
system76-power 1.2.8.

**Resolved at 20:52.** The USB0 cable on the Io board was not seated. Reseating it
brought the board back, and it stayed up with zero errors from then on.

The cables had been reordered on the Io board during the 16:35 to 17:12 power-off. From
then on the board's microcontroller, which is powered only through that USB cable,
reset within seconds to a minute of every boot. The CPU fans stopped at the same time
and now run directly off the motherboard's CPU_FAN header. Nothing in software changed
and nothing in software could have caused either symptom.

## Outcome

| Check | Before (20:39 boot) | After USB0 reseat (20:52) |
|---|---|---|
| Io on USB | gone: dropped 7 s after enumeration | present: driver bound, PWM writes answered |
| Write failures | 112 on the earlier boot | 0 |
| Intake fan via Io | unreadable | 630–660 rpm at 36 % duty, so the board's 12 V is fine |
| CPU fans | not spinning, package 87–95 °C | spinning on the motherboard header, package 41–58 °C |
| CPUOUT0 tach | — | 0 rpm, nothing is plugged into it now |

**Still open.** Whether the Io's CPUOUT0 header can still drive a fan. On the 20:39
boot, with the two CPU cables in their documented positions, the CPU fans did not
spin. That points at CPUOUT0, the splitter board or that cable, and the hours the board
ran miswired may have damaged the mux behind CPUOUT0. Test when convenient by plugging
one fan into CPUOUT0 and reading fan1 in the dashboard. Leaving the CPU fans on the
motherboard header is a perfectly good permanent arrangement: the BIOS controls them,
and the Io only ever passed that signal through.

## The Io v1 board's connectors, top to bottom

From System76's Mira wiring guide and the board's KiCad design. Seen through the
front-right 2.5" drive cutout, the connectors run in a single column down the board's
edge. The four white 4-pin headers are *not* four fan headers.

| # | Connector | What it is |
|---|---|---|
| 1 | POWER0 | 4-pin floppy plug from the PSU. 12 V for the fans, 5 V/12 V for the SATA drives. Does not power the microcontroller. |
| 2 | INTAKE0 | Bottom case fan. Normal 4-pin fan header. |
| 3 | CPUOUT0 | **Cable up to the CPU fan splitter** on the top crossbar, which feeds both CPU fans. Powered 12 V. PWM comes from a mux: motherboard signal until the host takes over, then the Io's own. *Now unused: the CPU fans run from the motherboard header.* |
| 4 | CPUIN0 | **Cable to the motherboard's CPU_FAN header.** Looks like a fan header but pin 2 is unconnected: no 12 V. A fan plugged here never spins. *Now unused: that cable feeds the splitter directly.* |
| 5 | DATA0–3 | Four SATA data sockets to motherboard ports. |
| 6 | PFP0 | Front power button cable, red/black/blue. Red on top. |
| 7 | USB0 | USB cable, red/white/green/black. Red on top. **The microcontroller's only power source, and the loose one.** Other end sits on the 4-hole row of the motherboard's USB 2.0 header. |
| 8 | PMB0 | Cable to the motherboard's PWR_SW / PWR_LED front-panel pins, red/black/blue. Red on top. |

Headers 6, 7 and 8 are three identical 1×4 pin headers stacked at the bottom, and
headers 2, 3 and 4 are three identical white fan headers. Those two groups are where a
reordering goes wrong. There is no EXHAUST header on this board despite the driver
exposing a third fan.

Photo with these positions numbered:
[System76 Mira wiring guide image](https://system76.com/tech-docs/originals/models/thelio-mira-b1.0/img/thelio-io-wiring.webp).
Pin nets from [thelio-io-hardware](https://github.com/system76/thelio-io-hardware/tree/a8e166cec9112d38d2bbe31a314689a8d7723ac8)
and [thelio-io-firmware PINS.md](https://github.com/system76/thelio-io-firmware/blob/master/PINS.md).

## Why a loose USB cable looks like a crashing board

- **Only power source.** In the v1 board's netlist, USB0 pin 1 is the ATmega32U4's VCC.
  The floppy plug's 5 V never reaches the chip. Every sag on a marginal USB0 contact is
  a full reset.
- **No watchdog.** The firmware disables the watchdog at init and only re-enables it to
  jump to the bootloader. A browned-out chip stays dead until power is removed, which is
  why the board vanished for hours at a time.
- **What the host sees.** Enumerate, driver's `IoRSET` answered, then the chip resets:
  writes time out (110), one garbled reply (`Unexpected LF`), USB disconnect,
  re-enumerate, and finally `connect-debounce failed` when the port's connect line
  bounces for the full 2 s window.
- **Why soft shutdowns didn't help.** Internal USB headers on this Gigabyte board stay on
  5 V standby while the machine is off, so a one-minute shutdown never actually reset
  the chip. Only reseating the cable, or a PSU switch-off, does.

## Timeline of boots

| Boot | Duration | Io board |
|---|---|---|
| Jul 31 05:22 → Sep 7 16:35 | 38 days | clean: zero Io messages in the retained log (Sep 2 to Sep 7) |
| *powered off 16:35 → 17:12* | | *cables reordered on the Io board* |
| Sep 7 17:12 | 2 h 24 m | lost: 5 disconnects in the first 2 min, debounce failure at 17:15:17, gone for the remaining 2 h |
| Sep 7 19:28 | 8 min | lost: 5 disconnects, debounce failure at 19:32:12 |
| Sep 7 19:40 | 9 min | hung: stayed on the bus but sent garbage, then timed out; a manual `usbreset` at 19:43 got 8 reset attempts, all timed out |
| Sep 7 19:52 | 42 min | lost: 112 write timeouts, 5 disconnects, debounce failure at 20:03:25; CPU package 87–92 °C with no CPU fan |
| Sep 7 20:39 | running | lost at 20:40:00, 7 s after enumeration, package to 95 °C. CPU fans moved to the motherboard header. Back at 20:52:13 after reseating USB0 live; stable since |

All boots on kernel 7.0.11.

## What the failure looked like in the log

```
19:52:15 usb 1-5.1: new full-speed USB device number 4       enumerates fine
19:52:15 system76-io 1-5.1:1.1: trying reset: 0             IoRSET only clears a flag; it does not reboot the chip
19:52:19 usb 1-5.1: USB disconnect, device number 4          chip resets 4 s later
19:52:20 usb 1-5.1: new full-speed USB device number 9
19:52:22 usb 1-5.1: USB disconnect, device number 9          and again
   ...     (device 10 same; device 11 stays up from 19:52:24)
19:53:29 usb 1-5.1: io_dev_set_duty failed: 110: io_dev_write   chip not draining USB: firmware loop stalled
   ...     (repeats every 4–5 s for 3 minutes, one pair per system76-power tick)
19:56:41 usb 1-5.1: USB disconnect, device number 11
20:03:19 usb 1-5.1: new full-speed USB device number 12
20:03:23 usb 1-5.1: USB disconnect, device number 12
20:03:25 usb 1-5-port1: connect-debounce failed            connect line bounced for 2 s; hub abandons the port
```

## Ruled out along the way

- **Kernel update.** Same 7.0.11 kernel in the clean 38-day boot and in every failing
  boot. Installed Jul 31, not that day.
- **Driver / DKMS.** Same system76-io-dkms 1.0.4 build, built cleanly Jul 31 for 7.0.11.
  The driver source has no reset-device, unbind, port control, autosuspend, timers or
  workqueues, so it cannot take a device off the bus.
- **Package changes.** Only google-chrome changed since Sep 5.
- **Firmware.** The firmware daemon lists the Io each boot and offers no update. Io v1
  firmware last changed in 2021. `IoRSET` does not reboot the chip, so every disconnect
  in the log was a real reset.
- **Noisy fan tach.** The firmware polls tach pins with a 1 ms debounce. No interrupts,
  so a bad tach signal cannot hang it.
- **Hub over-current.** The hub port's over-current counter stayed at zero throughout,
  so the board was not drawing too much current.
- **Upstream bugs.** No GitHub issue, forum or Reddit thread reproduces this signature.

## Current wiring

1. **CPU fans:** the cable that used to run from the motherboard's CPU_FAN header to
   CPUIN0 now runs from that header straight up to the crossbar splitter. The BIOS
   drives both CPU fans. The Io's CPUF reading is 0 by design.
2. **Io board:** POWER0, INTAKE0, PFP0, USB0 and PMB0 connected; CPUOUT0 and CPUIN0
   empty. USB0 reseated and holding.
3. **To confirm it stays healthy** after a cold boot, run `uv run fanwatch --log` and
   check the Io board never goes ABSENT.

## Sources checked

- Kernel journal for the last six boots, `last -x reboot`, dpkg and apt logs, dkms build
  logs, sysfs USB, hub-port and hwmon trees, system76-power and system76-firmware-daemon
  journals.
- Driver source at `/usr/src/system76-io-1.0.4*/`, read in full. Firmware source
  `src/cmd/thelio.c`, `device.c`, `tach.c`, `PINS.md`, `PROTOCOL.md` from
  system76/thelio-io-firmware.
- [System76 Thelio Mira b1.0 wiring guide](https://system76.com/tech-docs/models/thelio-mira-b1.0/repairs/),
  [Mira r1.0 repair page](https://system76.com/tech-docs/models/thelio-mira-r1.0/repairs.html),
  [Io v1 KiCad design](https://github.com/system76/thelio-io-hardware/tree/a8e166cec9112d38d2bbe31a314689a8d7723ac8),
  [pop-os/system76-io-dkms](https://github.com/pop-os/system76-io-dkms),
  [cosmic-epoch #2153](https://github.com/pop-os/cosmic-epoch/issues/2153).

Log excerpts are verbatim except where marked as condensed.
