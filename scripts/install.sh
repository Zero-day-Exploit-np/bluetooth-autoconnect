#!/usr/bin/env bash
# install.sh — one-command installer for bluetooth-autoconnect
# Usage:  sudo bash scripts/install.sh
# Supports: Ubuntu, Debian, Kali Linux, Linux Mint, Fedora,
#           Arch Linux, Manjaro, openSUSE Tumbleweed/Leap
# ------------------------------------------------------------
set -euo pipefail

# ── constants ────────────────────────────────────────────────
REPO_URL="https://github.com/Zero-day-Exploit-np/bluetooth-autoconnect"
APP_NAME="bluetooth-autoconnect"
INSTALL_PREFIX="/usr"
BIN_DIR="${INSTALL_PREFIX}/bin"
SYSTEM_UNIT_DIR="/usr/lib/systemd/system"
USER_UNIT_DIR="/usr/lib/systemd/user"
CONFIG_DIR="/etc/${APP_NAME}"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"
VENV_DIR="/opt/${APP_NAME}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── colour helpers ───────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${GREEN}[✔]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[!]${RESET} $*"; }
error()   { echo -e "${RED}[✘]${RESET} $*" >&2; }
section() { echo -e "\n${BOLD}──────────────────────────────────────${RESET}"; echo -e "${BOLD} $*${RESET}"; echo -e "${BOLD}──────────────────────────────────────${RESET}"; }

# ── privilege check ──────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    error "This installer must be run as root."
    echo "  Try:  sudo bash scripts/install.sh"
    exit 1
fi

# ── detect distribution ──────────────────────────────────────
detect_distro() {
    if [[ -f /etc/os-release ]]; then
        # shellcheck source=/dev/null
        source /etc/os-release
        DISTRO_ID="${ID,,}"          # lowercase
        DISTRO_ID_LIKE="${ID_LIKE,,:-}"
    else
        error "Cannot detect distribution: /etc/os-release not found."
        exit 1
    fi

    if [[ "$DISTRO_ID" =~ ^(ubuntu|debian|kali|linuxmint|raspbian|pop)$ ]] \
        || [[ "$DISTRO_ID_LIKE" =~ debian ]]; then
        DISTRO_FAMILY="debian"
    elif [[ "$DISTRO_ID" =~ ^(fedora|rhel|centos|almalinux|rocky)$ ]] \
        || [[ "$DISTRO_ID_LIKE" =~ fedora ]] \
        || [[ "$DISTRO_ID_LIKE" =~ rhel ]]; then
        DISTRO_FAMILY="fedora"
    elif [[ "$DISTRO_ID" =~ ^(arch|manjaro|endeavouros|garuda)$ ]] \
        || [[ "$DISTRO_ID_LIKE" =~ arch ]]; then
        DISTRO_FAMILY="arch"
    elif [[ "$DISTRO_ID" =~ ^(opensuse|suse|opensuse-tumbleweed|opensuse-leap)$ ]] \
        || [[ "$DISTRO_ID_LIKE" =~ suse ]]; then
        DISTRO_FAMILY="opensuse"
    else
        error "Unsupported distribution: ${DISTRO_ID}"
        echo "  Supported: Ubuntu, Debian, Kali, Mint, Fedora, Arch, Manjaro, openSUSE"
        exit 1
    fi

    info "Detected: ${PRETTY_NAME:-$DISTRO_ID} (family: ${DISTRO_FAMILY})"
}

# ── install system dependencies ──────────────────────────────
install_system_deps() {
    section "Installing system dependencies"

    case "$DISTRO_FAMILY" in
        debian)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq
            apt-get install -y --no-install-recommends \
                python3 python3-venv python3-pip \
                bluez dbus systemd
            ;;
        fedora)
            dnf install -y python3 python3-pip bluez systemd
            ;;
        arch)
            pacman -Sy --noconfirm --needed python bluez bluez-utils systemd
            ;;
        opensuse)
            zypper install -y --no-recommends \
                python3 python3-pip bluez systemd
            ;;
    esac

    info "System dependencies installed."
}

# ── install Python package into isolated venv ────────────────
install_python_package() {
    section "Installing bluetooth-autoconnect"

    python3 -m venv --system-site-packages "${VENV_DIR}"
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet "${PROJECT_ROOT}"

    # Expose the binary system-wide via a symlink
    ln -sf "${VENV_DIR}/bin/${APP_NAME}" "${BIN_DIR}/${APP_NAME}"

    info "Installed to ${VENV_DIR} — binary linked at ${BIN_DIR}/${APP_NAME}"
}

# ── install default config ───────────────────────────────────
install_config() {
    section "Installing configuration"

    mkdir -p "${CONFIG_DIR}"
    if [[ ! -f "${CONFIG_FILE}" ]]; then
        install -m 644 "${PROJECT_ROOT}/etc/${APP_NAME}/config.yaml" \
            "${CONFIG_FILE}"
        info "Default config installed at ${CONFIG_FILE}"
    else
        warn "Config already exists at ${CONFIG_FILE} — skipping (not overwritten)."
    fi
}

# ── install systemd units ────────────────────────────────────
install_systemd_units() {
    section "Installing systemd service units"

    install -Dm644 \
        "${PROJECT_ROOT}/systemd/${APP_NAME}.service" \
        "${SYSTEM_UNIT_DIR}/${APP_NAME}.service"

    install -Dm644 \
        "${PROJECT_ROOT}/systemd/${APP_NAME}-user.service" \
        "${USER_UNIT_DIR}/${APP_NAME}.service"

    systemctl daemon-reload
    info "System unit installed:  ${SYSTEM_UNIT_DIR}/${APP_NAME}.service"
    info "User unit installed:    ${USER_UNIT_DIR}/${APP_NAME}.service"
}

# ── enable and start the system-wide service ─────────────────
enable_service() {
    section "Enabling and starting the service"

    systemctl enable --now "${APP_NAME}.service"
    info "Service enabled and started."
    echo ""
    systemctl status "${APP_NAME}.service" --no-pager -l || true
}

# ── verify installation ──────────────────────────────────────
verify_install() {
    section "Verifying installation"

    if "${BIN_DIR}/${APP_NAME}" --version &>/dev/null; then
        VERSION=$("${BIN_DIR}/${APP_NAME}" --version 2>&1)
        info "Binary works:  ${VERSION}"
    else
        error "Binary check failed — installation may be incomplete."
        exit 1
    fi
}

# ── main ─────────────────────────────────────────────────────
main() {
    echo ""
    echo -e "${BOLD}bluetooth-autoconnect installer${RESET}"
    echo -e "Repository: ${REPO_URL}"
    echo ""

    detect_distro
    install_system_deps
    install_python_package
    install_config
    install_systemd_units
    verify_install
    enable_service

    section "Installation complete"
    echo -e "  ${GREEN}bluetooth-autoconnect is running.${RESET}"
    echo ""
    echo "  Check status:   systemctl status ${APP_NAME}"
    echo "  Follow logs:    journalctl -u ${APP_NAME} -f"
    echo "  Trigger rescan: systemctl kill -s SIGHUP ${APP_NAME}"
    echo "  Uninstall:      sudo bash scripts/uninstall.sh"
    echo ""
}

main "$@"
