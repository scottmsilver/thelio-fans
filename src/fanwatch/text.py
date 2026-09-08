"""Sanitising of device-supplied text before it reaches a terminal."""

MAX_TEXT = 256


def sanitize(text: str) -> str:
    """Make device-supplied text safe for terminals: printable characters only, bounded."""
    return "".join(c if c.isprintable() else "?" for c in text[:MAX_TEXT])
