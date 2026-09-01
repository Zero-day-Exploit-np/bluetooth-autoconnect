<p align="center">
  <img src="https://raw.githubusercontent.com/Zero-day-Exploit-np/bluetooth-autoconnect/main/docs/assets/logo.png" alt="bluetooth-autoconnect" width="120" />
</p>

<h1 align="center">bluetooth-autoconnect</h1>

<p align="center">
  <strong>Automatically reconnect paired Bluetooth devices on Linux — no desktop environment required.</strong><br>
  Event-driven, systemd-native, zero-configuration.
</p>

<p align="center">
  <a href="https://pypi.org/project/bluetooth-autoconnect/"><img alt="PyPI" src="https://img.shields.io/pypi/v/bluetooth-autoconnect?color=blue&label=PyPI"></a>
  <a href="https://pypi.org/project/bluetooth-autoconnect/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/bluetooth-autoconnect"></a>
  <a href="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/test.yml"><img alt="Tests" src="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/test.yml/badge.svg"></a>
  <a href="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/lint.yml"><img alt="Lint" src="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/actions/workflows/lint.yml/badge.svg"></a>
  <a href="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/releases"><img alt="Release" src="https://img.shields.io/github/v/release/Zero-day-Exploit-np/bluetooth-autoconnect?label=release"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/Zero-day-Exploit-np/bluetooth-autoconnect"></a>
</p>

<p align="center">
  <a href="#-installation">Install</a> •
  <a href="#-usage">Usage</a> •
  <a href="#-configuration">Config</a> •
  <a href="#-automatic-reconnect-behavior">How it works</a> •
  <a href="#-troubleshooting">Troubleshooting</a> •
  <a href="#-contributing">Contributing</a>
</p>

---

## Why bluetooth-autoconnect?

Most Linux Bluetooth tools reconnect devices only when a desktop session is available. **bluetooth-autoconnect** is different:

- Runs as a **system service** — reconnects at boot, before any user logs in
- Talks to [BlueZ](https://www.bluez.org/) directly over **D-Bus** — no shelling out to `bluetoothctl`, no polling
- Handles the **"silent return" gap** — devices that come back into range without firing a D-Bus event are caught by a periodic background scanner
- Backs off **per device** — a flaky headset won't block your keyboard from reconnecting
- Works headlessly on servers, Raspberry Pi, embedded Linux, kiosk machines, and normal desktops alike

---

## ✨ Feature Highlights

| Feature | Details |
|---|---|
| **Instant event-driven reconnect** | Subscribes to BlueZ D-Bus signals; reacts within milliseconds of adapter power-on, device appearance, or disconnect |
| **Periodic background scan** | Wakes every 30 s (configurable) to catch devices that return silently — fixes the most common reconnect gap |
| **Per-device exponential backoff** | 1 min → 2 → 4 → 8 → 16 min cap; each MAC is tracked independently |
| **Multi-adapter support** | Scans all powered adapters simultaneously |
| **Safe by default** | Only ever connects `Paired: yes` + `Trusted: yes` devices; no automatic pairing |
| **systemd integration** | System-wide *and* per-user service units; structured journal logging |
| **Health check** | `bluetooth-autoconnect doctor` — instant PASS/FAIL diagnostics |
| **Zero runtime dependencies** | Pure Python D-Bus client; no native extensions required |

---

## 📦 Installation

Choose the method that fits your workflow.

### Option 1 — PyPI (quickest)

```bash
pip install bluetooth-autoconnect
```

> Requires Python 3.10+. For journal integration install the optional extra:
> ```bash
> pip install "bluetooth-autoconnect[journal]"
> ```

Then install the systemd service manually:

```bash
# System-wide (runs at boot, recommended)
sudo install -Dm644 /path/to/systemd/bluetooth-autoconnect.service \
    /usr/lib/systemd/system/bluetooth-autoconnect.service
sudo systemctl daemon-reload
sudo systemctl enable --now bluetooth-autoconnect
```

### Option 2 — Automatic installer (recommended for full setup)

Clones the repo, installs deps, creates an isolated virtualenv, wires up systemd, and starts the service — all in one step:

```bash
git clone https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect.git
cd bluetooth-autoconnect
sudo bash scripts/install.sh
```

Done. The service is running.

### Option 3 — Native distro packages

For proper package-manager-managed installs with dependency tracking:

| Distro family | Build guide |
|---|---|
| Debian / Ubuntu / Kali / Mint | [`packaging/debian/README.md`](packaging/debian/README.md) |
| Arch Linux / Manjaro | [`packaging/arch/README.md`](packaging/arch/README.md) |
| Fedora / openSUSE | [`packaging/fedora/README.md`](packaging/fedora/README.md) |

---

## 🖥️ Supported Distributions

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

---

## 🚀 Usage

```
bluetooth-autoconnect [--daemon] [--debug] [--rescan-interval SECONDS]
                      [--max-attempts N] [--max-concurrency N] [--version]
                      [doctor]
```

### Commands

| Command | Behaviour |
|---|---|
| `bluetooth-autoconnect` | One-shot scan — connect all paired+trusted devices once, then exit |
| `bluetooth-autoconnect --daemon` | Run continuously; reconnects via D-Bus events **and** periodic scans |
| `bluetooth-autoconnect doctor` | Run health checks; print PASS/FAIL for each system component |
| `bluetooth-autoconnect --version` | Print installed version and exit |

### Flags

| Flag | Default | Description |
|---|---|---|
| `--debug` | off | Structured DEBUG-level logging with per-device fields |
| `--max-attempts N` | `5` | Connect attempts per device before giving up |
| `--max-concurrency N` | `5` | Maximum simultaneous connect attempts |
| `--rescan-interval SECONDS` | `30` | Periodic background scan interval. `0` = disable |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | All eligible devices connected (or none were needed) |
| `1` | At least one eligible device failed to connect |
| `2` | Fatal D-Bus / BlueZ startup error |
| `130` | Interrupted by Ctrl-C |

### Examples

```bash
# Connect everything right now (one-shot)
bluetooth-autoconnect

# Run as a daemon — the same mode the systemd service uses
bluetooth-autoconnect --daemon

# Debug a device that isn't reconnecting
bluetooth-autoconnect --daemon --debug

# Scan more aggressively (every 10 s instead of every 30 s)
bluetooth-autoconnect --daemon --rescan-interval 10

# Disable periodic scanning; rely only on D-Bus events
bluetooth-autoconnect --daemon --rescan-interval 0

# Allow more retries for a flaky headset
bluetooth-autoconnect --max-attempts 10

# Check system health before troubleshooting
bluetooth-autoconnect doctor
```

---

## ⚙️ Configuration

The config file lives at `/etc/bluetooth-autoconnect/config.yaml`.  
It is **never overwritten** by updates.

```yaml
retry:
  max_attempts: 5       # connect attempts per device before giving up
  base_delay: 1.0       # seconds before first retry
  max_delay: 60.0       # cap on per-attempt delay
  multiplier: 2.0       # exponential backoff factor

daemon:
  rescan_interval_seconds: 30   # periodic scan interval (0 = disabled)
  max_concurrency: 5            # simultaneous connect attempts

logging:
  level: INFO           # change to DEBUG for verbose output

# Per-device priority (higher value = connect first)
# device_priorities:
#   AA:BB:CC:DD:EE:FF: 250

# Prevent specific devices from ever auto-connecting
# blacklist:
#   - AA:BB:CC:DD:EE:FF
```

---

## 🔄 Automatic Reconnect Behavior

bluetooth-autoconnect uses two complementary mechanisms.

### 1 — Event-driven reconnect (instant)

The daemon subscribes to BlueZ D-Bus signals and fires an immediate connect attempt when any of these events arrive:

| D-Bus signal | Trigger condition |
|---|---|
| `Adapter.Powered = true` | Bluetooth adapter switched on |
| `InterfacesAdded` (Device) | Known device object appeared on the bus |
| `Device.Connected = false` | A connected device dropped off |
| `Device.RSSI` updated | Device advertisement received — it's back in range |
| `Device.Trusted = true` | Device was just marked trusted |
| `Device.Paired = true` | Device was just paired |

### 2 — Periodic background scan (the silent-return fix)

Some devices return to range **without advertising any D-Bus event** — slowly waking headphones, congested RF environments, BLE devices with long advertisement intervals. The event-driven path never fires for these.

The periodic scanner wakes every `rescan_interval_seconds` (default 30 s), enumerates all disconnected trusted devices, skips any still inside their backoff window, and retries the rest.

```
t=0s     Device disconnects → immediate attempt (fails: page-timeout)
t=1s     Backoff: wait 60 s
t=61s    Periodic scan → device unreachable → fail (backoff: 120 s)
t=181s   Periodic scan → device unreachable → fail (backoff: 240 s)
  ···
t=Xm     Device returns silently — no D-Bus event
t=Xm+30s Periodic scan → reconnect succeeds → backoff cleared ✓
```

### Per-device backoff schedule

Each MAC address tracks its own backoff independently:

| Consecutive failures | Cooldown before next attempt |
|---|---|
| 1 | 1 minute |
| 2 | 2 minutes |
| 3 | 4 minutes |
| 4 | 8 minutes |
| 5+ | 16 minutes (hard cap: 30 minutes) |

Backoff **resets immediately** on:
- Successful reconnect
- `Device.RSSI` signal (device is in range)
- `Device.Connected = true` (device connected on its own)

### Trigger an immediate rescan at any time

```bash
sudo systemctl kill -s SIGHUP bluetooth-autoconnect
```

---

## 🔧 How Device Selection Works

A device is auto-connected **only when BlueZ reports both**:

- `Paired: yes` — you completed the pairing handshake, **and**
- `Trusted: yes` — you (or BlueZ) marked the device as trusted

Unpaired devices, paired-but-untrusted devices, and unknown nearby devices are silently skipped and logged at `DEBUG` level. This tool **never pairs or trusts devices on its own**.

To trust an already-paired device:

```bash
bluetoothctl trust AA:BB:CC:DD:EE:FF
```

---

## 🛠️ Service Management

Two systemd units ship with the project:

| Unit | Scope | Starts at |
|---|---|---|
| `bluetooth-autoconnect.service` | System-wide (runs as root) | Boot |
| `bluetooth-autoconnect.service` (user) | Per-user session | Login |

### System-wide service

```bash
# View status
sudo systemctl status bluetooth-autoconnect

# Control
sudo systemctl start   bluetooth-autoconnect
sudo systemctl stop    bluetooth-autoconnect
sudo systemctl restart bluetooth-autoconnect

# Enable / disable at boot
sudo systemctl enable  bluetooth-autoconnect
sudo systemctl disable bluetooth-autoconnect

# Stream live logs
journalctl -u bluetooth-autoconnect -f

# Trigger an immediate full rescan without restarting
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

`bluetooth-autoconnect doctor` checks every prerequisite and prints a clear report:

```
bluetooth-autoconnect doctor

  [PASS] bluetooth.service — active
  [PASS] D-Bus system bus  — socket reachable
  [PASS] BlueZ available   — org.bluez found
  [PASS] Adapter hci0      — powered, address=AA:BB:CC:DD:EE:FF
  [PASS] Trusted device: JBL Speaker — connected
  [WARN] Trusted device: Sony WH-1000XM5 — not connected (mac=11:22:33:44:55:66)
```

Exit code `0` = all checks passed. Exit code `1` = at least one hard failure.

---

## 🔄 Update

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

Removes the service, binary symlink, and virtualenv. Optionally removes the configuration directory. Your Bluetooth pairing data in BlueZ is never touched.

---

## ❓ Troubleshooting

### `org.bluez is not available on the system bus`

BlueZ is not running. Start it:

```bash
sudo systemctl enable --now bluetooth
sudo systemctl status bluetooth
```

### Devices are found but never connect

1. Confirm the device is **paired and trusted**:
   ```bash
   bluetoothctl info AA:BB:CC:DD:EE:FF
   # Must show:  Paired: yes   Trusted: yes
   ```
2. If `Trusted: no`: `bluetoothctl trust AA:BB:CC:DD:EE:FF`
3. Run with `--debug` to see per-attempt structured logs

### Permission denied on `Connect()`

The system-wide service has full BlueZ access. The per-user service may need your user in the `bluetooth` group:

```bash
sudo usermod -aG bluetooth "$USER"
# Log out and back in
```

### Daemon doesn't react when a device comes back into range

Enable the periodic scanner (it's on by default). If you've disabled it, re-enable it:

```bash
bluetooth-autoconnect --daemon --rescan-interval 30
```

Or in the config file:
```yaml
daemon:
  rescan_interval_seconds: 30
```

For the full guide, see [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

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
├── cli.py              argument parsing and entry point
├── connector.py        retry / backoff / concurrency
├── daemon.py           event loop, periodic scan, signal handling
├── dbus_client.py      BlueZ D-Bus wrapper (dbus-next)
├── doctor.py           health-check diagnostics
├── exceptions.py       exception hierarchy
├── logging_setup.py    stdout + journal logging
└── models.py           Adapter / Device dataclasses

tests/                  pytest suite — no real D-Bus required
systemd/                system + user service units
scripts/                install.sh   uninstall.sh   update.sh
packaging/              debian/   arch/   fedora/
docs/                   INSTALL.md   TROUBLESHOOTING.md   FAQ.md
```

---

## 🤝 Contributing

Contributions are welcome. Please:

1. Fork the repository
2. Create a feature branch — `git checkout -b feat/my-feature`
3. Add tests for new behaviour
4. Verify `make test lint typecheck` all pass
5. Open a pull request with a clear description

For bug reports, use the [issue tracker](https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/issues) and include the output of `bluetooth-autoconnect doctor` and `journalctl -u bluetooth-autoconnect --since "1 hour ago"`.

---

## 📄 License

MIT — see [`LICENSE`](LICENSE).

---

## 👤 Author

**Bikram Kumar Das** · [github.com/Zero-day-Exploit-np](https://github.com/Zero-day-Exploit-np)

---

<p align="center">
  If bluetooth-autoconnect saves you frustration, consider giving it a ⭐ on GitHub.
</p>
