"""
State machine tests for BitrateEngine, driven with explicit timestamps
against a fake OBS client. Run: python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication

from config_manager import ConfigManager
from bitrate_engine import BitrateEngine, StreamState

_app = QCoreApplication.instance() or QCoreApplication([])


class FakeOBS:
    def __init__(self):
        self.is_connected = True
        self.current_scene = "Main"
        self.vis = {("Main", "Cam"): True, ("Main", "Banner"): False}
        self.switches = []

    def switch_scene(self, scene):
        if not self.is_connected:
            return False
        self.current_scene = scene
        self.switches.append(scene)
        return True

    def get_source_visibility(self, scene, source):
        return self.vis.get((scene, source))

    def set_source_visibility(self, scene, source, visible):
        if not self.is_connected or (scene, source) not in self.vis:
            return False
        self.vis[(scene, source)] = visible
        return True


class QuietEngine(BitrateEngine):
    def _init_audio(self):  # no audio device needed for tests
        self._audio_player = None
        self._audio_output = None


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = ConfigManager(config_dir=self.tmp.name)
        with self.cfg.batch():
            self.cfg.set("thresholds", "low_bitrate_kbps", 1000)
            self.cfg.set("thresholds", "grace_period_s", 3)
            self.cfg.set("thresholds", "recovery_delay_s", 5)
            self.cfg.set("thresholds", "cooldown_s", 10)
            self.cfg.set("thresholds", "trust_ingest_offline_for_disconnect", True)
            self.cfg.set("thresholds", "adaptive_floor_kbps", 0)
            self.cfg.set("scenes", "low_bitrate_scene", "Low Bitrate")
            self.cfg.set("scenes", "disconnect_scene", "BRB")
        self.obs = FakeOBS()
        self.eng = QuietEngine(self.cfg, self.obs)
        self.log = []
        self.eng.log_entry.connect(self.log.append)
        self.t = 1000.0

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, avg, seconds=1, online=True):
        """Advance time one second per step with the given average bitrate."""
        self.eng.set_stream_online(online)
        for _ in range(seconds):
            self.t += 1
            self.eng._evaluate_thresholds(avg, self.t)
            self.eng._reconcile(self.t)


class TestSceneMode(EngineTestBase):
    def test_low_bitrate_switches_after_grace_and_restores(self):
        self.feed(500, 3)
        self.assertEqual(self.eng.state, StreamState.NORMAL)  # grace not yet elapsed
        self.feed(500, 1)
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)
        self.assertEqual(self.obs.current_scene, "Low Bitrate")
        self.feed(3000, 12)  # recovery delay, then the rest of the 10 s cooldown
        self.assertEqual(self.eng.state, StreamState.NORMAL)
        self.assertEqual(self.obs.current_scene, "Main")

    def test_manual_scene_change_is_not_overridden_on_recovery(self):
        self.feed(500, 4)
        self.obs.current_scene = "Chatting"  # streamer moves on by hand
        self.feed(3000, 12)
        self.assertEqual(self.eng.state, StreamState.NORMAL)
        self.assertEqual(self.obs.current_scene, "Chatting")
        self.assertTrue(any("changed manually" in m for m in self.log))

    def test_escalation_to_disconnect_bypasses_cooldown(self):
        self.feed(500, 4)
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)
        self.feed(0, 1, online=False)  # well inside the 10 s cooldown
        self.assertEqual(self.eng.state, StreamState.DISCONNECTED)
        self.assertEqual(self.obs.current_scene, "BRB")
        self.feed(3000, 15)
        self.assertEqual(self.obs.current_scene, "Main")  # original scene, not Low Bitrate

    def test_cooldown_retries_without_new_grace_and_logs_once(self):
        self.cfg.set("thresholds", "recovery_delay_s", 0)
        self.feed(500, 4)                      # LOW at t+4
        self.feed(3000, 1)                     # recovery blocked by cooldown
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)
        self.feed(3000, 5)
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)
        self.assertEqual(sum("COOLDOWN" in m for m in self.log), 1)
        self.feed(3000, 5)                     # cooldown over: transitions on the first allowed sample
        self.assertEqual(self.eng.state, StreamState.NORMAL)

    def test_zero_grace_triggers_immediately(self):
        self.cfg.set("thresholds", "grace_period_s", 0)
        self.feed(500, 1)
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)


class TestRetries(EngineTestBase):
    def test_action_applied_when_obs_reconnects(self):
        self.obs.is_connected = False
        self.feed(500, 4)
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)
        self.assertEqual(self.obs.current_scene, "Main")
        self.obs.is_connected = True
        self.feed(500, 3)
        self.assertEqual(self.obs.current_scene, "Low Bitrate")
        self.feed(3000, 12)
        self.assertEqual(self.obs.current_scene, "Main")

    def test_restore_retried_when_obs_was_down_at_recovery(self):
        self.feed(500, 4)
        self.obs.is_connected = False
        self.feed(3000, 12)
        self.assertEqual(self.eng.state, StreamState.NORMAL)
        self.obs.is_connected = True
        self.feed(3000, 3)
        self.assertEqual(self.obs.current_scene, "Main")

    def test_whitelisted_scene_holds_action_until_scene_changes(self):
        self.cfg.set("scenes", "whitelist", ["Starting Soon"])
        self.obs.current_scene = "Starting Soon"
        self.feed(500, 6)
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)
        self.assertEqual(self.obs.current_scene, "Starting Soon")
        self.assertEqual(sum("whitelisted" in m for m in self.log), 1)
        self.obs.current_scene = "Main"
        self.feed(500, 3)
        self.assertEqual(self.obs.current_scene, "Low Bitrate")

    def test_reset_session_keeps_pending_restore(self):
        self.feed(500, 4)
        self.eng.reset_session()
        self.feed(3000, 12)
        self.assertEqual(self.obs.current_scene, "Main")


class TestSourceMode(EngineTestBase):
    def test_mixed_modes_disconnect_scene_then_low_source(self):
        with self.cfg.batch():
            self.cfg.set("scenes", "low_bitrate_mode", "source")
            self.cfg.set("scenes", "low_bitrate_source_scene", "Main")
            self.cfg.set("scenes", "low_bitrate_source_name", "Banner")
            self.cfg.set("scenes", "low_bitrate_source_action", "show")
        self.feed(0, 4, online=False)
        self.assertEqual(self.obs.current_scene, "BRB")
        # Publisher back but weak: should leave BRB and show the banner on Main
        self.feed(500, 12)
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)
        self.assertEqual(self.obs.current_scene, "Main")
        self.assertTrue(self.obs.vis[("Main", "Banner")])
        self.feed(3000, 15)
        self.assertEqual(self.eng.state, StreamState.NORMAL)
        self.assertFalse(self.obs.vis[("Main", "Banner")])

    def test_same_source_in_both_states_restores_original(self):
        with self.cfg.batch():
            for key in ("low_bitrate", "disconnect"):
                self.cfg.set("scenes", f"{key}_mode", "source")
                self.cfg.set("scenes", f"{key}_source_scene", "Main")
                self.cfg.set("scenes", f"{key}_source_name", "Cam")
                self.cfg.set("scenes", f"{key}_source_action", "hide")
        self.feed(500, 4)
        self.assertFalse(self.obs.vis[("Main", "Cam")])
        self.feed(0, 1, online=False)
        self.assertFalse(self.obs.vis[("Main", "Cam")])
        self.feed(3000, 15)
        self.assertTrue(self.obs.vis[("Main", "Cam")])
        self.assertEqual(self.obs.switches, [])


class TestOverride(EngineTestBase):
    def test_override_blocks_transitions_and_resets_timers(self):
        self.eng.set_override(True)
        for _ in range(10):
            self.t += 1
            self.eng.process_bitrate(0, 0, 0)  # override path uses process_bitrate
        self.assertEqual(self.eng.state, StreamState.NORMAL)
        self.feed(500, 2)
        self.eng.set_override(False)
        self.assertFalse(self.eng._in_grace)


if __name__ == "__main__":
    unittest.main()
