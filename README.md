# bluetooth-autoconnect

A production-ready Linux command-line tool and background service that
automatically detects paired, trusted Bluetooth devices and reconnects
them — whenever Bluetooth is enabled, an adapter powers on, you log in,
or a device comes back into range.

Talks directly to [BlueZ](http://www.bluez.org/) over D-Bus (no shelling
out to `bluetoothctl`), so it's fast, event-driven, and works with any
number of adapters and devices — headphones, speakers, keyboards, mice,
and anything else you've already paired.

## Features

- **Event-driven daemon**: reacts instantly to adapter power-on, device
  discovery, and disconnect events via the BlueZ D-Bus API — no polling.
- **Multi-adapter aware**: scans and connects devices across every
  powered Bluetooth adapter on the system.
- **Safe by default**: only ever connects devices that are both *paired*
  and *trusted*; everything else is skipped and logged.
- **Resilient**: failed connections retry with exponential backoff;
  multiple devices connect concurrently without blocking each other.
- **Graceful signal handling**: `SIGTERM`/`SIGINT` for clean shutdown,
  `SIGHUP` to trigger an immediate full rescan.
- **systemd-native**: ships both a system-wide service (starts at boot)
  and a per-user service (starts at login), with journal logging.
- **Portable**: pure-Python D-Bus client (`dbus-next`), no compiled
  extensions required. Packaged for Debian/Ubuntu/Kali/Mint (`.deb`),
  Arch/Manjaro (`PKGBUILD`), and Fedora/openSUSE (`.rpm`).

## Supported distributions

Debian, Ubuntu, Kali Linux, Linux Mint, Fedora, Arch Linux, Manjaro, and
openSUSE — anywhere BlueZ and systemd are available.

## How it decides what to connect

A device is only ever auto-connected if BlueZ reports **both**:

- `Paired: true` — you've completed a pairing handshake with it, and
- `Trusted: true` — BlueZ (or you) has marked it trusted.

Anything else — a nearby unpaired device, or a paired-but-untrusted one
— is skipped and logged at debug level. Nothing is ever paired or
trusted automatically; this tool only reconnects devices you've already
approved.

## Installation

Pick the path for your distribution.

### Quick install (any distro, via pip)

```bash
python3 -m venv ~/.local/share/bluetooth-autoconnect-venv
~/.local/share/bluetooth-autoconnect-venv/bin/pip install .
sudo ln -s ~/.local/share/bluetooth-autoconnect-venv/bin/bluetooth-autoconnect \
    /usr/local/bin/bluetooth-autoconnect
```

Or, simpler, if you're fine installing into your user site-packages:

```bash
pip install --user .
```

Then see [Systemd integration](#systemd-integration) below to make it
run automatically.

### Debian / Ubuntu / Kali / Mint

See [`packaging/debian/README.md`](packaging/debian/README.md) for full
`.deb` build instructions. Summary:

```bash
sudo apt install -y build-essential debhelper dh-python python3-all \
    python3-setuptools python3-pip devscripts
cp -r packaging/debian/debian .
dpkg-buildpackage -us -uc -b
sudo apt install ../bluetooth-autoconnect_1.0.0-1_all.deb
```

### Arch Linux / Manjaro

See [`packaging/arch/README.md`](packaging/arch/README.md). Summary:

```bash
sudo pacman -S --needed base-devel python python-build python-installer \
    python-wheel python-dbus-next bluez bluez-utils
cp packaging/arch/PKGBUILD .
makepkg -si
```

### Fedora / openSUSE

See [`packaging/fedora/README.md`](packaging/fedora/README.md). Summary
(Fedora):

```bash
sudo dnf install -y rpm-build rpmdevtools python3-devel python3-pip \
    python3-setuptools python3-wheel systemd-rpm-macros
rpmdev-setuptree
tar --transform 's,^,bluetooth-autoconnect-1.0.0/,' \
    -czf ~/rpmbuild/SOURCES/bluetooth-autoconnect-1.0.0.tar.gz .
cp packaging/fedora/bluetooth-autoconnect.spec ~/rpmbuild/SPECS/
rpmbuild -ba ~/rpmbuild/SPECS/bluetooth-autoconnect.spec
sudo dnf install ~/rpmbuild/RPMS/noarch/bluetooth-autoconnect-*.rpm
```

For full step-by-step guidance across every distribution, see
[`docs/INSTALL.md`](docs/INSTALL.md).

## Usage

```
bluetooth-autoconnect [-h] [--daemon] [--verbose]
                       [--max-attempts N] [--max-concurrency N] [--version]
```

| Command | Behavior |
|---|---|
| `bluetooth-autoconnect` | Scan all powered adapters and connect every paired+trusted device once, then exit. |
| `bluetooth-autoconnect --daemon` | Run continuously as a background service, reacting to D-Bus events (adapter powered on, device in range, device disconnected) and reconnecting devices automatically. |
| `bluetooth-autoconnect --verbose` | Enable debug-level logging (combine with either mode above). |
| `bluetooth-autoconnect --help` | Show usage instructions. |

Other flags:

- `--max-attempts N` — connection attempts per device before giving up (default: 5).
- `--max-concurrency N` — max simultaneous connection attempts (default: 5).
- `--version` — print the installed version.

### Examples

```bash
# One-shot: connect everything paired+trusted, right now
bluetooth-autoconnect

# Run forever, reconnecting devices as they come into range
bluetooth-autoconnect --daemon

# Same, with verbose debug logs (useful when troubleshooting)
bluetooth-autoconnect --daemon --verbose

# Be more patient with a flaky headset: 10 attempts instead of 5
bluetooth-autoconnect --max-attempts 10
```

Exit codes: `0` if every attempted device connected (or none needed to),
`1` if at least one eligible device failed to connect, `2` on a fatal
D-Bus/BlueZ startup error, `130` on Ctrl-C.

## Systemd integration

Two service units are provided in [`systemd/`](systemd/):

- **`bluetooth-autoconnect.service`** — system-wide, starts at boot,
  runs as root, reconnects devices for the whole machine regardless of
  who's logged in.
- **`bluetooth-autoconnect-user.service`** — per-user, starts at login,
  useful on multi-user machines or when you'd rather not run as root.

Install whichever fits your setup:

```bash
# System-wide
make systemd-install
sudo systemctl enable --now bluetooth-autoconnect.service

# Per-user
make systemd-user-install
systemctl --user enable --now bluetooth-autoconnect.service
```

Both restart automatically on failure and start `bluetooth-autoconnect
--daemon`. Once enabled, you never need to run the command by hand
again — reconnects happen automatically going forward.

Check logs with:

```bash
journalctl -u bluetooth-autoconnect -f          # system service
journalctl --user -u bluetooth-autoconnect -f   # user service
```

Trigger an immediate rescan without restarting the service:

```bash
sudo systemctl kill -s SIGHUP bluetooth-autoconnect.service
```

## Development

```bash
make venv              # create .venv with dev dependencies
source .venv/bin/activate
make test               # run pytest
make lint                # run flake8
make format              # run black
make typecheck            # run mypy
```

Project layout:

```
bluetooth_autoconnect/
├── __init__.py        # version metadata
├── __main__.py         # `python -m bluetooth_autoconnect`
├── cli.py               # argument parsing, entry point
├── connector.py          # retry/backoff/concurrency logic
├── daemon.py               # event loop, signal handling
├── dbus_client.py            # BlueZ D-Bus wrapper (dbus-next)
├── exceptions.py               # error hierarchy
├── logging_setup.py             # stdout + journal logging
└── models.py                     # Adapter / Device dataclasses
tests/                              # pytest suite (no real D-Bus needed)
systemd/                             # system + user service units
packaging/                            # debian/, arch/, fedora/
docs/                                  # install & troubleshooting guides
```

## Troubleshooting

See [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) for common
issues (permissions, D-Bus policy, devices not auto-connecting, etc.).

## License

MIT — see [`LICENSE`](LICENSE).
