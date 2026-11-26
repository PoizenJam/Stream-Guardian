# Stream Guardian

Real-time stream bitrate monitor with automatic OBS scene protection.  
Monitors **Oryx SRS**, **MediaMTX**, or any custom JSON-based ingest server,
and protects your stream when bitrate drops.

[![Build Release](https://github.com/PoizenJam/Stream-Guardian/actions/workflows/release.yml/badge.svg)](https://github.com/PoizenJam/Stream-Guardian/actions/workflows/release.yml)

---

## Quick Install

From the [Releases page](https://github.com/PoizenJam/Stream-Guardian/releases), either:

- **Installer:** `StreamGuardian-X.Y.Z-Setup.exe` — per-user install (no admin prompt), Start menu shortcut, uninstaller. Windows SmartScreen may warn because the app isn't code-signed: choose *More info*, then *Run anyway*.
- **Portable:** `StreamGuardian-X.Y.Z-Windows.zip` — extract and run `StreamGuardian.exe`.

Settings live in `%APPDATA%\StreamGuardian` either way. Upgrading from 1.x converts them automatically (see *Several streams* below).

---

## Features

### Several Streams
- Monitor **any number of streams at once** (e.g. two cameras on one MediaMTX server), each with its own ingest settings, thresholds and protection actions
- A shared **scene arbiter** keeps them from fighting over OBS: the most serious problem is shown (disconnect beats low bitrate), and your scene comes back only when every stream is healthy
- Dashboard stream picker with a coloured status chip per stream; log lines, tray messages, webhooks (`{stream}`), CSV files and session summaries are labelled per stream
- 1.x settings and presets are converted on first load into one stream named "Main"

### Core Monitoring
- **Real-time bitrate graph** — rolling plot with current bitrate, average overlay, and threshold lines
- **Configurable rolling average** over a user-specified window
- **Session statistics** — peak, min, average, std dev, total drops, uptime
- **Session summary** in the Log tab on Reset and on quit: drops, time spent in low bitrate and disconnected, longest outage
- **SRT link quality** (MediaMTX with an SRT publisher) — packet loss and round-trip time on the dashboard, with optional limits that trigger low-bitrate protection

### Stream Protection (independently configurable per state)
- **Scene switching mode** — switch to a designated OBS scene on low bitrate or disconnect
- **Source visibility mode** — toggle specific source visibility within a scene instead of switching
- Each mode (low bitrate, disconnect) is independently configured — mix and match
- Automatic restoration when bitrate recovers (skipped if you changed scenes yourself during the outage)
- Actions that couldn't run (OBS disconnected, whitelisted scene) are retried automatically once possible
- **Per-scene targets** — use a different protection scene depending on the scene you're on (e.g. "Gaming" drops to "Gaming BRB")

### Stability & Reliability
- **Grace period** — wait N seconds below threshold before triggering (avoids false positives)
- **Recovery delay** — require N seconds of stable bitrate before switching back
- **Cooldown timer** — minimum time between auto-switches (prevents rapid flipping); escalation to Disconnected is never delayed
- **Hysteresis** — the three above combine to create robust, jitter-free behavior

### OBS Integration
- Connects via built-in **OBS WebSocket** (OBS 28+)
- **Custom transition override** — use a specific OBS transition (e.g., Fade) for auto-switches; your own transition is put back afterwards
- **Scene whitelist** — scenes exempt from auto-switching (e.g., "Starting Soon", "Ending")
- Auto-reconnect with configurable retry interval
- **Event-driven** — scene changes made in OBS are picked up instantly over an obs-websocket event connection (falls back to 1-second polling if events are unavailable)
- **OBS custom events** — every state change is broadcast as an obs-websocket `CustomEvent` (`{"source": "StreamGuardian", "event": "state_changed", "stream", "state", "previous", "test", "avg_kbps"}`) for Streamer.bot, Advanced Scene Switcher, browser sources
- **Recording chapters** — optional chapter marker in the OBS recording at each drop and recovery (OBS 30.2+, Hybrid MP4)
- **Stream overlay** — a local Browser Source page showing live state and bitrate (`http://127.0.0.1:8765/`, options `?stream=Name&hide_when_normal=1&show_bitrate=0&scale=1.5`)

### Notifications
- **Audio alerts** — customizable sound files per event (low bitrate, disconnect, recovery) with volume control
- **Webhook notifications** — Discord, Slack, or custom webhook with customizable message templates per event
- Uses `{bitrate}` and `{stream}` placeholders in templates
- Test button to verify webhook connectivity

### Operational
- **System tray mode** — minimize to tray for unobtrusive monitoring
- **Manual override hotkey** — configurable system-wide keyboard shortcut to toggle auto-switching (works while OBS or a game has focus on Windows)
- Auto-switching always starts **ON** when the app launches
- **Override toggle** — dashboard button + tray menu + hotkey all control the same state
- **Always on top** mode
- **Rehearse** — Test Low / Test Disconnect buttons run your real protection actions on demand, so you can check your BRB setup before going live (no grace period or cooldown, sounds play, no webhooks, not counted in stats, ends after 2 minutes)
- **Responsive UI** — monitoring and OBS requests run on a background thread, so the window never freezes if OBS stalls
- **Single instance** — launching a second copy shows a warning and brings the running window forward instead
- **Start with Windows** (minimized to the tray) and **always start minimized** options
- **Update notice** — checks GitHub Releases at launch and from *Help > Check for Updates* (never downloads anything itself)

### Configuration
- **Persistent settings** saved between sessions (JSON)
- **Config presets** — save, load, delete, import, and export named presets
- **Light/Dark themes** matching OBS Studio design conventions

### Logging
- **CSV bitrate logging** — timestamp, bitrate, average, state for every sample, plus an event column (state changes, tests) and SRT packet loss / RTT when available
- **Event log** — scrolling in-app log with export capability

---

## Requirements

- **Python 3.10+**
- **OBS Studio 28+** (built-in WebSocket server)
- An ingest server with an HTTP stats API: **Oryx / SRS**, **MediaMTX**, or any JSON endpoint (generic mode)

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
cd stream-guardian
pip install -r requirements.txt
```

### 2. Configure OBS WebSocket

In OBS Studio: **Tools → WebSocket Server Settings** → Enable, note port (default 4455), set password if desired.

### 3. Run

```bash
python main.py
```

### 4. Configure in the Settings tab

1. **Ingest Server** — choose the backend (Oryx, MediaMTX, generic) and set host/port
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

Output: `dist/StreamGuardian` (or `.exe` on Windows)

---

## Architecture

```
stream-guardian/
├── main.py                 # Entry point
├── config_manager.py       # Persistent settings (v2: several streams), migration, presets
├── scene_arbiter.py        # Shared scene switching/restoring across streams
├── overlay_server.py       # Local Browser Source overlay (HTTP)
├── updater.py              # GitHub Releases update notice
├── autostart.py            # Start with Windows (HKCU Run entry)
├── ingest_client.py        # Oryx / MediaMTX / generic API poller + SRT link stats (background thread)
├── obs_client.py           # OBS WebSocket client (requests + events) + source visibility control
├── bitrate_engine.py       # State machine, rehearsal mode, webhooks, CSV log, session summary (own thread)
├── single_instance.py      # Blocks a second copy; brings the running window forward
├── gui/
│   ├── main_window.py      # Main window, menus, tray, hotkey binding
│   ├── dashboard.py        # Real-time graph + stat cards + indicators
│   ├── settings_tab.py     # All config with protection mode widgets
│   ├── presets_tab.py      # Preset save/load/import/export
│   ├── log_tab.py          # Event log viewer
│   ├── global_hotkey.py    # System-wide override hotkey (RegisterHotKey on Windows)
│   └── themes.py           # OBS-style dark/light themes
├── tests/                  # Unit tests (python -m unittest discover -s tests)
├── requirements.txt
├── build.spec              # PyInstaller build config
├── build.bat / build.sh    # Build scripts
├── installer.iss           # Inno Setup installer (built by the release workflow)
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

Each transition executes either a scene switch or source visibility toggle depending on the per-state mode configuration. When moving between LOW_BITRATE and DISCONNECTED, the previous state's action is undone before the new one is applied. Escalating from LOW_BITRATE to DISCONNECTED is immediate and is not held back by the cooldown.

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
| Override Hotkey | Ctrl+Shift+F12 | System-wide shortcut to toggle auto-switching |
| Read SRT link stats | On | MediaMTX only: show packet loss and RTT for an SRT publisher |
| Max Packet Loss | Off | Enter low-bitrate protection above this loss % (SRT) |
| Max Round-Trip Time | Off | Enter low-bitrate protection above this RTT (SRT) |
| OBS custom event | On | Broadcast a CustomEvent on each state change |
| Recording chapters | Off | Chapter marker per drop/recovery (OBS 30.2+, Hybrid MP4) |
| Overlay | Off, port 8765 | Local Browser Source page; LAN access off by default |
| Start with Windows | Off | Start minimized to the tray at sign-in |
| Check for updates | On | Notice only, at launch |

---

## Tips

- **Tune thresholds to your stream.** At 6000 kbps, try low=2000-3000 and disconnect=100-500.
- **Source visibility mode** is great if you want to keep your scene layout and just overlay a "low quality" banner or hide your camera feed.
- **Scene switch mode** is simpler and works best if you have dedicated "BRB" or "Technical Difficulties" scenes.
- **Mix modes**: use source toggle for low bitrate (subtle) and scene switch for disconnect (obvious).
- **Save presets** for different scenarios: "Local LAN", "Remote IRL", "Collab".
- **Webhook templates** support `{bitrate}` placeholder for dynamic messages.
- **Rehearse before you go live** with Test Low / Test Disconnect on the dashboard.
- **SRT limits**: watch the SRT Link card on a good day first, then set Max Packet Loss a few points above your normal level.

---

## Building from Source

```bash
git clone https://github.com/PoizenJam/Stream-Guardian.git
cd stream-guardian
pip install -r requirements.txt
pip install pyinstaller
pyinstaller build.spec --noconfirm
```

The executable will be in `dist/StreamGuardian.exe`.

## Release Process

Unit tests run on every push to `main` and on pull requests (Windows, Python 3.12 and 3.14). Releases are built and published automatically by GitHub Actions when a version tag is pushed. The workflow runs the tests, then produces an installer, a portable Windows EXE zip and a source zip, and creates a draft release for review.

1. Bump `APP_VERSION` in `config_manager.py` (e.g. `1.2.0` → `1.2.1`)
2. Commit and push the version bump
3. Tag the release:
   ```bash
   git tag v1.2.1
   git push origin v1.2.1
   ```
4. GitHub Actions will:
   - Build `StreamGuardian-1.2.1-Windows.zip` and `StreamGuardian-1.2.1-Setup.exe` on a Windows runner with the embedded icon
   - Package `StreamGuardian-1.2.1-Source.zip` with the source files
   - Create a **draft** GitHub Release with all three files attached and auto-generated release notes
5. Review the draft release on GitHub, edit notes if needed, and publish

---

## License

MIT
