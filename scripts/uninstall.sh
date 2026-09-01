#!/usr/bin/env bash
# uninstall.sh — remove bluetooth-autoconnect from the system
# Usage:  sudo bash scripts/uninstall.sh
# ------------------------------------------------------------
set -euo pipefail

APP_NAME="bluetooth-autoconnect"
BIN_DIR="/usr/bin"
SYSTEM_UNIT_DIR="/usr/lib/systemd/system"
USER_UNIT_DIR="/usr/lib/systemd/user"
CONFIG_DIR="/etc/${APP_NAME}"
VENV_DIR="/opt/${APP_NAME}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${GREEN}[✔]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[!]${RESET} $*"; }
section() { echo -e "\n${BOLD}──────────────────────────────────────${RESET}"; echo -e "${BOLD} $*${RESET}"; echo -e "${BOLD}──────────────────────────────────────${RESET}"; }

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}[✘]${RESET} This uninstaller must be run as root." >&2
    echo "  Try:  sudo bash scripts/uninstall.sh"
    exit 1
fi

echo ""
echo -e "${BOLD}bluetooth-autoconnect uninstaller${RESET}"
echo ""

# ── stop and disable systemd services ───────────────────────
section "Stopping services"

if systemctl is-active --quiet "${APP_NAME}.service" 2>/dev/null; then
    systemctl stop "${APP_NAME}.service"
    info "System service stopped."
fi

if systemctl is-enabled --quiet "${APP_NAME}.service" 2>/dev/null; then
    systemctl disable "${APP_NAME}.service"
    info "System service disabled."
fi

# Disable user units for every logged-in user that has it enabled
while IFS=: read -r username _ uid _ _ homedir _; do
    [[ "$uid" -lt 1000 ]] && continue          # skip system accounts
    user_unit="${homedir}/.config/systemd/user/${APP_NAME}.service"
    if [[ -f "$user_unit" ]] || systemctl --user --machine="${username}@" \
            is-enabled "${APP_NAME}.service" &>/dev/null 2>&1; then
        systemctl --user --machine="${username}@" \
            disable --now "${APP_NAME}.service" 2>/dev/null || true
        warn "Disabled user service for ${username}."
    fi
done < /etc/passwd

# ── remove systemd unit files ────────────────────────────────
section "Removing unit files"

for f in \
    "${SYSTEM_UNIT_DIR}/${APP_NAME}.service" \
    "${USER_UNIT_DIR}/${APP_NAME}.service"; do
    if [[ -f "$f" ]]; then
        rm -f "$f"
        info "Removed ${f}"
    fi
done

systemctl daemon-reload
info "systemd daemon reloaded."

# ── remove binary symlink ────────────────────────────────────
section "Removing binary"

if [[ -L "${BIN_DIR}/${APP_NAME}" ]]; then
    rm -f "${BIN_DIR}/${APP_NAME}"
    info "Removed symlink ${BIN_DIR}/${APP_NAME}"
elif [[ -f "${BIN_DIR}/${APP_NAME}" ]]; then
    rm -f "${BIN_DIR}/${APP_NAME}"
    info "Removed binary ${BIN_DIR}/${APP_NAME}"
fi

# Also check /usr/local/bin for legacy pip-installed binaries
if [[ -f "/usr/local/bin/${APP_NAME}" ]]; then
    rm -f "/usr/local/bin/${APP_NAME}"
    info "Removed /usr/local/bin/${APP_NAME}"
fi

# ── remove virtualenv ────────────────────────────────────────
section "Removing virtual environment"

if [[ -d "${VENV_DIR}" ]]; then
    rm -rf "${VENV_DIR}"
    info "Removed virtualenv at ${VENV_DIR}"
fi

# Also uninstall any system-level pip install (legacy path)
if python3 -m pip show "${APP_NAME}" &>/dev/null 2>&1; then
    python3 -m pip uninstall -y "${APP_NAME}" || true
    info "Removed pip package (system Python)."
fi

# ── preserve or remove config ────────────────────────────────
section "Configuration"

if [[ -d "${CONFIG_DIR}" ]]; then
    echo -e "${YELLOW}[?]${RESET} Keep configuration at ${CONFIG_DIR}? [Y/n] \c"
    read -r keep_config </dev/tty || keep_config="Y"
    if [[ "${keep_config,,}" == "n" ]]; then
        rm -rf "${CONFIG_DIR}"
        info "Removed ${CONFIG_DIR}"
    else
        warn "Configuration preserved at ${CONFIG_DIR}"
    fi
fi

section "Uninstall complete"
echo -e "  ${GREEN}bluetooth-autoconnect has been removed.${RESET}"
echo ""
