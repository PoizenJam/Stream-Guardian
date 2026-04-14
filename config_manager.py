"""
Configuration manager for Bitrate Guardian.
Handles persistent settings, preset save/load, and defaults.
"""
import json
import os
import copy
from pathlib import Path

# === App identity — change these to rename/version the app ===
APP_NAME = "Stream Guardian"           # Display name (window title, tray, webhooks)
APP_INTERNAL_NAME = "StreamGuardian"   # Folder/exe name (no spaces, used for paths)
APP_VERSION = "1.0.0"

DEFAULT_CONFIG = {
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
        "enabled": False,
        "hotkey": "Ctrl+Shift+F12",
    },
}


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
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    def load(self):
        if self.config_path.exists():
            try:
                with open(self.config_path, "r") as f:
                    saved = json.load(f)
                self._merge(self.config, saved)
            except (json.JSONDecodeError, IOError):
                pass

    def save(self):
        with open(self.config_path, "w") as f:
            json.dump(self.config, f, indent=2)

    def get(self, *keys):
        val = self.config
        for k in keys:
            val = val[k]
        return val

    def set(self, *keys_and_value):
        keys = keys_and_value[:-1]
        value = keys_and_value[-1]
        d = self.config
        for k in keys[:-1]:
            d = d[k]
        d[keys[-1]] = value
        self.save()

    def reset(self):
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.save()

    # --- Presets ---
    def save_preset(self, name: str):
        path = self.presets_dir / f"{name}.json"
        with open(path, "w") as f:
            json.dump(self.config, f, indent=2)

    def load_preset(self, name: str) -> bool:
        path = self.presets_dir / f"{name}.json"
        if not path.exists():
            return False
        try:
            with open(path, "r") as f:
                preset = json.load(f)
            self.config = copy.deepcopy(DEFAULT_CONFIG)
            self._merge(self.config, preset)
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
            import shutil
            shutil.copy2(src, dest_path)

    def import_preset(self, src_path: str, name: str = None):
        src = Path(src_path)
        if name is None:
            name = src.stem
        dest = self.presets_dir / f"{name}.json"
        import shutil
        shutil.copy2(src, dest)

    @staticmethod
    def _merge(base: dict, overlay: dict):
        for k, v in overlay.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                ConfigManager._merge(base[k], v)
            elif k in base:
                base[k] = v
