"""
Configuration manager for Stream Guardian.
Handles persistent settings, preset save/load, and defaults.

Config format v2 (Stream Guardian 2.0): several monitored streams. Each entry
in "streams" has its own ingest settings ("srs"), thresholds and protection
actions (the per-stream keys of "scenes"); OBS, notifications, the scene
whitelist, transitions and the UI stay global. StreamConfig gives one stream a
get()/set() view with the same call style as ConfigManager, so the engine and
ingest poller read their settings unchanged. A v1 config or preset (flat
"srs"/"thresholds"/"scenes") is migrated to one stream named "Main".
ConfigManager.get()/set() on a per-stream key resolve to the first stream.

Saves are atomic (write temp file, fsync, os.replace) and the previous good
file is kept as config.json.bak. A config that fails to parse is moved aside
as config.corrupt-<timestamp>.json instead of being silently overwritten
with defaults on the next save.
"""
import json
import os
import copy
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# === App identity — change these to rename/version the app ===
APP_NAME = "Stream Guardian"           # Display name (window title, tray, webhooks)
APP_INTERNAL_NAME = "StreamGuardian"   # Folder/exe name (no spaces, used for paths)
APP_VERSION = "2.0.1"
CONFIG_VERSION = 2

# Flat (v1-style) defaults; DEFAULT_CONFIG / DEFAULT_STREAM are derived below.
_FLAT_DEFAULTS = {
    "srs": {
        "backend": "oryx",             # "oryx", "mediamtx", or "generic"
        "host": "localhost",
        "port": 2022,
        "use_ssl": False,
        "api_path": "/api/v1/streams/",
        "auth_token": "",
        "stream_app": "live",
        "stream_name": "",
        "poll_interval_ms": 1000,
        "use_raw_bitrate": True,
        "raw_bitrate_avg_window": 3,
        "generic_bitrate_key": "",      # dot-path into JSON for generic mode
        # MediaMTX only: also read SRT link stats (packet loss, RTT) for the
        # connected publisher. One extra API request per poll.
        "link_stats_enabled": True,
    },
    "obs": {
        "host": "localhost",
        "port": 4455,
        "password": "",
        "reconnect_interval_s": 5,
    },
    "thresholds": {
        "low_bitrate_kbps": 1000,
        "disconnect_kbps": 100,
        "averaging_window_s": 5,
        "grace_period_s": 3,
        "recovery_delay_s": 5,
        "cooldown_s": 10,
        # --- False-positive mitigation (adaptive bitrate / static-scene aware) ---
        # When True, the DISCONNECTED state is only triggered when the ingest
        # server reports the stream is actually offline (no path / no data
        # arriving), instead of relying on the bitrate threshold alone.
        # This eliminates the most common false positive: an adaptive encoder
        # (e.g. Moblin SRT) legitimately dropping bitrate on a static/dark
        # scene. Works with Oryx, MediaMTX, and generic backends — all of them
        # emit stream_online(False, "") when the publisher is gone.
        "trust_ingest_offline_for_disconnect": True,
        # If average bitrate is at or above this floor AND the stream is
        # "stable" (low coefficient of variation, see below), suppress the
        # LOW_BITRATE trigger. Set to 0 to disable. A typical value is the
        # encoder's expected static-scene minimum (e.g. 500–800 kbps for a
        # 3000 kbps target).
        "adaptive_floor_kbps": 0,
        # Coefficient of variation (std/avg) below which the stream is
        # considered "stable adaptive" rather than "network-troubled".
        # 0.25 means std must be under 25% of the mean. Lower = stricter.
        "stability_cv_threshold": 0.25,
        # --- SRT link quality triggers (MediaMTX + SRT publisher only) ---
        # Enter LOW_BITRATE when packet loss over the averaging window exceeds
        # this percentage, or the smoothed round-trip time exceeds this many
        # milliseconds. 0 disables each check.
        "max_packet_loss_pct": 0.0,
        "max_rtt_ms": 0,
    },
    "scenes": {
        "low_bitrate_scene": "Low Bitrate",
        "disconnect_scene": "Be Right Back",
        "whitelist": [],
        "use_custom_transition": False,
        "transition_name": "Fade",
        "transition_duration_ms": 300,
        # Source visibility mode (independent per state)
        "low_bitrate_mode": "scene",        # "scene" or "source"
        "low_bitrate_source_scene": "",      # scene containing the source
        "low_bitrate_source_name": "",       # source to toggle
        "low_bitrate_source_action": "hide", # "hide" or "show"
        "disconnect_mode": "scene",          # "scene" or "source"
        "disconnect_source_scene": "",
        "disconnect_source_name": "",
        "disconnect_source_action": "hide",
        # Per-scene targets: [{"when_on": scene, "low": scene, "disconnect": scene}].
        # When the streamer is on "when_on", these replace the stream's normal
        # scene-mode targets (blank = use the normal one).
        "scene_overrides": [],
    },
    "graph": {
        "history_seconds": 120,
        "show_average_line": True,
        "show_threshold_lines": True,
    },
    "notifications": {
        "audio_alerts": False,
        "audio_volume": 80,
        "audio_low_bitrate": "",
        "audio_disconnect": "",
        "audio_recovery": "",
        "webhook_enabled": False,
        "webhook_url": "",
        "webhook_low_bitrate_msg": "⚠️ Stream bitrate dropped below threshold ({bitrate} kbps avg)",
        "webhook_disconnect_msg": "🔴 Stream disconnected / critically low bitrate ({bitrate} kbps avg)",
        "webhook_recovery_msg": "✅ Stream recovered ({bitrate} kbps avg)",
    },
    "logging": {
        "enabled": False,
        "directory": "",
        "interval_s": 1,
    },
    "gui": {
        "theme": "dark",
        "start_minimized": False,
        "minimize_to_tray": True,
        "always_on_top": False,
    },
    "override": {
        # The override on/off state is intentionally not stored: auto-switch
        # always starts ON so protection can't stay silently disabled after a
        # restart. Old configs with "enabled" are ignored by _merge.
        "hotkey": "Ctrl+Shift+F12",
    },
    "startup": {
        "start_with_windows": False,   # HKCU Run entry, starts minimized to tray
        "check_updates": True,         # GitHub Releases check at launch (notice only)
    },
    "integrations": {
        # obs-websocket CustomEvent on every state change, for Streamer.bot,
        # Advanced Scene Switcher, browser sources, etc.
        "obs_custom_event": True,
        # Chapter marker in the OBS recording on real state changes
        # (OBS 30.2+, Hybrid MP4 recordings only)
        "record_chapters": False,
    },
    "overlay": {
        "enabled": False,      # local web page for an OBS Browser Source
        "port": 8765,
        "allow_lan": False,    # False = 127.0.0.1 only
    },
}

STREAM_SECTIONS = ("srs", "thresholds")
STREAM_SCENE_KEYS = (
    "low_bitrate_scene", "disconnect_scene",
    "low_bitrate_mode", "low_bitrate_source_scene", "low_bitrate_source_name",
    "low_bitrate_source_action",
    "disconnect_mode", "disconnect_source_scene", "disconnect_source_name",
    "disconnect_source_action",
    "scene_overrides",
)


def is_stream_key(section: str, key: str = None) -> bool:
    if section in STREAM_SECTIONS:
        return True
    return section == "scenes" and key in STREAM_SCENE_KEYS


def _stream_from_flat(flat: dict, name: str = "Main", sid: str = "main") -> dict:
    return {
        "id": sid,
        "name": name,
        "srs": copy.deepcopy(flat.get("srs", {})),
        "thresholds": copy.deepcopy(flat.get("thresholds", {})),
        "scenes": {k: copy.deepcopy(v) for k, v in flat.get("scenes", {}).items()
                   if k in STREAM_SCENE_KEYS},
    }


DEFAULT_STREAM = _stream_from_flat(_FLAT_DEFAULTS)
DEFAULT_CONFIG = {k: copy.deepcopy(v) for k, v in _FLAT_DEFAULTS.items() if k not in STREAM_SECTIONS}
DEFAULT_CONFIG["scenes"] = {k: v for k, v in DEFAULT_CONFIG["scenes"].items()
                            if k not in STREAM_SCENE_KEYS}
DEFAULT_CONFIG["config_version"] = CONFIG_VERSION
DEFAULT_CONFIG["streams"] = [copy.deepcopy(DEFAULT_STREAM)]


def migrate_config(data: dict) -> dict:
    """Bring a loaded config or preset to the current format (in place)."""
    if not isinstance(data, dict):
        return data
    if "streams" not in data:
        # v1: flat srs/thresholds/scenes -> one stream named "Main"
        stream = _stream_from_flat(data)
        data.pop("srs", None)
        data.pop("thresholds", None)
        if isinstance(data.get("scenes"), dict):
            for k in STREAM_SCENE_KEYS:
                data["scenes"].pop(k, None)
        data["streams"] = [stream]
    data["config_version"] = CONFIG_VERSION
    return data


def new_stream_id() -> str:
    return uuid.uuid4().hex[:8]


class StreamConfig:
    """One stream's view of the configuration, with ConfigManager's call style.
    Per-stream keys read and write that stream's entry; everything else goes
    to the global config."""

    def __init__(self, root: "ConfigManager", stream_id: str):
        self.root = root
        self.id = stream_id
        self._last = None  # last seen settings, used after the stream is deleted

    @property
    def data(self) -> dict:
        for s in self.root.config["streams"]:
            if s.get("id") == self.id:
                self._last = s
                return s
        # Deleted (an engine is retiring): keep using this stream's own last
        # settings, never another stream's.
        if self._last is not None:
            return self._last
        return self.root.config["streams"][0]

    @property
    def name(self) -> str:
        return self.data.get("name", "Stream")

    def get(self, *keys):
        if len(keys) >= 2 and is_stream_key(keys[0], keys[1]):
            val = self.data[keys[0]]
            for k in keys[1:]:
                val = val[k]
            return val
        return self.root.get(*keys)

    def set(self, *keys_and_value):
        keys, value = keys_and_value[:-1], keys_and_value[-1]
        if len(keys) >= 2 and is_stream_key(keys[0], keys[1]):
            d = self.data[keys[0]]
            for k in keys[1:-1]:
                d = d[k]
            d[keys[-1]] = value
            self.root.save()
        else:
            self.root.set(*keys_and_value)

    def batch(self):
        return self.root.batch()

    def save(self) -> bool:
        return self.root.save()


class ConfigManager:
    def __init__(self, config_dir: str = None):
        if config_dir is None:
            config_dir = os.path.join(
                os.environ.get("APPDATA", os.path.expanduser("~")),
                APP_INTERNAL_NAME,
            )
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.presets_dir = self.config_dir / "presets"
        self.presets_dir.mkdir(exist_ok=True)
        self.config_path = self.config_dir / "config.json"
        self.backup_path = self.config_dir / "config.json.bak"
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self._batch_depth = 0
        self._dirty = False
        # Human-readable note for the log when load had to recover
        self.load_message = ""
        # Last save failure (empty when the last save worked); shown in the Log tab
        self.save_error = ""
        self.load()

    def load(self):
        if not self.config_path.exists():
            return
        saved = self._read_json(self.config_path)
        if saved is not None:
            was_v1 = "streams" not in saved
            self._apply_loaded(saved)
            if was_v1:
                # Keep the 1.x file for a rollback; config.json.bak gets
                # overwritten by the next save, this one doesn't.
                keep = self.config_dir / "config.v1-backup.json"
                try:
                    if not keep.exists():
                        shutil.copy2(self.config_path, keep)
                except OSError:
                    pass
                self.load_message = (
                    "settings upgraded to the Stream Guardian 2.0 format (one stream named \"Main\"); "
                    "your 1.x settings are kept as config.v1-backup.json"
                )
                self.save()
            return

        # Unreadable: keep the bad file for inspection and fall back to the backup
        stamp = time.strftime("%Y%m%d_%H%M%S")
        corrupt = self.config_dir / f"config.corrupt-{stamp}.json"
        try:
            shutil.move(str(self.config_path), str(corrupt))
        except OSError:
            pass
        backup = self._read_json(self.backup_path) if self.backup_path.exists() else None
        if backup is not None:
            self._apply_loaded(backup)
            self.load_message = f"config.json was unreadable; restored from backup (bad file kept as {corrupt.name})"
        else:
            self.load_message = f"config.json was unreadable and no backup exists; using defaults (bad file kept as {corrupt.name})"
        self.save()

    def _apply_loaded(self, data: dict):
        """Merge a loaded (possibly v1) config onto fresh defaults."""
        data = migrate_config(copy.deepcopy(data))
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        streams = data.pop("streams", None)
        self._merge(self.config, data)
        self.config["config_version"] = CONFIG_VERSION
        self.config["streams"] = self._normalize_streams(streams)

    @staticmethod
    def _normalize_streams(streams) -> list:
        """Each stream merged onto DEFAULT_STREAM; ids and names made unique."""
        out, seen_ids, seen_names = [], set(), set()
        for raw in streams if isinstance(streams, list) else []:
            if not isinstance(raw, dict):
                continue
            s = copy.deepcopy(DEFAULT_STREAM)
            ConfigManager._merge(s, raw)
            sid = str(raw.get("id") or "")
            if not sid or sid in seen_ids:
                sid = new_stream_id()
            s["id"] = sid
            name = str(raw.get("name") or "Stream").strip() or "Stream"
            base, n = name, 2
            while name in seen_names:
                name, n = f"{base} {n}", n + 1
            s["name"] = name
            seen_ids.add(sid)
            seen_names.add(name)
            out.append(s)
        return out or [copy.deepcopy(DEFAULT_STREAM)]

    # --- Streams ---

    def streams(self) -> list:
        return self.config["streams"]

    def stream_view(self, stream_id: str) -> StreamConfig:
        return StreamConfig(self, stream_id)

    def add_stream(self, name: str, copy_from: str = None) -> str:
        """Add a stream (copying another's settings if given). Returns its id."""
        src = None
        if copy_from:
            src = next((s for s in self.streams() if s["id"] == copy_from), None)
        s = copy.deepcopy(src if src else DEFAULT_STREAM)
        s["id"] = new_stream_id()
        s["name"] = name
        self.config["streams"].append(s)
        self.config["streams"] = self._normalize_streams(self.config["streams"])
        self.save()
        return s["id"]

    def remove_stream(self, stream_id: str) -> bool:
        if len(self.streams()) <= 1:
            return False
        self.config["streams"] = [s for s in self.streams() if s["id"] != stream_id]
        self.save()
        return True

    def rename_stream(self, stream_id: str, name: str):
        for s in self.streams():
            if s["id"] == stream_id:
                s["name"] = name
        self.config["streams"] = self._normalize_streams(self.config["streams"])
        self.save()

    @staticmethod
    def _read_json(path: Path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None

    def save(self) -> bool:
        """Never raises: an exception escaping a Qt slot closes the app in
        PyQt6. Failures are recorded in save_error instead."""
        if self._batch_depth:
            self._dirty = True
            return True
        try:
            self._atomic_write(self.config_path, self.config, backup=True)
            self.save_error = ""
            return True
        except OSError as e:
            self.save_error = f"could not save settings to {self.config_path}: {e}"
            return False

    def _atomic_write(self, path: Path, data: dict, backup: bool = False):
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            # ASCII-only (emoji escaped), as in v1.0/v1.1: readable whatever
            # encoding opens it, including an older Stream Guardian.
            json.dump(data, f, indent=2, ensure_ascii=True)
            f.flush()
            os.fsync(f.fileno())
        if backup and path.exists():
            try:
                shutil.copy2(path, self.backup_path)
            except OSError:
                pass
        # On Windows the replace fails while another program (OneDrive,
        # antivirus, an editor) has the file open. Retry for about 2 s, then
        # fall back to overwriting in place (not atomic, but saves the change).
        for _ in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.1)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=True)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    @contextmanager
    def batch(self):
        """Group many set() calls into one save (Apply Settings did ~50 writes)."""
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0 and self._dirty:
                self._dirty = False
                self.save()

    def get(self, *keys):
        if len(keys) >= 2 and is_stream_key(keys[0], keys[1]):
            return StreamConfig(self, self.streams()[0]["id"]).get(*keys)
        val = self.config
        for k in keys:
            val = val[k]
        return val

    def set(self, *keys_and_value):
        keys = keys_and_value[:-1]
        value = keys_and_value[-1]
        if len(keys) >= 2 and is_stream_key(keys[0], keys[1]):
            StreamConfig(self, self.streams()[0]["id"]).set(*keys_and_value)
            return
        d = self.config
        for k in keys[:-1]:
            d = d[k]
        d[keys[-1]] = value
        self.save()

    def reset(self):
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.save()

    # --- Presets ---
    def save_preset(self, name: str) -> bool:
        try:
            self._atomic_write(self.presets_dir / f"{name}.json", self.config)
            return True
        except OSError as e:
            self.save_error = f"could not save preset '{name}': {e}"
            return False

    def load_preset(self, name: str) -> bool:
        path = self.presets_dir / f"{name}.json"
        if not path.exists():
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                preset = json.load(f)
            self._apply_loaded(preset)  # migrates v1 presets too
            self.save()
            return True
        except (json.JSONDecodeError, IOError):
            return False

    def delete_preset(self, name: str):
        path = self.presets_dir / f"{name}.json"
        if path.exists():
            path.unlink()

    def list_presets(self) -> list:
        return sorted(
            p.stem for p in self.presets_dir.glob("*.json")
        )

    def export_preset(self, name: str, dest_path: str):
        src = self.presets_dir / f"{name}.json"
        if src.exists():
            shutil.copy2(src, dest_path)

    def import_preset(self, src_path: str, name: str = None):
        src = Path(src_path)
        if name is None:
            name = src.stem
        dest = self.presets_dir / f"{name}.json"
        shutil.copy2(src, dest)

    @staticmethod
    def _merge(base: dict, overlay: dict):
        for k, v in overlay.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                ConfigManager._merge(base[k], v)
            elif k in base:
                base[k] = v
