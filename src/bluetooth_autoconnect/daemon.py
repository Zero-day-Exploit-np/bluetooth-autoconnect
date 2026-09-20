"""Long-running daemon mode with periodic background rescanning.

Architecture
------------
Two concurrent tasks run inside ``run_forever()``:

1. **Event loop** — waits on ``_rescan_event`` (set by backend callbacks) or
   ``_stop_event``.  Fires immediately when a Bluetooth event arrives.

2. **Periodic scanner** — wakes every *rescan_interval* seconds, runs a
   windowed active discovery scan, attempts to reconnect any disconnected
   trusted device that is not in backoff.

Active discovery
----------------
The root cause of "device returns but doesn't reconnect" is that BlueZ
preserves the ``Device1`` D-Bus object for a paired device even when the
device is physically absent.  Without active discovery, BlueZ has no
reason to emit new ``PropertiesChanged`` or ``RSSI`` signals for a device
that left and came back — so the daemon has no event to act on.

The fix: when there are eligible disconnected devices, call
``org.bluez.Adapter1.StartDiscovery()`` for a short window
(``discovery_duration_seconds``, default 8 s) during each periodic scan.
BlueZ then actively scans for nearby devices and emits property updates
(RSSI, ServicesResolved, etc.) when it re-detects a known device.  The
existing RSSI handler in ``_handle_device_properties_changed`` already
calls ``_cooldown.reset(mac)`` and triggers an immediate reconnect — this
is now the mechanism that detects a returning device.

Discovery lifecycle per scan pass
----------------------------------
::

    Disconnected paired/trusted device exists
            ↓
    start_discovery(adapter)          ← new
            ↓
    Wait discovery_duration_seconds   ← new
            ↓
    BlueZ emits RSSI/property updates for returning devices
            ↓
    _handle_device_properties_changed fires → rescan triggered
            ↓
    run_once() → connect_device()
            ↓
    stop_discovery(adapter)           ← new

If no device returns during the window, discovery is stopped and the
scanner waits until the next ``rescan_interval`` tick.

Per-device backoff
------------------
After every failed reconnect the device is put into a cooldown whose
duration grows exponentially (1 min → 2 min → 4 min → 8 min → 16 min,
capped at 30 min).  When a device reconnects successfully the cooldown
entry is removed so the next disconnect starts the sequence over.

The backoff is respected by BOTH the event-driven path and the periodic
scanner.  However, a strong "device seen" signal (RSSI update) always
resets the backoff so a device that has clearly returned is never
permanently suppressed.

Hook state-transition gating
-----------------------------
Hooks are fired **only on genuine state transitions**, not on every
backend event or every successful ``connect_all`` return.

Platform abstraction
--------------------
The daemon accepts any object that satisfies the
:class:`~bluetooth_autoconnect.backends.BluetoothBackend` protocol.
Discovery methods (``start_discovery`` / ``stop_discovery``) are optional
on the protocol and detected via ``hasattr`` at runtime, so non-Linux
backends are not affected.
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
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"

# ── Backoff constants ──────────────────────────────────────────────────────────
_BACKOFF_BASE_SECONDS: float = 60.0  # 1 minute
_BACKOFF_MULTIPLIER: float = 2.0
_BACKOFF_MAX_SECONDS: float = 1800.0  # 30 minutes
_BACKOFF_MAX_LEVEL: int = 5  # 1m 2m 4m 8m 16m → cap

# ── Discovery defaults ────────────────────────────────────────────────────────
_DEFAULT_DISCOVERY_DURATION: float = 8.0  # seconds per scan window


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

    def seconds_remaining(self) -> float:
        """Return seconds until this cooldown expires (0 if already ready)."""
        return max(0.0, self.retry_after - time.monotonic())


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
        if mac in self._entries:
            self._entries.pop(mac)
            logger.debug("backoff reset: mac=%s", mac)

    def filter_ready(self, devices: list[Device]) -> list[Device]:
        """Return only devices that are past their backoff window."""
        ready: list[Device] = []
        for d in devices:
            entry = self._entries.get(d.address)
            if entry is None or entry.ready:
                ready.append(d)
            else:
                logger.debug(
                    "reconnect suppressed by backoff:"
                    " mac=%s name=%r retry_in=%.0fs level=%d",
                    d.address,
                    d.name,
                    entry.seconds_remaining(),
                    entry.level,
                )
        return ready

    def seconds_until_ready(self, mac: str) -> float:
        """Return seconds until *mac* is ready for a retry attempt."""
        entry = self._entries.get(mac)
        if entry is None:
            return 0.0
        return entry.seconds_remaining()


# ── Connection state tracker ──────────────────────────────────────────────────


class _DeviceStateTracker:
    """Guards hook execution so hooks only fire on genuine state transitions."""

    def __init__(self) -> None:
        self._state: dict[str, bool | None] = {}

    def record_connected(self, mac: str) -> bool:
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
        previous = self._state.get(mac)
        self._state[mac] = False
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
        self._state.pop(mac, None)
        logger.debug("state-tracker: removed mac=%s", mac)

    def get(self, mac: str) -> bool | None:
        return self._state.get(mac)

    def __len__(self) -> int:
        return len(self._state)


# ── Discovery manager ─────────────────────────────────────────────────────────


class _DiscoveryManager:
    """Controls windowed active discovery for a single adapter.

    A single :class:`_DiscoveryManager` instance is created per adapter.
    It ensures that:

    * Discovery is only started when there are actually disconnected
      eligible devices waiting.
    * Discovery runs for a bounded window (``duration_seconds``), then
      stops, preventing continuous scanning.
    * Only one discovery session is active at a time per adapter; no
      duplicate ``StartDiscovery()`` calls are made.
    * ``StopDiscovery()`` is always called after the window ends, even
      if an exception occurred.

    The backend's ``start_discovery`` / ``stop_discovery`` methods may not
    be present (e.g. on Windows or in test doubles); presence is checked
    via ``hasattr`` before calling.
    """

    def __init__(self, adapter_path: str, duration_seconds: float) -> None:
        self.adapter_path = adapter_path
        self.duration_seconds = duration_seconds
        self._running = False

    async def run_window(self, backend: BluetoothBackend) -> bool:
        """Start discovery, wait for the window duration, then stop.

        Returns ``True`` if discovery was started (or was already running),
        ``False`` if the backend does not support discovery or the adapter
        is not ready.

        This method is safe to call concurrently — if a window is already
        in progress, the call returns ``False`` immediately.
        """
        if self._running:
            return False

        if not hasattr(backend, "start_discovery"):
            return False

        self._running = True
        try:
            logger.debug(
                "discovery: starting %.0fs window on %s",
                self.duration_seconds,
                self.adapter_path,
            )
            started = await backend.start_discovery(self.adapter_path)
            if not started:
                return False

            await asyncio.sleep(self.duration_seconds)
            return True
        except Exception:  # noqa: BLE001
            return False
        finally:
            self._running = False
            if hasattr(backend, "stop_discovery"):
                try:
                    await backend.stop_discovery(self.adapter_path)
                except Exception:  # noqa: BLE001
                    pass
            logger.debug("discovery: window ended on %s", self.adapter_path)


# ── Daemon ────────────────────────────────────────────────────────────────────


class AutoConnectDaemon:
    """Event-driven + periodic-scan + active-discovery Bluetooth daemon.

    Args:
        policy:                Per-attempt retry policy.
        max_concurrency:       Max simultaneous connect calls.
        rescan_interval:       Seconds between periodic scan passes.
                               ``0`` disables periodic scanning.
        discovery_duration:    Seconds to hold a BlueZ discovery window
                               open during each scan pass.  Set to ``0``
                               to disable active discovery (not recommended
                               for headless systems).
        hook_runner:           Optional hook executor.
        backend:               Platform backend.  ``None`` → auto-detect.
    """

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        max_concurrency: int = 5,
        rescan_interval: float = 30.0,
        discovery_duration: float = _DEFAULT_DISCOVERY_DURATION,
        hook_runner: HookRunner | None = None,
        backend: BluetoothBackend | None = None,
    ) -> None:
        self.policy = policy or RetryPolicy()
        self.max_concurrency = max_concurrency
        self.rescan_interval = rescan_interval
        self.discovery_duration = discovery_duration
        self.hook_runner = hook_runner
        self.client: BluetoothBackend = (
            backend if backend is not None else create_backend()
        )
        self._stop_event = asyncio.Event()
        self._rescan_event = asyncio.Event()
        self._cooldown = _CooldownRegistry()
        self._state_tracker = _DeviceStateTracker()
        # Per-adapter discovery managers; populated lazily on first scan.
        self._discovery_managers: dict[str, _DiscoveryManager] = {}

    def _get_discovery_manager(self, adapter_path: str) -> _DiscoveryManager:
        """Return (creating if necessary) the manager for *adapter_path*."""
        if adapter_path not in self._discovery_managers:
            self._discovery_managers[adapter_path] = _DiscoveryManager(
                adapter_path=adapter_path,
                duration_seconds=self.discovery_duration,
            )
        return self._discovery_managers[adapter_path]

    # ── Core scan-and-connect ─────────────────────────────────────────────

    async def run_once(self) -> dict[str, bool]:
        """Enumerate adapters/devices, attempt to connect all eligible ones.

        Returns a dict mapping MAC address → success bool.  The cooldown
        registry is updated: failures advance backoff, successes reset it.

        Note: this method does NOT run active discovery.  Discovery is
        managed by ``_run_one_periodic_scan``.  ``run_once`` is also called
        on startup and in response to D-Bus events, where devices are
        already known to be reachable.
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
                    "device: name=%r mac=%s Paired=%s Trusted=%s Connected=%s",
                    device.name,
                    device.address,
                    device.paired,
                    device.trusted,
                    device.connected,
                )

            for device in devices:
                if not device.is_autoconnect_eligible:
                    logger.debug(
                        "skipping mac=%s: Paired=%s Trusted=%s",
                        device.address,
                        device.paired,
                        device.trusted,
                    )

            needs_connect = [d for d in eligible if not d.connected]
            candidates = self._cooldown.filter_ready(needs_connect)

            for device in eligible:
                if device.connected:
                    all_results[device.address] = True

            if not candidates:
                if needs_connect:
                    logger.info(
                        "%s: %d disconnected device(s) deferred"
                        " — all in backoff (periodic scanner will retry)",
                        adapter.name,
                        len(needs_connect),
                    )
                continue

            logger.info(
                "%s: attempting to connect %d device(s): %s",
                adapter.name,
                len(candidates),
                ", ".join(f"{d.name} ({d.address})" for d in candidates),
            )

            results = await connect_all(
                candidates,
                self.client.connect_device,
                policy=self.policy,
                max_concurrency=self.max_concurrency,
            )
            for addr, ok in results.items():
                if ok:
                    logger.info("reconnect succeeded: mac=%s", addr)
                    self._cooldown.reset(addr)
                else:
                    logger.info("reconnect failed: mac=%s — scheduling backoff", addr)
                    self._cooldown.record_failure(addr)
            all_results.update(results)

        succeeded = sum(1 for ok in all_results.values() if ok)
        total = len(all_results)
        if total:
            logger.info(
                "Connection pass complete: %d/%d device(s) connected.",
                succeeded,
                total,
            )
        return all_results

    # ── Periodic scan with active discovery ───────────────────────────────

    async def _run_one_periodic_scan(self) -> None:
        """Execute one periodic scan pass with active discovery.

        For each powered adapter that has eligible disconnected devices:

        1.  Start a BlueZ discovery window (``StartDiscovery``).
        2.  Hold it open for ``discovery_duration`` seconds so BlueZ can
            detect nearby devices and emit ``PropertiesChanged`` signals.
        3.  Stop discovery.

        If BlueZ re-sees a paired device during the window it emits an
        RSSI or property update.  ``_handle_device_properties_changed``
        picks this up, resets backoff, and sets ``_rescan_event`` — this
        triggers ``run_once()`` which calls ``connect_device()``.

        After the discovery window, we also attempt a direct connection
        for any devices that are still disconnected and not in backoff.
        This handles the case where the device returned but did not
        generate any property update during the window (e.g. BR/EDR
        devices that are connectable but not advertising).
        """
        logger.debug("periodic rescan started")
        try:
            adapters = await self.client.get_adapters()
            powered = [a for a in adapters if a.powered]

            for adapter in powered:
                devices = await self.client.get_devices(adapter_path=adapter.path)
                eligible_disconnected = [
                    d for d in devices
                    if d.is_autoconnect_eligible and not d.connected
                ]

                if not eligible_disconnected:
                    logger.debug(
                        "periodic scan %s: no disconnected trusted devices",
                        adapter.name,
                    )
                    continue

                logger.info(
                    "periodic scan %s: %d disconnected device(s) waiting: %s",
                    adapter.name,
                    len(eligible_disconnected),
                    ", ".join(
                        f"{d.name} ({d.address})" for d in eligible_disconnected
                    ),
                )
                logger.info(
                    "periodic scan %s: waiting for device(s) to return"
                    " — starting discovery window (%.0fs)",
                    adapter.name,
                    self.discovery_duration,
                )

                # ── Active discovery window ───────────────────────────────
                # During this window BlueZ scans for nearby devices and
                # emits PropertiesChanged (RSSI etc.) when it sees them.
                # The RSSI handler in _handle_device_properties_changed
                # will reset backoff and set _rescan_event, which causes
                # run_once() to fire immediately with a fresh candidate list.
                if self.discovery_duration > 0:
                    mgr = self._get_discovery_manager(adapter.path)
                    ran = await mgr.run_window(self.client)
                    if not ran:
                        logger.debug(
                            "periodic scan %s: discovery window skipped"
                            " (backend does not support discovery"
                            " or window already running)",
                            adapter.name,
                        )
                    else:
                        logger.debug(
                            "periodic scan %s: discovery window complete",
                            adapter.name,
                        )

                # ── Attempt connection for ready candidates ───────────────
                # Re-fetch device state after the discovery window; BlueZ
                # may have updated RSSI / Connected properties.
                devices = await self.client.get_devices(adapter_path=adapter.path)
                eligible_disconnected = [
                    d for d in devices
                    if d.is_autoconnect_eligible and not d.connected
                ]
                candidates = self._cooldown.filter_ready(eligible_disconnected)

                if not candidates:
                    if eligible_disconnected:
                        logger.debug(
                            "periodic scan %s: %d device(s) still in backoff"
                            " after discovery window",
                            adapter.name,
                            len(eligible_disconnected),
                        )
                    else:
                        logger.debug(
                            "periodic scan %s: discovery window ended"
                            " — no devices seen",
                            adapter.name,
                        )
                    continue

                logger.info(
                    "periodic scan %s: attempting %d device(s): %s",
                    adapter.name,
                    len(candidates),
                    ", ".join(f"{d.name} ({d.address})" for d in candidates),
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
                    else:
                        logger.debug(
                            "periodic scan: reconnect failed mac=%s"
                            " — backoff advanced",
                            addr,
                        )
                        self._cooldown.record_failure(addr)

        except Exception:  # noqa: BLE001
            logger.exception("Error during periodic scan")

    async def _periodic_scan_loop(self) -> None:
        """Background task: periodic scan with active discovery."""
        if self.rescan_interval <= 0:
            logger.debug(
                "periodic scan disabled (rescan_interval=%.0f)",
                self.rescan_interval,
            )
            return

        logger.info(
            "periodic scan started: interval=%.0fs discovery_window=%.0fs",
            self.rescan_interval,
            self.discovery_duration,
        )

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
        """Route a backend event to the appropriate handler."""
        if interface == ADAPTER_IFACE:
            if (
                event_type in ("added", "properties_changed")
                and changed.get("Powered") is True
            ):
                logger.debug("adapter powered on: path=%s — triggering rescan", path)
                logger.info("Adapter %s powered on; triggering rescan.", path)
                # Clear any stale discovery manager for this adapter so it
                # gets a fresh one after the power cycle.
                self._discovery_managers.pop(path, None)
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

        Device-return detection
        ~~~~~~~~~~~~~~~~~~~~~~~
        The following property changes are treated as "device seen" signals
        and trigger an immediate reconnect (after backoff reset):

        * ``RSSI`` — BlueZ received an advertisement from the device.
        * ``ServicesResolved`` — profile resolution completed.

        In addition to the above, ``InterfacesAdded`` (event_type="added")
        triggers a rescan via ``_on_dbus_event``.

        This multi-signal approach means the daemon does NOT need to rely
        exclusively on a new ``InterfacesAdded`` event — which BlueZ may not
        emit for an already-known device that comes back into range.
        """
        mac = path.rsplit("/dev_", 1)[-1].replace("_", ":").upper()

        if changed.get("Connected") is False:
            logger.info("device disconnected: mac=%s path=%s", mac, path)

            if self.hook_runner is not None:
                should_fire = self._state_tracker.record_disconnected(mac)
                if should_fire:
                    await self._fire_disconnect_hook(path)
                else:
                    logger.debug("hook: suppressed duplicate DISCONNECTED mac=%s", mac)
            else:
                self._state_tracker.record_disconnected(mac)

            if self._cooldown.is_ready(mac):
                logger.debug(
                    "device disconnected: mac=%s — scheduling immediate reconnect",
                    mac,
                )
                self._rescan_event.set()
            else:
                remaining = self._cooldown.seconds_until_ready(mac)
                logger.info(
                    "device disconnected: mac=%s — in backoff (%.0fs remaining);"
                    " periodic scanner will retry",
                    mac,
                    remaining,
                )

        elif changed.get("Connected") is True:
            logger.info(
                "device connected: mac=%s path=%s — resetting backoff", mac, path
            )
            self._cooldown.reset(mac)

            if self.hook_runner is not None:
                should_fire = self._state_tracker.record_connected(mac)
                if should_fire:
                    await self._fire_connect_hook(path, mac)
                else:
                    logger.debug("hook: suppressed duplicate CONNECTED mac=%s", mac)
            else:
                self._state_tracker.record_connected(mac)

        elif "RSSI" in changed:
            # BlueZ received an advertisement from this device.
            # This is the primary "device returned" signal when active
            # discovery is running.
            rssi = changed.get("RSSI")
            logger.info(
                "device seen: mac=%s rssi=%s"
                " — resetting backoff and scheduling reconnect",
                mac,
                rssi,
            )
            self._cooldown.reset(mac)
            self._rescan_event.set()

        elif changed.get("ServicesResolved") is True:
            # Profile service resolution completed — device is reachable.
            logger.info(
                "device services resolved: mac=%s"
                " — triggering reconnect",
                mac,
            )
            self._cooldown.reset(mac)
            self._rescan_event.set()

        elif changed.get("Trusted") is True:
            logger.info("device marked trusted: mac=%s — triggering rescan", mac)
            self._rescan_event.set()

        elif changed.get("Paired") is True:
            logger.info("device paired: mac=%s — triggering rescan", mac)
            self._rescan_event.set()

    # ── Hook helpers ──────────────────────────────────────────────────────

    async def _fire_connect_hook(self, device_path: str, mac: str) -> None:
        """Look up a device and fire the on_connect hook."""
        assert self.hook_runner is not None
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
            logger.debug("hook: using synthetic device record for connect mac=%s", mac)

        self.hook_runner.fire(HookEvent.CONNECTED, device)

    async def _fire_disconnect_hook(self, device_path: str) -> None:
        """Look up a device by path and fire the on_disconnect hook."""
        assert self.hook_runner is not None
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
