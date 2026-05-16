# Changelog

All notable changes to this project are documented here.
Versioning: **X.Y.Z** — X = major, Y = feature, Z = bugfix.

## [2.9.1] – 2026-05-16

### Added
- **Boot timing measurement**: oneshot systemd service `kid2tune-boot-timing` records
  seconds-since-power-on for each milestone: multi-user.target, LMS port 9000,
  LMS RPC ready, Spotty plugin available, Squeezelite registered, kid2tune web UI
- New card on Settings page shows the last cold-boot measurement
- Endpoint: `GET /api/boot-timing` returns the JSON from `/var/lib/lms-controller/boot_timing.json`
- First measurement on lilabox: 54s total cold-boot (27s system + 26s LMS startup + 1s register)

## [2.9.0] – 2026-05-16

### Added
- **Play history**: stores the last 10 unique playback items (deduplicated by value)
  - New page `/history` with full list and "Replay" button per entry
  - Top-3 preview block on the index page directly below Quick-Play
  - New nav item "Verlauf / History"
  - Captures plays from RFID cards, Quick-Play URLs and alarms
  - Spotify/URL plays without label get title/artist fetched from LMS after 3s
  - Stored in `app/play_history.json` (not synced, local-only)
  - Endpoints: `GET /api/history`, `POST /api/history/play`, `POST /api/history/clear`
- **One-click LMS plugin updates** on the index page
  - Card auto-checks `http://lms:9000/settings/server/plugins.html` for pending plugin updates every 5 min
  - When updates available: shows "X Updates verfügbar: Plugin A, Plugin B" + green "Updates installieren" button
  - One click: scrapes form, preserves currently-installed plugins, includes LMS CSRF `rand` token, submits, restarts LMS service — no manual LMS UI navigation
  - When all up to date: shows "Alle Plugins sind aktuell" + a fallback "LMS neu starten" button
  - Endpoints: `GET /api/lms/plugins/check`, `POST /api/lms/plugins/update`, `POST /api/lms/restart`
  - Service restart tries `lyrionmusicserver`, falls back to `logitechmediaserver`
  - Designed for users overwhelmed by the LMS plugin manager — single button, full automation

## [2.8.2] – 2026-04-14

### Changed
- Complete README rewrite covering all features: LCD layout editor, button detect mode, auto update check, multiroom sync, reserved GPIO pins, LCD variables reference, updated config example, troubleshooting

## [2.8.1] – 2026-04-14

### Added
- LCD idle screen shows "Update available" instead of "Online" when a new version is on GitHub
- Update check runs automatically in background every 30 minutes (non-blocking)
- Shows "Local" when offline (no update check possible)

## [2.8.0] – 2026-04-14

### Added
- **LCD Play Layout Editor**: Customize the 4 LCD lines during playback via web UI
  - Drag & drop variables: `{title}`, `{artist}`, `{album}`, `{elapsed}`, `{duration}`, `{volume}`, `{mode}`, `{date}`, `{time}`, `{hostname}`, `{ip}`, `{status}`
  - Live preview with example data
  - Lines with `{title}` or `{artist}` scroll automatically
  - New nav item "LCD" in menu bar
  - Config stored in `lcd.play_layout` in config.json

## [2.7.1] – 2026-04-14

### Changed
- All remaining German text translated to English: log messages, return messages, install.sh output, HTML/JS comments, CSS comments
- Complete English codebase — no German strings left outside of `de.json`

## [2.7.0] – 2026-04-14

### Added
- 3D printed case: STL files for 6-panel enclosure in `3d/` directory

## [2.6.5] – 2026-04-14

### Fixed
- Multiroom sync: `sync_to()` had parameters swapped — master became slave, playback stopped. Now master correctly adds slaves to its group.

## [2.6.4] – 2026-04-12

### Fixed
- Update buttons always clickable (were disabled when version check said "no update")
- Version check now sorts tags by version number, not alphabetically
- Boxes with old versions can now update even after repo rename

## [2.6.3] – 2026-04-12

### Fixed
- Play/pause toggle not working: LMS `pause toggle` command doesn't resume from pause state. Now checks current mode and sends explicit `play` or `pause` command.
- Affects both physical buttons and web UI play/pause

## [2.6.2] – 2026-04-12

### Fixed
- JavaScript crash on all pages: `const _t` was declared in both base.html and page templates, causing a duplicate declaration error that broke all JS functionality
- Button detect, multiroom, sync, wifi, bluetooth, RFID pages all affected

## [2.6.1] – 2026-04-12

### Fixed
- Button detect mode: ignore GPIOs that are already LOW at start (e.g. permanently connected buttons)
- Detection now waits for a HIGH→LOW transition instead of just checking for LOW

## [2.6.0] – 2026-04-12

### Added
- **Button detect mode**: Automatically detects which GPIO pin is pressed
  - Start detection on the Buttons page, press a physical button, GPIO is shown
  - Assign detected pin to a function (vol+, vol-, next, prev, pause, backlight)
  - Reserved pins excluded: GPIO 0-3 (I2C/LCD), 8-11 (SPI/RFID), 25 (RFID RST)

### Changed
- GPIO dropdown in Buttons page now excludes SPI and RFID pins

## [2.5.3] – 2026-04-12

### Fixed
- Squeezelite install moved after LMS (prevents OOM on Pi Zero 2W with 512MB RAM)

## [2.5.2] – 2026-04-12

### Fixed
- Installer now copies `lang/` directory and `config.json.template`
- Removed non-functional curl one-liner from README (requires full git clone)

## [2.5.1] – 2026-04-12

### Fixed
- Race condition in multiroom sync (proper threading.Lock instead of bool flag)
- Future results now checked in multiroom sync (failed boxes no longer added)
- Missing i18n strings in multiroom API responses
- Redundant ternary in RFID edit (identical branches)
- `tar.extractall()` now uses `filter="data"` (Python 3.12+ security)
- Update package no longer includes `sync_pending.json` or `config.json.lock`
- Imports cleaned up in web_app.py (threading/datetime moved to top)

### Removed
- Dead code: unused hostapd/dnsmasq config generators in wifi_manager.py
- Unused imports (`json`, `tempfile`, `shutil`) and variables (`CONFIG_PATH`) across modules

### Changed
- `config.json.template` now includes `language`, `known_boxes`, `github_token`
- Removed empty `rfid_sync_v2.json` from repository

## [2.5.0] – 2026-04-12

### Added
- **Internationalization (i18n):** Full web interface localization
- JSON-based language files (`app/lang/de.json`, `app/lang/en.json`)
- Language selector dropdown in Settings
- New languages can be added by copying a JSON file
- `i18n.py` module with `t()` function for Python and Jinja2 templates
- LMS link in navbar (opens Lyrion Music Server in new tab)
- LICENSE file (MIT)
- `requirements.txt` with Python dependencies
- `CHANGELOG.md` with version history

### Changed
- All templates use `t()` calls instead of hardcoded strings
- All API responses use translated strings
- Version consistently set to 2.5.0 across all files
- README, changelog, and code comments translated to English

### Fixed
- Update manager now copies `lang/` directory
- i18n module starts gracefully without language files (keys as fallback)

## [2.4.1] – 2026-04-06

### Fixed
- Manual backlight-off persists during playback

## [2.4.0] – 2026-04-04

### Changed
- AP mode fully migrated to `nmcli`
- Various bugfixes and code audit

## [2.3.10] – 2026-04-04

### Fixed
- Sync no longer interrupts active playback

## [2.3.9] – 2026-04-04

### Improved
- Multiroom sync with verify loop

## [2.3.8] – 2026-04-04

### Improved
- Known boxes displayed immediately
- Subnet scan only on explicit click

## [2.3.7] – 2026-04-04

### Improved
- All-boxes update runs asynchronously
- Check-then-update logic

## [2.3.6] – 2026-04-04

### Fixed
- All-boxes update: own box updated last
- GitHub token forwarded to other boxes

## [2.3.5] – 2026-04-04

### Fixed
- Multiroom sync: timeout handling and parallel join

## [2.3.4] – 2026-04-04

### Improved
- LCD: German umlauts corrected (custom chars)
- LCD: Play/pause custom characters added

## [2.3.3] – 2026-04-04

### Added
- Dashboard integrated into main page
- Quick-play function
- Box cache for faster display

### Fixed
- Various sync fixes

## [2.3.2] – 2026-04-04

### Added
- GitHub token support for private repos in git update

## [2.3.0] – 2026-04-04

### Added
- Git-based update system
- Always-online mode
- Extended LCD status display

### Improved
- RAM optimization (hardware daemon consolidated)

## [2.2.0] – 2026-03-30

### Added
- Initial release
- RFID card control (MIFARE Classic, Ultralight, NTAG)
- Flask web interface with player, RFID management, sync, WiFi, Bluetooth
- I2C LCD display (20x4) with scroll and custom characters
- GPIO buttons (volume, track, pause, backlight)
- NAS sync via SMB/CIFS
- Multi-room support
- WiFi manager with access point fallback
- Bluetooth audio support
- Alarm function
- Standby manager (deep standby)
- Installer for Raspberry Pi Zero 2W
