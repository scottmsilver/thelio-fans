# Fan Dashboard Implementation Plan

> Execute in this session, with focused review and verification.

**Goal:** Provide a live, read-only fan and USB diagnostic for this Thelio Mira.

**Architecture:** `fan_probe.py` collects typed snapshots from Linux sysfs.
`fan_tui.py` renders curses panels and offers text/JSON snapshots.

**Tech Stack:** Python 3 standard library (curses, dataclasses, unittest).

1. Write `tests/test_probe.py` fixtures for rotation, zero RPM, missing or bad
   readings, hardware faults, temperature limits, USB bindings and unplugging.
   Run `python3 -m unittest discover -s tests -v` to verify missing implementation.
2. Implement `fan_probe.py`: rediscover sensors, preserve read errors, identify
   controller by USB VID/PID, separate faults from unknown and stopped states.
   Run the fixture tests until passing.
3. Implement `fan_tui.py`: colorful responsive panels, RPM histories, sensor table,
   driver panel, scrolling, pause/refresh/quit, --once and --json. Validate interval
   arguments and gracefully report non-TTY use.
4. Write `README.md` with launch commands, controls, limitations and kernel source.
5. Run unit tests, real hardware snapshots and PTY smoke tests including resize,
   pause/resume and quit. Review implementation and resolve material findings.
