# Installation Guide

This guide covers installing `bluetooth-autoconnect` from source on every
supported distribution, plus first-run verification.

## 1. Prerequisites (all distributions)

- Python 3.9 or newer
- BlueZ (`bluetoothd`) installed and its systemd unit (`bluetooth.service`)
  enabled
- `systemd` (for the provided service units — the CLI itself works
  without systemd too)
- At least one Bluetooth device already **paired and trusted** (pair it
  once interactively with `bluetoothctl` or your desktop's Bluetooth
  settings panel — this tool never pairs devices itself)

Check BlueZ is running:

```bash
systemctl status bluetooth.service
```

If it's not running:

```bash
sudo systemctl enable --now bluetooth.service
```

## 2. Install the Python package

### Option A: From source with pip (works on every distro)

```bash
git clone https://github.com/example/bluetooth-autoconnect.git
cd bluetooth-autoconnect
pip install --user .
```

This installs the `bluetooth-autoconnect` console script into
`~/.local/bin` — make sure that directory is on your `PATH`:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### Option B: Native distro packages

Use the packaging recipes under `packaging/` for a properly integrated
install (dependency tracking, uninstall support, etc.):

| Distro family | Guide |
|---|---|
| Debian, Ubuntu, Kali, Mint | [`packaging/debian/README.md`](../packaging/debian/README.md) |
| Arch, Manjaro | [`packaging/arch/README.md`](../packaging/arch/README.md) |
| Fedora, openSUSE | [`packaging/fedora/README.md`](../packaging/fedora/README.md) |

## 3. Verify it works

Run a one-shot scan:

```bash
bluetooth-autoconnect --verbose
```

You should see log lines listing your adapter(s), how many paired+trusted
devices were found, and connection attempts. If you have no devices
paired yet, pair one first:

```bash
bluetoothctl
[bluetoothctl]# power on
[bluetoothctl]# agent on
[bluetoothctl]# scan on
# wait for your device to appear, then:
[bluetoothctl]# pair AA:BB:CC:DD:EE:FF
[bluetoothctl]# trust AA:BB:CC:DD:EE:FF
[bluetoothctl]# connect AA:BB:CC:DD:EE:FF
[bluetoothctl]# exit
```

Then re-run `bluetooth-autoconnect --verbose` and confirm it reports the
device as already connected or successfully connects it.

## 4. Install as an always-on service

Decide between the **system-wide** service (runs at boot, for all
users) or the **per-user** service (runs at login, no root needed).

### System-wide

```bash
sudo install -Dm644 systemd/bluetooth-autoconnect.service \
    /etc/systemd/system/bluetooth-autoconnect.service
sudo systemctl daemon-reload
sudo systemctl enable --now bluetooth-autoconnect.service
```

> Note: the unit's `ExecStart` points at `/usr/bin/bluetooth-autoconnect`.
> If you installed with `pip install --user`, either change that path to
> your actual install location (e.g. `/home/you/.local/bin/bluetooth-autoconnect`)
> or install system-wide instead: `sudo pip install .`

### Per-user

```bash
mkdir -p ~/.config/systemd/user
install -Dm644 systemd/bluetooth-autoconnect-user.service \
    ~/.config/systemd/user/bluetooth-autoconnect.service
systemctl --user daemon-reload
systemctl --user enable --now bluetooth-autoconnect.service
```

> The user unit's `ExecStart` uses `%h/.local/bin/bluetooth-autoconnect`,
> matching a `pip install --user` layout. Adjust if you installed
> elsewhere.

Both `make systemd-install` and `make systemd-user-install` automate the
steps above — see the [`Makefile`](../Makefile).

## 5. Confirm the service is active

```bash
sudo systemctl status bluetooth-autoconnect.service     # system-wide
systemctl --user status bluetooth-autoconnect.service   # per-user
```

Watch it react in real time by turning your paired device off and back
on, or walking it out of and back into range:

```bash
journalctl -u bluetooth-autoconnect -f          # system-wide
journalctl --user -u bluetooth-autoconnect -f   # per-user
```

You should see a "disconnected" or "device is back in range" log line
followed by a reconnect attempt within a couple of seconds.

## Uninstalling

```bash
sudo systemctl disable --now bluetooth-autoconnect.service 2>/dev/null
systemctl --user disable --now bluetooth-autoconnect.service 2>/dev/null
pip uninstall bluetooth-autoconnect
sudo rm -f /etc/systemd/system/bluetooth-autoconnect.service
rm -f ~/.config/systemd/user/bluetooth-autoconnect.service
```

(Or use your distro's package manager if you installed a `.deb`/`.rpm`/
Arch package: `apt remove`, `dnf remove`, `pacman -R`.)
