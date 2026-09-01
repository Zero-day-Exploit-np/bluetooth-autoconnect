"""Long-running daemon mode."""

from __future__ import annotations

import asyncio
import logging
import signal

from .connector import RetryPolicy, connect_all
from .dbus_client import ADAPTER_IFACE, DEVICE_IFACE, BlueZClient
from .exceptions import BlueZNotAvailableError, DBusConnectionError

logger = logging.getLogger("bluetooth_autoconnect.daemon")


class AutoConnectDaemon:
    def __init__(
        self,
        policy: RetryPolicy | None = None,
        max_concurrency: int = 5,
    ) -> None:
        self.policy = policy or RetryPolicy()
        self.max_concurrency = max_concurrency
        self.client = BlueZClient()
        self._stop_event = asyncio.Event()
        self._rescan_event = asyncio.Event()

    async def run_once(self) -> dict[str, bool]:
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
            devices = await self.client.get_devices(adapter_path=adapter.path)
            eligible = [d for d in devices if d.is_autoconnect_eligible]
            logger.info(
                "%s: %d device(s) known, %d paired+trusted",
                adapter.name,
                len(devices),
                len(eligible),
            )
            results = await connect_all(
                devices,
                self.client.connect_device,
                policy=self.policy,
                max_concurrency=self.max_concurrency,
            )
            all_results.update(results)

        succeeded = sum(1 for ok in all_results.values() if ok)
        logger.info(
            "Connection pass complete: %d/%d eligible device(s) connected.",
            succeeded,
            len(all_results),
        )
        return all_results

    async def _on_dbus_event(
        self,
        event_type: str,
        path: str,
        interface: str,
        changed: dict,  # type: ignore[type-arg]
    ) -> None:
        if interface == ADAPTER_IFACE:
            if (
                event_type in ("added", "properties_changed")
                and changed.get("Powered") is True
            ):
                logger.info("Adapter %s powered on; triggering rescan.", path)
                self._rescan_event.set()
        elif interface == DEVICE_IFACE:
            if event_type == "added":
                logger.debug("New device object appeared: %s", path)
                self._rescan_event.set()
            elif event_type == "properties_changed":
                if changed.get("Connected") is False:
                    logger.info(
                        "Device %s disconnected; will attempt reconnect.", path
                    )
                    self._rescan_event.set()
                elif "RSSI" in changed:
                    logger.debug("Device %s is back in range.", path)
                    self._rescan_event.set()

    def _install_signal_handlers(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        def _shutdown() -> None:
            logger.info("Shutdown signal received; stopping daemon.")
            self._stop_event.set()

        def _rescan() -> None:
            logger.info("SIGHUP received; triggering full rescan.")
            self._rescan_event.set()

        loop.add_signal_handler(signal.SIGTERM, _shutdown)
        loop.add_signal_handler(signal.SIGINT, _shutdown)
        loop.add_signal_handler(signal.SIGHUP, _rescan)

    async def run_forever(self) -> None:
        try:
            await self.client.connect()
        except (DBusConnectionError, BlueZNotAvailableError) as exc:
            logger.error("Fatal startup error: %s", exc)
            raise

        loop = asyncio.get_running_loop()
        self._install_signal_handlers(loop)

        await self.client.subscribe(self._on_dbus_event)
        logger.info("Subscribed to BlueZ D-Bus events.")
        await self.run_once()

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
            await self.client.close()
            logger.info("Daemon stopped.")
