"""Logging configuration for bluetooth-autoconnect.

Logs are always written to stdout (so ``journalctl`` captures them when
run under systemd, and so they're visible when run interactively). If the
optional ``systemd`` Python bindings are installed, we additionally attach
a native journal handler that preserves structured fields and priority
levels; this is a soft dependency and its absence is not an error.
"""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(verbose: bool = False) -> logging.Logger:
    """Configure and return the root application logger.

    Args:
        verbose: If True, sets the log level to DEBUG; otherwise INFO.

    Returns:
        The configured ``bluetooth_autoconnect`` logger instance.
    """
    level = logging.DEBUG if verbose else logging.INFO

    logger = logging.getLogger("bluetooth_autoconnect")
    logger.setLevel(level)
    logger.propagate = False

    # Avoid duplicate handlers if configure_logging() is called twice
    # (e.g. in tests).
    logger.handlers.clear()

    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    stream_handler.setLevel(level)
    logger.addHandler(stream_handler)

    try:
        from systemd.journal import JournalHandler  # type: ignore

        journal_handler = JournalHandler(SYSLOG_IDENTIFIER="bluetooth-autoconnect")
        journal_handler.setLevel(level)
        logger.addHandler(journal_handler)
    except ImportError:
        # The 'systemd' python bindings (python3-systemd) are optional.
        # stdout logging is sufficient because systemd captures a
        # service's stdout/stderr into the journal automatically.
        logger.debug(
            "python3-systemd not installed; relying on stdout capture "
            "for journal logging."
        )

    return logger
