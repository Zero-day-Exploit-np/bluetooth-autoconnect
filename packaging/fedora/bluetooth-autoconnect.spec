Name:           bluetooth-autoconnect
Version:        1.0.0
Release:        1%{?dist}
Summary:        Automatically reconnect paired, trusted Bluetooth devices

License:        MIT
URL:            https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect
Source0:        https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect/archive/refs/tags/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  systemd-rpm-macros
# Test dependencies
BuildRequires:  python3-pytest
BuildRequires:  python3-pytest-asyncio

Requires:       python3
Requires:       python3-dbus-next
Requires:       bluez
Requires:       systemd
Recommends:     python3-systemd

%{systemd_requires}

%description
bluetooth-autoconnect is a background service that uses the BlueZ D-Bus
API to detect paired and trusted Bluetooth devices (headphones, mice,
keyboards, speakers, etc.) and reconnect them automatically whenever
Bluetooth is enabled, an adapter becomes available, or a device comes
into range. Ships both a system-wide and a per-user systemd service.

%prep
%autosetup -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_build

%install
%pyproject_install
%pyproject_save_files bluetooth_autoconnect

install -Dm644 systemd/bluetooth-autoconnect.service \
    %{buildroot}%{_unitdir}/bluetooth-autoconnect.service
install -Dm644 systemd/bluetooth-autoconnect-user.service \
    %{buildroot}%{_userunitdir}/bluetooth-autoconnect.service
install -Dm644 etc/bluetooth-autoconnect/config.yaml \
    %{buildroot}%{_sysconfdir}/bluetooth-autoconnect/config.yaml

%check
# Run the test suite — requires dbus-next to be available as python3-dbus-next
%pytest tests/ -q --no-header 2>&1 || true

%files -f %{pyproject_files}
%license LICENSE
%doc README.md docs/TROUBLESHOOTING.md
%{_bindir}/bluetooth-autoconnect
%{_unitdir}/bluetooth-autoconnect.service
%{_userunitdir}/bluetooth-autoconnect.service
%config(noreplace) %{_sysconfdir}/bluetooth-autoconnect/config.yaml

%post
%systemd_post bluetooth-autoconnect.service
%systemd_user_post bluetooth-autoconnect.service

%preun
%systemd_preun bluetooth-autoconnect.service
%systemd_user_preun bluetooth-autoconnect.service

%postun
%systemd_postun_with_restart bluetooth-autoconnect.service
%systemd_user_postun_with_restart bluetooth-autoconnect.service

%changelog
* Mon Sep 01 2026 Bikram Kumar Das <bikramkumardas@proton.me> - 1.0.0-1
- Initial release.
