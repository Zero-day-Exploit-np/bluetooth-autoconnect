Name:           bluetooth-autoconnect
Version:        1.0.0
Release:        1%{?dist}
Summary:        Automatically reconnect paired, trusted Bluetooth devices

License:        MIT
URL:            https://github.com/example/bluetooth-autoconnect
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  systemd-rpm-macros

Requires:       python3
Requires:       python3-dbus-next
Requires:       bluez
Requires:       systemd
Recommends:     python3-systemd

%{?systemd_requires}

%description
bluetooth-autoconnect is a background service that uses the BlueZ D-Bus
API to detect paired and trusted Bluetooth devices (headphones, mice,
keyboards, speakers, etc.) and reconnect them automatically whenever
Bluetooth is enabled, an adapter becomes available, or a device comes
into range. Ships both a system-wide and a per-user systemd service.

%prep
%autosetup -n %{name}-%{version}

%build
%py3_build

%install
%py3_install

install -Dm644 systemd/bluetooth-autoconnect.service \
    %{buildroot}%{_unitdir}/bluetooth-autoconnect.service
install -Dm644 systemd/bluetooth-autoconnect-user.service \
    %{buildroot}%{_userunitdir}/bluetooth-autoconnect.service

%files
%license LICENSE
%doc README.md docs/TROUBLESHOOTING.md
%{python3_sitelib}/bluetooth_autoconnect*
%{_bindir}/bluetooth-autoconnect
%{_unitdir}/bluetooth-autoconnect.service
%{_userunitdir}/bluetooth-autoconnect.service

%post
%systemd_post bluetooth-autoconnect.service

%preun
%systemd_preun bluetooth-autoconnect.service

%postun
%systemd_postun_with_restart bluetooth-autoconnect.service

%changelog
* Tue Sep 01 2026 bluetooth-autoconnect contributors <maintainers@example.com> - 1.0.0-1
- Initial release.
