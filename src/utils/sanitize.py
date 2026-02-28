"""
Log sanitization utilities.

Prevents log injection (CWE-117) by stripping newlines, carriage returns,
and other control characters from user-provided values before they reach
log formatters.

Usage:
    from src.utils.sanitize import sanitize_log

    logger.info("query=%s", sanitize_log(user_query))
"""

from __future__ import annotations

import re

# Match ASCII control chars (0x00-0x1F, 0x7F) except tab (0x09).
# Explicitly includes \x0d (carriage return / \r) to block CR-only log injection.
# Breakdown:
#   \x00-\x08  NUL … BS  (skip \x09 = tab — preserved for readability)
#   \x0a       LF  (\n)
#   \x0b       VT
#   \x0c       FF
#   \x0d       CR  (\r)  ← explicit: prevents carriage-return log injection
#   \x0e-\x1f  SO … US
#   \x7f       DEL
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0a-\x0d\x0e-\x1f\x7f]")


def sanitize_log(value: object) -> str:
    """
    Sanitize a value for safe inclusion in log messages.

    Replaces newlines, carriage returns, and other control characters
    with a visible placeholder to prevent log injection / log forging.
    """
    s = str(value)
    return _CONTROL_CHARS_RE.sub("\\\\n", s)
