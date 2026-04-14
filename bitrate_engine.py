"""
Bitrate tracking engine.
Maintains rolling window, computes averages, manages threshold state machine,
and triggers scene switches / source visibility toggles via OBS client.
Includes audio alert playback and webhook notification dispatch.
"""
import time
import csv
import os
import json
import threading
from collections import deque
from enum import Enum, auto
from pathlib import Path
from datetime import datetime

import numpy as np
import requests as http_requests

from PyQt6.QtCore import QObject, pyqtSignal, QUrl
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

from config_manager import APP_NAME, APP_INTERNAL_NAME


class StreamState(Enum):
    NORMAL = auto()
    LOW_BITRATE = auto()
    DISCONNECTED = auto()


class BitrateEngine(QObject):
    """Core bitrate analysis, scene-switching / source toggling, alerts."""

    state_changed = pyqtSignal(str, str)  # new_state, old_state
    stats_updated = pyqtSignal(dict)  # {current, average, min, max, std, state, ...}
    scene_switch_triggered = pyqtSignal(str, str)  # new_scene, reason
    source_toggle_triggered = pyqtSignal(str, str, bool, str)  # scene, source, visible, reason
    log_entry = pyqtSignal(str)  # log message

    def __init__(self, config_manager, obs_client, parent=None):
        super().__init__(parent)
        self.cfg = config_manager
        self.obs = obs_client

        # Rolling data
        self._history = deque()  # (timestamp, bitrate_kbps)
        self._graph_history = deque()  # full graph history
        self._max_graph_seconds = 300

        # State machine
        self._state = StreamState.NORMAL
        self._saved_scene = ""
        self._state_enter_time = 0.0
        self._last_switch_time = 0.0
        self._grace_start = 0.0
        self._in_grace = False
        self._recovery_start = 0.0
        self._in_recovery = False

        # Source visibility restore tracking
        # Stores {state_name: (scene, source, original_visibility)} for undo
        self._source_restore = {}

        # Session stats
        self._session_start = time.time()
        self._total_samples = 0
        self._total_bitrate = 0.0
        self._peak_bitrate = 0.0
        self._min_bitrate = float("inf")
        self._drop_count = 0

        # Logging
        self._log_file = None
        self._csv_writer = None

        # Audio
        self._audio_player = None
        self._audio_output = None
        self._init_audio()

    # --- Audio setup ---

    def _init_audio(self):
        try:
            self._audio_player = QMediaPlayer()
            self._audio_output = QAudioOutput()
            self._audio_player.setAudioOutput(self._audio_output)
        except Exception:
            self._audio_player = None
            self._audio_output = None

    def _play_audio(self, file_path: str):
        """Play an audio file for alert notification."""
        if not self._audio_player or not file_path:
            return
        if not os.path.isfile(file_path):
            return
        try:
            vol = self.cfg.get("notifications", "audio_volume") / 100.0
            self._audio_output.setVolume(vol)
            self._audio_player.setSource(QUrl.fromLocalFile(file_path))
            self._audio_player.play()
        except Exception as e:
            self.log_entry.emit(f"Audio playback error: {e}")

    # --- Webhook ---

    def _send_webhook(self, message: str):
        """Send a Discord/generic webhook notification in a background thread."""
        url = self.cfg.get("notifications", "webhook_url")
        if not url:
            return

        def _post():
            try:
                # Discord webhook format
                if "discord.com/api/webhooks" in url:
                    payload = {"content": message, "username": APP_NAME}
                else:
                    # Generic webhook (Slack-compatible)
                    payload = {
                        "text": message,
                        "source": APP_NAME,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    }
                resp = http_requests.post(
                    url, json=payload, timeout=10,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code >= 400:
                    self.log_entry.emit(f"Webhook HTTP {resp.status_code}")
            except Exception as e:
                self.log_entry.emit(f"Webhook error: {e}")

        threading.Thread(target=_post, daemon=True).start()

    def _fire_notifications(self, new_state: StreamState, avg_kbps: float):
        """Dispatch audio and webhook notifications for a state transition."""
        audio_enabled = self.cfg.get("notifications", "audio_alerts")
        webhook_enabled = self.cfg.get("notifications", "webhook_enabled")

        bitrate_str = f"{avg_kbps:.0f}"

        if new_state == StreamState.LOW_BITRATE:
            if audio_enabled:
                self._play_audio(self.cfg.get("notifications", "audio_low_bitrate"))
            if webhook_enabled:
                msg = self.cfg.get("notifications", "webhook_low_bitrate_msg")
                self._send_webhook(msg.replace("{bitrate}", bitrate_str))

        elif new_state == StreamState.DISCONNECTED:
            if audio_enabled:
                self._play_audio(self.cfg.get("notifications", "audio_disconnect"))
            if webhook_enabled:
                msg = self.cfg.get("notifications", "webhook_disconnect_msg")
                self._send_webhook(msg.replace("{bitrate}", bitrate_str))

        elif new_state == StreamState.NORMAL:
            if audio_enabled:
                self._play_audio(self.cfg.get("notifications", "audio_recovery"))
            if webhook_enabled:
                msg = self.cfg.get("notifications", "webhook_recovery_msg")
                self._send_webhook(msg.replace("{bitrate}", bitrate_str))

    # --- Properties ---

    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def graph_data(self):
        return list(self._graph_history)

    # --- Main processing ---

    def process_bitrate(self, total_kbps: float, video_kbps: float, audio_kbps: float):
        """Process a new bitrate sample. Called on every SRS poll."""
        now = time.time()

        # Update history
        self._history.append((now, total_kbps))
        self._graph_history.append((now, total_kbps))

        # Trim history to averaging window
        window_s = self.cfg.get("thresholds", "averaging_window_s")
        cutoff = now - window_s
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

        # Trim graph history
        graph_cutoff = now - self._max_graph_seconds
        while self._graph_history and self._graph_history[0][0] < graph_cutoff:
            self._graph_history.popleft()

        # Compute stats
        if self._history:
            values = [v for _, v in self._history]
            avg = sum(values) / len(values)
            std = float(np.std(values)) if len(values) > 1 else 0.0
        else:
            avg = 0.0
            std = 0.0

        # Session stats
        self._total_samples += 1
        self._total_bitrate += total_kbps
        self._peak_bitrate = max(self._peak_bitrate, total_kbps)
        if total_kbps > 0:
            self._min_bitrate = min(self._min_bitrate, total_kbps)

        session_avg = self._total_bitrate / self._total_samples if self._total_samples else 0

        stats = {
            "current_kbps": total_kbps,
            "video_kbps": video_kbps,
            "audio_kbps": audio_kbps,
            "average_kbps": avg,
            "std_kbps": std,
            "state": self._state.name,
            "session_avg_kbps": session_avg,
            "session_peak_kbps": self._peak_bitrate,
            "session_min_kbps": self._min_bitrate if self._min_bitrate != float("inf") else 0,
            "session_drops": self._drop_count,
            "uptime_s": now - self._session_start,
            "samples_in_window": len(self._history),
        }
        self.stats_updated.emit(stats)

        # Run state machine if override is not active
        if not self.cfg.get("override", "enabled"):
            self._evaluate_thresholds(avg, now)

        # Log
        self._write_log(now, total_kbps, avg, self._state.name)

    def _evaluate_thresholds(self, avg_kbps: float, now: float):
        low_thresh = self.cfg.get("thresholds", "low_bitrate_kbps")
        disc_thresh = self.cfg.get("thresholds", "disconnect_kbps")
        grace_s = self.cfg.get("thresholds", "grace_period_s")
        recovery_s = self.cfg.get("thresholds", "recovery_delay_s")

        # Determine target state from average bitrate
        if avg_kbps < disc_thresh:
            target = StreamState.DISCONNECTED
        elif avg_kbps < low_thresh:
            target = StreamState.LOW_BITRATE
        else:
            target = StreamState.NORMAL

        # --- Transition logic ---

        if self._state == StreamState.NORMAL:
            if target in (StreamState.LOW_BITRATE, StreamState.DISCONNECTED):
                if not self._in_grace:
                    self._in_grace = True
                    self._grace_start = now
                    return
                if now - self._grace_start < grace_s:
                    return
                self._in_grace = False
                self._transition_to(target, now, avg_kbps)
            else:
                self._in_grace = False

        elif self._state == StreamState.LOW_BITRATE:
            if target == StreamState.DISCONNECTED:
                self._in_grace = False
                self._in_recovery = False
                self._transition_to(StreamState.DISCONNECTED, now, avg_kbps)
            elif target == StreamState.NORMAL:
                if not self._in_recovery:
                    self._in_recovery = True
                    self._recovery_start = now
                    return
                if now - self._recovery_start < recovery_s:
                    return
                self._in_recovery = False
                self._transition_to(StreamState.NORMAL, now, avg_kbps)
            else:
                self._in_recovery = False

        elif self._state == StreamState.DISCONNECTED:
            if target == StreamState.NORMAL:
                if not self._in_recovery:
                    self._in_recovery = True
                    self._recovery_start = now
                    return
                if now - self._recovery_start < recovery_s:
                    return
                self._in_recovery = False
                self._transition_to(StreamState.NORMAL, now, avg_kbps)
            elif target == StreamState.LOW_BITRATE:
                if not self._in_recovery:
                    self._in_recovery = True
                    self._recovery_start = now
                    return
                if now - self._recovery_start < recovery_s:
                    return
                self._in_recovery = False
                self._transition_to(StreamState.LOW_BITRATE, now, avg_kbps)
            else:
                self._in_recovery = False

    def _transition_to(self, new_state: StreamState, now: float, avg_kbps: float):
        cooldown_s = self.cfg.get("thresholds", "cooldown_s")
        time_since_last = now - self._last_switch_time

        if time_since_last < cooldown_s:
            self.log_entry.emit(
                f"COOLDOWN: transition to {new_state.name} blocked "
                f"({time_since_last:.1f}s < {cooldown_s}s cooldown)"
            )
            return

        old_state = self._state
        current_scene = self.obs.current_scene

        self.log_entry.emit(
            f"TRANSITION: {old_state.name} → {new_state.name}, "
            f"scene='{current_scene}', avg={avg_kbps:.0f} kbps"
        )

        # Check scene whitelist
        whitelist = self.cfg.get("scenes", "whitelist")
        if current_scene in whitelist and old_state == StreamState.NORMAL:
            self.log_entry.emit(
                f"Scene '{current_scene}' is whitelisted, skipping auto-switch"
            )
            return

        # --- Execute action based on mode (scene switch or source visibility) ---

        if new_state == StreamState.NORMAL:
            # Restore: undo whatever was done
            self._restore_from_state(old_state)
        elif new_state == StreamState.LOW_BITRATE:
            if old_state == StreamState.NORMAL:
                self._saved_scene = current_scene
            self._drop_count += 1
            self._execute_protection("low_bitrate", now)
        elif new_state == StreamState.DISCONNECTED:
            if old_state == StreamState.NORMAL:
                self._saved_scene = current_scene
            if old_state != StreamState.LOW_BITRATE:
                self._drop_count += 1
            # If escalating from LOW_BITRATE, undo low_bitrate source toggle first
            if old_state == StreamState.LOW_BITRATE:
                self._undo_source_toggle("LOW_BITRATE")
            self._execute_protection("disconnect", now)

        # Fire notifications
        self._fire_notifications(new_state, avg_kbps)

        self._state = new_state
        self._state_enter_time = now
        self._last_switch_time = now
        self.state_changed.emit(new_state.name, old_state.name)

    def _execute_protection(self, state_key: str, now: float):
        """Execute either scene switch or source visibility toggle for a given state."""
        mode = self.cfg.get("scenes", f"{state_key}_mode")

        if mode == "source":
            self._execute_source_toggle(state_key)
        else:
            self._execute_scene_switch(state_key, now)

    def _execute_scene_switch(self, state_key: str, now: float):
        """Switch to the configured scene for this state."""
        target_scene = self.cfg.get("scenes", f"{state_key}_scene")
        current_scene = self.obs.current_scene
        reason = "Low bitrate" if state_key == "low_bitrate" else "Disconnect"

        self.log_entry.emit(
            f"SWITCH: state_key={state_key}, target='{target_scene}', "
            f"current='{current_scene}', obs_connected={self.obs.is_connected}"
        )

        if not target_scene:
            self.log_entry.emit(f"SWITCH SKIPPED: no target scene configured for {state_key}")
            return

        if target_scene == current_scene:
            self.log_entry.emit(f"SWITCH SKIPPED: already on '{target_scene}'")
            return

        success = self.obs.switch_scene(target_scene)
        if success:
            self.scene_switch_triggered.emit(target_scene, reason)
            self.log_entry.emit(f"Scene → '{target_scene}': {reason}")
        else:
            self.log_entry.emit(f"SWITCH FAILED: obs.switch_scene('{target_scene}') returned False")

    def _execute_source_toggle(self, state_key: str):
        """Toggle a source's visibility for the given state."""
        scene = self.cfg.get("scenes", f"{state_key}_source_scene")
        source = self.cfg.get("scenes", f"{state_key}_source_name")
        action = self.cfg.get("scenes", f"{state_key}_source_action")

        if not scene or not source:
            self.log_entry.emit(f"Source visibility not configured for {state_key}")
            return

        # Save current state for restore
        current_vis = self.obs.get_source_visibility(scene, source)
        state_name = "LOW_BITRATE" if state_key == "low_bitrate" else "DISCONNECTED"
        self._source_restore[state_name] = (scene, source, current_vis)

        target_vis = (action == "show")
        success = self.obs.set_source_visibility(scene, source, target_vis)
        action_word = "Show" if target_vis else "Hide"
        reason = f"{action_word} '{source}' in '{scene}'"

        if success:
            self.source_toggle_triggered.emit(scene, source, target_vis, state_name)
            self.log_entry.emit(f"Source toggle: {reason}")
        else:
            self.log_entry.emit(f"Failed: {reason}")

    def _restore_from_state(self, old_state: StreamState):
        """Restore to normal: undo scene switch or source toggle."""
        # Undo source toggles
        self._undo_source_toggle(old_state.name)

        # Restore saved scene (only if scene mode was used)
        old_key = "low_bitrate" if old_state == StreamState.LOW_BITRATE else "disconnect"
        mode = self.cfg.get("scenes", f"{old_key}_mode")

        if mode == "scene" and self._saved_scene:
            current_scene = self.obs.current_scene
            if current_scene != self._saved_scene:
                success = self.obs.switch_scene(self._saved_scene)
                if success:
                    self.scene_switch_triggered.emit(self._saved_scene, "Bitrate recovered")
                    self.log_entry.emit(f"Scene restored → '{self._saved_scene}'")
            self._saved_scene = ""

    def _undo_source_toggle(self, state_name: str):
        """Restore a source to its original visibility for the given state."""
        if state_name in self._source_restore:
            scene, source, original_vis = self._source_restore.pop(state_name)
            if original_vis is not None:
                self.obs.set_source_visibility(scene, source, original_vis)
                self.log_entry.emit(
                    f"Source restored: '{source}' in '{scene}' → "
                    f"{'visible' if original_vis else 'hidden'}"
                )

    # --- Logging ---

    def start_logging(self):
        log_dir = self.cfg.get("logging", "directory")
        if not log_dir:
            log_dir = str(Path.home() / APP_INTERNAL_NAME / "logs")
        Path(log_dir).mkdir(parents=True, exist_ok=True)

        filename = f"bitrate_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filepath = os.path.join(log_dir, filename)
        self._log_file = open(filepath, "w", newline="")
        self._csv_writer = csv.writer(self._log_file)
        self._csv_writer.writerow(["timestamp", "bitrate_kbps", "avg_kbps", "state"])
        self.log_entry.emit(f"Logging started: {filepath}")

    def stop_logging(self):
        if self._log_file:
            self._log_file.close()
            self._log_file = None
            self._csv_writer = None
            self.log_entry.emit("Logging stopped")

    def _write_log(self, ts: float, bitrate: float, avg: float, state: str):
        if self._csv_writer:
            dt = datetime.fromtimestamp(ts).isoformat()
            self._csv_writer.writerow([dt, f"{bitrate:.1f}", f"{avg:.1f}", state])
            self._log_file.flush()

    def reset_session(self):
        self._session_start = time.time()
        self._total_samples = 0
        self._total_bitrate = 0.0
        self._peak_bitrate = 0.0
        self._min_bitrate = float("inf")
        self._drop_count = 0
        self._history.clear()
        self._graph_history.clear()
        self._state = StreamState.NORMAL
        self._saved_scene = ""
        self._source_restore.clear()
        self._in_grace = False
        self._in_recovery = False
