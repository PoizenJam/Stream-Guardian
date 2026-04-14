# Stream Guardian

Real-time stream bitrate monitor with automatic OBS scene protection.  
Monitors **Oryx SRS**, **MediaMTX**, or any custom JSON-based ingest server,
and protects your stream when bitrate drops.

[![Build Release](https://github.com/PoizenJam/stream-guardian/actions/workflows/release.yml/badge.svg)](https://github.com/PoizenJam/stream-guardian/actions/workflows/release.yml)

---

## Quick Install

Download the latest `StreamGuardian-X.Y.Z-Windows.zip` from the [Releases page](https://github.com/PoizenJam/stream-guardian/releases), extract, and run `StreamGuardian.exe`. No installation required.

---

## Features

### Core Monitoring
- **Real-time bitrate graph** — rolling plot with current bitrate, average overlay, and threshold lines
- **Configurable rolling average** over a user-specified window
- **Session statistics** — peak, min, average, std dev, total drops, uptime

### Stream Protection (independently configurable per state)
- **Scene switching mode** — switch to a designated OBS scene on low bitrate or disconnect
- **Source visibility mode** — toggle specific source visibility within a scene instead of switching
- Each mode (low bitrate, disconnect) is independently configured — mix and match
- Automatic restoration when bitrate recovers

### Stability & Reliability
- **Grace period** — wait N seconds below threshold before triggering (avoids false positives)
- **Recovery delay** — require N seconds of stable bitrate before switching back
- **Cooldown timer** — minimum time between any two auto-switches (prevents rapid flipping)
- **Hysteresis** — the three above combine to create robust, jitter-free behavior

### OBS Integration
- Connects via built-in **OBS WebSocket** (OBS 28+)
- **Custom transition override** — use a specific OBS transition (e.g., Fade) for auto-switches
- **Scene whitelist** — scenes exempt from auto-switching (e.g., "Starting Soon", "Ending")
- Auto-reconnect with configurable retry interval

### Notifications
- **Audio alerts** — customizable sound files per event (low bitrate, disconnect, recovery) with volume control
- **Webhook notifications** — Discord, Slack, or custom webhook with customizable message templates per event
- Uses `{bitrate}` placeholder in templates for dynamic values
- Test button to verify webhook connectivity

### Operational
- **System tray mode** — minimize to tray for unobtrusive monitoring
- **Manual override hotkey** — configurable keyboard shortcut to toggle auto-switching
- **Override toggle** — dashboard button + tray menu + hotkey all control the same state
- **Always on top** mode

### Configuration
- **Persistent settings** saved between sessions (JSON)
- **Config presets** — save, load, delete, import, and export named presets
- **Light/Dark themes** matching OBS Studio design conventions

### Logging
- **CSV bitrate logging** — timestamp, bitrate, average, state for every sample
- **Event log** — scrolling in-app log with export capability

---

## Requirements

- **Python 3.10+**
- **OBS Studio 28+** (built-in WebSocket server)
- **Oryx SRS** media server (or any SRS-based server with the stats API)

### Python Dependencies

```
PyQt6>=6.5
pyqtgraph>=0.13
obsws-python>=1.7
requests>=2.28
numpy>=1.24
```

---

## Quick Start

### 1. Install

```bash
cd bitrate-guardian
pip install -r requirements.txt
```

### 2. Configure OBS WebSocket

In OBS Studio: **Tools → WebSocket Server Settings** → Enable, note port (default 4455), set password if desired.

### 3. Run

```bash
python main.py
```

### 4. Configure in the Settings tab

1. **SRS Connection** — set your Oryx SRS host/port
2. **OBS Connection** — match your OBS WebSocket settings
3. **Thresholds** — set low bitrate and disconnect thresholds
4. **Low Bitrate Protection** — choose Scene Switch or Source Visibility Toggle, configure target
5. **Disconnect Protection** — same, independently configured
6. Click **Apply Settings**

---

## Protection Modes

Each protection state (low bitrate and disconnect) can be independently configured to use one of two modes:

### Scene Switch Mode (default)
Switches OBS to a designated scene (e.g., "Low Bitrate" or "Be Right Back"). Restores the original scene on recovery.

### Source Visibility Toggle Mode
Instead of switching scenes, toggles a specific source's visibility within a scene. For example:
- **Low bitrate**: Show a "Low Quality" overlay on your main scene
- **Disconnect**: Hide the camera source and show a "BRB" image

To configure: select the scene containing the source, click **Fetch Sources** to populate the dropdown, select the source, and choose the action (show or hide). The original visibility is saved and restored automatically on recovery.

---

## Building a Distributable Executable

```bash
# Windows
build.bat

# Linux / macOS
chmod +x build.sh && ./build.sh
```

Output: `dist/BitrateGuardian` (or `.exe` on Windows)

---

## Architecture

```
bitrate-guardian/
├── main.py                 # Entry point
├── config_manager.py       # Persistent settings + preset management
├── srs_client.py           # Oryx SRS API poller (background thread)
├── obs_client.py           # OBS WebSocket client + source visibility control
├── bitrate_engine.py       # State machine, audio alerts, webhook dispatch
├── gui/
│   ├── main_window.py      # Main window, menus, tray, hotkey binding
│   ├── dashboard.py        # Real-time graph + stat cards + indicators
│   ├── settings_tab.py     # All config with protection mode widgets
│   ├── presets_tab.py      # Preset save/load/import/export
│   ├── log_tab.py          # Event log viewer
│   └── themes.py           # OBS-style dark/light themes
├── requirements.txt
├── build.spec              # PyInstaller build config
├── build.bat / build.sh    # Build scripts
```

### State Machine

```
                    ┌───────────────────────┐
                    │       NORMAL          │
                    │  (live scene/source)  │
                    └──────┬────────┬───────┘
           avg < low_thresh│        │avg < disc_thresh
           (after grace)   │        │(after grace)
                    ┌──────▼──┐  ┌──▼────────────┐
                    │   LOW   │──│  DISCONNECTED  │
                    │ BITRATE │  │                │
                    └──┬──────┘  └──────┬─────────┘
                       │                │
           (after      │                │(after
            recovery)  │                │ recovery)
                    ┌──▼────────────────▼───┐
                    │       NORMAL          │
                    │ (restored original)   │
                    └───────────────────────┘
```

Each transition executes either a scene switch or source visibility toggle depending on the per-state mode configuration. When escalating from LOW_BITRATE → DISCONNECTED, the low-bitrate action is undone before the disconnect action is applied.

---

## Key Settings

| Setting | Default | Description |
|---------|---------|-------------|
| Low Bitrate Threshold | 1000 kbps | Trigger low-bitrate protection below this |
| Disconnect Threshold | 100 kbps | Trigger disconnect protection below this |
| Averaging Window | 5 s | Rolling window for average calculation |
| Grace Period | 3 s | Seconds below threshold before triggering |
| Recovery Delay | 5 s | Stable seconds required before restoring |
| Cooldown | 10 s | Minimum seconds between any auto-actions |
| Override Hotkey | Ctrl+Shift+F12 | Global shortcut to toggle auto-switching |

---

## Tips

- **Tune thresholds to your stream.** At 6000 kbps, try low=2000-3000 and disconnect=100-500.
- **Source visibility mode** is great if you want to keep your scene layout and just overlay a "low quality" banner or hide your camera feed.
- **Scene switch mode** is simpler and works best if you have dedicated "BRB" or "Technical Difficulties" scenes.
- **Mix modes**: use source toggle for low bitrate (subtle) and scene switch for disconnect (obvious).
- **Save presets** for different scenarios: "Local LAN", "Remote IRL", "Collab".
- **Webhook templates** support `{bitrate}` placeholder for dynamic messages.

---

## Building from Source

```bash
git clone https://github.com/PoizenJam/stream-guardian.git
cd stream-guardian
pip install -r requirements.txt
pip install pyinstaller
pyinstaller build.spec --noconfirm
```

The executable will be in `dist/StreamGuardian.exe`.

## Release Process

Releases are built and published automatically by GitHub Actions when a version tag is pushed. The workflow produces both a Windows EXE zip and a source zip, and creates a draft release for review.

1. Bump `APP_VERSION` in `config_manager.py` (e.g. `1.2.0` → `1.2.1`)
2. Commit and push the version bump
3. Tag the release:
   ```bash
   git tag v1.2.1
   git push origin v1.2.1
   ```
4. GitHub Actions will:
   - Build `StreamGuardian-1.2.1-Windows.zip` on a Windows runner with the embedded icon
   - Package `StreamGuardian-1.2.1-Source.zip` with the source files
   - Create a **draft** GitHub Release with both zips attached and auto-generated release notes
5. Review the draft release on GitHub, edit notes if needed, and publish

---

## License

MIT
