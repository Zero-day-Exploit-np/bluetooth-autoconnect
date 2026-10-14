# Troubleshooting

## "org.bluez is not available on the system bus"

BlueZ isn't running, or isn't installed.

```bash
sudo systemctl status bluetooth.service
sudo systemctl enable --now bluetooth.service
```

If it's not installed:

```bash
# Debian/Ubuntu/Kali/Mint
sudo apt install bluez

# Fedora
sudo dnf install bluez

# Arch/Manjaro
sudo pacman -S bluez bluez-utils

# openSUSE
sudo zypper install bluez
```

## "Could not connect to the D-Bus system bus"

The D-Bus system daemon isn't reachable. This is rare outside of
containers/minimal environments:

```bash
sudo systemctl status dbus.service
```

If running inside a container, make sure `/var/run/dbus/system_bus_socket`
is bind-mounted in from the host.

## Devices are found but never connect (Permission denied / not authorized)

BlueZ's D-Bus policy in `/etc/dbus-1/system.d/bluetooth.conf` normally
allows any local user to call `Connect()` on already-paired devices, but
some hardened or custom configurations restrict this to `root` or a
specific group. Symptoms: repeated retries that all fail with something
like `org.freedesktop.DBus.Error.AccessDenied`.

Fixes, in order of preference:

1. Run the **system-wide** service (as root) instead of the per-user one.
2. Add your user to the `bluetooth` or `lp` group if your distro uses one
   for this purpose, then log out and back in.
3. As a last resort, add a custom polkit/D-Bus policy rule granting your
   user access to `org.bluez.Device1.Connect` — consult your
   distribution's BlueZ documentation, since exact group/policy names
   vary.

## A device just won't reconnect no matter what

1. Confirm it's actually paired **and** trusted:

   ```bash
   bluetoothctl info AA:BB:CC:DD:EE:FF
   ```

   Look for `Paired: yes` and `Trusted: yes`. If `Trusted: no`:

   ```bash
   bluetoothctl trust AA:BB:CC:DD:EE:FF
   ```

2. Run with `--verbose` to see per-attempt logs:

   ```bash
   bluetooth-autoconnect --verbose
   ```

3. Some devices (especially older Bluetooth Classic headsets) only
   accept an *incoming* connection initiated by the peripheral itself,
   not the host. If BlueZ's `Connect()` consistently fails with
   `org.bluez.Error.NotAvailable` or `br-connection-page-timeout`, put
   the device into pairing/connect mode on its own side and let it
   initiate; `bluetooth-autoconnect`'s daemon mode will pick up the
   resulting `Connected: true` property change and won't fight it.

4. Check `dmesg`/`journalctl -k` for underlying Bluetooth controller
   firmware errors, which are outside this tool's control.

## The daemon isn't reacting to devices coming into range

- Make sure the adapter is actually **powered on** — a powered-off
  adapter never sees advertisements:

  ```bash
  bluetoothctl show
  ```

- Some devices only advertise (become visible) for a short window after
  waking up. The daemon reacts to `PropertiesChanged` signals as they
  arrive, so if BlueZ never sees the device, there's nothing to react
  to — this is a BlueZ/firmware-level visibility issue, not something
  `bluetooth-autoconnect` controls.

- Trigger a manual rescan without restarting the service:

  ```bash
  sudo systemctl kill -s SIGHUP bluetooth-autoconnect.service
  ```

## High CPU / log spam from rapid reconnect loops

If a device is flapping (repeatedly connecting and disconnecting, e.g.
low battery or interference), you'll see repeated retry logs. The
exponential backoff (default: 1s, 2s, 4s, 8s, 16s, capped at 60s) should
keep this from becoming a tight loop; if you want fewer, more patient
retries, lower `--max-attempts` or raise the daemon's debounce tolerance
by editing `daemon.py`'s 1-second rescan debounce if you're building
from source.

## `ModuleNotFoundError: No module named 'dbus_next'`

The `dbus-next` dependency isn't installed in whatever Python
environment is running `bluetooth-autoconnect`:

```bash
pip install --user dbus-next
# or, if using a virtualenv, make sure it's activated first
```

## Logs aren't showing up in `journalctl`

- If you installed the optional `python3-systemd` binding
  (`pip install bluetooth-autoconnect[journal]` or your distro's
  `python3-systemd` package), logs are sent to the journal directly with
  structured fields.
- Otherwise, `bluetooth-autoconnect` logs to stdout, and systemd
  automatically captures a service's stdout into the journal — this
  works out of the box with no extra dependency. If you're running the
  command manually (not via systemd), stdout logs simply print to your
  terminal instead, which is expected.

## Still stuck?

Run with `--verbose` and check the full log output — every skip decision
(untrusted, unpaired, already connected) and every retry attempt is
logged at DEBUG level, which usually pinpoints exactly where things are
going wrong.
