"""Diagnostic health-check command: ``bluetooth-autoconnect doctor``.

Performs a series of checks and prints PASS/FAIL for each one, making
it straightforward to spot configuration problems without reading logs.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import dataclass, field

# Colour codes — disabled automatically when stdout is not a TTY.
_IS_TTY = sys.stdout.isatty()
_GREEN = "\033[32m" if _IS_TTY else ""
_RED = "\033[31m" if _IS_TTY else ""
_YELLOW = "\033[33m" if _IS_TTY else ""
_BOLD = "\033[1m" if _IS_TTY else ""
_RESET = "\033[0m" if _IS_TTY else ""


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    warning: bool = False  # soft failure — printed in yellow


@dataclass
class DoctorReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    @property
    def all_passed(self) -> bool:
        return all(r.passed or r.warning for r in self.results)

    @property
    def has_failures(self) -> bool:
        return any(not r.passed and not r.warning for r in self.results)


# ── individual checks ─────────────────────────────────────────────────────────


def _check_systemd_unit(unit: str) -> CheckResult:
    """Check whether a systemd unit is active."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            timeout=5,
            capture_output=True,
        )
        if result.returncode == 0:
            return CheckResult(name=unit, passed=True, detail="active")
        # Try to get the actual status string for the detail field.
        status = subprocess.run(
            ["systemctl", "is-active", unit],
            timeout=5,
            capture_output=True,
            text=True,
        )
        detail = status.stdout.strip() or "not active"
        return CheckResult(name=unit, passed=False, detail=detail)
    except FileNotFoundError:
        return CheckResult(
            name=unit,
            passed=False,
            detail="systemctl not found — not a systemd system?",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(name=unit, passed=False, detail="timeout")


def _check_dbus() -> CheckResult:
    """Verify the D-Bus system bus socket is reachable."""
    import socket as _socket

    for path in (
        "/run/dbus/system_bus_socket",
        "/var/run/dbus/system_bus_socket",
    ):
        try:
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(path)
            sock.close()
            return CheckResult(
                name="D-Bus system bus",
                passed=True,
                detail=f"socket reachable at {path}",
            )
        except OSError:
            continue

    return CheckResult(
        name="D-Bus system bus",
        passed=False,
        detail=(
            "system bus socket not reachable"
            " (/run/dbus/system_bus_socket,"
            " /var/run/dbus/system_bus_socket)"
        ),
    )


async def _check_bluez_async() -> tuple[CheckResult, list, list]:
    """Connect to BlueZ, enumerate adapters and devices.

    Returns a 3-tuple of (bluez_check, adapters, devices).
    """
    from .dbus_client import BlueZClient
    from .exceptions import BlueZNotAvailableError, DBusConnectionError

    client = BlueZClient()
    try:
        await client.connect()
    except DBusConnectionError as exc:
        return (
            CheckResult(
                name="BlueZ available",
                passed=False,
                detail=f"D-Bus connection failed: {exc}",
            ),
            [],
            [],
        )
    except BlueZNotAvailableError as exc:
        return (
            CheckResult(
                name="BlueZ available",
                passed=False,
                detail=str(exc),
            ),
            [],
            [],
        )

    try:
        adapters = await client.get_adapters()
        devices = await client.get_devices()
    except Exception as exc:  # noqa: BLE001
        await client.close()
        return (
            CheckResult(
                name="BlueZ available",
                passed=False,
                detail=f"Error querying managed objects: {exc}",
            ),
            [],
            [],
        )
    finally:
        await client.close()

    return (
        CheckResult(name="BlueZ available", passed=True, detail="org.bluez found"),
        adapters,
        devices,
    )


# ── report rendering ──────────────────────────────────────────────────────────


def _render(report: DoctorReport) -> None:
    """Print the report to stdout."""
    print(f"\n{_BOLD}bluetooth-autoconnect doctor{_RESET}\n")
    for r in report.results:
        if r.passed:
            tag = f"{_GREEN}[PASS]{_RESET}"
        elif r.warning:
            tag = f"{_YELLOW}[WARN]{_RESET}"
        else:
            tag = f"{_RED}[FAIL]{_RESET}"

        line = f"  {tag} {r.name}"
        if r.detail:
            line += f"  — {r.detail}"
        print(line)

    print()
    if report.has_failures:
        print(f"  {_RED}{_BOLD}Some checks failed." f"  See details above.{_RESET}\n")
    else:
        print(f"  {_GREEN}{_BOLD}All checks passed.{_RESET}\n")


# ── public entry point ────────────────────────────────────────────────────────


def run_doctor() -> int:
    """Run all health checks and return an exit code (0 = all passed)."""
    report = DoctorReport()

    # 1. bluetooth.service
    report.add(_check_systemd_unit("bluetooth.service"))

    # 2. D-Bus system bus
    report.add(_check_dbus())

    # 3. BlueZ + adapters + devices  (one async call for all three)
    bluez_result, adapters, devices = asyncio.run(_check_bluez_async())
    report.add(bluez_result)

    if bluez_result.passed:
        # 4. Adapters
        if not adapters:
            report.add(
                CheckResult(
                    name="Bluetooth adapter",
                    passed=False,
                    detail="no adapters detected",
                )
            )
        else:
            for adapter in adapters:
                status = "powered" if adapter.powered else "not powered"
                report.add(
                    CheckResult(
                        name=f"Adapter {adapter.name}",
                        passed=adapter.powered,
                        detail=(
                            f"{status} — address={adapter.address}"
                            f" path={adapter.path}"
                        ),
                        warning=not adapter.powered,
                    )
                )

        # 5. Paired devices
        paired = [d for d in devices if d.paired]
        if not paired:
            report.add(
                CheckResult(
                    name="Paired devices",
                    passed=False,
                    detail="no paired devices found",
                    warning=True,
                )
            )
        else:
            report.add(
                CheckResult(
                    name="Paired devices",
                    passed=True,
                    detail=f"{len(paired)} paired device(s) found",
                )
            )

        # 6. Trusted devices + connection state
        trusted = [d for d in devices if d.trusted and d.paired]
        if not trusted:
            report.add(
                CheckResult(
                    name="Trusted devices",
                    passed=False,
                    detail=(
                        "no paired+trusted devices found —"
                        " run: bluetoothctl trust <MAC>"
                    ),
                    warning=True,
                )
            )
        else:
            for device in trusted:
                if device.connected:
                    detail = f"connected — mac={device.address}"
                else:
                    detail = f"not connected — mac={device.address}"
                report.add(
                    CheckResult(
                        name=f"Trusted device: {device.name}",
                        passed=device.connected,
                        detail=detail,
                        warning=not device.connected,
                    )
                )

    _render(report)
    return 0 if not report.has_failures else 1
