"""
Bitrate tracking engine.
Maintains rolling window, computes averages, manages threshold state machine,
and triggers scene switches / source visibility toggles via OBS client.
Includes webhook notification dispatch; audio alerts are requested through
the play_audio signal and played by the GUI thread.

Threading (v1.2.0): MainWindow moves this object to its own QThread, so OBS
requests made here can no longer freeze the window. Everything the GUI asks
of the engine goes through the @pyqtSlot methods below via queued signals;
the GUI reads engine data only from the stats_updated payload.
"""
import time
import csv
import os
import threading
from collections import deque
from enum import Enum, auto
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import requests as http_requests

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from config_manager import APP_NAME, APP_INTERNAL_NAME

# Simulated outages end on their own after this long, in case End Test is forgotten
TEST_MODE_MAX_S = 120


class StreamState(Enum):
    NORMAL = auto()
    LOW_BITRATE = auto()
    DISCONNECTED = auto()


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


class BitrateEngine(QObject):
    """Core bitrate analysis, scene-switching / source toggling, alerts."""

    state_changed = pyqtSignal(str, str)  # new_state, old_state
    stats_updated = pyqtSignal(dict)  # {current, average, ..., "graph": [(t, kbps)], "link": {...}}
    scene_switch_triggered = pyqtSignal(str, str)  # new_scene, reason
    source_toggle_triggered = pyqtSignal(str, str, bool, str)  # scene, source, visible, reason
    log_entry = pyqtSignal(str)  # log message
    play_audio = pyqtSignal(str, int)  # file path, volume 0-100 (played on the GUI thread)
    test_mode_changed = pyqtSignal(str)  # "" when no test, else "LOW_BITRATE" / "DISCONNECTED"

    def __init__(self, config_manager, obs_client, parent=None):
        super().__init__(parent)
        self.cfg = config_manager
        self.obs = obs_client

        # Rolling data
        self._history = deque()  # (timestamp, bitrate_kbps)
        self._graph_history = deque()  # full graph history
        # Matches the dashboard Graph Window maximum (600 s)
        self._max_graph_seconds = 600

        # SRT link stats, per poll: (timestamp, packets_received_delta,
        # packets_lost_delta, packets_dropped_delta, rtt_ms)
        self._link_history = deque()
        self._link_available = False
        self._link_summary = {"available": False}

        # State machine
        self._state = StreamState.NORMAL
        self._state_enter_time = time.time()
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

        # Test mode (simulated outage from the dashboard buttons)
        self._test_state = None  # StreamState being simulated, or None
        self._test_until = 0.0
        self._period_is_test = False  # current state period started as a test

        # De-duplicates repeated log lines (cooldown, whitelist, OBS down)
        self._last_once_msg = ""

        # Recent stats for stability check (populated each evaluate)
        self._last_std = 0.0
        self._last_avg = 0.0

        # Authoritative stream-online state from the ingest server. Defaults
        # to True so we don't trigger a false disconnect before the first
        # poll arrives.
        self._stream_online = True

        # Logging
        self._log_file = None
        self._csv_writer = None
        self._pending_event = ""  # written into the next CSV row's event column

        self._reset_session_stats()

    def _reset_session_stats(self):
        now = time.time()
        self._session_start = now
        self._total_samples = 0
        self._total_bitrate = 0.0
        self._peak_bitrate = 0.0
        self._min_bitrate = float("inf")
        self._drop_count = 0
        # Real (non-test) seconds spent in each state, and outage tracking
        self._time_in_state = {s: 0.0 for s in StreamState}
        self._accum_from = now  # start of the not-yet-accumulated part of the current period
        self._outage_start = None if self._state == StreamState.NORMAL else now
        self._longest_outage = 0.0

    # --- Audio ---

    def _play_audio(self, file_path: str):
        """Ask the GUI thread to play an alert sound (QMediaPlayer must live there)."""
        if not file_path or not os.path.isfile(file_path):
            return
        self.play_audio.emit(file_path, int(self.cfg.get("notifications", "audio_volume")))

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

    def _fire_notifications(self, new_state: StreamState, avg_kbps: float, test: bool = False):
        """Dispatch audio and webhook notifications for a state transition.
        Tests play the sounds (so they can be checked) but never send webhooks,
        so a rehearsal doesn't alert a Discord channel."""
        audio_enabled = self.cfg.get("notifications", "audio_alerts")
        webhook_enabled = self.cfg.get("notifications", "webhook_enabled") and not test

        bitrate_str = f"{avg_kbps:.0f}"
        keys = {
            StreamState.LOW_BITRATE: ("audio_low_bitrate", "webhook_low_bitrate_msg"),
            StreamState.DISCONNECTED: ("audio_disconnect", "webhook_disconnect_msg"),
            StreamState.NORMAL: ("audio_recovery", "webhook_recovery_msg"),
        }
        audio_key, msg_key = keys[new_state]
        if audio_enabled:
            self._play_audio(self.cfg.get("notifications", audio_key))
        if webhook_enabled:
            msg = self.cfg.get("notifications", msg_key)
            self._send_webhook(msg.replace("{bitrate}", bitrate_str))

    # --- Properties (thread-safe enough for tests and post-shutdown reads) ---

    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def graph_data(self):
        return list(self._graph_history)

    @property
    def override(self) -> bool:
        return self._override

    @property
    def test_state(self):
        return self._test_state

    # --- Slots (called from the GUI thread through queued signals) ---

    @pyqtSlot(bool)
    def set_override(self, enabled: bool):
        """Manual override: True stops all automatic OBS actions."""
        self._override = bool(enabled)
        # Timers started before the pause would otherwise fire instantly on resume
        self._in_grace = False
        self._in_recovery = False

    @pyqtSlot(bool)
    def set_logging(self, enabled: bool):
        self.stop_logging()
        if enabled:
            self.start_logging()

    @pyqtSlot()
    def reset_session(self):
        """Reset session statistics only. The state machine and any pending
        scene/source restores are kept; clearing them mid-outage left OBS
        stuck on the BRB scene."""
        self.log_summary()
        self._reset_session_stats()
        self._history.clear()
        self._graph_history.clear()

    @pyqtSlot(str)
    def simulate(self, kind: str):
        """Rehearse the protection setup. kind: "low", "disconnect", or "" to end.

        A test transition skips the grace period and cooldown, is not counted
        in session stats, sends no webhooks, and ends automatically after
        TEST_MODE_MAX_S. Real evaluation is paused while a test runs."""
        now = time.time()
        if kind in ("low", "disconnect"):
            target = StreamState.LOW_BITRATE if kind == "low" else StreamState.DISCONNECTED
            self._test_state = target
            self._test_until = now + TEST_MODE_MAX_S
            self.log_entry.emit(
                f"TEST: simulating {target.name} (ends automatically after {TEST_MODE_MAX_S}s)"
            )
            if self._state != target:
                self._transition_to(target, now, self._last_avg, test=True)
            self.test_mode_changed.emit(target.name)
        else:
            self._end_test(now, "ended")

    def _end_test(self, now: float, how: str):
        if self._test_state is None:
            return
        self._test_state = None
        self.log_entry.emit(f"TEST: {how}, returning to normal")
        if self._state != StreamState.NORMAL:
            self._transition_to(StreamState.NORMAL, now, self._last_avg, test=True)
        self._in_grace = False
        self._in_recovery = False
        self.test_mode_changed.emit("")

    # --- Main processing ---

    @pyqtSlot(float, float, float)
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
        while self._link_history and self._link_history[0][0] < cutoff:
            self._link_history.popleft()

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
        self._last_avg = avg
        self._link_summary = self._summarize_link()

        # Session stats
        self._total_samples += 1
        self._total_bitrate += total_kbps
        self._peak_bitrate = max(self._peak_bitrate, total_kbps)
        if total_kbps > 0:
            self._min_bitrate = min(self._min_bitrate, total_kbps)

        session_avg = self._total_bitrate / self._total_samples if self._total_samples else 0

        # Test mode safety net
        if self._test_state is not None and now >= self._test_until:
            self._end_test(now, f"timed out after {TEST_MODE_MAX_S}s")

        # Run state machine unless manual override or a test is active
        if not self._override:
            if self._test_state is None:
                self._evaluate_thresholds(avg, now)
            self._reconcile(now)

        stats = {
            "current_kbps": total_kbps,
            "video_kbps": video_kbps,
            "audio_kbps": audio_kbps,
            "average_kbps": avg,
            "std_kbps": std,
            "state": self._state.name,
            "test": self._test_state.name if self._test_state else "",
            "session_avg_kbps": session_avg,
            "session_peak_kbps": self._peak_bitrate,
            "session_min_kbps": self._min_bitrate if self._min_bitrate != float("inf") else 0,
            "session_drops": self._drop_count,
            "uptime_s": now - self._session_start,
            "samples_in_window": len(self._history),
            "link": dict(self._link_summary),
            # Snapshot for the graph: the GUI must not iterate the live deque
            # from another thread.
            "graph": list(self._graph_history),
        }
        self.stats_updated.emit(stats)

        # Log
        self._write_log(now, total_kbps, avg, self._state.name)

    @pyqtSlot(bool, str)
    def set_stream_online(self, online: bool, stream_id: str = ""):
        """Connected straight to ingest_client.stream_online. Authoritative
        ingest-side indication of whether a publisher is currently connected.
        Used by the false-positive-aware disconnect logic; every backend
        (Oryx, MediaMTX, generic) emits it."""
        online = bool(online)
        if online and not self._stream_online:
            # Publisher just came back: drop the offline zeros from the
            # averaging window, otherwise recovery detours through LOW_BITRATE
            # (BRB -> Low Bitrate scene -> live) until they age out.
            self._history.clear()
            self._link_history.clear()
        self._stream_online = online

    @pyqtSlot(dict)
    def set_link_stats(self, stats: dict):
        """Connected to ingest_client.link_stats. stats is either
        {"available": False} or per-poll deltas:
        {"available": True, "received": n, "lost": n, "dropped": n, "rtt_ms": x}."""
        if not stats.get("available"):
            self._link_available = False
            self._link_history.clear()
            return
        self._link_available = True
        self._link_history.append((
            time.time(),
            max(int(stats.get("received", 0)), 0),
            max(int(stats.get("lost", 0)), 0),
            max(int(stats.get("dropped", 0)), 0),
            float(stats.get("rtt_ms", 0.0)),
        ))

    def _summarize_link(self) -> dict:
        """Loss and drop percentages over the averaging window, latest RTT."""
        if not self._link_available or not self._link_history:
            return {"available": False}
        received = sum(h[1] for h in self._link_history)
        lost = sum(h[2] for h in self._link_history)
        dropped = sum(h[3] for h in self._link_history)
        total = received + lost
        return {
            "available": True,
            "loss_pct": (lost / total * 100.0) if total else 0.0,
            "drop_pct": (dropped / total * 100.0) if total else 0.0,
            "rtt_ms": self._link_history[-1][4],
        }

    def _link_problem(self) -> str:
        """Reason text if SRT link quality crosses a configured limit, else ""."""
        link = self._link_summary
        if not link.get("available"):
            return ""
        max_loss = self.cfg.get("thresholds", "max_packet_loss_pct")
        max_rtt = self.cfg.get("thresholds", "max_rtt_ms")
        if max_loss > 0 and link["loss_pct"] > max_loss:
            return f"packet loss {link['loss_pct']:.1f}% > {max_loss}%"
        if max_rtt > 0 and link["rtt_ms"] > max_rtt:
            return f"RTT {link['rtt_ms']:.0f} ms > {max_rtt} ms"
        return ""

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
        #   - OR the SRT link is in trouble (packet loss / RTT limits, off by
        #     default). Not subject to the adaptive suppression: loss is a
        #     network problem regardless of what the encoder is doing.
        if trust_offline:
            disconnected = not self._stream_online
        else:
            disconnected = avg_kbps < disc_thresh

        if disconnected:
            return StreamState.DISCONNECTED
        if self._link_problem():
            return StreamState.LOW_BITRATE
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


    def _accumulate_time(self, now: float):
        """Credit time since the last accumulation to the current state, unless
        the current period is a test."""
        if not self._period_is_test:
            self._time_in_state[self._state] += max(now - self._accum_from, 0.0)
        self._accum_from = now

    def _transition_to(self, new_state: StreamState, now: float, avg_kbps: float,
                       test: bool = False) -> bool:
        """Change state and run the matching OBS action. Returns False if the
        cooldown blocked it (state unchanged). Test transitions skip the
        cooldown, don't start one, and are kept out of session stats."""
        old_state = self._state
        cooldown_s = self.cfg.get("thresholds", "cooldown_s")
        since_last = now - self._last_switch_time
        # Cooldown exists to stop flapping. It must not keep a disconnected
        # stream on the low-bitrate scene, so LOW -> DISCONNECTED bypasses it.
        escalation = old_state == StreamState.LOW_BITRATE and new_state == StreamState.DISCONNECTED

        if since_last < cooldown_s and not escalation and not test:
            # Stable text so _log_once prints it once per blocked transition
            self._log_once(
                f"COOLDOWN: transition to {new_state.name} held until the "
                f"{cooldown_s}s cooldown since the last switch ends"
            )
            return False

        self._last_once_msg = ""
        reason = ""
        if not test and new_state == StreamState.LOW_BITRATE:
            reason = self._link_problem()
        self.log_entry.emit(
            f"{'TEST ' if test else ''}TRANSITION: {old_state.name} -> {new_state.name}, "
            f"scene='{self.obs.current_scene}', avg={avg_kbps:.0f} kbps"
            + (f", {reason}" if reason else "")
        )

        # Session bookkeeping (real transitions only)
        self._accumulate_time(now)
        if not test:
            if old_state == StreamState.NORMAL and new_state != StreamState.NORMAL:
                self._drop_count += 1
                self._outage_start = now
            elif new_state == StreamState.NORMAL and self._outage_start is not None:
                self._longest_outage = max(self._longest_outage, now - self._outage_start)
                self._outage_start = None
        self._period_is_test = test and new_state != StreamState.NORMAL
        self._pending_event = f"{'TEST ' if test else ''}{old_state.name}->{new_state.name}"

        self._state = new_state
        self._state_enter_time = now
        if not test:
            self._last_switch_time = now

        if new_state == StreamState.NORMAL:
            self._action_applied = True
            self._restore_all()
        else:
            if old_state != StreamState.NORMAL:
                # Leaving one protection state for the other: undo its source
                # toggle first. Scene handling happens in _apply_protection.
                self._undo_source_toggle(old_state.name)
            self._action_applied = False
            self._apply_protection(now)

        self._fire_notifications(new_state, avg_kbps, test=test)
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
        self._log_file = open(filepath, "w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._log_file)
        # New columns are appended after the original four so existing
        # spreadsheets/scripts that read by position keep working.
        self._csv_writer.writerow([
            "timestamp", "bitrate_kbps", "avg_kbps", "state",
            "event", "packet_loss_pct", "rtt_ms",
        ])
        self.log_entry.emit(f"Logging started: {filepath}")

    def stop_logging(self):
        if self._log_file:
            self._log_file.close()
            self._log_file = None
            self._csv_writer = None
            self.log_entry.emit("Logging stopped")

    def _write_log(self, ts: float, bitrate: float, avg: float, state: str):
        event, self._pending_event = self._pending_event, ""
        if self._csv_writer:
            dt = datetime.fromtimestamp(ts).isoformat()
            link = self._link_summary
            loss = f"{link['loss_pct']:.2f}" if link.get("available") else ""
            rtt = f"{link['rtt_ms']:.0f}" if link.get("available") else ""
            self._csv_writer.writerow([dt, f"{bitrate:.1f}", f"{avg:.1f}", state, event, loss, rtt])
            self._log_file.flush()

    # --- Session summary ---

    def log_summary(self) -> str:
        """Log the session summary and, if CSV logging is on, write it as a
        row too (the Log tab is gone once the app has quit)."""
        text = self.session_summary()
        self.log_entry.emit(text)
        if self._csv_writer:
            self._csv_writer.writerow([datetime.now().isoformat(), "", "", self._state.name, text, "", ""])
            self._log_file.flush()
        return text

    def session_summary(self, now: float = None) -> str:
        """One-line summary of the session so far (real outages only)."""
        now = time.time() if now is None else now
        times = dict(self._time_in_state)
        if not self._period_is_test:
            times[self._state] += max(now - self._accum_from, 0.0)
        longest = self._longest_outage
        if self._outage_start is not None:
            longest = max(longest, now - self._outage_start)
        parts = [
            f"SESSION SUMMARY: {format_duration(now - self._session_start)} monitored",
            f"{self._drop_count} drop{'s' if self._drop_count != 1 else ''}",
            f"low bitrate {format_duration(times[StreamState.LOW_BITRATE])}",
            f"disconnected {format_duration(times[StreamState.DISCONNECTED])}",
        ]
        if self._drop_count:
            parts.append(f"longest outage {format_duration(longest)}")
        if self._total_samples:
            parts.append(f"avg {self._total_bitrate / self._total_samples:.0f} kbps")
        return ", ".join(parts)
