# Fan dashboard

User approved a fan diagnostic with a nice TUI. Build a read-only Linux Python
program with no third-party dependencies, using curses for a colorful dashboard.
Discover hwmon sensors each refresh; never assume stable hwmon numbering. Show
fan RPM, raw PWM duty percentage, short RPM histories, temperatures and reported
limits, and the System76 USB controller and interface driver bindings. Display
missing or malformed measurements as unknown, zero RPM as stopped (possibly
intentional), and hardware faults as faults. Rotation is evidence of movement,
not proof of sufficient cooling. Never write hardware settings.

Provide q to quit, space to pause, r to refresh, scrolling for smaller terminals,
and --once/--json for noninteractive diagnostics. Keep acquisition separate from
presentation. Verify using fake sysfs fixtures and a real pseudo-terminal run.

This directory is initially empty and is not a Git repository. Work directly
here. Hardware checks must run locally, since the relevant controller is here.
