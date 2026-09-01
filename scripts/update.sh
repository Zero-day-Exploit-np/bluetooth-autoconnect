#!/usr/bin/env bash
# update.sh — update bluetooth-autoconnect to the latest version
# Usage:  sudo bash scripts/update.sh
#         sudo bash scripts/update.sh --branch main
# ------------------------------------------------------------
set -euo pipefail

APP_NAME="bluetooth-autoconnect"
REPO_URL="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect"
BIN_DIR="/usr/bin"
SYSTEM_UNIT_DIR="/usr/lib/systemd/system"
USER_UNIT_DIR="/usr/lib/systemd/user"
VENV_DIR="/opt/${APP_NAME}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BRANCH="main"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${GREEN}[✔]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[!]${RESET} $*"; }
error()   { echo -e "${RED}[✘]${RESET} $*" >&2; }
section() { echo -e "\n${BOLD}──────────────────────────────────────${RESET}"; echo -e "${BOLD} $*${RESET}"; echo -e "${BOLD}──────────────────────────────────────${RESET}"; }

# ── parse args ───────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch) BRANCH="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: sudo bash scripts/update.sh [--branch <name>]"
            echo "  --branch  Git branch to update from (default: main)"
            exit 0 ;;
        *) error "Unknown argument: $1"; exit 1 ;;
    esac
done

# ── privilege check ──────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    error "This updater must be run as root."
    echo "  Try:  sudo bash scripts/update.sh"
    exit 1
fi

echo ""
echo -e "${BOLD}bluetooth-autoconnect updater${RESET}"
echo -e "Repository: ${REPO_URL}"
echo ""

# ── record current version ───────────────────────────────────
OLD_VERSION="unknown"
if command -v "${APP_NAME}" &>/dev/null; then
    OLD_VERSION=$("${APP_NAME}" --version 2>&1 | awk '{print $NF}')
fi
info "Current version: ${OLD_VERSION}"

# ── pull latest source ───────────────────────────────────────
section "Pulling latest source"

if [[ ! -d "${PROJECT_ROOT}/.git" ]]; then
    error "Project root is not a git repository: ${PROJECT_ROOT}"
    echo "  Re-clone from: ${REPO_URL}"
    exit 1
fi

cd "${PROJECT_ROOT}"
git fetch --tags origin
git checkout "${BRANCH}"
git pull --ff-only origin "${BRANCH}"
info "Source updated to $(git describe --tags --always)."

# ── reinstall Python package ─────────────────────────────────
section "Reinstalling Python package"

if [[ -d "${VENV_DIR}" ]]; then
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet --upgrade "${PROJECT_ROOT}"
    info "Package upgraded in ${VENV_DIR}"
else
    warn "Virtualenv not found at ${VENV_DIR} — running full install instead."
    bash "${SCRIPT_DIR}/install.sh"
    exit 0
fi

# Ensure symlink is still correct (may change across upgrades)
ln -sf "${VENV_DIR}/bin/${APP_NAME}" "${BIN_DIR}/${APP_NAME}"
info "Binary symlink refreshed at ${BIN_DIR}/${APP_NAME}"

# ── update systemd units ─────────────────────────────────────
section "Updating systemd unit files"

install -Dm644 \
    "${PROJECT_ROOT}/systemd/${APP_NAME}.service" \
    "${SYSTEM_UNIT_DIR}/${APP_NAME}.service"

install -Dm644 \
    "${PROJECT_ROOT}/systemd/${APP_NAME}-user.service" \
    "${USER_UNIT_DIR}/${APP_NAME}.service"

systemctl daemon-reload
info "Unit files updated and daemon reloaded."

# ── restart running service ──────────────────────────────────
section "Restarting service"

if systemctl is-active --quiet "${APP_NAME}.service"; then
    systemctl restart "${APP_NAME}.service"
    info "System service restarted."
else
    warn "System service was not running — not started automatically."
    warn "Start it with:  sudo systemctl start ${APP_NAME}.service"
fi

# ── report new version ───────────────────────────────────────
NEW_VERSION=$("${BIN_DIR}/${APP_NAME}" --version 2>&1 | awk '{print $NF}')
section "Update complete"
echo -e "  ${GREEN}${OLD_VERSION}  →  ${NEW_VERSION}${RESET}"
echo ""
echo "  Check status:   systemctl status ${APP_NAME}"
echo "  Follow logs:    journalctl -u ${APP_NAME} -f"
echo ""
