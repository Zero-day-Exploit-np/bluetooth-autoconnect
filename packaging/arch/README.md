# Building on Arch Linux / Manjaro

## Prerequisites

```bash
sudo pacman -S --needed base-devel python python-build python-installer \
    python-wheel python-setuptools python-dbus-next bluez bluez-utils
```

## Build & install

Run these from the **project root**:

```bash
cp packaging/arch/PKGBUILD .
makepkg -si
```

`makepkg -si` builds the package and installs it (`-i`) after resolving
dependencies (`-s`) via `pacman`.

## Enable the service

```bash
sudo systemctl enable --now bluetooth-autoconnect.service
```

or, for the per-user service:

```bash
systemctl --user enable --now bluetooth-autoconnect.service
```

## Publishing to the AUR

To publish this as a real AUR package, host a release tarball (e.g. a
GitHub Release asset), update `source=()` in `PKGBUILD` to point at it,
compute the real checksum with `updpkgsums`, and generate `.SRCINFO`:

```bash
updpkgsums
makepkg --printsrcinfo > .SRCINFO
```

## Cleaning up

```bash
rm -f PKGBUILD .SRCINFO
rm -rf pkg src *.pkg.tar.zst
```
