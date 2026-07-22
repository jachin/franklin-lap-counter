#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# Franklin Lap Counter — self-contained Pi / Linux installer
#
# Usage:
#   sudo ./install.sh                        # interactive prompts
#   sudo ./install.sh --with-sway            # full kiosk (no prompts)
#   sudo ./install.sh --with-gui             # deploy GUI, no sway
#   sudo ./install.sh                        # headless (default with --help)
#
# Re-run to update: pulls latest code, rebuilds, restarts services.
###############################################################################

APP_NAME="franklin-lap-counter"
APP_DIR="/opt/${APP_NAME}"
VENV_DIR="${APP_DIR}/.venv"
BIN_DIR="${APP_DIR}/bin"
STATIC_DIR="${APP_DIR}/static"
DB_DIR="${APP_DIR}/db"
RUN_DIR="${APP_DIR}/run"
SYSTEMD_DIR="${APP_DIR}/systemd"
CONFIG_DIR="/etc/franklin"
LOG_DIR="/var/log/franklin"
FRANKLIN_USER="franklin"
FRANKLIN_GROUP="franklin"
FRANKLIN_HOME="/home/${FRANKLIN_USER}"
REDIS_SOCKET="${RUN_DIR}/redis.sock"
FRANKLIN_TARGET="franklin.target"

# Primary fork for development (upstream: jachinrupe/franklin-lap-counter)
REPO_URL="https://github.com/JugglerMaster/franklin-lap-counter.git"
GIT_BRANCH="systemd-services"
RELEASE_API="https://api.github.com/repos/JugglerMaster/franklin-lap-counter/releases/latest"

PYTHON_PACKAGES=(
    textual
    "redis<6"
    typing-extensions
    aiohttp
    pygments
    rich
)

SYSTEM_PACKAGES=(
    python3
    python3-pip
    python3-venv
    python3-gi
    gir1.2-gtk-4.0
    redis-server
    tmux
    zsh
    ncurses-bin
    lnav
    fonts-noto-color-emoji
    fontconfig
    libudev-dev
    git
    curl
)

SWAY_PACKAGES=(
    sway
    xwayland
    seatd
)

# ── helpers ──────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
prompt() { echo -e "${BLUE}[INPUT]${NC} $*"; }

# ── argument parsing / interactive prompts ──────────────────────────────

INSTALL_MODE=""
OFFLINE=false
SWAY_ENABLED=false
GUI_ENABLED=false
AUTOLOGIN_ENABLED=false

usage() {
    cat <<EOF
Usage: sudo $0 [OPTIONS]

Install or update Franklin Lap Counter on this machine.

Modes (mutually exclusive):
  --with-sway    Full kiosk mode: autologin + sway Wayland compositor + GTK GUI
  --with-gui     Deploy GTK GUI (user launches manually, no sway)
  (default)      Headless mode: background services + web apps, TUI available

Other:
  --offline      Skip git clone/pull and release downloads — use local files only
  --help         Show this message and exit

Re-run to update — skips system packages and user creation, updates code and
restarts services.
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-sway) INSTALL_MODE="sway"; SWAY_ENABLED=true; GUI_ENABLED=true; AUTOLOGIN_ENABLED=true; shift ;;
        --with-gui)  INSTALL_MODE="gui";  GUI_ENABLED=true;                    shift ;;
        --offline)   OFFLINE=true;                                              shift ;;
        --help)      usage ;;
        *)           error "Unknown option: $1"; usage ;;
    esac
done

# Interactive prompts if mode not specified
if [[ -z "$INSTALL_MODE" ]]; then
    echo ""
    prompt "Select install mode:"
    echo "  1) Full kiosk  — boots to GUI (autologin + sway Wayland), dedicated Franklin machine"
    echo "  2) GUI-ready   — GTK GUI available, you launch it manually"
    echo "  3) Headless    — background services + web apps + TUI (default)"
    read -rp "Choose [1-3] (default: 3): " mode_choice
    case "${mode_choice:-3}" in
        1) INSTALL_MODE="sway"; SWAY_ENABLED=true; GUI_ENABLED=true; AUTOLOGIN_ENABLED=true ;;
        2) INSTALL_MODE="gui";  GUI_ENABLED=true ;;
        *) INSTALL_MODE="headless" ;;
    esac
fi

# ── pre-flight checks ───────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (sudo)."
    exit 1
fi

# Detect OS
if ! . /etc/os-release 2>/dev/null; then
    error "Cannot detect OS"
    exit 1
fi

info "Detected OS: $NAME $VERSION_ID ($VERSION_CODENAME)"

if [[ "$ID" != "raspbian" && "$ID" != "debian" && "$ID" != "ubuntu" ]]; then
    warn "Untested OS '$ID'. Franklin primarily targets Debian/Raspbian."
    prompt "Continue anyway?"
    read -rp "[y/N] " yn
    if [[ ! "$yn" =~ ^[yY] ]]; then
        info "Installation cancelled."
        exit 0
    fi
fi

ARCH=$(uname -m)
info "Architecture: $ARCH"
# For Rust binary, map to deb arch
case "$ARCH" in
    aarch64|arm64) DEB_ARCH="arm64" ;;
    x86_64|amd64)  DEB_ARCH="amd64" ;;
    *)             DEB_ARCH="$ARCH" ;;
esac

# Only aarch64 is supported for the hardware monitor (lnav requires 64-bit)
if [[ "$DEB_ARCH" == "armhf" ]]; then
    die "Franklin requires a 64-bit Raspberry Pi. Detected $ARCH but only arm64/amd64 are supported."
fi

# ── are we updating or installing fresh? ─────────────────────────────────

IS_UPDATE=false
if [[ -d "$APP_DIR/lib" ]]; then
    IS_UPDATE=true
    info "Existing installation detected at $APP_DIR — running in update mode."
    info "System packages, users, and config will NOT be modified."
fi

# ── ensure directories ──────────────────────────────────────────────────

if ! $IS_UPDATE; then
    info "Creating directory structure..."
    mkdir -p "$BIN_DIR" "$STATIC_DIR" "$DB_DIR" "$RUN_DIR" "$SYSTEMD_DIR"
    mkdir -p "$CONFIG_DIR" "$LOG_DIR"
fi

# ── system packages (fresh install only) ─────────────────────────────────

if ! $IS_UPDATE; then
    info "Installing system packages..."
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${SYSTEM_PACKAGES[@]}"

    if $SWAY_ENABLED; then
        info "Installing Wayland/sway packages..."
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${SWAY_PACKAGES[@]}"
    fi
fi

# ── franklin user (fresh install only) ───────────────────────────────────

if ! $IS_UPDATE; then
    if id "$FRANKLIN_USER" &>/dev/null; then
        info "User '$FRANKLIN_USER' already exists"
    else
        info "Creating system user '$FRANKLIN_USER'..."
        useradd --system --create-home --home-dir "$FRANKLIN_HOME" \
            --shell /usr/bin/zsh --groups dialout,video,input,render \
            "$FRANKLIN_USER"
    fi

    # Ensure supplementary groups exist
    for grp in dialout video input render; do
        if getent group "$grp" >/dev/null; then
            usermod -aG "$grp" "$FRANKLIN_USER" 2>/dev/null || true
        fi
    done
fi

# ── get the code ─────────────────────────────────────────────────────────

if $OFFLINE; then
    info "Offline mode — using files already at $APP_DIR"
elif $IS_UPDATE; then
    info "Updating code from repository..."
    if [[ -d "$APP_DIR/.git" ]]; then
        git -C "$APP_DIR" pull --ff-only origin "$GIT_BRANCH" || warn "git pull failed, continuing with existing code"
    else
        warn "Not a git repository, trying release download..."
    fi
else
    info "Cloning repository to $APP_DIR..."
    if [[ -d "$APP_DIR" && -z "$(ls -A "$APP_DIR/lib" 2>/dev/null)" ]]; then
        git clone --branch "$GIT_BRANCH" "$REPO_URL" "$APP_DIR" 2>/dev/null || {
            warn "git clone failed, will try release download"
        }
    elif [[ ! -d "$APP_DIR/.git" ]]; then
        rm -rf "$APP_DIR"
        git clone --branch "$GIT_BRANCH" "$REPO_URL" "$APP_DIR" 2>/dev/null || {
            warn "git clone failed, will try release download"
        }
    fi
fi

# If git is not available, try to download a release tarball
if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
    if $OFFLINE; then
        error "No Franklin code found at $APP_DIR (no pyproject.toml). Copy files first, then re-run."
        exit 1
    fi
    info "Downloading latest release tarball..."
    TMP_TAR=$(mktemp)
    # Use gh cli or curl to get latest release
    if command -v gh &>/dev/null; then
        gh release download --repo JugglerMaster/franklin-lap-counter --archive tar.gz --output "$TMP_TAR" 2>/dev/null || true
    fi
    if [[ ! -f "$TMP_TAR" || ! -s "$TMP_TAR" ]]; then
        LATEST_URL=$(curl -sL "$RELEASE_API" | python3 -c "import sys,json; print(json.load(sys.stdin)['tarball_url'])" 2>/dev/null || true)
        if [[ -n "$LATEST_URL" ]]; then
            curl -sL "$LATEST_URL" -o "$TMP_TAR"
        fi
    fi
    if [[ -s "$TMP_TAR" ]]; then
        mkdir -p "${APP_DIR}_tmp"
        tar xzf "$TMP_TAR" -C "${APP_DIR}_tmp" --strip-components=1
        rsync -a "${APP_DIR}_tmp/" "$APP_DIR/"
        rm -rf "${APP_DIR}_tmp"
    fi
    rm -f "$TMP_TAR"
fi

if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
    error "Failed to get Franklin code (no pyproject.toml found)"
    exit 1
fi

# ── lay out files into /opt structure ───────────────────────────────────

info "Laying out application files..."

# Python scripts → lib/
copy_python() {
    local file="$1"
    if [[ -f "$APP_DIR/$file" ]]; then
        cp "$APP_DIR/$file" "$APP_DIR/"
    fi
}

for f in franklin-race-recorder.py franklin-gui.py franklin-tui.py \
         scoreboard_web_app.py referee_web_app.py healthcheck_web_app.py \
         driver_web_app.py database.py gui_config.py racer_colors.py \
         redis_commands.py start_franklin_gui_session.py; do
    copy_python "$f"
done

# Python package → race/
if [[ -d "$APP_DIR/race" ]]; then
    rm -rf "$APP_DIR/race"
    cp -r "$APP_DIR/race" "$APP_DIR/race"
fi

# Static web files → static/
if [[ -d "$APP_DIR/static" ]]; then
    rm -rf "$STATIC_DIR"
    cp -r "$APP_DIR/static" "$STATIC_DIR"
fi

# Start script → bin/
cp "$APP_DIR/scripts/start_franklin.py" "$BIN_DIR/franklin-start"
cp "$APP_DIR/scripts/start_franklin_gui_session.py" "$BIN_DIR/"

# Systemd service files → systemd/ (source)
cp "$APP_DIR"/systemd/*.service "$SYSTEMD_DIR/" 2>/dev/null || true
cp "$APP_DIR"/systemd/*.target "$SYSTEMD_DIR/" 2>/dev/null || true
chmod 644 "$SYSTEMD_DIR"/*

# ── Python virtual environment ───────────────────────────────────────────

info "Setting up Python virtual environment..."
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv --system-site-packages "$VENV_DIR"
fi

# Install/upgrade pip packages
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
if [[ -f "$APP_DIR/pyproject.toml" ]]; then
    "$VENV_DIR/bin/pip" install --quiet -e "$APP_DIR" 2>/dev/null || true
fi

# ── Rust binary ──────────────────────────────────────────────────────────

install_rust_binary() {
    # Offline mode — skip download, check if binary already exists
    if $OFFLINE; then
        if [[ -f "$BIN_DIR/franklin-hardware-monitor" ]]; then
            info "Rust binary already present (offline mode)"
            return 0
        else
            warn "No Rust binary found at $BIN_DIR (offline mode). Copy a pre-built binary there first."
            return 1
        fi
    fi

    # Try to download pre-built .deb from GitHub Releases
    local deb_url
    deb_url=$(curl -sL "$RELEASE_API" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for asset in data.get('assets', []):
    if '${DEB_ARCH}' in asset['name'] and asset['name'].endswith('.deb'):
        print(asset['browser_download_url'])
        break
" 2>/dev/null || true)

    if [[ -n "$deb_url" ]]; then
        info "Downloading pre-built Rust binary from GitHub Releases..."
        TMP_DEB=$(mktemp /tmp/franklin-XXXXXX.deb)
        if curl -sL "$deb_url" -o "$TMP_DEB"; then
            dpkg -i "$TMP_DEB" 2>/dev/null || true
            rm -f "$TMP_DEB"
            if command -v franklin-hardware-monitor &>/dev/null; then
                cp "$(command -v franklin-hardware-monitor)" "$BIN_DIR/"
                info "Installed pre-built Rust binary"
                return 0
            fi
        fi
        rm -f "$TMP_DEB"
    fi

    # Fall back to building from source
    info "Building Rust binary from source (this may take a while on a Pi)..."
    if command -v cargo &>/dev/null; then
        cd "$APP_DIR"
        if [[ -f "rust/Cargo.toml" ]]; then
            cargo build --release --manifest-path rust/Cargo.toml --bin franklin-hardware-monitor 2>&1 | tail -5
            if [[ -f "rust/target/release/franklin-hardware-monitor" ]]; then
                cp "rust/target/release/franklin-hardware-monitor" "$BIN_DIR/"
                info "Rust binary built successfully"
                return 0
            fi
        fi
    fi

    warn "Could not build or download Rust binary. The hardware monitor will not be available."
    warn "Install Rust with: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
    warn "Then run this script again to build the binary."
    return 1
}

install_rust_binary || true

# ── systemd service installation ─────────────────────────────────────────

info "Installing systemd service files..."
for unit in "$SYSTEMD_DIR"/*.service "$SYSTEMD_DIR"/*.target; do
    if [[ -f "$unit" ]]; then
        cp "$unit" /etc/systemd/system/
        chmod 644 "/etc/systemd/system/$(basename "$unit")"
    fi
done
systemctl daemon-reload

# ── create /var/log/franklin directory ──────────────────────────────────

mkdir -p "$LOG_DIR"
chown "$FRANKLIN_USER:$FRANKLIN_GROUP" "$LOG_DIR"

# ── create /etc/franklin/config ──────────────────────────────────────────

if [[ ! -f "$CONFIG_DIR/config" ]]; then
    info "Creating default config at $CONFIG_DIR/config..."
    cat > "$CONFIG_DIR/config" <<EOF
# Franklin Lap Counter configuration
# This file is sourced by the systemd service files.

FRANKLIN_REDIS_SOCKET=${REDIS_SOCKET}
FRANKLIN_APP_DIR=${APP_DIR}
EOF
fi

# ── ownership ────────────────────────────────────────────────────────────

info "Setting ownership..."
chown -R "${FRANKLIN_USER}:${FRANKLIN_GROUP}" "$APP_DIR" "$CONFIG_DIR" "$LOG_DIR" 2>/dev/null || true

# ── sway + autologin (--with-sway mode) ─────────────────────────────────

if $SWAY_ENABLED && ! $IS_UPDATE; then
    info "Configuring autologin and sway for kiosk mode..."

    SWAY_CONFIG_DIR="${FRANKLIN_HOME}/.config/sway"
    mkdir -p "$SWAY_CONFIG_DIR"

    # Write sway config
    cat > "${SWAY_CONFIG_DIR}/config" <<'SWAYEOF'
# Franklin Lap Counter — sway config
set $mod Mod4

# Terminal
bindsym $mod+Return exec foot

# Launcher
bindsym $mod+d exec wmenu-run

# Focus
bindsym $mod+j focus left
bindsym $mod+k focus down
bindsym $mod+l focus up
bindsym $mod+semicolon focus right

# Move
bindsym $mod+Shift+j move left
bindsym $mod+Shift+k move down
bindsym $mod+Shift+l move up
bindsym $mod+Shift+semicolon move right

# Workspace switching
bindsym $mod+1 workspace 1
bindsym $mod+2 workspace 2
bindsym $mod+3 workspace 3
bindsym $mod+4 workspace 4

# Move to workspace
bindsym $mod+Shift+1 move container to workspace 1
bindsym $mod+Shift+2 move container to workspace 2
bindsym $mod+Shift+3 move container to workspace 3
bindsym $mod+Shift+4 move container to workspace 4

# Reload config
bindsym $mod+Shift+c reload

# Exit sway
bindsym $mod+Shift+e exec swaynag -t warning -m 'Exit sway?' -b 'Yes' 'swaymsg exit'

# Autostart Franklin GUI
exec /opt/franklin-lap-counter/start_franklin_gui_session.py
SWAYEOF

    chown -R "${FRANKLIN_USER}:${FRANKLIN_GROUP}" "$SWAY_CONFIG_DIR"

    # Configure autologin on tty1
    mkdir -p /etc/systemd/system/getty@tty1.service.d
    cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty -o '-p -f -- \\u' --autologin ${FRANKLIN_USER} --noclear %I \$TERM
EOF

    # Add sway launch to .zprofile
    ZPROFILE="${FRANKLIN_HOME}/.zprofile"
    if ! grep -q "sway" "$ZPROFILE" 2>/dev/null; then
        cat >> "$ZPROFILE" <<'ZEOF'

# Start sway on tty1 if not already running
if [ -z "$WAYLAND_DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec sway
fi
ZEOF
        chown "${FRANKLIN_USER}:${FRANKLIN_GROUP}" "$ZPROFILE"
    fi

    systemctl enable getty@tty1.service 2>/dev/null || true
    systemctl daemon-reload
fi

# ── enable + start services ──────────────────────────────────────────────

info "Enabling Franklin services..."
systemctl enable "${FRANKLIN_TARGET}" 2>/dev/null || true

info "Starting Franklin services..."
systemctl start "${FRANKLIN_TARGET}" 2>/dev/null || {
    warn "Some services may have failed to start. Check: systemctl status franklin.target"
}

# ── summary ──────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Franklin Lap Counter installation complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  App directory:  $APP_DIR"
echo "  Config:         $CONFIG_DIR/config"
echo "  Logs:           $LOG_DIR"
echo "  Database:       $DB_DIR/franklin.db"
echo "  Redis socket:   $REDIS_SOCKET"
echo ""

echo "  Running services:"
systemctl list-units --no-pager "franklin-*" --all 2>/dev/null | grep -v "^  " | grep -v "^$" || true
echo ""

if $SWAY_ENABLED; then
    echo "  Mode: Full kiosk — reboot to start the Franklin GUI automatically."
elif $GUI_ENABLED; then
    echo "  Mode: GUI-ready — run 'sudo -u ${FRANKLIN_USER} ${VENV_DIR}/bin/python ${APP_DIR}/franklin-gui.py' to launch."
else
    echo "  Mode: Headless — background services + web apps + TUI."
    echo "  Run the TUI: sudo -u ${FRANKLIN_USER} ${VENV_DIR}/bin/python ${APP_DIR}/franklin-tui.py"
fi
echo ""
echo "  Manage services: systemctl start|stop|status ${FRANKLIN_TARGET}"
echo "  View logs:       journalctl -u franklin-redis -f"
echo "  Update:          Run this script again (pulls latest code)"
echo ""
echo "  Web apps:"
echo "    Scoreboard:   http://$(hostname -I | awk '{print $1}'):8085"
echo "    Referee:      http://$(hostname -I | awk '{print $1}'):8081"
echo "    Health check: http://$(hostname -I | awk '{print $1}'):8082"
echo "    Driver:       http://$(hostname -I | awk '{print $1}'):8083"
echo ""
echo "═══════════════════════════════════════════════════════════════"
