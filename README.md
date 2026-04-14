# kid2tune

Raspberry Pi-based RFID music box for kids — place a card, play a tune. Controls [Lyrion Music Server](https://github.com/LMS-Community/slimserver) (LMS/Squeezebox) via RFID cards, LCD display, physical buttons and web interface. Supports multi-room audio, Bluetooth output, NAS sync and more.

*Dedicated to my kids L&E*

## Features

- **RFID card control** (RC522) — place a card to play music (tracks, albums, playlists, Spotify, URLs, local files)
- **LCD display** (20x4 I2C) — customizable layout for playback, idle status, system info
- **LCD Play Layout Editor** — design the 4 LCD lines during playback via web UI with variable blocks
- **GPIO buttons** — play/pause, next/prev, volume up/down, LCD backlight
- **Button detect mode** — automatically identifies which GPIO a button is connected to
- **Web interface** — full control via browser (port 80), dark theme
- **Multi-room audio** — synchronized playback across multiple boxes
- **Bluetooth audio** — switchable output to BT speakers
- **NAS sync** — synchronize RFID mappings and music files via SMB/CIFS
- **WiFi AP fallback** — creates its own WiFi (`<hostname>-kid2tune`) when no known network is available
- **Sleep timer** — with fade-out
- **Alarms** — scheduled playback via RFID mapping
- **Deep standby** — safe sleep mode (filesystem read-only, safe to unplug)
- **Auto standby** — configurable, e.g. after 30 min without music
- **Always online** — guaranteed online after every boot, no standby hangover
- **Auto update check** — LCD shows "Update available" when a new version is on GitHub
- **LCD system status** — LMS, Spotify, buttons, RFID, WiFi at a glance
- **Git updates** — one-click update from GitHub, always clickable
- **OTA updates** — distribute code updates to all boxes on the network
- **Multi-language** — UI available in German and English, easily extensible via JSON files
- **RAM optimized** — 3 instead of 6 processes, ~45 MB RAM savings
- **3D printed case** — STL files for a complete enclosure included

## Hardware

### Components

- Raspberry Pi Zero 2W
- MicroSD card (min. 16 GB recommended)
- RC522 RFID reader (SPI)
- LCD display 2004A with HD44780 controller (20x4, I2C via PCF8574 adapter)
- 6 push buttons (GPIO)
- USB sound card
- Passive speaker + amplifier

### GPIO pin assignment (default)

| Function       | GPIO (BCM) | Pin |
|----------------|------------|-----|
| Volume up      | 19         | 35  |
| Volume down    | 26         | 37  |
| Next track     | 16         | 36  |
| Previous track | 23         | 16  |
| Play/Pause     | 21         | 40  |
| LCD backlight  | 20         | 38  |
| I2C SDA        | 2          | 3   |
| I2C SCL        | 3          | 5   |
| SPI MOSI       | 10         | 19  |
| SPI MISO       | 9          | 21  |
| SPI SCLK       | 11         | 23  |
| SPI CE0        | 8          | 24  |
| RFID RST       | 25         | 22  |

GPIO pins are configurable via the web UI (/buttons). Use the **Button detect mode** to automatically identify which GPIO each button is connected to.

Reserved pins (excluded from assignment): GPIO 0-3 (I2C/LCD), 8-11 (SPI/RFID), 25 (RFID RST).

## Installation

### Prerequisites

- Raspberry Pi with Raspberry Pi OS (Bookworm/Trixie, 64-bit)
- Internet connection
- SSH access

### Quick start

```bash
git clone https://github.com/hannskanns4/kid2tune.git /tmp/kid2tune && sudo bash /tmp/kid2tune/install.sh && sudo reboot
```

### What the installer does

1. Install system packages (Python, I2C, SPI, Bluetooth, hostapd, dnsmasq)
2. Create temporary swap (1 GB, for Pi Zero 2W with 512 MB RAM)
3. Enable I2C + SPI in `/boot/firmware/config.txt`
4. Configure USB sound card as default audio
5. Download and install Lyrion Music Server
6. Install Squeezelite (after LMS to avoid OOM on Pi Zero 2W)
7. Create Python venv at `/opt/lms-controller/venv`
8. Copy app files, templates, language files to `/opt/lms-controller/`
9. Generate `config.json` from template
10. Create and enable systemd services
11. Boot optimization (reduce GPU RAM, disable unnecessary services)

### After installation

The box is accessible at:

- **Web UI**: `http://<hostname or IP>` (port 80)
- **LMS Web UI**: `http://<hostname or IP>:9000`

## Architecture

### Services

| Service          | File                  | Function                          |
|------------------|-----------------------|-----------------------------------|
| `lms-web`        | `web_app.py`          | Flask web interface + WiFi + Bluetooth (threads) |
| `lms-rfid`       | `rfid_handler.py`     | RFID card reader daemon           |
| `lms-hardware`   | `hardware_daemon.py`  | LCD display + GPIO buttons (single process) |
| `squeezelite`    | (system)              | Audio player for LMS              |

WiFi + Bluetooth run as threads in `lms-web`, LCD + buttons run together in `lms-hardware` (~45 MB RAM savings vs separate processes).

### Modules

| File                  | Function                                        |
|-----------------------|-------------------------------------------------|
| `config_manager.py`   | Central config management with file locking     |
| `lms_client.py`       | JSON-RPC wrapper for LMS with player ID cache   |
| `sync_manager.py`     | NAS sync for RFID mappings and music files      |
| `multiroom_manager.py`| Multi-room synchronization                      |
| `standby_manager.py`  | Deep standby (stop services, ro filesystem)     |
| `update_manager.py`   | Git-based update system                         |
| `i18n.py`             | Internationalization (JSON-based translations)  |

### Data flow

```
RFID card   -->  rfid_handler   -->  lms_client  -->  LMS Server  -->  Squeezelite  -->  Audio
Web UI      -->  web_app        -->  lms_client  -/
Buttons     -->  button_handler -->  lms_client  -/
LCD         <--  lcd_display    <--  lms_client (status polling)
```

### File structure on the Pi

```
/opt/lms-controller/
  config.json           # Configuration (RFID mappings, buttons, sync, etc.)
  config.json.lock      # File lock for atomic access
  sync_pending.json     # Offline queue for NAS sync
  version.txt           # Current version
  venv/                 # Python virtual environment
  *.py                  # Application modules
  templates/            # HTML templates (Jinja2)
  static/               # CSS
  lang/                 # Language files (de.json, en.json, ...)

/home/music/            # Local music files
/mnt/lms-sync/          # NAS mount point (SMB/CIFS)

/tmp/lms_last_rfid      # Last unknown RFID UID
/tmp/lms_sleep_timer    # Sleep timer countdown (seconds)
/tmp/lcd_backlight      # LCD backlight status (0/1)
/tmp/lms_standby_active # Deep standby flag
/tmp/multiroom_active   # Multiroom status (JSON)
```

## Web Interface

### Pages

| Path          | Page            | Function                                    |
|---------------|-----------------|---------------------------------------------|
| `/`           | Player          | Playback control, volume, progress, multiroom |
| `/rfid`       | RFID            | Scan, assign and manage cards               |
| `/alarms`     | Alarms          | Configure scheduled playback                |
| `/sync`       | NAS Sync        | SMB share configuration, push/pull          |
| `/wifi`       | WiFi            | Connect to networks, configure AP mode      |
| `/bluetooth`  | Bluetooth       | Pair devices, switch audio output           |
| `/buttons`    | Buttons         | GPIO pin assignment + auto-detect mode      |
| `/lcd-layout` | LCD             | Customize LCD play mode layout              |
| `/settings`   | Settings        | Hostname, timing, standby, language, updates |

### RFID card types

| Type         | Function                                    |
|--------------|---------------------------------------------|
| `track`      | Play a single song from LMS library         |
| `album`      | Play an album                               |
| `playlist`   | Play a playlist                             |
| `url`        | Play URL/stream/Spotify link                |
| `local`      | Play a local music file                     |
| `bluetooth`  | Switch audio to BT device                   |
| `multiroom`  | Activate/deactivate multi-room sync         |
| `sleep`      | Start/stop sleep timer                      |
| `shutdown`   | Safely shut down the box                    |

## Usage

### Physical buttons

| Action                          | Buttons                             |
|---------------------------------|-------------------------------------|
| Volume +/-                      | Vol+ / Vol-                         |
| Next/previous track             | Next / Prev                         |
| Play/pause                      | Pause (short press)                 |
| Stop                            | Pause (long press, >1s)             |
| Skip +10 tracks                 | Next (long press)                   |
| Jump to playlist start          | Prev (long press)                   |
| LCD backlight                   | LCD button                          |
| Deep standby request            | Vol+ + Vol- + Pause (hold 5s)       |
| Confirm standby                 | Vol+ press (within 15s)             |
| Wake from standby               | Any button                          |

Hold time (default: 5s) and confirmation timeout (default: 15s) are configurable in Settings.

### LCD display

#### Idle mode (no music)
```
Line 1: Date + time
Line 2: Hostname / IP (alternating every 5s)
Line 3: Online / Update available / Local
Line 4: L:OK S:OK B:OK R:OK W:OK
```

- **Line 3** shows `Online` when connected, `Update available` when a new version is on GitHub (checked every 30 min), or `Local` when offline
- **Line 4** status indicators: **L** = LMS | **S** = Spotify | **B** = Buttons | **R** = RFID | **W** = WiFi

#### Play mode (customizable)
Default layout:
```
Line 1: {title}          (scrolls if > 20 chars)
Line 2: {artist}         (scrolls if > 20 chars)
Line 3: {elapsed}/{duration}  {mode}
Line 4: {date}  {time}
```

Customize via **LCD** page in the web UI. Available variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `{title}` | Track title | `Twinkle Twinkle Star` |
| `{artist}` | Artist name | `Kids Songs` |
| `{album}` | Album name | `Lullabies Vol.3` |
| `{elapsed}` | Elapsed time | `02:34` |
| `{duration}` | Total duration | `04:12` |
| `{volume}` | Volume % | `65` |
| `{mode}` | Play/Pause/Stop | `▶PLAY` |
| `{date}` | Date | `14.04.2026` |
| `{time}` | Time | `10:23:45` |
| `{hostname}` | Box hostname | `lilabox` |
| `{ip}` | IP address | `192.168.1.42` |
| `{status}` | Online/Local | `Online` |

### Deep standby

3 ways to activate:
1. **Buttons**: Hold Vol+ + Vol- + Pause for 5s, LCD shows "STANDBY?", press Vol+ to confirm
2. **Web UI**: Settings page -> "Deep Standby"
3. **Automatic**: After configurable idle time (default: 30 min)

In standby:
- LCD off, all services stopped, filesystem read-only
- **Safe to unplug** (no data loss)
- Web UI and button handler remain active for wake

Wake up: Press any button or Web UI -> "Wake up"

### Multi-room

1. Place a "multiroom" RFID card on the master box (or use the web UI)
2. Master scans the network for other kid2tune boxes
3. Slave boxes redirect their Squeezelite to the master LMS
4. All players are synchronized — master controls playback
5. Place the same card again = end multiroom

The master correctly maintains its sync group — slaves join without interrupting playback.

### NAS sync

Synchronizes RFID mappings and music files via an SMB/CIFS network share:

- **Last-write-wins**: Newer timestamp wins on conflicts
- **Tombstones**: Deletions are propagated
- **Offline queue**: Changes are buffered when NAS is unavailable
- **Music sync**: Local files are automatically uploaded/downloaded

Configuration at `/sync` in the web UI.

## Configuration

Main configuration in `/opt/lms-controller/config.json`:

```json
{
  "version": "2.8",
  "language": "de",
  "lms_host": "localhost",
  "lms_port": 9000,
  "lcd": {
    "i2c_address": "0x27",
    "cols": 20,
    "rows": 4,
    "play_layout": ["{title}", "{artist}", "{elapsed}/{duration}  {mode}", "{date}  {time}"]
  },
  "buttons": { "vol_up": 19, "vol_down": 26, "next": 16, "prev": 23, "pause": 21, "lcd_backlight": 20 },
  "wifi": { "ap_ssid": "mybox-kid2tune", "ap_password": "Geheim123!", "ap_channel": 7, "check_interval": 30 },
  "rfid_mappings": {},
  "sync": { "enabled": false, "nas_share": "", "username": "", "password": "", "mount_point": "/mnt/lms-sync", "box_id": "my-box" },
  "volume_max": 100,
  "auto_standby_minutes": 30,
  "display_off_minutes": 30,
  "shutdown": { "hold_time": 5, "confirm_timeout": 15 },
  "alarms": [],
  "bluetooth": { "active_device": "", "auto_reconnect": true, "check_interval": 15 },
  "known_boxes": {},
  "github_token": ""
}
```

Most settings are configurable via the web UI.

## Internationalization

The web interface supports multiple languages. Language files are stored as JSON in `app/lang/`:

- `de.json` — German (default)
- `en.json` — English

### Adding a new language

1. Copy `app/lang/en.json` to `app/lang/xx.json` (e.g. `fr.json`)
2. Translate all values (keys stay the same)
3. The new language appears automatically in Settings -> Language

## Updates

### Git update (recommended)

Via the web UI: **Settings -> Software Update**

- **"Check for update"** — compares local version with GitHub tags (sorted by version number)
- **"Update this box"** — pulls code via `git clone`, copies files, restarts services (always clickable)
- **"Update all boxes"** — updates all boxes on the network automatically

`config.json` is **never** overwritten during updates.

The LCD automatically checks for updates every 30 minutes and displays "Update available" on the idle screen.

### Manual via SSH

```bash
git clone --depth 1 https://github.com/hannskanns4/kid2tune.git /tmp/kid2tune
sudo cp /tmp/kid2tune/app/*.py /opt/lms-controller/
sudo cp /tmp/kid2tune/app/templates/*.html /opt/lms-controller/templates/
sudo cp /tmp/kid2tune/app/lang/*.json /opt/lms-controller/lang/
sudo cp /tmp/kid2tune/app/version.txt /opt/lms-controller/
sudo systemctl restart lms-web lms-rfid lms-hardware
```

## Versioning

**X.Y.Z** — X = major (breaking changes), Y = feature, Z = bugfix

## Troubleshooting

### Check service status

```bash
for svc in lyrionmusicserver squeezelite lms-web lms-rfid lms-hardware; do
  echo "$svc: $(systemctl is-active $svc)"
done

# Logs for a service
sudo journalctl -u lms-web -n 50 --no-pager
```

### Common issues

| Problem | Cause | Solution |
|---------|-------|----------|
| LCD blank but lit | Wrong I2C address | LCD daemon tries 0x27 and 0x3F automatically. Check `sudo i2cdetect -y 1` |
| RFID not reading | SPI not enabled | `sudo raspi-config` -> Interface -> SPI enable |
| No sound | Wrong sound card | Check `aplay -l`, adjust `/etc/asound.conf` |
| WiFi AP no connection | dnsmasq config missing | Restart: `sudo systemctl restart lms-web` |
| LMS install OOM kill | Not enough RAM (Pi Zero) | `install.sh` installs squeezelite after LMS and creates 1 GB swap |
| Box broken after power loss | Filesystem corrupt | Check SD card with `fsck`, or use deep standby |
| Update says "no update" | Version tag comparison | Update button is always clickable regardless of check result |
| Play/pause button stuck | LMS pause toggle bug | Fixed: explicit play/pause command based on current mode |

## 3D Printed Case

STL files for a 3D printed enclosure are included in the [`3d/`](3d/) directory:

| File | Part |
|------|------|
| `kid2tune-top.stl` | Top panel (RFID reader area) |
| `kid2tune-bottom.stl` | Bottom panel |
| `kid2tune-front.stl` | Front panel (speaker cutout) |
| `kid2tune-back.stl` | Back panel |
| `kid2tune-left.stl` | Left side panel |
| `kid2tune-right.stl` | Right side panel |

The case consists of 6 panels that snap/glue together.

## License

GPL-2.0 — see [LICENSE](LICENSE) for details.

---
*Dedicated to my kids L&E*
