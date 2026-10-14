"""Unit tests for bluetooth_autoconnect.cli argument parsing."""

from __future__ import annotations

import pytest

from bluetooth_autoconnect.cli import build_parser


class TestArgumentParsing:
    def test_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        assert args.daemon is False
        assert args.verbose is False
        assert args.max_attempts == 5
        assert args.max_concurrency == 5

    def test_daemon_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--daemon"])
        assert args.daemon is True

    def test_verbose_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--verbose"])
        assert args.verbose is True

    def test_daemon_and_verbose_combined(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--daemon", "--verbose"])
        assert args.daemon is True
        assert args.verbose is True

    def test_max_attempts_override(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--max-attempts", "10"])
        assert args.max_attempts == 10

    def test_help_exits_cleanly(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.code == 0

    def test_version_exits_cleanly(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--version"])
        assert exc_info.value.code == 0
