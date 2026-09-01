"""Logging configuration for bluetooth-autoconnect."""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(verbose: bool = False, debug: bool = False) -> logging.Logger:
    """Configure the root ``bluetooth_autoconnect`` logger.

    Args:
        verbose: Deprecated alias for *debug*; kept for backward compat.
        debug:   Enable DEBUG-level output.
    """
    level = logging.DEBUG if (verbose or debug) else logging.INFO
    logger = logging.getLogger("bluetooth_autoconnect")
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers.clear()

    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    stream_handler.setLevel(level)
    logger.addHandler(stream_handler)

    try:
        from systemd.journal import JournalHandler

        journal_handler = JournalHandler(SYSLOG_IDENTIFIER="bluetooth-autoconnect")
        journal_handler.setLevel(level)
        logger.addHandler(journal_handler)
    except ImportError:
        logger.debug(
            "python3-systemd not installed;"
            " relying on stdout capture for journal logging."
        )

    return logger
