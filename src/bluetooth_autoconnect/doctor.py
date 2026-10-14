"""Diagnostic health-check command: ``bluetooth-autoconnect doctor``.

Performs a series of checks and prints PASS/FAIL for each one, making
it straightforward to spot configuration problems without reading logs.

The check that queries the Bluetooth stack now accepts any
:class:`~bluetooth_autoconnect.backends.BluetoothBackend` implementation
so it works correctly on both Linux (BlueZ) and Windows (WinRT).
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backends import BluetoothBackend

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


# ── Platform-specific checks ──────────────────────────────────────────────────


def _check_systemd_unit(unit: str) -> CheckResult:
    """Check whether a systemd unit is active (Linux only)."""
    import subprocess

    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            timeout=5,
            capture_output=True,
        )
        if result.returncode == 0:
            return CheckResult(name=unit, passed=True, detail="active")
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
    """Verify the D-Bus system bus socket is reachable (Linux only)."""
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


def _check_winrt_available() -> CheckResult:
    """Verify the winrt package is importable (Windows only)."""
    try:
        import winrt.windows.devices.bluetooth  # noqa: F401

        return CheckResult(
            name="WinRT Bluetooth",
            passed=True,
            detail="winrt package available",
        )
    except ImportError:
        return CheckResult(
            name="WinRT Bluetooth",
            passed=False,
            detail=(
                "winrt package not found. "
                "Install: pip install bluetooth-autoconnect[windows]"
            ),
        )


# ── Backend check ─────────────────────────────────────────────────────────────


async def _check_backend_async(
    backend: BluetoothBackend | None = None,
) -> tuple[CheckResult, list, list]:
    """Connect to the Bluetooth backend and enumerate adapters/devices.

    Parameters
    ----------
    backend:
        A :class:`~bluetooth_autoconnect.backends.BluetoothBackend` instance
        to use.  When ``None``, :func:`~bluetooth_autoconnect.backends.create_backend`
        selects the platform-appropriate one automatically.

    Returns
    -------
    tuple[CheckResult, list[Adapter], list[Device]]
    """
    from .backends import create_backend
    from .exceptions import BackendError

    if backend is None:
        try:
            backend = create_backend()
        except BackendError as exc:
            return (
                CheckResult(
                    name="Bluetooth backend",
                    passed=False,
                    detail=str(exc),
                ),
                [],
                [],
            )

    try:
        await backend.connect()
    except BackendError as exc:
        return (
            CheckResult(
                name="Bluetooth backend",
                passed=False,
                detail=f"Connection failed: {exc}",
            ),
            [],
            [],
        )

    try:
        adapters = await backend.get_adapters()
        devices = await backend.get_devices()
    except Exception as exc:  # noqa: BLE001
        await backend.close()
        return (
            CheckResult(
                name="Bluetooth backend",
                passed=False,
                detail=f"Error querying devices: {exc}",
            ),
            [],
            [],
        )
    finally:
        await backend.close()

    return (
        CheckResult(
            name="Bluetooth backend",
            passed=True,
            detail="backend available",
        ),
        adapters,
        devices,
    )


# ── Report rendering ──────────────────────────────────────────────────────────


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
        print(f"  {_RED}{_BOLD}Some checks failed.  See details above.{_RESET}\n")
    else:
        print(f"  {_GREEN}{_BOLD}All checks passed.{_RESET}\n")


# ── Public entry point ────────────────────────────────────────────────────────


async def _check_bluez_async(
    backend: BluetoothBackend | None = None,
) -> tuple[CheckResult, list, list]:
    """Alias for :func:`_check_backend_async` — preserved for backward compatibility.

    Existing tests patch this name on the doctor module.
    """
    return await _check_backend_async(backend)


def run_doctor(backend: BluetoothBackend | None = None) -> int:
    """Run all health checks and return an exit code (0 = all passed).

    Parameters
    ----------
    backend:
        Optional pre-constructed backend to use for the device checks.
        Mainly useful for testing.  When ``None``, the platform-appropriate
        backend is selected automatically.
    """
    from .backends import get_platform_name

    report = DoctorReport()
    platform = get_platform_name()

    # ── Platform-specific pre-checks ──────────────────────────────────────
    if platform == "linux":
        report.add(_check_systemd_unit("bluetooth.service"))
        report.add(_check_dbus())
    elif platform == "windows":
        report.add(_check_winrt_available())

    # ── Backend + adapter + device checks ─────────────────────────────────
    # Call _check_bluez_async with no arguments so that existing tests which
    # monkeypatch this name with a zero-argument coroutine continue to work.
    # The backend parameter is forwarded to _check_backend_async directly
    # when a caller passes an explicit backend to run_doctor().
    if backend is None:
        backend_result, adapters, devices = asyncio.run(_check_bluez_async())
    else:
        backend_result, adapters, devices = asyncio.run(_check_backend_async(backend))
    report.add(backend_result)

    if backend_result.passed:
        # Adapters
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

        # Paired devices
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

        # Trusted devices + connection state
        trusted = [d for d in devices if d.trusted and d.paired]
        if not trusted:
            report.add(
                CheckResult(
                    name="Trusted devices",
                    passed=False,
                    detail=(
                        "no paired+trusted devices found — "
                        "run: bluetoothctl trust <MAC>"
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
