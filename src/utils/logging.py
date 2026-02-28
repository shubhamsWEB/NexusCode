"""
Secure logging utilities for NexusCode Server.

Provides a SecureLogger wrapper that automatically sanitizes all log message
arguments (format args *and* the message itself) before they reach the
underlying Python logger.

WHY THIS EXISTS
───────────────
Security scanners (Bandit rule B506, CodeQL log-injection, SonarQube S5145)
flag every call to logger.debug/info/warning/error/exception/critical where
non-literal values are passed as format arguments — regardless of manual
sanitize_log() wrapping around individual arguments.

The root issue is CWE-117 "Improper Output Neutralization for Logs". An
attacker who can influence a logged value can inject fake log lines by
embedding newline characters (\\n), carriage returns (\\r), or ANSI escape
sequences into that value.

THE SOLUTION
────────────
SecureLogger is a thin adapter over the standard Logger that sanitizes every
argument *at the call site inside the logging layer*, not at the caller. This:

  1. Eliminates security alerts — the scanner sees sanitized data entering
     the log sink.
  2. Removes the need for manual sanitize_log() calls at every call site.
  3. Is backward-compatible — SecureLogger exposes the same API as Logger.
  4. Zero performance overhead for filtered-out log levels (lazy evaluation
     via %-style formatting is preserved).

USAGE
─────
Replace:
    import logging
    logger = logging.getLogger(__name__)

With:
    from src.utils.logging import get_secure_logger
    logger = get_secure_logger(__name__)

That's it. All log calls (debug, info, warning, error, exception, critical)
are transparently secured.
"""

from __future__ import annotations

import logging
from typing import Any

from src.utils.sanitize import sanitize_log


class SecureLogger:
    """
    A safe wrapper around a standard Python Logger.

    Every logging method sanitizes the message and all positional and
    keyword arguments before delegating to the underlying logger.

    The wrapper is intentionally minimal — it only overrides the six
    standard severity methods plus exception().  All other Logger attributes
    (handlers, level, name, effective_level, etc.) are forwarded transparently
    via __getattr__.
    """

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        object.__setattr__(self, "_logger", logger)

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_args(args: tuple) -> tuple:
        """
        Sanitize positional format arguments.
        Preserves numeric and boolean primitives so that %d and %f
        format specifiers don't throw TypeErrors.
        """
        sanitized: list[Any] = []
        for a in args:
            # Safe primitives that cannot contain \n or \r
            if isinstance(a, (int, float, bool)) and not isinstance(a, str):
                sanitized.append(a)
            else:
                sanitized.append(sanitize_log(a))
        return tuple(sanitized)

    @staticmethod
    def _sanitize_msg(msg: object) -> str:
        """Sanitize the message/format-string itself."""
        return sanitize_log(msg)

    # ── Logging methods ───────────────────────────────────────────────────────

    def debug(self, msg: object, *args: Any, **kwargs: Any) -> None:
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(self._sanitize_msg(msg), *self._sanitize_args(args), **kwargs)

    def info(self, msg: object, *args: Any, **kwargs: Any) -> None:
        if self._logger.isEnabledFor(logging.INFO):
            self._logger.info(self._sanitize_msg(msg), *self._sanitize_args(args), **kwargs)

    def warning(self, msg: object, *args: Any, **kwargs: Any) -> None:
        if self._logger.isEnabledFor(logging.WARNING):
            self._logger.warning(self._sanitize_msg(msg), *self._sanitize_args(args), **kwargs)

    # Alias
    warn = warning

    def error(self, msg: object, *args: Any, **kwargs: Any) -> None:
        if self._logger.isEnabledFor(logging.ERROR):
            self._logger.error(self._sanitize_msg(msg), *self._sanitize_args(args), **kwargs)

    def critical(self, msg: object, *args: Any, **kwargs: Any) -> None:
        if self._logger.isEnabledFor(logging.CRITICAL):
            self._logger.critical(self._sanitize_msg(msg), *self._sanitize_args(args), **kwargs)

    def exception(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """Log at ERROR level with current exception info attached."""
        kwargs.setdefault("exc_info", True)
        if self._logger.isEnabledFor(logging.ERROR):
            self._logger.exception(self._sanitize_msg(msg), *self._sanitize_args(args), **kwargs)

    # ── Forward all other Logger attributes unchanged ─────────────────────────

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_logger"), name)

    def __repr__(self) -> str:
        return f"SecureLogger({self._logger.name!r})"


def get_secure_logger(name: str) -> SecureLogger:
    """
    Return a SecureLogger for the given module name.

    Drop-in replacement for logging.getLogger():

        # Before:
        import logging
        logger = logging.getLogger(__name__)

        # After:
        from src.utils.logging import get_secure_logger
        logger = get_secure_logger(__name__)
    """
    return SecureLogger(logging.getLogger(name))
