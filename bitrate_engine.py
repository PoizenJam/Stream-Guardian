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
from datetime import datetime, timezone

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
        # Matches the dashboard Graph Window maximum (600 s); was 300, which
        # silently capped the graph at 5 minutes.
        self._max_graph_seconds = 600

        # State machine
        self._state = StreamState.NORMAL
        self._state_enter_time = 0.0
        self._last_switch_time = 0.0
        self._grace_start = 0.0
        self._in_grace = False
        self._recovery_start = 0.0
        self._in_recovery = False

        # Scene restore tracking. _saved_scene is the scene the streamer was on
        # before the first auto-switch; _protection_scene is the scene we
        # switched to. Recovery only switches back if OBS is still on
        # _protection_scene, so a manual scene change during an outage wins.
        self._saved_scene = ""
        self._protection_scene = ""

        # Source visibility restore tracking
        # Stores {state_name: (scene, source, original_visibility)} for undo
        self._source_restore = {}

        # Whether the OBS action for the current protection state has been
        # carried out. False when OBS was down, the scene was whitelisted, or
        # the request failed; _reconcile() retries until it succeeds.
        self._action_applied = True
        self._last_action_attempt = 0.0

        # Manual override (auto-switch OFF). Deliberately not persisted: a
        # restart always comes back with protection active.
        self._override = False

        # De-duplicates repeated log lines (cooldown, whitelist, OBS down)
        self._last_once_msg = ""

        # Session stats
        self._session_start = time.time()
        self._total_samples = 0
        self._total_bitrate = 0.0
        self._peak_bitrate = 0.0
        self._min_bitrate = float("inf")
        self._drop_count = 0

        # Recent stats for stability check (populated each evaluate)
        self._last_std = 0.0

        # Authoritative stream-online state from the ingest server. Set by
        # main_window when ingest_client emits stream_online(...). Defaults
        # to True so we don't trigger a false disconnect before the first
        # poll arrives.
        self._stream_online = True

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
                        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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

    @property
    def override(self) -> bool:
        return self._override

    def set_override(self, enabled: bool):
        """Manual override: True stops all automatic OBS actions."""
        self._override = bool(enabled)
        # Timers started before the pause would otherwise fire instantly on resume
        self._in_grace = False
        self._in_recovery = False

    # --- Main processing ---

    def process_bitrate(self, total_kbps: float, video_kbps: float, audio_kbps: float):
        """Process a new bitrate sample. Called on every ingest poll."""
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
        self._last_std = std

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

        # Run state machine unless manual override is active
        if not self._override:
            self._evaluate_thresholds(avg, now)
            self._reconcile(now)

        # Log
        self._write_log(now, total_kbps, avg, self._state.name)

    def set_stream_online(self, online: bool):
        """Called by main_window from ingest_client.stream_online signal.
        Authoritative ingest-side indication of whether a publisher is
        currently connected. Used by the false-positive-aware disconnect
        logic; the same signal is emitted by all backends (Oryx, MediaMTX,
        generic) so this works uniformly across them."""
        online = bool(online)
        if online and not self._stream_online:
            # Publisher just came back: drop the offline zeros from the
            # averaging window, otherwise recovery detours through LOW_BITRATE
            # (BRB -> Low Bitrate scene -> live) until they age out.
            self._history.clear()
        self._stream_online = online

    def _target_state(self, avg_kbps: float) -> StreamState:
        low_thresh = self.cfg.get("thresholds", "low_bitrate_kbps")
        disc_thresh = self.cfg.get("thresholds", "disconnect_kbps")
        trust_offline = self.cfg.get("thresholds", "trust_ingest_offline_for_disconnect")
        adaptive_floor = self.cfg.get("thresholds", "adaptive_floor_kbps")
        cv_thresh = self.cfg.get("thresholds", "stability_cv_threshold")

        # DISCONNECTED:
        #   - With trust_ingest_offline on (default), only when the ingest
        #     server reports the stream is gone. This is the authoritative
        #     signal and avoids the false positive where an adaptive encoder
        #     (Moblin SRT, x264 capped CRF, etc.) legitimately drops bitrate
        #     on a static or dark scene.
        #   - Otherwise, legacy behaviour: bitrate below the disconnect threshold.
        #
        # LOW_BITRATE:
        #   - avg < low threshold, BUT suppressed if the stream looks like a
        #     stable adaptive coast: avg >= adaptive_floor AND coefficient of
        #     variation (std/avg) < cv_thresh. A real network problem produces
        #     erratic bitrate (high CV); a static scene at low encoder cost
        #     produces a smooth low plateau (low CV).
        if trust_offline:
            disconnected = not self._stream_online
        else:
            disconnected = avg_kbps < disc_thresh

        if disconnected:
            return StreamState.DISCONNECTED
        if avg_kbps < low_thresh:
            if (
                adaptive_floor > 0
                and avg_kbps >= adaptive_floor
                and avg_kbps > 0
                and (self._last_std / avg_kbps) < cv_thresh
            ):
                return StreamState.NORMAL
            return StreamState.LOW_BITRATE
        return StreamState.NORMAL

    def _evaluate_thresholds(self, avg_kbps: float, now: float):
        grace_s = self.cfg.get("thresholds", "grace_period_s")
        recovery_s = self.cfg.get("thresholds", "recovery_delay_s")
        target = self._target_state(avg_kbps)

        # Timers are started and checked in the same call, so a 0 s grace or
        # recovery fires immediately. Timers are only cleared once the
        # transition actually happens: if the cooldown blocks it, the next
        # sample retries straight away instead of waiting a full grace period
        # again.

        if self._state == StreamState.NORMAL:
            if target == StreamState.NORMAL:
                self._in_grace = False
                return
            if not self._in_grace:
                self._in_grace = True
                self._grace_start = now
            if now - self._grace_start < grace_s:
                return
            if self._transition_to(target, now, avg_kbps):
                self._in_grace = False
            return

        # In a protection state
        if target == self._state:
            self._in_recovery = False
            return

        if self._state == StreamState.LOW_BITRATE and target == StreamState.DISCONNECTED:
            # Escalation is immediate: the stream is already known to be in trouble
            if self._transition_to(target, now, avg_kbps):
                self._in_recovery = False
            return

        # Moving to a less severe state (LOW->NORMAL, DISC->NORMAL, DISC->LOW)
        if not self._in_recovery:
            self._in_recovery = True
            self._recovery_start = now
        if now - self._recovery_start < recovery_s:
            return
        if self._transition_to(target, now, avg_kbps):
            self._in_recovery = False

    def _log_once(self, msg: str):
        if msg != self._last_once_msg:
            self._last_once_msg = msg
            self.log_entry.emit(msg)

    def _transition_to(self, new_state: StreamState, now: float, avg_kbps: float) -> bool:
        """Change state and run the matching OBS action. Returns False if the
        cooldown blocked it (state unchanged)."""
        old_state = self._state
        cooldown_s = self.cfg.get("thresholds", "cooldown_s")
        since_last = now - self._last_switch_time
        # Cooldown exists to stop flapping. It must not keep a disconnected
        # stream on the low-bitrate scene, so LOW -> DISCONNECTED bypasses it.
        escalation = old_state == StreamState.LOW_BITRATE and new_state == StreamState.DISCONNECTED

        if since_last < cooldown_s and not escalation:
            # Stable text so _log_once prints it once per blocked transition
            self._log_once(
                f"COOLDOWN: transition to {new_state.name} held until the "
                f"{cooldown_s}s cooldown since the last switch ends"
            )
            return False

        self._last_once_msg = ""
        self.log_entry.emit(
            f"TRANSITION: {old_state.name} -> {new_state.name}, "
            f"scene='{self.obs.current_scene}', avg={avg_kbps:.0f} kbps"
        )

        self._state = new_state
        self._state_enter_time = now
        self._last_switch_time = now

        if new_state == StreamState.NORMAL:
            self._action_applied = True
            self._restore_all()
        else:
            if old_state == StreamState.NORMAL:
                self._drop_count += 1
            else:
                # Leaving one protection state for the other: undo its source
                # toggle first. Scene handling happens in _apply_protection.
                self._undo_source_toggle(old_state.name)
            self._action_applied = False
            self._apply_protection(now)

        self._fire_notifications(new_state, avg_kbps)
        self.state_changed.emit(new_state.name, old_state.name)
        return True

    # --- OBS actions ---

    def _state_key(self, state: StreamState) -> str:
        return "low_bitrate" if state == StreamState.LOW_BITRATE else "disconnect"

    def _reconcile(self, now: float):
        """Retry OBS work that could not be done at transition time: OBS was
        disconnected, the request failed, or the streamer was on a whitelisted
        scene. Throttled so a persistent failure doesn't hammer OBS."""
        if now - self._last_action_attempt < 3.0 or not self.obs.is_connected:
            return
        if self._state != StreamState.NORMAL:
            if not self._action_applied:
                self._apply_protection(now)
        elif self._saved_scene or self._source_restore:
            self._last_action_attempt = now
            self._restore_all()

    def _apply_protection(self, now: float):
        self._last_action_attempt = now
        key = self._state_key(self._state)

        if not self.obs.is_connected:
            self._log_once(f"OBS not connected: {key} action will run when OBS reconnects")
            return

        current = self.obs.current_scene
        whitelist = self.cfg.get("scenes", "whitelist")
        # Whitelist only blocks leaving the streamer's own scene. Once we have
        # switched away (saved scene set), moving between protection scenes is fine.
        if current in whitelist and not self._saved_scene:
            self._log_once(
                f"Scene '{current}' is whitelisted: {key} action held until the scene changes"
            )
            return

        mode = self.cfg.get("scenes", f"{key}_mode")
        if mode == "source":
            # A previous scene-mode state may have switched away; go back to
            # the streamer's scene before applying an in-scene toggle.
            if not self._restore_saved_scene():
                return
            ok = self._execute_source_toggle(key)
        else:
            ok = self._execute_scene_switch(key)
        self._action_applied = ok
        if ok:
            self._last_once_msg = ""

    def _execute_scene_switch(self, state_key: str) -> bool:
        """Switch to the configured scene for this state. True when done or
        when there is nothing to do."""
        target_scene = self.cfg.get("scenes", f"{state_key}_scene")
        current_scene = self.obs.current_scene
        reason = "Low bitrate" if state_key == "low_bitrate" else "Disconnect"

        if not target_scene:
            self._log_once(f"SWITCH SKIPPED: no target scene configured for {state_key}")
            return True

        if target_scene == current_scene:
            if self._saved_scene:
                self._protection_scene = target_scene
            self.log_entry.emit(f"SWITCH SKIPPED: already on '{target_scene}'")
            return True

        if not self.obs.switch_scene(target_scene):
            self._log_once(f"SWITCH FAILED: could not switch to '{target_scene}' (will retry)")
            return False

        if not self._saved_scene:
            self._saved_scene = current_scene
        self._protection_scene = target_scene
        self.scene_switch_triggered.emit(target_scene, reason)
        self.log_entry.emit(f"Scene -> '{target_scene}': {reason}")
        return True

    def _execute_source_toggle(self, state_key: str) -> bool:
        """Show or hide the configured source for this state. True when done
        or when nothing is configured."""
        scene = self.cfg.get("scenes", f"{state_key}_source_scene")
        source = self.cfg.get("scenes", f"{state_key}_source_name")
        action = self.cfg.get("scenes", f"{state_key}_source_action")
        state_name = "LOW_BITRATE" if state_key == "low_bitrate" else "DISCONNECTED"

        if not scene or not source:
            self._log_once(f"Source visibility not configured for {state_key}")
            return True

        current_vis = self.obs.get_source_visibility(scene, source)
        if current_vis is None:
            self._log_once(f"Source '{source}' not found in scene '{scene}' (will retry)")
            return False

        target_vis = (action == "show")
        # Keep the first recorded original so a retry doesn't save our own change
        self._source_restore.setdefault(state_name, (scene, source, current_vis))

        verb = "Show" if target_vis else "Hide"
        if not self.obs.set_source_visibility(scene, source, target_vis):
            self._log_once(f"Failed: {verb} '{source}' in '{scene}' (will retry)")
            return False

        self.source_toggle_triggered.emit(scene, source, target_vis, state_name)
        self.log_entry.emit(f"Source toggle: {verb} '{source}' in '{scene}'")
        return True

    def _restore_all(self):
        """Undo every outstanding protection action."""
        for state_name in list(self._source_restore):
            self._undo_source_toggle(state_name)
        self._restore_saved_scene()

    def _restore_saved_scene(self) -> bool:
        """Switch back to the streamer's scene if we switched away. Returns
        False only if a needed switch failed (kept for retry)."""
        if not self._saved_scene:
            return True
        if not self.obs.is_connected:
            return False

        current = self.obs.current_scene
        if current != self._protection_scene:
            self.log_entry.emit(
                f"Scene changed manually to '{current}' during protection; "
                f"not switching back to '{self._saved_scene}'"
            )
        else:
            if not self.obs.switch_scene(self._saved_scene):
                return False
            self.scene_switch_triggered.emit(self._saved_scene, "Bitrate recovered")
            self.log_entry.emit(f"Scene restored -> '{self._saved_scene}'")

        self._saved_scene = ""
        self._protection_scene = ""
        return True

    def _undo_source_toggle(self, state_name: str):
        """Restore a source to its original visibility for the given state.
        The record is kept if OBS refuses, so _reconcile can retry."""
        if state_name not in self._source_restore:
            return
        scene, source, original_vis = self._source_restore[state_name]
        if original_vis is None or self.obs.set_source_visibility(scene, source, original_vis):
            del self._source_restore[state_name]
            if original_vis is not None:
                self.log_entry.emit(
                    f"Source restored: '{source}' in '{scene}' -> "
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
        """Reset session statistics only. The state machine and any pending
        scene/source restores are kept; clearing them mid-outage (as before)
        left OBS stuck on the BRB scene."""
        self._session_start = time.time()
        self._total_samples = 0
        self._total_bitrate = 0.0
        self._peak_bitrate = 0.0
        self._min_bitrate = float("inf")
        self._drop_count = 0
        self._history.clear()
        self._graph_history.clear()
