#!/usr/bin/env bash
set -euo pipefail

APP_NAME="bluetooth-autoconnect"
CONFIG_DIR="/etc/${APP_NAME}"
SERVICE_DIR="/etc/systemd/system"

if [[ $EUID -ne 0 ]]; then
  echo "This installer must run as root." >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  DISTRO="debian"
elif command -v dnf >/dev/null 2>&1; then
  DISTRO="fedora"
elif command -v pacman >/dev/null 2>&1; then
  DISTRO="arch"
elif command -v zypper >/dev/null 2>&1; then
  DISTRO="opensuse"
else
  echo "Unsupported distribution." >&2
  exit 1
fi

echo "Detected distribution: ${DISTRO}"

case "$DISTRO" in
  debian)
    apt-get update
    apt-get install -y python3 python3-pip bluez systemd
    ;;
  fedora)
    dnf install -y python3 python3-pip bluez systemd
    ;;
  arch)
    pacman -Sy --noconfirm python bluez systemd
    ;;
  opensuse)
    zypper install -y python3 python3-pip bluez systemd
    ;;
 esac

python3 -m pip install --upgrade pip
python3 -m pip install .

mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
  cat > "$CONFIG_DIR/config.yaml" <<'EOF'
retry:
  max_attempts: 5
  base_delay: 1.0
  max_delay: 60.0
  multiplier: 2.0
logging:
  level: INFO
  structured: true
  rotate: weekly
daemon:
  scan_interval: 30
  max_concurrency: 5
  enable_automatic_reconnect: true
adapter: null
device_priorities:
  default: 100
blacklist: []
whitelist: []
EOF
fi

cp -f systemd/bluetooth-autoconnect.service "$SERVICE_DIR/"
cp -f systemd/bluetooth-autoconnect-user.service "$SERVICE_DIR/"

systemctl daemon-reload
systemctl enable --now bluetooth-autoconnect.service
systemctl enable --now bluetooth-autoconnect-user.service || true

echo "Installation complete. Check status with: systemctl status bluetooth-autoconnect"
