#!/usr/bin/env bash
# =============================================================================
# kid2tune Installer v2.7.1 for Raspberry Pi Zero 2W
# Installs: Lyrion Music Server, Squeezelite, RFID Controller, I2C LCD,
#           GPIO Buttons, Flask Web Interface
# Usage:    sudo bash install.sh
#
# The app files (Python, Templates, CSS) are in the app/ directory
# and will be copied to /opt/lms-controller/.
# =============================================================================

set -euo pipefail
IFS=$'\n\t'

# ── Color Helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()  { echo -e "\n${CYAN}══════════════════════════════════════════${NC}"; echo -e "${CYAN}  $*${NC}"; echo -e "${CYAN}══════════════════════════════════════════${NC}"; }

# ── Version & Configuration ────────────────────────────────────────────────
VERSION="2.7.1"
SWAP_FILE="/var/tmp/install_swap"
BTN_VOL_UP=19
BTN_VOL_DOWN=26
BTN_NEXT=16
BTN_PREV=23
BTN_PAUSE=21
BTN_LCD_BACKLIGHT=20

APP_DIR="/opt/lms-controller"
VENV="$APP_DIR/venv"
PYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# =============================================================================
# STEP 0: Prerequisites
# =============================================================================
step "Step 0: Prerequisites"

[[ $EUID -eq 0 ]] || error "Please run as root: sudo bash install.sh"
[[ -d "$SCRIPT_DIR/app" ]] || error "app/ directory not found. Please run from the repo directory."

ARCH=$(uname -m)
if [[ "$ARCH" != "aarch64" && "$ARCH" != "armv7l" ]]; then
    warn "Architecture '$ARCH' is unusual for Raspberry Pi – continue?"
    read -r -p "  Continue? [y/N] " ans
    [[ "$ans" =~ ^[jJyY]$ ]] || error "Aborted."
fi

info "Architecture: $ARCH"
info "Hostname: $(hostname)"
info "Version: $VERSION"

# Create temporary swap (Pi Zero 2W has only 512MB RAM)
if ! swapon --show | grep -q "$SWAP_FILE"; then
    info "Creating temporary swap (1GB) for installation (Pi Zero 2W has only 512MB RAM)..."
    dd if=/dev/zero of="$SWAP_FILE" bs=1M count=1024 status=none 2>/dev/null || true
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" >/dev/null 2>&1 || true
    swapon "$SWAP_FILE" 2>/dev/null || true
fi

# =============================================================================
# STEP 1: Prepare system & install packages
# =============================================================================
step "Step 1: Prepare system & install packages"

export DEBIAN_FRONTEND=noninteractive

# Repair dpkg if a previous installation was interrupted
dpkg --configure -a 2>/dev/null || true

# Remove half-installed LMS packages (they block apt completely)
for pkg in lyrionmusicserver logitechmediaserver; do
    if dpkg -s "$pkg" 2>/dev/null | grep -qE "^Status:.*(half|reinst|unpacked|config)"; then
        warn "$pkg is in a broken state – removing..."
        dpkg --remove --force-remove-reinstreq "$pkg" 2>/dev/null || true
    fi
done

apt-get install -f -y 2>/dev/null || true

# Wait until dpkg lock is free (in case another apt process is running)
while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
    warn "dpkg lock busy – waiting 5 seconds..."
    sleep 5
done

apt-get update -qq

apt-get install -y \
    curl wget git \
    python3 python3-pip python3-venv python3-dev \
    i2c-tools python3-smbus \
    alsa-utils \
    build-essential \
    libssl-dev \
    libjpeg-dev \
    cifs-utils \
    hostapd \
    dnsmasq \
    iw \
    wireless-tools \
    bluez \
    bluez-tools \
    bluez-alsa-utils \
    libasound2-plugin-bluez \
    python3-lgpio

info "System packages installed."

# =============================================================================
# STEP 2: Install Lyrion Music Server
# =============================================================================
step "Step 2: Install Lyrion Music Server"

LMS_PKG=""
dpkg -s lyrionmusicserver   2>/dev/null | grep -q "^Status: install ok installed" && LMS_PKG="lyrionmusicserver"   || true
dpkg -s logitechmediaserver 2>/dev/null | grep -q "^Status: install ok installed" && LMS_PKG="logitechmediaserver" || true

if [[ -n "$LMS_PKG" ]]; then
    info "LMS already installed ($LMS_PKG)."
else
    LMS_VERSION=""
    RELEASE_JSON=$(curl -sf --max-time 15 \
        "https://api.github.com/repos/LMS-Community/slimserver/releases/latest" || echo "")
    if [[ -n "$RELEASE_JSON" ]]; then
        LMS_VERSION=$(echo "$RELEASE_JSON" | grep '"tag_name"' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
    fi
    [[ -z "$LMS_VERSION" ]] && LMS_VERSION=$(curl -sfI --max-time 15 \
        "https://github.com/LMS-Community/slimserver/releases/latest" \
        | grep -i "^location:" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
    [[ -z "$LMS_VERSION" ]] && LMS_VERSION="9.1.0"

    info "LMS version: $LMS_VERSION"
    LMS_URL="https://downloads.lms-community.org/LyrionMusicServer_v${LMS_VERSION}/lyrionmusicserver_${LMS_VERSION}_all.deb"

    # Download to /home/pi instead of /tmp (tmpfs has too little space on Pi Zero 2W)
    LMS_DEB="/home/pi/lms_install.deb"
    wget --show-progress -O "$LMS_DEB" "$LMS_URL" || error "LMS download failed."

    while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
        warn "dpkg lock busy – waiting 5 seconds..."
        sleep 5
    done

    apt-get install -y "$LMS_DEB" \
        || { dpkg -i "$LMS_DEB" 2>/dev/null; apt-get install -f -y; } \
        || error "LMS installation failed."
    rm -f "$LMS_DEB"
fi

# Hard check: LMS MUST be installed before proceeding
dpkg -s lyrionmusicserver 2>/dev/null | grep -q "^Status: install ok installed" \
    || dpkg -s logitechmediaserver 2>/dev/null | grep -q "^Status: install ok installed" \
    || error "LMS is NOT installed – aborting. Please check manually."
info "LMS installed and verified."

# Install squeezelite AFTER LMS (avoids RAM issues on Pi Zero 2W)
apt-get install -y squeezelite
info "Squeezelite installed."

# Prepare Bluetooth
rfkill unblock bluetooth 2>/dev/null || true
systemctl enable bluetooth 2>/dev/null || true
systemctl start bluetooth  2>/dev/null || true
sed -i 's/^#AutoEnable=true/AutoEnable=true/' /etc/bluetooth/main.conf 2>/dev/null || true
systemctl enable bluealsa 2>/dev/null || true
systemctl start bluealsa  2>/dev/null || true
info "Bluetooth enabled."

# Don't auto-start hostapd/dnsmasq
systemctl stop hostapd   2>/dev/null || true
systemctl stop dnsmasq   2>/dev/null || true
systemctl disable hostapd 2>/dev/null || true
systemctl disable dnsmasq 2>/dev/null || true
systemctl unmask hostapd  2>/dev/null || true
info "hostapd/dnsmasq disabled."

# =============================================================================
# STEP 3: Enable I2C + SPI
# =============================================================================
step "Step 3: Enable I2C + SPI"

if [[ -f /boot/firmware/config.txt ]]; then
    CONFIG_FILE="/boot/firmware/config.txt"
else
    CONFIG_FILE="/boot/config.txt"
fi

enable_dtparam() {
    local param="$1"
    if grep -q "^${param}" "$CONFIG_FILE" 2>/dev/null; then
        info "$param already active."
    elif grep -q "^#${param}" "$CONFIG_FILE" 2>/dev/null; then
        sed -i "s/^#${param}/${param}/" "$CONFIG_FILE"
        info "$param enabled."
    else
        echo "$param" >> "$CONFIG_FILE"
        info "$param added."
    fi
}

enable_dtparam "dtparam=i2c_arm=on"
enable_dtparam "dtparam=spi=on"

grep -q "^i2c-dev" /etc/modules 2>/dev/null || echo "i2c-dev" >> /etc/modules
modprobe i2c-dev   2>/dev/null || true
modprobe spi-bcm2835 2>/dev/null || true
info "I2C + SPI enabled."

# =============================================================================
# STEP 4: Configure USB sound card
# =============================================================================
step "Step 4: Configure USB sound card"

USB_CARD_IDX=$(aplay -l 2>/dev/null | grep -i "usb" | head -1 | grep -o "card [0-9]*" | grep -o "[0-9]*" || echo "1")

cat > /etc/asound.conf << EOF
# Softvol + dmix: reduces CPU noise/EMI compared to direct hw access
pcm.!default {
    type plug
    slave.pcm "softvol"
}

pcm.softvol {
    type softvol
    slave.pcm "dmixer"
    control {
        name "SoftMaster"
        card $USB_CARD_IDX
    }
    min_dB -51.0
    max_dB 0.0
}

pcm.dmixer {
    type dmix
    ipc_key 1024
    slave {
        pcm "hw:$USB_CARD_IDX,0"
        period_time 0
        period_size 1024
        buffer_size 4096
        rate 44100
    }
}

ctl.!default {
    type hw
    card $USB_CARD_IDX
}
EOF
info "ALSA (softvol+dmix) set to card $USB_CARD_IDX."

# Disable onboard audio (Pi Zero 2W has no analog output,
# but the snd_bcm2835 driver can cause EMI interference)
if grep -q "^dtparam=audio=on" "$CONFIG_FILE" 2>/dev/null; then
    sed -i "s/^dtparam=audio=on/dtparam=audio=off/" "$CONFIG_FILE"
    info "Onboard audio disabled (was on)."
elif ! grep -q "^dtparam=audio=" "$CONFIG_FILE" 2>/dev/null; then
    echo "dtparam=audio=off" >> "$CONFIG_FILE"
    info "Onboard audio disabled."
else
    info "Onboard audio already disabled."
fi

# Blacklist snd_bcm2835 kernel module (dtparam alone is not always sufficient)
echo "blacklist snd_bcm2835" > /etc/modprobe.d/blacklist-onboard-audio.conf
info "snd_bcm2835 module blacklisted."

# =============================================================================
# STEP 5: Set up Python environment
# =============================================================================
step "Step 5: Set up Python environment"

mkdir -p "$APP_DIR/templates" "$APP_DIR/static" "$APP_DIR/lang"
python3 -m venv "$VENV"
$PIP install --upgrade pip -q
$PIP install RPi.GPIO spidev mfrc522 smbus2 RPLCD flask requests gpiozero -q
info "Python packages installed."

# =============================================================================
# STEP 6: Copy app files
# =============================================================================
step "Step 6: Copy app files"

# Python files
cp "$SCRIPT_DIR/app/"*.py "$APP_DIR/"
cp "$SCRIPT_DIR/app/version.txt" "$APP_DIR/"
info "Python files copied."

# Templates
cp "$SCRIPT_DIR/app/templates/"*.html "$APP_DIR/templates/"
info "HTML templates copied."

# Static (CSS)
cp "$SCRIPT_DIR/app/static/"* "$APP_DIR/static/" 2>/dev/null || true
info "Static files copied."

# Language files
cp "$SCRIPT_DIR/app/lang/"*.json "$APP_DIR/lang/" 2>/dev/null || true
info "Language files copied."

# Config template
cp "$SCRIPT_DIR/app/config.json.template" "$APP_DIR/" 2>/dev/null || true

# Music directory
mkdir -p /home/music
chmod 777 /home/music

# =============================================================================
# STEP 7: Create config.json
# =============================================================================
step "Step 7: Configuration"

if [[ ! -f "$APP_DIR/config.json" ]]; then
    HOSTNAME=$(hostname)
    BOX_ID="${HOSTNAME}-$(cat /etc/machine-id 2>/dev/null | cut -c1-8 || echo unknown)"
    sed -e "s/__VERSION__/$VERSION/" \
        -e "s/__BTN_VOL_UP__/$BTN_VOL_UP/" \
        -e "s/__BTN_VOL_DOWN__/$BTN_VOL_DOWN/" \
        -e "s/__BTN_NEXT__/$BTN_NEXT/" \
        -e "s/__BTN_PREV__/$BTN_PREV/" \
        -e "s/__BTN_PAUSE__/$BTN_PAUSE/" \
        -e "s/__BTN_LCD_BACKLIGHT__/$BTN_LCD_BACKLIGHT/" \
        -e "s/__HOSTNAME__/$HOSTNAME/" \
        -e "s/__BOX_ID__/$BOX_ID/" \
        "$SCRIPT_DIR/app/config.json.template" > "$APP_DIR/config.json"
    info "config.json created."
else
    info "config.json already exists – not overwritten."
fi

# Always update version
echo "$VERSION" > "$APP_DIR/version.txt"
python3 -c "
import json
with open('$APP_DIR/config.json') as f: cfg = json.load(f)
cfg['version'] = '$VERSION'
with open('$APP_DIR/config.json', 'w') as f: json.dump(cfg, f, indent=2)
" 2>/dev/null || true
info "Version $VERSION set."

# venv: enable system packages for lgpio
sed -i 's/include-system-site-packages = false/include-system-site-packages = true/' "$VENV/pyvenv.cfg" 2>/dev/null || true

# =============================================================================
# STEP 8: Create systemd services
# =============================================================================
step "Step 8: Systemd services"

# v2.3: Only 3 services instead of 6 (RAM optimization)
# - lms-web: Flask + WiFi thread + Bluetooth thread + Alarm + Auto-Standby
# - lms-rfid: RFID reader (isolated due to SPI sensitivity)
# - lms-hardware: LCD + Buttons in one process
for svc_name in rfid hardware web; do
    svc_file="/etc/systemd/system/lms-${svc_name}.service"
    py_file="${svc_name}"
    [[ "$svc_name" == "rfid" ]]     && py_file="rfid_handler"
    [[ "$svc_name" == "hardware" ]] && py_file="hardware_daemon"
    [[ "$svc_name" == "web" ]]      && py_file="web_app"

    restart_sec=5

    cat > "$svc_file" << SVCEOF
[Unit]
Description=kid2tune ${svc_name}
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
ExecStart=$PYTHON $APP_DIR/${py_file}.py
WorkingDirectory=$APP_DIR
Restart=always
RestartSec=$restart_sec
KillMode=control-group
KillSignal=SIGTERM
TimeoutStopSec=10
User=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF
done

# Disable and remove old services (upgrade from v2.2)
for old_svc in lms-lcd lms-buttons lms-wifi lms-bluetooth; do
    systemctl stop "$old_svc" 2>/dev/null || true
    systemctl disable "$old_svc" 2>/dev/null || true
    rm -f "/etc/systemd/system/${old_svc}.service"
done

# squeezelite service
cat > /etc/systemd/system/squeezelite.service << 'SQEOF'
[Unit]
Description=Squeezelite (Squeezebox Player)
After=sound.target network-online.target lyrionmusicserver.service
Wants=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
ExecStartPre=/bin/sleep 1
ExecStart=/bin/sh -c '/usr/bin/squeezelite -n $(hostname) -s 127.0.0.1 -o default -b 512:1024 -c flac,pcm,mp3,ogg'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SQEOF

info "Service files created."

# =============================================================================
# STEP 9: Enable services
# =============================================================================
step "Step 9: Enable services"

systemctl daemon-reload
update-rc.d squeezelite disable 2>/dev/null || true

# Start LMS first and wait (needs a lot of RAM at startup)
for candidate in lyrionmusicserver logitechmediaserver; do
    if systemctl list-unit-files "${candidate}.service" 2>/dev/null | grep -q "${candidate}"; then
        systemctl enable "$candidate" 2>/dev/null || true
        systemctl restart "$candidate" 2>/dev/null || true
        info "$candidate started – waiting until ready..."
        for i in $(seq 1 30); do
            if curl -sf --max-time 2 "http://127.0.0.1:9000/" >/dev/null 2>&1; then
                info "$candidate is ready."
                break
            fi
            sleep 2
        done
        break
    fi
done

# Start squeezelite (requires running LMS)
systemctl enable squeezelite 2>/dev/null || true
systemctl restart squeezelite 2>/dev/null || true
sleep 3
info "Squeezelite started."

# Start controller services one by one (not all at once -> RAM)
# v2.3: Only 3 services (web contains WiFi+BT, hardware contains LCD+Buttons)
for svc in lms-web lms-rfid lms-hardware; do
    systemctl enable  "$svc" 2>/dev/null || true
    systemctl restart "$svc" 2>/dev/null || warn "$svc failed."
    sleep 2
    info "$svc enabled."
done

# =============================================================================
# STEP 10: Boot Optimization
# =============================================================================
step "Step 10: Boot optimization"

# Disable unnecessary services
touch /etc/cloud/cloud-init.disabled
for svc in cloud-init-main cloud-init-local cloud-init-network cloud-config cloud-final ModemManager NetworkManager-wait-online udisks2; do
    systemctl disable "$svc" 2>/dev/null || true
    systemctl mask "$svc" 2>/dev/null || true
done
info "Unnecessary services disabled."

# Boot config
BOOT_CFG=""
[[ -f /boot/firmware/config.txt ]] && BOOT_CFG="/boot/firmware/config.txt"
[[ -z "$BOOT_CFG" && -f /boot/config.txt ]] && BOOT_CFG="/boot/config.txt"
if [[ -n "$BOOT_CFG" ]]; then
    grep -q "gpu_mem=" "$BOOT_CFG" || echo -e "\ngpu_mem=16" >> "$BOOT_CFG"
    grep -q "boot_delay=" "$BOOT_CFG" || echo "boot_delay=0" >> "$BOOT_CFG"
    sed -i 's/^camera_auto_detect=1/camera_auto_detect=0/' "$BOOT_CFG"
    sed -i 's/^display_auto_detect=1/display_auto_detect=0/' "$BOOT_CFG"
    info "Boot config optimized."
fi

# ── RAM Optimization ────────────────────────────────────────────────────────
# Permanent swap (256 MB) as safety net for Pi Zero 2W
PERM_SWAP="/var/swap"
if [[ ! -f "$PERM_SWAP" ]]; then
    dd if=/dev/zero of="$PERM_SWAP" bs=1M count=256 status=none 2>/dev/null || true
    chmod 600 "$PERM_SWAP"
    mkswap "$PERM_SWAP" >/dev/null 2>&1 || true
    echo "$PERM_SWAP none swap sw 0 0" >> /etc/fstab
    swapon "$PERM_SWAP" 2>/dev/null || true
    info "Permanent swap (256 MB) created."
else
    info "Permanent swap already exists."
fi

# Reduce swappiness (prefer RAM, only use swap when needed)
echo "vm.swappiness=10" > /etc/sysctl.d/99-musicbox.conf
sysctl -p /etc/sysctl.d/99-musicbox.conf 2>/dev/null || true
info "vm.swappiness=10 set."

# Limit journald storage (saves ~20-50 MB)
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/musicbox.conf << 'EOF'
[Journal]
SystemMaxUse=16M
RuntimeMaxUse=8M
EOF
systemctl restart systemd-journald 2>/dev/null || true
info "journald limited to 16 MB."

# Quiet boot
CMDLINE=""
[[ -f /boot/firmware/cmdline.txt ]] && CMDLINE="/boot/firmware/cmdline.txt"
[[ -z "$CMDLINE" && -f /boot/cmdline.txt ]] && CMDLINE="/boot/cmdline.txt"
[[ -n "$CMDLINE" ]] && ! grep -q "quiet" "$CMDLINE" && sed -i 's/$/ quiet loglevel=3/' "$CMDLINE"

# Disable WiFi power save
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/wifi-powersave-off.conf << 'EOF'
[connection]
wifi.powersave = 2
EOF
iw wlan0 set power_save off 2>/dev/null || true
info "WiFi power save disabled."

# =============================================================================
# STEP 11: Permissions
# =============================================================================
step "Step 11: Permissions"

chmod +x "$APP_DIR/"*.py
chown -R root:root "$APP_DIR"
chmod 644 "$APP_DIR/config.json"
info "Permissions set."

# =============================================================================
# DONE
# =============================================================================
PI_IP=$(hostname -I | awk '{print $1}' || echo "<Pi-IP>")

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║     kid2tune v${VERSION} – Installation complete!          ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║  LMS Web-UI:       http://${PI_IP}:9000              ${NC}"
echo -e "${GREEN}║  Controller-Web:   http://${PI_IP}:80                ${NC}"
echo -e "${GREEN}║  Dashboard:        http://${PI_IP}:80/dashboard      ${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║  GPIO: Vol+=${BTN_VOL_UP} Vol-=${BTN_VOL_DOWN} Next=${BTN_NEXT} Prev=${BTN_PREV} Pause=${BTN_PAUSE}   ${NC}"
echo -e "${GREEN}║  AP-Fallback: $(hostname)-kid2tune / Geheim123!     ${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
# Remove temporary swap
if swapon --show | grep -q "$SWAP_FILE"; then
    swapoff "$SWAP_FILE" 2>/dev/null || true
    rm -f "$SWAP_FILE"
    info "Temporary swap removed."
fi

warn "Please reboot now:  sudo reboot"
