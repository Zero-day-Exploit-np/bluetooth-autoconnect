# Building a .deb package

This directory contains a `debian/` metadata folder for building a Debian
package with standard `debhelper` + `pybuild` tooling. It works as-is on
Debian, Ubuntu, Kali Linux, and Linux Mint.

## Prerequisites

```bash
sudo apt update
sudo apt install -y build-essential debhelper dh-python \
    python3-all python3-setuptools python3-pip devscripts
```

## Steps

1. From the **project root** (not this `packaging/` directory), copy the
   `debian/` metadata folder into place:

   ```bash
   cp -r packaging/debian/debian .
   ```

2. Build the package:

   ```bash
   dpkg-buildpackage -us -uc -b
   ```

3. The resulting `.deb` file is written one directory **above** the
   project root, e.g. `../bluetooth-autoconnect_1.0.0-1_all.deb`.

4. Install it:

   ```bash
   sudo apt install ../bluetooth-autoconnect_1.0.0-1_all.deb
   ```

5. Enable the service:

   ```bash
   sudo systemctl enable --now bluetooth-autoconnect.service
   ```

   or, for the per-user service:

   ```bash
   systemctl --user enable --now bluetooth-autoconnect.service
   ```

## Cleaning up

```bash
rm -rf debian ../bluetooth-autoconnect_*.deb ../bluetooth-autoconnect_*.buildinfo \
    ../bluetooth-autoconnect_*.changes
```

## Notes

- `dbus-next` is pulled in via pip as `python3-dbus-next` if your
  distribution ships it, otherwise `postinst`/dependency resolution
  falls back to installing it with `pip3 install dbus-next` — see
  `debian/control`'s `Depends` line.
- The package installs `bluetooth-autoconnect.service` (system-wide) via
  `dh_installsystemd` and `bluetooth-autoconnect-user.service` under
  `/usr/lib/systemd/user/` for per-user use.
