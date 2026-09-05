"""Long-running daemon mode with periodic background rescanning.

Architecture
------------
Two concurrent tasks run inside ``run_forever()``:

1. **Event loop** — waits on ``_rescan_event`` (set by backend callbacks) or
   ``_stop_event``.  Fires immediately when a Bluetooth event arrives.

2. **Periodic scanner** — wakes every *rescan_interval* seconds and attempts
   to reconnect any disconnected trusted device that is not in backoff.

Per-device backoff
------------------
After every failed reconnect the device is put into a cooldown whose
duration grows exponentially (1 min → 2 min → 4 min → 8 min → 16 min,
capped at 30 min).  When a device reconnects successfully the cooldown
entry is removed so the next disconnect starts the sequence over.

Hook state-transition gating
-----------------------------
Hooks are fired **only on genuine state transitions**, not on every
backend event or every successful ``connect_all`` return.

``CONNECTED`` hooks are fired exclusively from the authoritative
``properties_changed(Connected=True)`` backend event — the only reliable
signal that full profile negotiation has completed.  They are *not* fired
from ``connect_all`` return values.

Platform abstraction
--------------------
The daemon accepts any object that satisfies the
:class:`~bluetooth_autoconnect.backends.BluetoothBackend` protocol.
On Linux this is
:class:`~bluetooth_autoconnect.backends.linux.LinuxBackend` (BlueZ D-Bus);
on Windows it is
:class:`~bluetooth_autoconnect.backends.windows.WindowsBackend` (WinRT).
The daemon never imports any platform-specific module itself.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Any

from .backends import BluetoothBackend, create_backend
from .connector import RetryPolicy, connect_all
from .exceptions import BackendError, BlueZNotAvailableError, DBusConnectionError
from .hooks import HookEvent, HookRunner
from .models import Device

logger = logging.getLogger("bluetooth_autoconnect.daemon")

# Interface name constants used when filtering backend events.
# Defined here (not imported from backends.linux) so daemon.py has no
# platform-specific dependency.
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"

# ── Backoff constants ──────────────────────────────────────────────────────────
_BACKOFF_BASE_SECONDS: float = 60.0  # 1 minute
_BACKOFF_MULTIPLIER: float = 2.0
_BACKOFF_MAX_SECONDS: float = 1800.0  # 30 minutes
_BACKOFF_MAX_LEVEL: int = 5  # 1m 2m 4m 8m 16m → cap


# ── Per-device cooldown tracker ───────────────────────────────────────────────


@dataclass
class _DeviceCooldown:
    """Tracks exponential backoff state for a single device MAC address."""

    mac: str
    level: int = 0  # number of consecutive failures
    retry_after: float = field(default_factory=time.monotonic)

    def record_failure(self) -> None:
        """Advance the backoff level and schedule the next allowed attempt."""
        self.level = min(self.level + 1, _BACKOFF_MAX_LEVEL)
        delay = min(
            _BACKOFF_BASE_SECONDS * (_BACKOFF_MULTIPLIER ** (self.level - 1)),
            _BACKOFF_MAX_SECONDS,
        )
        self.retry_after = time.monotonic() + delay
        logger.debug(
            "backoff: mac=%s level=%d next_attempt_in=%.0fs",
            self.mac,
            self.level,
            delay,
        )

    @property
    def ready(self) -> bool:
        """Return True when the cooldown period has elapsed."""
        return time.monotonic() >= self.retry_after


class _CooldownRegistry:
    """Single-loop registry of per-device cooldowns."""

    def __init__(self) -> None:
        self._entries: dict[str, _DeviceCooldown] = {}

    def is_ready(self, mac: str) -> bool:
        """Return True if the device may be retried right now."""
        entry = self._entries.get(mac)
        return entry is None or entry.ready

    def record_failure(self, mac: str) -> None:
        entry = self._entries.get(mac)
        if entry is None:
            entry = _DeviceCooldown(mac=mac)
            self._entries[mac] = entry
        entry.record_failure()

    def reset(self, mac: str) -> None:
        """Remove cooldown — called when a device successfully connects."""
        self._entries.pop(mac, None)
        logger.debug("backoff reset: mac=%s", mac)

    def filter_ready(self, devices: list[Device]) -> list[Device]:
        """Return only devices that are past their backoff window."""
        ready, skipped = [], []
        for d in devices:
            if self.is_ready(d.address):
                ready.append(d)
            else:
                skipped.append(d)
        for d in skipped:
            entry = self._entries[d.address]
            remaining = max(0.0, entry.retry_after - time.monotonic())
            logger.debug(
                "periodic scan: skipping mac=%s name=%r (backoff, retry_in=%.0fs)",
                d.address,
                d.name,
                remaining,
            )
        return ready


# ── Connection state tracker ──────────────────────────────────────────────────


class _DeviceStateTracker:
    """Guards hook execution so hooks only fire on genuine state transitions.

    Each device MAC is tracked with a tri-state:

    * ``None``  — state not yet known (daemon just started, or device newly seen)
    * ``True``  — last confirmed state is *connected*
    * ``False`` — last confirmed state is *disconnected*

    A hook fires **only** when a signal arrives that contradicts the stored
    state:

    * ``record_connected``    fires if stored state is not ``True``
    * ``record_disconnected`` fires if stored state is not ``False``

    This suppresses:

    * Duplicate ``Connected=False`` signals (multiple profiles disconnect
      individually on Linux; similar duplicate events occur on Windows).
    * Spurious ``Connected=False`` from failed reconnect attempts where the
      device was already considered disconnected.
    * Any ``Connected=True`` that arrives while the device is already known
      to be connected.
    """

    def __init__(self) -> None:
        self._state: dict[str, bool | None] = {}

    def record_connected(self, mac: str) -> bool:
        """Record that *mac* is now connected.

        Returns
        -------
        bool
            ``True`` if this is a genuine transition and a hook should fire.
            ``False`` if the device was already known connected (suppress).
        """
        previous = self._state.get(mac)
        self._state[mac] = True
        should_fire = previous is not True
        if not should_fire:
            logger.debug(
                "state-tracker: suppressing duplicate CONNECTED mac=%s"
                " (previous=%s)",
                mac,
                previous,
            )
        else:
            logger.debug(
                "state-tracker: CONNECTED transition mac=%s (previous=%s → True)",
                mac,
                previous,
            )
        return should_fire

    def record_disconnected(self, mac: str) -> bool:
        """Record that *mac* is now disconnected.

        Returns
        -------
        bool
            ``True`` if this is a genuine transition and a hook should fire.
            ``False`` if the device was already known disconnected (suppress).
        """
        previous = self._state.get(mac)
        self._state[mac] = False
        # For an *unknown* device (None), fire on first disconnect — the
        # device was presumably connected before the daemon started.
        should_fire = previous is not False
        if not should_fire:
            logger.debug(
                "state-tracker: suppressing duplicate DISCONNECTED mac=%s"
                " (previous=%s)",
                mac,
                previous,
            )
        else:
            logger.debug(
                "state-tracker: DISCONNECTED transition mac=%s"
                " (previous=%s → False)",
                mac,
                previous,
            )
        return should_fire

    def remove(self, mac: str) -> None:
        """Remove a device from the tracker (device unpaired / removed)."""
        self._state.pop(mac, None)
        logger.debug("state-tracker: removed mac=%s", mac)

    def get(self, mac: str) -> bool | None:
        """Return the current tracked state for *mac*, or ``None`` if unknown."""
        return self._state.get(mac)

    def __len__(self) -> int:
        return len(self._state)


# ── Daemon ────────────────────────────────────────────────────────────────────


class AutoConnectDaemon:
    """Event-driven + periodic-scan Bluetooth auto-connect daemon.

    Args:
        policy:           Per-attempt retry policy for a single connect call.
        max_concurrency:  Max simultaneous connect calls.
        rescan_interval:  Seconds between periodic background scans.
                          Set to 0 to disable periodic scanning entirely.
        hook_runner:      Optional :class:`~bluetooth_autoconnect.hooks.HookRunner`
                          that fires user scripts on connect/disconnect events.
                          Pass ``None`` (the default) to disable hooks entirely.
        backend:          Platform backend implementing
                          :class:`~bluetooth_autoconnect.backends.BluetoothBackend`.
                          When ``None`` (the default), the appropriate backend for
                          the current platform is selected automatically.
    """

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        max_concurrency: int = 5,
        rescan_interval: float = 30.0,
        hook_runner: HookRunner | None = None,
        backend: BluetoothBackend | None = None,
    ) -> None:
        self.policy = policy or RetryPolicy()
        self.max_concurrency = max_concurrency
        self.rescan_interval = rescan_interval
        self.hook_runner = hook_runner
        # Injected backend (for testing / cross-platform); auto-detect if None.
        self.client: BluetoothBackend = (
            backend if backend is not None else create_backend()
        )
        self._stop_event = asyncio.Event()
        self._rescan_event = asyncio.Event()
        self._cooldown = _CooldownRegistry()
        self._state_tracker = _DeviceStateTracker()

    # ── Core scan-and-connect ─────────────────────────────────────────────

    async def run_once(self) -> dict[str, bool]:
        """Enumerate adapters/devices, attempt to connect all eligible ones.

        Returns a dict mapping MAC address → success bool.  The cooldown
        registry is updated: failures advance backoff, successes reset it.

        Hook behaviour
        ~~~~~~~~~~~~~~
        ``on_connect`` hooks are intentionally **not** fired here, even when
        ``connect_all`` returns ``True`` for a device.  The reason: a
        successful ``connect_device()`` call only means the OS accepted the
        attempt — the device has not finished profile negotiation yet.  The
        authoritative ``Connected=True`` backend event fires the hook via
        ``_handle_device_properties_changed``, preventing duplicates.
        """
        adapters = await self.client.get_adapters()
        if not adapters:
            logger.warning("No Bluetooth adapters found.")
            return {}

        powered = [a for a in adapters if a.powered]
        if not powered:
            logger.warning(
                "%d adapter(s) found but none are powered on.", len(adapters)
            )
            return {}

        logger.info(
            "Scanning %d powered adapter(s): %s",
            len(powered),
            ", ".join(a.name for a in powered),
        )

        all_results: dict[str, bool] = {}
        for adapter in powered:
            logger.debug("adapter path=%s address=%s", adapter.path, adapter.address)
            devices = await self.client.get_devices(adapter_path=adapter.path)
            eligible = [d for d in devices if d.is_autoconnect_eligible]
            logger.info(
                "%s: %d device(s) known, %d paired+trusted",
                adapter.name,
                len(devices),
                len(eligible),
            )
            for device in eligible:
                logger.debug(
                    "trusted device detected: name=%r mac=%s path=%s connected=%s",
                    device.name,
                    device.address,
                    device.path,
                    device.connected,
                )
            results = await connect_all(
                devices,
                self.client.connect_device,
                policy=self.policy,
                max_concurrency=self.max_concurrency,
            )
            for addr, ok in results.items():
                if ok:
                    logger.debug("connection succeeded: mac=%s", addr)
                    self._cooldown.reset(addr)
                    # on_connect hooks are NOT fired here — see docstring.
                else:
                    logger.debug("connection failed: mac=%s", addr)
                    self._cooldown.record_failure(addr)
            all_results.update(results)

        succeeded = sum(1 for ok in all_results.values() if ok)
        logger.info(
            "Connection pass complete: %d/%d eligible device(s) connected.",
            succeeded,
            len(all_results),
        )
        return all_results

    # ── Periodic scan ─────────────────────────────────────────────────────

    async def _run_one_periodic_scan(self) -> None:
        """Execute a single periodic-scan pass: enumerate and reconnect.

        Extracted from ``_periodic_scan_loop`` so tests can drive it directly
        without waiting for the sleep timer.

        Hook behaviour
        ~~~~~~~~~~~~~~
        Same as ``run_once``: ``on_connect`` hooks are not fired here.
        The authoritative ``Connected=True`` backend event handles that.
        """
        logger.debug("periodic rescan started")
        try:
            adapters = await self.client.get_adapters()
            powered = [a for a in adapters if a.powered]

            for adapter in powered:
                devices = await self.client.get_devices(adapter_path=adapter.path)
                eligible = [
                    d for d in devices if d.is_autoconnect_eligible and not d.connected
                ]

                candidates = self._cooldown.filter_ready(eligible)

                if not candidates:
                    logger.debug(
                        "periodic scan: no candidates on %s"
                        " (all connected or in backoff)",
                        adapter.name,
                    )
                    continue

                for device in candidates:
                    logger.debug(
                        "periodic scan: disconnected trusted device"
                        " found — mac=%s name=%r path=%s",
                        device.address,
                        device.name,
                        device.path,
                    )

                results = await connect_all(
                    candidates,
                    self.client.connect_device,
                    policy=self.policy,
                    max_concurrency=self.max_concurrency,
                )

                for addr, ok in results.items():
                    if ok:
                        logger.info(
                            "periodic scan: reconnect successful mac=%s", addr
                        )
                        self._cooldown.reset(addr)
                        # on_connect hooks NOT fired here — same reason as run_once.
                    else:
                        logger.debug("periodic scan: reconnect failed mac=%s", addr)
                        self._cooldown.record_failure(addr)

        except Exception:  # noqa: BLE001
            logger.exception("Error during periodic scan")

    async def _periodic_scan_loop(self) -> None:
        """Background task: reconnect disconnected devices every N seconds."""
        if self.rescan_interval <= 0:
            logger.debug(
                "periodic scan disabled (rescan_interval=%.0f)",
                self.rescan_interval,
            )
            return

        logger.debug("periodic scan started: interval=%.0fs", self.rescan_interval)

        while not self._stop_event.is_set():
            await asyncio.sleep(self.rescan_interval)

            if self._stop_event.is_set():
                break

            await self._run_one_periodic_scan()

    # ── Backend event callbacks ───────────────────────────────────────────

    async def _on_dbus_event(
        self,
        event_type: str,
        path: str,
        interface: str,
        changed: dict[str, Any],
    ) -> None:
        """Route a backend event to the appropriate handler.

        The name ``_on_dbus_event`` is kept for backward compatibility with
        existing tests.  On Windows the same callback receives synthetic
        events with the same structure, using the same interface name
        constants.
        """
        if interface == ADAPTER_IFACE:
            if (
                event_type in ("added", "properties_changed")
                and changed.get("Powered") is True
            ):
                logger.debug("adapter powered on: path=%s — triggering rescan", path)
                logger.info("Adapter %s powered on; triggering rescan.", path)
                self._rescan_event.set()
        elif interface == DEVICE_IFACE:
            if event_type == "added":
                logger.debug(
                    "new device object appeared: path=%s — triggering rescan",
                    path,
                )
                self._rescan_event.set()
            elif event_type == "properties_changed":
                await self._handle_device_properties_changed(path, changed)

    async def _handle_device_properties_changed(
        self,
        path: str,
        changed: dict[str, Any],
    ) -> None:
        """Dispatch on individual property changes for a Device object.

        Connection state (``Connected``) changes are routed through
        ``_DeviceStateTracker`` to ensure hooks fire only once per genuine
        transition, regardless of how many redundant signals the backend emits.
        """
        mac = path.rsplit("/dev_", 1)[-1].replace("_", ":").upper()

        if changed.get("Connected") is False:
            logger.debug(
                "device disconnected: path=%s — scheduling reconnect", path
            )
            logger.info("Device %s disconnected; will attempt reconnect.", path)

            if self.hook_runner is not None:
                should_fire = self._state_tracker.record_disconnected(mac)
                if should_fire:
                    await self._fire_disconnect_hook(path)
                else:
                    logger.debug(
                        "hook: suppressed duplicate DISCONNECTED mac=%s", mac
                    )
            else:
                self._state_tracker.record_disconnected(mac)

            self._rescan_event.set()

        elif changed.get("Connected") is True:
            logger.debug("device connected: path=%s — resetting backoff", path)
            self._cooldown.reset(mac)

            if self.hook_runner is not None:
                should_fire = self._state_tracker.record_connected(mac)
                if should_fire:
                    await self._fire_connect_hook(path, mac)
                else:
                    logger.debug(
                        "hook: suppressed duplicate CONNECTED mac=%s", mac
                    )
            else:
                self._state_tracker.record_connected(mac)

        elif "RSSI" in changed:
            logger.debug(
                "device back in range: path=%s rssi=%s"
                " — resetting backoff and triggering rescan",
                path,
                changed.get("RSSI"),
            )
            self._cooldown.reset(mac)
            self._rescan_event.set()

        elif changed.get("Trusted") is True:
            logger.debug(
                "device marked trusted: path=%s — triggering rescan", path
            )
            self._rescan_event.set()

        elif changed.get("Paired") is True:
            logger.debug("device paired: path=%s — triggering rescan", path)
            self._rescan_event.set()

    # ── Hook helpers ──────────────────────────────────────────────────────

    async def _fire_connect_hook(self, device_path: str, mac: str) -> None:
        """Look up a device and fire the on_connect hook.

        Args:
            device_path: Backend-specific device identifier.
            mac:         MAC address derived from the path or event.
        """
        assert self.hook_runner is not None  # guarded by caller
        device: Device | None = None
        try:
            all_devices = await self.client.get_devices()
            device = next((d for d in all_devices if d.path == device_path), None)
        except Exception:  # noqa: BLE001
            logger.debug(
                "hook: could not query backend for connect device path=%s",
                device_path,
            )

        if device is None:
            adapter_path = device_path.rsplit("/", 1)[0]
            device = Device(
                path=device_path,
                address=mac,
                name=mac,
                adapter_path=adapter_path,
                paired=False,
                trusted=False,
                connected=True,
            )
            logger.debug(
                "hook: using synthetic device record for connect mac=%s", mac
            )

        self.hook_runner.fire(HookEvent.CONNECTED, device)

    async def _fire_disconnect_hook(self, device_path: str) -> None:
        """Look up a device by path and fire the on_disconnect hook.

        If the device cannot be found in the backend (it may have been removed
        immediately after disconnect), a synthetic Device record is constructed
        from the path so the hook still receives ``BT_DEVICE_MAC``.

        Args:
            device_path: Backend-specific device identifier.
        """
        assert self.hook_runner is not None  # guarded by caller
        device: Device | None = None
        try:
            all_devices = await self.client.get_devices()
            device = next((d for d in all_devices if d.path == device_path), None)
        except Exception:  # noqa: BLE001
            logger.debug(
                "hook: could not query backend for disconnect device path=%s",
                device_path,
            )

        if device is None:
            mac = device_path.rsplit("/dev_", 1)[-1].replace("_", ":").upper()
            adapter_path = device_path.rsplit("/", 1)[0]
            device = Device(
                path=device_path,
                address=mac,
                name=mac,
                adapter_path=adapter_path,
                paired=False,
                trusted=False,
                connected=False,
            )
            logger.debug(
                "hook: using synthetic device record for disconnect mac=%s", mac
            )

        self.hook_runner.fire(HookEvent.DISCONNECTED, device)

    # ── Signal handlers ───────────────────────────────────────────────────

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        def _shutdown() -> None:
            logger.info("Shutdown signal received; stopping daemon.")
            self._stop_event.set()

        def _rescan() -> None:
            logger.info("SIGHUP received; triggering full rescan.")
            self._rescan_event.set()

        loop.add_signal_handler(signal.SIGTERM, _shutdown)
        loop.add_signal_handler(signal.SIGINT, _shutdown)
        loop.add_signal_handler(signal.SIGHUP, _rescan)

    # ── Main entry point ──────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Start the daemon: event loop + periodic scanner run concurrently."""
        try:
            await self.client.connect()
        except (DBusConnectionError, BlueZNotAvailableError, BackendError) as exc:
            logger.error("Fatal startup error: %s", exc)
            raise

        loop = asyncio.get_running_loop()
        self._install_signal_handlers(loop)

        await self.client.subscribe(self._on_dbus_event)
        logger.info("Subscribed to Bluetooth backend events.")

        # Initial scan on startup
        await self.run_once()

        # Launch the periodic scanner as a background task
        periodic_task = asyncio.create_task(
            self._periodic_scan_loop(),
            name="periodic-scan",
        )

        try:
            while not self._stop_event.is_set():
                rescan_task = asyncio.create_task(self._rescan_event.wait())
                stop_task = asyncio.create_task(self._stop_event.wait())
                done, pending = await asyncio.wait(
                    {rescan_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()

                if self._stop_event.is_set():
                    break

                if self._rescan_event.is_set():
                    self._rescan_event.clear()
                    await asyncio.sleep(1.0)
                    try:
                        await self.run_once()
                    except Exception:  # noqa: BLE001
                        logger.exception("Error during rescan pass")
        finally:
            periodic_task.cancel()
            try:
                await periodic_task
            except asyncio.CancelledError:
                pass
            await self.client.close()
            logger.info("Daemon stopped.")
