"""Reading the fanctl service's status file. Written by a different, unprivileged user,
so it is read defensively: regular files only, bounded size, a JSON object at the top."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

MAX_STATE_BYTES = 64 * 1024


def read_state(path: Path) -> dict[str, object] | None:
    """The fanctl state file as a dict, or None if absent, malformed or not a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, MAX_STATE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_STATE_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None
