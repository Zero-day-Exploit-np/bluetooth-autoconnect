# Building an RPM (Fedora and openSUSE)

The same spec file works for both Fedora (`rpmbuild`/`dnf`) and openSUSE
(`rpmbuild`/`zypper`, or `osc` for OBS builds).

## Fedora

### Prerequisites

```bash
sudo dnf install -y rpm-build rpmdevtools python3-devel python3-pip \
    python3-setuptools python3-wheel systemd-rpm-macros
```

### Build

```bash
rpmdev-setuptree
tar --transform 's,^,bluetooth-autoconnect-1.0.0/,' \
    -czf ~/rpmbuild/SOURCES/bluetooth-autoconnect-1.0.0.tar.gz \
    --exclude='.git' --exclude='dist' --exclude='build' .
cp packaging/fedora/bluetooth-autoconnect.spec ~/rpmbuild/SPECS/
rpmbuild -ba ~/rpmbuild/SPECS/bluetooth-autoconnect.spec
```

The built RPM appears under `~/rpmbuild/RPMS/noarch/`.

### Install

```bash
sudo dnf install ~/rpmbuild/RPMS/noarch/bluetooth-autoconnect-1.0.0-1.*.noarch.rpm
sudo systemctl enable --now bluetooth-autoconnect.service
```

## openSUSE

### Prerequisites

```bash
sudo zypper install -y rpm-build python3-devel python3-pip \
    python3-setuptools python3-wheel systemd-rpm-macros
```

### Build

Same as Fedora above — the spec file is distribution-agnostic. You can
also build it properly in the Open Build Service (OBS) by creating a
package with `bluetooth-autoconnect.spec` and the source tarball as its
two sources.

### Install

```bash
sudo zypper install --allow-unsigned-rpm \
    ~/rpmbuild/RPMS/noarch/bluetooth-autoconnect-1.0.0-1.*.noarch.rpm
sudo systemctl enable --now bluetooth-autoconnect.service
```

## Notes

- If `python3-dbus-next` is not packaged in your distro's repos yet,
  install it with `pip3 install --user dbus-next` before running the
  service, or adjust the spec's `Requires:` line and use a `%pyproject_*`
  macro chain with vendored wheels for an offline build.
