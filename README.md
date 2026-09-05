<p align="center">
  <img src="https://raw.githubusercontent.com/Zero-day-Exploit-np/bluetooth-autoconnect/main/docs/assets/logo.png" alt="bluetooth-autoconnect" width="120" />
</p>

<h1 align="center">bluetooth-autoconnect</h1>

<p align="center">
  <strong>Automatically reconnect paired Bluetooth devices — event-driven, systemd-native, zero-configuration.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/bluetooth-autoconnect/"><img alt="PyPI" src="https://img.shields.io/pypi/v/bluetooth-autoconnect?color=blue&label=PyPI"></a>
  <a href="https://pypi.org/project/bluetooth-autoconnect/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/bluetooth-autoconnect"></a>
  <a href="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/test.yml"><img alt="Tests" src="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/test.yml/badge.svg"></a>
  <a href="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/lint.yml"><img alt="Lint" src="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/lint.yml/badge.svg"></a>
  <a href="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/releases"><img alt="Release" src="https://img.shields.io/github/v/release/Zero-day-Exploit-np/bluetooth-autoconnect?label=release"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/github/license/Zero-day-Exploit-np/bluetooth-autoconnect"></a>
  <a href="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/test.yml"><img alt="Coverage" src="https://img.shields.io/badge/coverage-90%25%2B-brightgreen"></a>
</p>

<p align="center">
  <a href="#what-is-bluetooth-autoconnect">Overview</a> •
  <a href="#-installation">Install</a> •
  <a href="#-usage">Usage</a> •
  <a href="#-hooks">Hooks</a> •
  <a href="#-configuration">Config</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-project-status">Platform Status</a> •
  <a href="#-roadmap">Roadmap</a> •
  <a href="#-troubleshooting">Troubleshooting</a> •
  <a href="#-contributing">Contributing</a>
</p>

---

## What is bluetooth-autoconnect?

Most Linux Bluetooth tools reconnect devices only when a desktop session is running. **bluetooth-autoconnect** is different — it works at the system level, before any user logs in, with no desktop environment required.

- Runs as a **systemd service** — reconnects headphones, mice, keyboards, and speakers at boot
- Talks to [BlueZ](https://www.bluez.org/) **directly over D-Bus** — no `bluetoothctl` shelling, no polling
- Fixes the **"silent return" gap** — catches devices that wake up without firing a D-Bus event via periodic background scanning
- Backs off **per device, independently** — a flaky headset does not delay your keyboard from reconnecting
- Fires **custom scripts** on connect/disconnect events via a hook system
- Built on a **cross-platform backend architecture** — Linux is fully supported today; Windows support is in active development

---

## ✨ Feature Highlights

| Feature | Details |
|---|---|
| **Instant event-driven reconnect** | Subscribes to BlueZ D-Bus signals; reacts within milliseconds of adapter power-on, device appearance, or disconnect |
| **Periodic background scan** | Wakes every 30 s (configurable) to catch devices that return silently |
| **Per-device exponential backoff** | 1 min → 2 → 4 → 8 → 16 min cap; each MAC address is tracked independently |
| **Multi-adapter support** | Scans all powered adapters simultaneously |
| **Hook system** | Run custom scripts automatically on connect or disconnect events |
| **Safe by default** | Only connects `Paired: yes` + `Trusted: yes` devices; never pairs automatically |
| **systemd-native** | System-wide and per-user service units; structured journal logging |
| **Doctor command** | `bluetooth-autoconnect doctor` — instant PASS/FAIL system diagnostics |
| **Cross-platform architecture** | `BluetoothBackend` protocol ready for Linux, Windows, and future platforms |
| **Zero native extensions** | Pure Python; no C extensions or compiled dependencies |

---

## 📦 Installation

### Option 1 — One-command installer (recommended)

Clones the repo, installs system dependencies, creates an isolated virtualenv, configures systemd, and starts the service:

```bash
git clone https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect.git
cd bluetooth-autoconnect
sudo bash scripts/install.sh
```

The service starts immediately. Devices reconnect automatically from this point on.

### Option 2 — PyPI

```bash
pip install bluetooth-autoconnect
```

> Requires Python 3.10+.  
> For structured journal logging, add the optional extra:
> ```bash
> pip install "bluetooth-autoconnect[journal]"
> ```

After installing via PyPI, enable the systemd service manually:

```bash
sudo install -Dm644 systemd/bluetooth-autoconnect.service \
    /usr/lib/systemd/system/bluetooth-autoconnect.service
sudo systemctl daemon-reload
sudo systemctl enable --now bluetooth-autoconnect
```

### Option 3 — Native distro packages

For package-manager-managed installs with proper dependency tracking:

| Distro family | Build guide |
|---|---|
| Debian / Ubuntu / Kali / Mint | [`packaging/debian/README.md`](packaging/debian/README.md) |
| Arch Linux / Manjaro | [`packaging/arch/README.md`](packaging/arch/README.md) |
| Fedora / openSUSE | [`packaging/fedora/README.md`](packaging/fedora/README.md) |

---

## 🖥️ Supported Platforms

### Linux distributions

| Distribution | Status | Package manager |
|---|---|---|
| Ubuntu 22.04 / 24.04 | ✅ Tested | apt |
| Debian 12 (Bookworm) | ✅ Tested | apt |
| Kali Linux (rolling) | ✅ Tested | apt |
| Linux Mint 21+ | ✅ Tested | apt |
| Fedora 39 / 40 | ✅ Tested | dnf |
| Arch Linux | ✅ Tested | pacman |
| Manjaro | ✅ Tested | pacman |
| openSUSE Tumbleweed | ✅ Tested | zypper |

**Requirements:** Python 3.10+, BlueZ ≥ 5.x, systemd, D-Bus.

### Platform overview

See [Project Status](#-project-status) for the full cross-platform readiness matrix.

---

## 🚀 Usage

```
bluetooth-autoconnect [--daemon] [--debug] [--rescan-interval SECONDS]
                      [--max-attempts N] [--max-concurrency N]
                      [--backend NAME] [--config FILE] [--version]
                      [doctor]
```

### Commands

| Command | Behaviour |
|---|---|
| `bluetooth-autoconnect` | One-shot scan — connect all paired+trusted devices once, then exit |
| `bluetooth-autoconnect --daemon` | Run continuously; reconnects via backend events and periodic scans |
| `bluetooth-autoconnect doctor` | Health-check diagnostics; prints PASS/FAIL for every system component |
| `bluetooth-autoconnect --version` | Print the installed version and exit |

### Flags

| Flag | Default | Description |
|---|---|---|
| `--debug` | off | Structured DEBUG-level logging with per-device fields |
| `--max-attempts N` | `5` | Connect attempts per device before giving up |
| `--max-concurrency N` | `5` | Maximum simultaneous connect attempts |
| `--rescan-interval SECONDS` | `30` | Periodic background scan interval (`0` = disable) |
| `--backend NAME` | auto | Force a specific backend: `linux` or `windows` |
| `--config FILE` | `/etc/bluetooth-autoconnect/config.yaml` | Path to configuration file |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | All eligible devices connected (or none were needed) |
| `1` | At least one eligible device failed to connect |
| `2` | Fatal backend / startup error |
| `130` | Interrupted by Ctrl-C |

### Examples

```bash
# Connect everything right now (one-shot)
bluetooth-autoconnect

# Run as a daemon (the same mode the systemd service uses)
bluetooth-autoconnect --daemon

# Debug a device that isn't reconnecting — structured per-attempt logs
bluetooth-autoconnect --daemon --debug

# Scan every 10 s instead of every 30 s
bluetooth-autoconnect --daemon --rescan-interval 10

# Disable periodic scanning; rely only on D-Bus events
bluetooth-autoconnect --daemon --rescan-interval 0

# Allow more retries for a flaky headset
bluetooth-autoconnect --max-attempts 10

# Run system health checks before troubleshooting
bluetooth-autoconnect doctor
```

---

## 🔄 Automatic Reconnect Behavior

bluetooth-autoconnect uses two complementary mechanisms to ensure devices reconnect as reliably as possible.

### 1 — Event-driven reconnect (instant)

The daemon subscribes to BlueZ D-Bus signals and fires an immediate reconnect attempt on any of these events:

| Signal | Trigger |
|---|---|
| `Adapter.Powered = true` | Bluetooth adapter switched on |
| `InterfacesAdded` (Device) | Known device object appeared on the bus |
| `Device.Connected = false` | A connected device dropped off |
| `Device.RSSI` updated | Device advertisement received — it's back in range |
| `Device.Trusted = true` | Device was just marked trusted |
| `Device.Paired = true` | Device was just paired |

### 2 — Periodic background scan (the silent-return fix)

Some devices return to range without firing any D-Bus event — slowly waking headphones, congested RF environments, or BLE devices with long advertisement intervals. The periodic scanner catches these.

```
t=0s     Device disconnects → immediate attempt (fails: page-timeout)
t=1s     Backoff: wait 60 s
t=61s    Periodic scan → still unreachable → fail (backoff: 120 s)
  ···
t=Xm     Device silently returns to range
t=Xm+30s Periodic scan → reconnect succeeds → backoff cleared ✓
```

### Per-device backoff schedule

| Consecutive failures | Wait before next attempt |
|---|---|
| 1 | 1 minute |
| 2 | 2 minutes |
| 3 | 4 minutes |
| 4 | 8 minutes |
| 5+ | 16 minutes (hard cap: 30 minutes) |

Backoff resets immediately on successful reconnect, an RSSI signal, or `Device.Connected = true`.

### Force an immediate rescan

```bash
sudo systemctl kill -s SIGHUP bluetooth-autoconnect
```

---

## 🔧 Device Selection

A device is auto-connected **only when BlueZ reports both**:

- `Paired: yes` — pairing handshake completed, **and**
- `Trusted: yes` — device marked trusted

Everything else is skipped silently. bluetooth-autoconnect **never pairs or trusts devices automatically**.

To trust an already-paired device:

```bash
bluetoothctl trust AA:BB:CC:DD:EE:FF
```

---

## 🪝 Hooks

Run custom scripts automatically when a device connects or disconnects. Hooks execute asynchronously — a slow or failing script cannot stall the daemon.

### Configuration

```yaml
# /etc/bluetooth-autoconnect/config.yaml
hooks:
  timeout_seconds: 30   # kill script after this many seconds (0 = no limit)

  on_connect:
    - /usr/local/bin/bt-connected.sh

  on_disconnect:
    - /usr/local/bin/bt-disconnected.sh
```

### Environment variables

Every hook script receives the triggering event through environment variables:

| Variable | Example | Description |
|---|---|---|
| `BT_EVENT` | `connected` | `connected` or `disconnected` |
| `BT_DEVICE_MAC` | `AA:BB:CC:DD:EE:FF` | MAC address of the device |
| `BT_DEVICE_NAME` | `JBL Speaker` | Human-readable device name |
| `BT_DEVICE_PATH` | `/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF` | Backend device path |
| `BT_ADAPTER_PATH` | `/org/bluez/hci0` | Backend adapter path |

### Example script

```bash
#!/usr/bin/env bash
# /usr/local/bin/bt-connected.sh

set -euo pipefail

echo "$(date): $BT_DEVICE_NAME connected ($BT_DEVICE_MAC)" >> /var/log/bt-events.log

# Route audio to the Bluetooth device
if command -v pactl &>/dev/null; then
    pactl set-default-sink bluez_sink.${BT_DEVICE_MAC//:/_}.a2dp_sink 2>/dev/null || true
fi
```

Make scripts executable:

```bash
chmod +x /usr/local/bin/bt-connected.sh
```

Hook failures are logged and swallowed — they will never crash the daemon.

---

## ⚙️ Configuration

The config file lives at `/etc/bluetooth-autoconnect/config.yaml` and is **never overwritten** by updates.

```yaml
retry:
  max_attempts: 5       # connect attempts per device before giving up
  base_delay: 1.0       # seconds before the first retry
  max_delay: 60.0       # cap on per-attempt delay
  multiplier: 2.0       # exponential backoff multiplier

daemon:
  rescan_interval_seconds: 30   # periodic background scan interval (0 = disabled)
  max_concurrency: 5            # maximum simultaneous connect attempts

logging:
  level: INFO           # set to DEBUG for verbose per-device logs

hooks:
  timeout_seconds: 30
  on_connect:
    - /usr/local/bin/bt-connected.sh
  on_disconnect:
    - /usr/local/bin/bt-disconnected.sh

# Per-device connection priority (higher = connect first)
# device_priorities:
#   AA:BB:CC:DD:EE:FF: 250

# Prevent specific devices from ever auto-connecting
# blacklist:
#   - AA:BB:CC:DD:EE:FF
```

---

## 🏗️ Architecture

bluetooth-autoconnect v1.2.0 introduces a clean platform abstraction layer. All reconnection logic, backoff tracking, and hook execution live in the platform-agnostic core. Only the backend layer touches OS-specific APIs.

```
┌─────────────────────────────────────────────┐
│              CLI  (cli.py)                  │
│         AutoConnectDaemon (daemon.py)       │
│   Connector · Hooks · Config · Doctor       │
│           ── Core Logic ──                  │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
        ┌──────────────────────┐
        │   BluetoothBackend   │  ← Protocol (backends/__init__.py)
        │     (Protocol)       │
        └──────────┬───────────┘
                   │
         ┌─────────┴──────────┐
         │                    │
         ▼                    ▼
┌─────────────────┐  ┌─────────────────────┐
│  LinuxBackend   │  │   WindowsBackend    │
│  (backends/     │  │   (backends/        │
│   linux.py)     │  │    windows.py)      │
│                 │  │                     │
│  BlueZ D-Bus    │  │  WinRT APIs         │
│  dbus-next      │  │  (architecture      │
│  ✅ Production  │  │   ready, WinRT      │
│                 │  │   impl in progress) │
└─────────────────┘  └─────────────────────┘
```

The `BluetoothBackend` protocol defines six methods every backend must implement:

```python
class BluetoothBackend(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def get_adapters(self) -> list[Adapter]: ...
    async def get_devices(self, adapter_path=None) -> list[Device]: ...
    async def connect_device(self, device_path: str) -> None: ...
    async def subscribe(self, callback: EventCallback) -> None: ...
```

The daemon receives a backend instance at startup via dependency injection:

```python
daemon = AutoConnectDaemon(backend=create_backend())
```

`create_backend()` auto-detects the current platform. You can override it:

```bash
bluetooth-autoconnect --daemon --backend linux
```

---

## 📊 Project Status

### Platform support matrix

| Platform | Status | Backend | Notes |
|---|---|---|---|
| **Linux** (BlueZ) | ✅ **Fully Supported** | `LinuxBackend` | Production-ready since v1.0.0 |
| **Windows** | 🚧 **Architecture Ready** | `WindowsBackend` | Backend skeleton in place; WinRT implementation in progress (v1.3.0+) |
| **macOS** | ❌ Not Supported | — | No CoreBluetooth backend yet; contributions welcome |

### Test coverage

| Metric | Value |
|---|---|
| Total automated tests | 262 |
| Code coverage | ≥ 90% |
| Platforms tested in CI | Linux (Ubuntu, Fedora, Arch) |
| Type checking | mypy strict — zero errors |
| Linting | ruff — zero warnings |

---

## 🛠️ Service Management

### System-wide service (recommended)

```bash
# Status
sudo systemctl status bluetooth-autoconnect

# Start / stop / restart
sudo systemctl start   bluetooth-autoconnect
sudo systemctl stop    bluetooth-autoconnect
sudo systemctl restart bluetooth-autoconnect

# Enable / disable at boot
sudo systemctl enable  bluetooth-autoconnect
sudo systemctl disable bluetooth-autoconnect

# Stream live logs
journalctl -u bluetooth-autoconnect -f

# Trigger immediate full rescan without restarting
sudo systemctl kill -s SIGHUP bluetooth-autoconnect
```

### Per-user service

```bash
# Enable at login
systemctl --user enable --now bluetooth-autoconnect

# Stream live logs
journalctl --user -u bluetooth-autoconnect -f
```

---

## 🔍 Diagnostics

`bluetooth-autoconnect doctor` checks every prerequisite and prints a clear PASS/FAIL/WARN report:

```
bluetooth-autoconnect doctor

  [PASS] bluetooth.service — active
  [PASS] D-Bus system bus  — socket reachable at /run/dbus/system_bus_socket
  [PASS] Bluetooth backend — backend available
  [PASS] Adapter hci0      — powered — address=AA:BB:CC:DD:EE:FF
  [PASS] Paired devices    — 3 paired device(s) found
  [PASS] Trusted device: JBL Speaker — connected — mac=AA:BB:CC:DD:EE:FF
  [WARN] Trusted device: Sony WH-1000XM5 — not connected — mac=11:22:33:44:55:66

  All checks passed.
```

Exit code `0` = all checks passed. Exit code `1` = at least one hard failure.

---

## 🔁 Update

```bash
cd bluetooth-autoconnect
sudo bash scripts/update.sh
```

Pulls the latest source, upgrades the package, refreshes systemd units, and restarts the service.

---

## 🗑️ Uninstall

```bash
cd bluetooth-autoconnect
sudo bash scripts/uninstall.sh
```

Removes the service, binary symlink, and virtualenv. Your Bluetooth pairing data in BlueZ is never touched.

---

## ❓ Troubleshooting

### `org.bluez is not available on the system bus`

BlueZ is not running:

```bash
sudo systemctl enable --now bluetooth
sudo systemctl status bluetooth
```

### Devices are found but never connect

1. Check the device is **paired and trusted**:
   ```bash
   bluetoothctl info AA:BB:CC:DD:EE:FF
   # Must show:  Paired: yes   Trusted: yes
   ```
2. If `Trusted: no`: `bluetoothctl trust AA:BB:CC:DD:EE:FF`
3. Run with `--debug` to see per-attempt structured logs

### Permission denied on `Connect()`

The system-wide service has full BlueZ access. The per-user service may need group membership:

```bash
sudo usermod -aG bluetooth "$USER"
# Log out and back in
```

### Daemon doesn't react when a device comes back into range

The periodic scanner handles this (enabled by default). If you've disabled it:

```bash
bluetooth-autoconnect --daemon --rescan-interval 30
```

Or in the config file:
```yaml
daemon:
  rescan_interval_seconds: 30
```

### Notifications firing in a loop (connect/disconnect cycling)

Upgrade to **v1.1.1** or later. Earlier versions had a bug where reconnect attempts fired false-positive `on_connect` hooks before profile negotiation completed.

For the full troubleshooting guide, see [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

---

## 👩‍💻 Development

```bash
git clone https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect.git
cd bluetooth-autoconnect
make venv
source .venv/bin/activate
```

| Task | Command |
|---|---|
| Run test suite (coverage ≥ 90%) | `make test` |
| Lint | `make lint` |
| Auto-format | `make format` |
| Type-check | `make typecheck` |
| Build wheel | `make build` |

### Project layout

```
src/bluetooth_autoconnect/
├── backends/
│   ├── __init__.py     BluetoothBackend protocol + create_backend() factory
│   ├── linux.py        LinuxBackend — BlueZ D-Bus via dbus-next
│   └── windows.py      WindowsBackend — WinRT skeleton (in progress)
├── cli.py              Argument parsing and main entry point
├── connector.py        Retry / backoff / concurrency logic
├── daemon.py           Event loop, periodic scan, signal handling
├── dbus_client.py      Backward-compat shim → backends/linux.py
├── doctor.py           Health-check diagnostics
├── exceptions.py       Exception hierarchy
├── hooks.py            Hook execution engine
├── logging_setup.py    stdout + journal logging
└── models.py           Adapter / Device dataclasses

tests/                  262 pytest tests — no real D-Bus required
systemd/                System + user service units
scripts/                install.sh   uninstall.sh   update.sh
packaging/              debian/   arch/   fedora/
docs/                   INSTALL.md   TROUBLESHOOTING.md   FAQ.md
```

### Adding a new backend

Implement the `BluetoothBackend` protocol from `backends/__init__.py`:

```python
from bluetooth_autoconnect.backends import BluetoothBackend

class MyBackend:
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def get_adapters(self) -> list[Adapter]: ...
    async def get_devices(self, adapter_path=None) -> list[Device]: ...
    async def connect_device(self, device_path: str) -> None: ...
    async def subscribe(self, callback: EventCallback) -> None: ...
```

Register it in `create_backend()` in `backends/__init__.py` and it will be available as `--backend myplatform`.

---

## 🗺️ Roadmap

### v1.3.0 — Windows device discovery

- Windows adapter enumeration via `Windows.Devices.Radios`
- Windows device enumeration (Classic Bluetooth + BLE)
- Windows adapter detection in `doctor`
- Basic `--backend windows` support

### v1.4.0 — Windows reconnect

- Windows connection status event subscriptions via WinRT
- RFCOMM and GATT connection support
- Periodic scanner on Windows
- Windows `doctor` health checks

### v2.0.0 — Official multi-platform release

- Fully supported Linux + Windows
- Windows native packaging (`.exe`, `.msi`)
- Windows service integration (Windows Service API)
- CI testing on Windows runners
- macOS backend investigation

> **Note:** Linux support is not affected by any of this work. The Linux backend is stable, production-ready, and changes to the backend architecture are fully backward-compatible.

---

## 📋 Release Highlights

### v1.2.0 — Cross-platform backend architecture

- **`BluetoothBackend` Protocol** — typed interface all backends must satisfy
- **`LinuxBackend`** — the existing BlueZ/D-Bus implementation, extracted to `backends/linux.py`
- **`WindowsBackend` skeleton** — architecture in place; WinRT implementation follows in v1.3.0+
- **`create_backend()` factory** — auto-detects the current platform; accepts `--backend` override
- **Backend-agnostic daemon** — `AutoConnectDaemon` accepts any `BluetoothBackend` via dependency injection
- **Backend-agnostic doctor** — platform pre-checks (D-Bus on Linux, WinRT on Windows) selected at runtime
- **`dbus_client.py` shim** — backward-compatible re-export; existing code and tests unchanged
- **262 automated tests** — all passing; 90%+ coverage

### v1.1.1 — Hook notification stability

- Fixed duplicate `on_connect` / `on_disconnect` notifications from repeated BlueZ `Connected=False` signals during profile negotiation
- State tracker (`_DeviceStateTracker`) ensures hooks fire only on genuine state transitions

### v1.1.0 — Hook system

- Execute custom scripts on device connect and disconnect
- Scripts receive device context via `BT_*` environment variables
- Configurable per-hook timeout with automatic process kill
- Full stdout/stderr capture and structured logging

### v1.0.0 — Initial release

- Event-driven reconnect via BlueZ D-Bus
- Periodic background scanner for silent device returns
- Per-device exponential backoff
- systemd system-wide and per-user service units
- `bluetooth-autoconnect doctor` diagnostics
- 90%+ test coverage from day one

---

## 🤝 Contributing

Contributions are welcome. Please:

1. Fork the repository
2. Create a feature branch — `git checkout -b feat/my-feature`
3. Add tests for new behaviour
4. Verify `make test lint typecheck` all pass
5. Open a pull request with a clear description

**Bug reports:** use the [issue tracker](https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/issues) and include the output of `bluetooth-autoconnect doctor` and `journalctl -u bluetooth-autoconnect --since "1 hour ago"`.

**Windows contributors:** the `WindowsBackend` skeleton in `backends/windows.py` is ready for WinRT implementation. See the [architecture section](#architecture) for the interface contract and the [roadmap](#roadmap) for the planned scope. Contributions for v1.3.0 are especially welcome.

---

## 📄 License

MIT — see [`LICENSE`](LICENSE).

---

## 👤 Author

**Bikram Kumar Das** · [github.com/Zero-day-Exploit-np](https://github.com/Zero-day-Exploit-np) · bikramkumardas@proton.me

---

<p align="center">
  If bluetooth-autoconnect saves you frustration, consider giving it a ⭐ on GitHub.
</p>
