#!/usr/bin/env bash
set -euo pipefail

APP_NAME="bluetooth-autoconnect"
SERVICE_DIR="/etc/systemd/system"

if [[ $EUID -ne 0 ]]; then
  echo "This uninstaller must run as root." >&2
  exit 1
fi

systemctl disable --now bluetooth-autoconnect.service 2>/dev/null || true
systemctl disable --now bluetooth-autoconnect-user.service 2>/dev/null || true
rm -f "$SERVICE_DIR/bluetooth-autoconnect.service"
rm -f "$SERVICE_DIR/bluetooth-autoconnect-user.service"
rm -f "/usr/local/bin/${APP_NAME}"
python3 -m pip uninstall -y "$APP_NAME" || true
rm -rf "/etc/${APP_NAME}"

systemctl daemon-reload

echo "Uninstall complete."
