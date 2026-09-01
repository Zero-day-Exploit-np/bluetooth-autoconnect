#!/usr/bin/env bash
set -euo pipefail

python3 -m pip install --upgrade pip
python3 -m pip install --upgrade bluetooth-autoconnect
systemctl daemon-reload
systemctl restart bluetooth-autoconnect.service || true
systemctl restart bluetooth-autoconnect-user.service || true

echo "Update complete."
