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
            "  bluetooth-autoconnect                       "
            "Scan adapters and connect trusted devices once.\n"
            "  bluetooth-autoconnect --daemon              "
            "Run continuously, reacting to D-Bus events.\n"
            "  bluetooth-autoconnect --daemon --debug      "
            "Run as a daemon with debug logging.\n"
            "  bluetooth-autoconnect --daemon              "
            "--rescan-interval 60  Periodic scan every 60 s.\n"
            "  bluetooth-autoconnect --daemon              "
            "--rescan-interval 0   Disable periodic scanning.\n"
            "  bluetooth-autoconnect doctor                "
            "Run health checks and show PASS/FAIL output.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── global flags ──────────────────────────────────────────────────────
    parser.add_argument(
        "--daemon", action="store_true", help="Run as a background service."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG-level logging (structured, per-device fields).",
    )
    # --verbose is kept as a backward-compatible alias for --debug
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=argparse.SUPPRESS,  # hidden; use --debug instead
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=5,
        metavar="N",
        help="Connection attempts per device before giving up.",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=5,
        metavar="N",
        help="Maximum simultaneous connection attempts.",
    )
    parser.add_argument(
        "--rescan-interval",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help=(
            "Seconds between periodic background rescans that reconnect"
            " out-of-range devices. Default: 30. Set to 0 to disable."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    # ── subcommands ───────────────────────────────────────────────────────
    subparsers = parser.add_subparsers(dest="subcommand")
    subparsers.add_parser(
        "doctor",
        help=(
            "Run diagnostic health checks"
            " (bluetooth.service, D-Bus, BlueZ, adapters, devices)."
        ),
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
            policy=policy,
            max_concurrency=args.max_concurrency,
            rescan_interval=args.rescan_interval,
        )
        await daemon.run_forever()
        return 0

    daemon = AutoConnectDaemon(
        policy=policy,
        max_concurrency=args.max_concurrency,
        rescan_interval=0,  # one-shot mode: no background scanning
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

    # --verbose is a deprecated alias; honour it transparently
    debug = args.debug or args.verbose
    configure_logging(debug=debug)

    # ── doctor subcommand (sync, no asyncio needed) ───────────────────────
    if args.subcommand == "doctor":
        from .doctor import run_doctor

        return run_doctor()

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
