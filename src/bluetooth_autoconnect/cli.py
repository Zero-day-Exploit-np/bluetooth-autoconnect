"""Command-line interface for bluetooth-autoconnect."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import sys
from typing import Any

from . import __version__
from .connector import RetryPolicy
from .daemon import AutoConnectDaemon
from .exceptions import (
    BluetoothAutoConnectError,
    BlueZNotAvailableError,
    DBusConnectionError,
)
from .logging_setup import configure_logging

logger = logging.getLogger("bluetooth_autoconnect.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bluetooth-autoconnect",
        description=(
            "Automatically detect all paired and trusted Bluetooth devices"
            " and reconnect them."
        ),
        epilog=(
            "Examples:\n"
            "  bluetooth-autoconnect                 "
            "Scan powered adapters and connect trusted devices once.\n"
            "  bluetooth-autoconnect --daemon         "
            "Run continuously, reconnecting devices as events occur.\n"
            "  bluetooth-autoconnect --daemon --verbose\n"
            "                                         "
            "Run as a daemon with debug logging.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--daemon", action="store_true", help="Run as a background service."
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable debug logging."
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=5,
        metavar="N",
        help="Maximum connection attempts per device before giving up.",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=5,
        metavar="N",
        help="Maximum number of devices to connect simultaneously.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    policy = RetryPolicy(max_attempts=args.max_attempts)

    async def _await_if_needed(result: Any) -> Any:
        if inspect.isawaitable(result):
            return await result
        return result

    if args.daemon:
        daemon = AutoConnectDaemon(
            policy=policy, max_concurrency=args.max_concurrency
        )
        await daemon.run_forever()
        return 0

    daemon = AutoConnectDaemon(
        policy=policy, max_concurrency=args.max_concurrency
    )
    try:
        await _await_if_needed(daemon.client.connect())
        results = await daemon.run_once()
    finally:
        await _await_if_needed(daemon.client.close())

    if not results:
        return 0
    return 0 if all(results.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(verbose=args.verbose)

    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        return 130
    except (DBusConnectionError, BlueZNotAvailableError) as exc:
        logger.error(str(exc))
        return 2
    except BluetoothAutoConnectError as exc:
        logger.error("Unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
