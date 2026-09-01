# bluetooth-autoconnect

> Automatically reconnect paired, trusted Bluetooth devices on Linux — headphones, mice, keyboards, speakers — the moment they come into range.

Talks directly to [BlueZ](http://www.bluez.org/) over D-Bus (no shelling out to `bluetoothctl`), so it's fast, event-driven, and works across any number of adapters. Ships as a systemd service that starts at boot and requires zero ongoing interaction.

[![Tests](https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/test.yml/badge.svg)](https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/test.yml)
[![Lint](https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/lint.yml/badge.svg)](https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/lint.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

---

## Quick Start

**One command installs everything and starts the service:**

```bash
git clone https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect.git
cd bluetooth-autoconnect
sudo bash scripts/install.sh
```

That's it. The service is running. Your paired, trusted devices will reconnect automatically from now on.

---

## Supported Distributions

| Distribution | Tested | Package manager |
|---|---|---|
| Ubuntu 22.04 / 24.04 | ✅ | apt |
| Debian 12 (Bookworm) | ✅ | apt |
| Kali Linux (rolling) | ✅ | apt |
| Linux Mint 21+ | ✅ | apt |
| Fedora 39 / 40 | ✅ | dnf |
| Arch Linux | ✅ | pacman |
| Manjaro | ✅ | pacman |
| openSUSE Tumbleweed | ✅ | zypper |

Requires: Python 3.10+, BlueZ, systemd, D-Bus.

---

## What It Does

- **Event-driven** — reacts instantly to adapter power-on, device appearance, and disconnect events via the BlueZ D-Bus API. No polling.
- **Multi-adapter** — scans and connects devices across every powered Bluetooth adapter simultaneously.
- **Safe by default** — only ever connects devices that are both *paired* and *trusted*. Nearby unknown devices are ignored.
- **Resilient** — failed connections retry with exponential backoff (1 s → 2 s → 4 s → … capped at 60 s). Multiple devices connect concurrently.
- **systemd-native** — ships a system-wide service (boot) and a per-user service (login). Logs to the journal automatically.

---

## Installation

### Automatic installer (recommended)

```bash
git clone https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect.git
cd bluetooth-autoconnect
sudo bash scripts/install.sh
```

The installer:
1. Detects your distribution automatically
2. Installs system dependencies (`bluez`, `dbus`, `python3`)
3. Creates an isolated virtualenv at `/opt/bluetooth-autoconnect`
4. Installs the package into that virtualenv
5. Symlinks the binary to `/usr/bin/bluetooth-autoconnect`
6. Installs the default config to `/etc/bluetooth-autoconnect/config.yaml`
7. Installs and enables the systemd service
8. Starts the service immediately

### Manual install (pip)

```bash
pip install --user .
```

Then follow the [systemd integration](#service-management) section below to enable the service.

### Native distro packages

For proper dependency tracking and package-manager-managed installs, see the guides under `packaging/`:

| Distro family | Guide |
|---|---|
| Debian / Ubuntu / Kali / Mint | [`packaging/debian/README.md`](packaging/debian/README.md) |
| Arch / Manjaro | [`packaging/arch/README.md`](packaging/arch/README.md) |
| Fedora / openSUSE | [`packaging/fedora/README.md`](packaging/fedora/README.md) |

---

## Usage

```
bluetooth-autoconnect [--daemon] [--debug] [--rescan-interval SECONDS]
                      [--max-attempts N] [--max-concurrency N] [--version]
                      [doctor]
```

| Command | Behaviour |
|---|---|
| `bluetooth-autoconnect` | Scan all powered adapters, connect every paired+trusted device once, then exit. |
| `bluetooth-autoconnect --daemon` | Run continuously. Reconnects via D-Bus events **and** periodic background scans. |
| `bluetooth-autoconnect --debug` | Enable DEBUG-level structured logging (combine with either mode). |
| `bluetooth-autoconnect --version` | Print installed version and exit. |
| `bluetooth-autoconnect doctor` | Run health checks and show PASS/FAIL output. |

### Options

| Flag | Default | Description |
|---|---|---|
| `--max-attempts N` | 5 | Connection attempts per device before giving up. |
| `--max-concurrency N` | 5 | Max simultaneous connection attempts. |
| `--rescan-interval SECONDS` | 30 | Seconds between periodic background scans. Set to `0` to disable. |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | All eligible devices connected (or none needed to). |
| 1 | At least one eligible device failed to connect. |
| 2 | Fatal D-Bus / BlueZ startup error. |
| 130 | Interrupted by Ctrl-C. |

### Examples

```bash
# One-shot: connect everything paired+trusted right now
bluetooth-autoconnect

# Run as a daemon (what the systemd service does)
bluetooth-autoconnect --daemon

# Debug a device that won't reconnect — verbose structured logs
bluetooth-autoconnect --daemon --debug

# Scan more aggressively: every 10 seconds
bluetooth-autoconnect --daemon --rescan-interval 10

# Disable periodic scanning, rely on D-Bus events only
bluetooth-autoconnect --daemon --rescan-interval 0

# Be more patient with a flaky headset
bluetooth-autoconnect --max-attempts 10

# Run health checks
bluetooth-autoconnect doctor
```

---

## Service Management

Two systemd units are provided:

| Unit | Scope | Starts at |
|---|---|---|
| `bluetooth-autoconnect.service` | System-wide (root) | Boot |
| `bluetooth-autoconnect.service` (user) | Per-user | Login |

### System-wide service

```bash
# Status
sudo systemctl status bluetooth-autoconnect

# Start / stop / restart
sudo systemctl start bluetooth-autoconnect
sudo systemctl stop bluetooth-autoconnect
sudo systemctl restart bluetooth-autoconnect

# Enable at boot / disable
sudo systemctl enable bluetooth-autoconnect
sudo systemctl disable bluetooth-autoconnect

# Live logs
journalctl -u bluetooth-autoconnect -f

# Trigger an immediate full rescan without restarting
sudo systemctl kill -s SIGHUP bluetooth-autoconnect
```

### Per-user service

```bash
# Enable at login
systemctl --user enable --now bluetooth-autoconnect

# Live logs
journalctl --user -u bluetooth-autoconnect -f
```

---

## Update

```bash
cd bluetooth-autoconnect
sudo bash scripts/update.sh
```

The updater pulls the latest source, upgrades the package in the virtualenv, refreshes the systemd units, and restarts the service.

---

## Uninstall

```bash
cd bluetooth-autoconnect
sudo bash scripts/uninstall.sh
```

Removes the service, binary symlink, virtualenv, and (optionally) the configuration directory. Your Bluetooth pairing data in BlueZ is never touched.

---

## Configuration

The default config lives at `/etc/bluetooth-autoconnect/config.yaml`. It is not overwritten on update.

```yaml
retry:
  max_attempts: 5      # attempts per device before giving up
  base_delay: 1.0      # seconds before first retry
  max_delay: 60.0      # cap on per-attempt backoff delay
  multiplier: 2.0      # exponential backoff factor

daemon:
  # Seconds between periodic background rescans (0 = disabled).
  # This is the primary fix for devices that return to range without
  # generating a BlueZ D-Bus event (see Automatic Reconnect Behavior).
  rescan_interval_seconds: 30
  max_concurrency: 5   # simultaneous connection attempts

logging:
  level: INFO          # DEBUG for verbose output

# Per-device priorities (higher = connect first)
# device_priorities:
#   AA:BB:CC:DD:EE:FF: 250

# Exclude specific devices from auto-connect
# blacklist:
#   - AA:BB:CC:DD:EE:FF
```

---

## How Device Selection Works

A device is auto-connected **only if BlueZ reports both**:

- `Paired: true` — you completed a pairing handshake, **and**
- `Trusted: true` — you (or BlueZ) marked it trusted

Anything else is skipped and logged at debug level. This tool never pairs or trusts devices on its own.

To trust an already-paired device:

```bash
bluetoothctl trust AA:BB:CC:DD:EE:FF
```

---

## Automatic Reconnect Behavior

bluetooth-autoconnect uses **two complementary mechanisms** to ensure devices reconnect as reliably as possible.

### 1. Event-driven reconnect (instant)

The daemon subscribes to BlueZ D-Bus signals. When one of the following events arrives, an immediate reconnect scan is triggered:

| Event | What happened |
|---|---|
| `Adapter.Powered = true` | Bluetooth adapter was switched on |
| `InterfacesAdded` (Device) | A known device object appeared on the bus |
| `Device.Connected = false` | A connected device dropped off |
| `Device.RSSI` updated | Device advertisement seen — device is back in range |
| `Device.Trusted = true` | A device was just trusted |
| `Device.Paired = true` | A device was just paired |

This covers the most common cases: locking/unlocking a laptop, turning Bluetooth off and on, or a device reconnecting from its own side.

### 2. Periodic background scan (the reconnect gap fix)

Some devices go out of range and come back **without generating any D-Bus event** — for example, headphones that wake slowly, or devices on noisy RF channels. In these cases the event-driven path never fires, so the daemon would never retry.

The periodic scanner wakes up every `rescan_interval_seconds` (default: 30 s), enumerates all disconnected trusted devices, and attempts to reconnect any that are not in their backoff window.

```
Timeline example:

t=0s    Device disconnects  → immediate reconnect attempt (fails: page-timeout)
t=1s    Backoff: wait 60 s
t=61s   Periodic scan fires → device still unreachable → fail (backoff: 120 s)
t=181s  Periodic scan fires → device still unreachable → fail (backoff: 240 s)
...
t=Xm    Device returns to range (no D-Bus event!)
t=Xm+30s Periodic scan fires → reconnect succeeds → backoff cleared
```

Control this with `--rescan-interval`:

```bash
bluetooth-autoconnect --daemon --rescan-interval 30   # default
bluetooth-autoconnect --daemon --rescan-interval 10   # more aggressive
bluetooth-autoconnect --daemon --rescan-interval 0    # disable (events only)
```

Or in `/etc/bluetooth-autoconnect/config.yaml`:

```yaml
daemon:
  rescan_interval_seconds: 30   # 0 = disabled
```

### Per-device smart backoff

To avoid hammering an unreachable device, each MAC address has its own backoff state independent of all others. After each failed reconnect attempt the cooldown doubles:

| Failure # | Wait before next attempt |
|---|---|
| 1 | 1 minute |
| 2 | 2 minutes |
| 3 | 4 minutes |
| 4 | 8 minutes |
| 5+ | 16 minutes (maximum: 30 minutes) |

The backoff is **reset immediately** when:
- A reconnect attempt succeeds.
- BlueZ reports `Device.RSSI` (device advertisement seen — device is in range).
- BlueZ reports `Device.Connected = true` (device connected on its own).

This means that if your headset turns itself on, the daemon will reconnect to it in at most one `rescan_interval` cycle — typically within 30 seconds.

### Forcing an immediate rescan

At any time:

```bash
sudo systemctl kill -s SIGHUP bluetooth-autoconnect
```

---

## Troubleshooting

### Service won't start — "org.bluez is not available"

BlueZ isn't running:

```bash
sudo systemctl enable --now bluetooth
sudo systemctl status bluetooth
```

### Devices found but never connect

1. Confirm the device is paired **and** trusted:
   ```bash
   bluetoothctl info AA:BB:CC:DD:EE:FF
   # Look for:  Paired: yes   Trusted: yes
   ```
2. If `Trusted: no`, run `bluetoothctl trust AA:BB:CC:DD:EE:FF`
3. Run with `--verbose` to see per-attempt logs

### Permission denied on Connect()

The system-wide service runs as root and has full BlueZ access. The per-user service needs your user in the `bluetooth` group on some distributions:

```bash
sudo usermod -aG bluetooth "$USER"
# Log out and back in
```

### Daemon doesn't react to device coming into range

Some devices only advertise for a short window after powering on. If BlueZ never sees the advertisement, there's nothing to react to — this is a firmware/BlueZ level issue. Trigger a manual rescan:

```bash
sudo systemctl kill -s SIGHUP bluetooth-autoconnect
```

### Logs not appearing in journalctl

Install the optional journal integration:
```bash
pip install "bluetooth-autoconnect[journal]"
```
Without it, stdout is captured by systemd automatically — logs still appear in `journalctl`, just without structured fields.

### Full troubleshooting guide

See [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) for all known issues.

---

## Development

```bash
# Clone and set up a dev environment
git clone https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect.git
cd bluetooth-autoconnect
make venv
source .venv/bin/activate

# Run tests
make test          # pytest with coverage (must be ≥ 90%)

# Lint and format
make lint          # ruff check
make format        # black
make typecheck     # mypy

# Build a wheel
make build         # output in dist/
```

### Project layout

```
src/bluetooth_autoconnect/
├── __init__.py         version metadata
├── __main__.py         python -m bluetooth_autoconnect
├── cli.py              argument parsing, entry point
├── connector.py        retry / backoff / concurrency
├── daemon.py           event loop, signal handling
├── dbus_client.py      BlueZ D-Bus wrapper (dbus-next)
├── exceptions.py       exception hierarchy
├── logging_setup.py    stdout + journal logging
└── models.py           Adapter / Device dataclasses

tests/                  pytest suite (no real D-Bus needed)
systemd/                system + user unit files
scripts/                install.sh  uninstall.sh  update.sh
packaging/              debian/  arch/  fedora/
docs/                   INSTALL.md  TROUBLESHOOTING.md  FAQ.md
```

### Running a specific test

```bash
pytest tests/test_connector.py -v
```

### Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Make your changes with tests
4. Ensure `make test lint typecheck` all pass
5. Open a pull request

---

## License

MIT — see [`LICENSE`](LICENSE).

---

## Author

**Bikram Kumar Das**
[github.com/Zero-day-Exploit-np](https://github.com/Zero-day-Exploit-np)
