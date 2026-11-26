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

    events = None
    chapters = None
    recording = False

    def broadcast_event(self, data):
        self.events = (self.events or []) + [data]
        return True

    def add_record_chapter(self, name):
        if not self.recording:
            return False, ""
        self.chapters = (self.chapters or []) + [name]
        return True, ""


class QuietEngine(BitrateEngine):
    """Records webhooks instead of sending them."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.webhooks = []

    def _send_webhook(self, message):
        self.webhooks.append(message)


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


class TestRehearsal(EngineTestBase):
    def test_test_low_switches_immediately_and_restores(self):
        self.cfg.set("notifications", "webhook_enabled", True)
        self.cfg.set("notifications", "webhook_url", "https://example.invalid/hook")
        self.eng._last_switch_time = 10**12  # pretend a cooldown is running
        self.eng.simulate("low")
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)
        self.assertEqual(self.obs.current_scene, "Low Bitrate")
        self.assertEqual(self.eng._drop_count, 0)
        self.assertEqual(self.eng.webhooks, [])  # rehearsals never alert Discord
        self.feed(3000, 20)  # real evaluation paused during the test
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)
        self.eng.simulate("")
        self.assertEqual(self.eng.state, StreamState.NORMAL)
        self.assertEqual(self.obs.current_scene, "Main")
        self.assertIsNone(self.eng.test_state)

    def test_test_mode_times_out(self):
        self.eng.simulate("disconnect")
        self.eng._test_until = 0  # force expiry on the next sample
        self.eng.process_bitrate(3000, 3000, 0)
        self.assertIsNone(self.eng.test_state)
        self.assertEqual(self.eng.state, StreamState.NORMAL)
        self.assertTrue(any("timed out" in m for m in self.log))

    def test_test_does_not_start_cooldown(self):
        self.eng.simulate("low")
        self.eng.simulate("")
        self.feed(500, 4)  # a real drop right after the test is handled normally
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)
        self.assertEqual(self.eng._drop_count, 1)


class TestLinkTriggers(EngineTestBase):
    def link(self, received, lost, rtt=30.0, seconds=1):
        for _ in range(seconds):
            self.eng.set_link_stats({"available": True, "received": received,
                                     "lost": lost, "dropped": 0, "rtt_ms": rtt})

    def test_packet_loss_triggers_low_even_at_good_bitrate(self):
        self.cfg.set("thresholds", "max_packet_loss_pct", 2.0)
        self.link(950, 50)  # 5 % loss
        self.eng._link_summary = self.eng._summarize_link()
        self.assertAlmostEqual(self.eng._link_summary["loss_pct"], 5.0)
        self.feed(3000, 4)
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)
        self.assertTrue(any("packet loss 5.0%" in m for m in self.log))

    def test_rtt_limit_and_off_by_default(self):
        self.link(1000, 0, rtt=900)
        self.eng._link_summary = self.eng._summarize_link()
        self.feed(3000, 5)
        self.assertEqual(self.eng.state, StreamState.NORMAL)  # limits default to off
        self.cfg.set("thresholds", "max_rtt_ms", 500)
        self.feed(3000, 4)
        self.assertEqual(self.eng.state, StreamState.LOW_BITRATE)

    def test_unavailable_link_clears(self):
        self.link(1000, 100)
        self.eng.set_link_stats({"available": False})
        self.assertEqual(self.eng._summarize_link(), {"available": False})


class TestSessionSummary(EngineTestBase):
    def test_time_in_state_and_longest_outage(self):
        eng = self.eng
        t0 = 5000.0
        eng._session_start = eng._accum_from = t0
        eng._transition_to(StreamState.LOW_BITRATE, t0 + 100, 500)
        eng._transition_to(StreamState.DISCONNECTED, t0 + 130, 0)
        eng._transition_to(StreamState.NORMAL, t0 + 190, 3000)
        eng.simulate("low")                      # tests are excluded
        eng._accum_from = t0 + 200
        eng._transition_to(StreamState.NORMAL, t0 + 260, 3000, test=True)
        eng._test_state = None
        summary = eng.session_summary(now=t0 + 3600)
        self.assertIn("1h00m monitored", summary)
        self.assertIn("1 drop,", summary)
        self.assertIn("low bitrate 30s", summary)
        self.assertIn("disconnected 1m00s", summary)
        self.assertIn("longest outage 1m30s", summary)

    def test_stats_payload_has_graph_and_link(self):
        got = []
        self.eng.stats_updated.connect(got.append)
        self.eng.process_bitrate(2500, 2500, 0)
        self.assertEqual(got[-1]["graph"][-1][1], 2500)
        self.assertEqual(got[-1]["link"], {"available": False})
        self.assertEqual(got[-1]["test"], "")


class TestCsvLog(EngineTestBase):
    def test_event_column_records_transitions(self):
        import csv
        self.cfg.set("logging", "directory", self.tmp.name)
        self.eng.start_logging()
        self.eng.simulate("low")
        self.eng.process_bitrate(3000, 3000, 0)
        self.eng.process_bitrate(3000, 3000, 0)
        self.eng.stop_logging()
        name = [n for n in os.listdir(self.tmp.name) if n.startswith("bitrate_")][0]
        with open(os.path.join(self.tmp.name, name), newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0][:4], ["timestamp", "bitrate_kbps", "avg_kbps", "state"])
        self.assertEqual(rows[0][4:], ["event", "packet_loss_pct", "rtt_ms"])
        self.assertEqual(rows[1][4], "TEST NORMAL->LOW_BITRATE")
        self.assertEqual(rows[2][4], "")

    def test_summary_written_to_csv(self):
        import csv
        self.cfg.set("logging", "directory", self.tmp.name)
        self.eng.start_logging()
        self.eng.process_bitrate(3000, 3000, 0)
        self.eng.log_summary()
        self.eng.stop_logging()
        name = [n for n in os.listdir(self.tmp.name) if n.startswith("bitrate_")][0]
        with open(os.path.join(self.tmp.name, name), newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertTrue(rows[-1][4].startswith("SESSION SUMMARY (Main):"))


class TestMultiStream(unittest.TestCase):
    """Two engines sharing one SceneArbiter, as the main window runs them."""

    def setUp(self):
        from scene_arbiter import SceneArbiter
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = ConfigManager(config_dir=self.tmp.name)
        first = self.cfg.streams()[0]["id"]
        second = self.cfg.add_stream("Cam 2")
        self.a_cfg = self.cfg.stream_view(first)
        self.b_cfg = self.cfg.stream_view(second)
        for view, low, brb in ((self.a_cfg, "Low A", "BRB A"), (self.b_cfg, "Low B", "BRB B")):
            with self.cfg.batch():
                view.set("thresholds", "grace_period_s", 0)
                view.set("thresholds", "recovery_delay_s", 0)
                view.set("thresholds", "cooldown_s", 0)
                view.set("scenes", "low_bitrate_scene", low)
                view.set("scenes", "disconnect_scene", brb)
        self.obs = FakeOBS()
        self.arb = SceneArbiter(self.obs)
        self.log = []
        self.arb.log_entry.connect(self.log.append)
        self.a = QuietEngine(self.a_cfg, self.obs, arbiter=self.arb)
        self.b = QuietEngine(self.b_cfg, self.obs, arbiter=self.arb)
        for e in (self.a, self.b):
            e.prefix_logs = True
            e.log_entry.connect(self.log.append)
        self.t = 1000.0

    def tearDown(self):
        self.tmp.cleanup()

    def step(self, engine, avg, online=True):
        self.t += 1
        engine.set_stream_online(online)
        engine._evaluate_thresholds(avg, self.t)
        engine._reconcile(self.t)

    def test_streams_have_separate_settings(self):
        self.assertEqual(self.a_cfg.get("scenes", "low_bitrate_scene"), "Low A")
        self.assertEqual(self.b_cfg.get("scenes", "low_bitrate_scene"), "Low B")
        self.assertEqual(self.a_cfg.get("scenes", "whitelist"), self.b_cfg.get("scenes", "whitelist"))

    def test_more_severe_scene_wins_and_original_restored_once_all_clear(self):
        self.step(self.a, 500)                    # A low -> "Low A"
        self.assertEqual(self.obs.current_scene, "Low A")
        self.step(self.b, 0, online=False)        # B disconnect outranks A's low
        self.assertEqual(self.obs.current_scene, "BRB B")
        self.step(self.b, 3000)                   # B recovers: A still low
        self.assertEqual(self.obs.current_scene, "Low A")
        self.step(self.a, 3000)                   # all clear -> back to Main
        self.assertEqual(self.obs.current_scene, "Main")

    def test_first_stream_recovering_does_not_strand_obs(self):
        self.step(self.a, 0, online=False)        # A: BRB A
        self.step(self.b, 0, online=False)        # B: same severity, A asked first
        self.assertEqual(self.obs.current_scene, "BRB A")
        self.step(self.a, 3000)                   # A clears: show B's request
        self.assertEqual(self.obs.current_scene, "BRB B")
        self.step(self.b, 3000)
        self.assertEqual(self.obs.current_scene, "Main")

    def test_manual_change_wins_on_release_but_new_disconnect_still_switches(self):
        self.step(self.a, 500)
        self.obs.current_scene = "Chatting"       # streamer picks a scene
        self.step(self.b, 0, online=False)        # a disconnect must still show
        self.assertEqual(self.obs.current_scene, "BRB B")
        self.step(self.b, 3000)
        self.step(self.a, 3000)
        self.assertEqual(self.obs.current_scene, "Chatting")  # their choice, not Main

    def test_log_lines_are_prefixed_with_stream_name(self):
        self.step(self.b, 500)
        self.assertTrue(any(m.startswith("[Cam 2] TRANSITION") for m in self.log))


class TestPerSceneTargetsAndIntegrations(EngineTestBase):
    def test_scene_override_used_for_matching_scene(self):
        self.cfg.set("scenes", "scene_overrides", [
            {"when_on": "Gaming", "low": "Gaming Low", "disconnect": ""},
        ])
        self.obs.current_scene = "Gaming"
        self.feed(500, 4)
        self.assertEqual(self.obs.current_scene, "Gaming Low")
        self.feed(0, 1, online=False)            # blank disconnect -> normal target
        self.assertEqual(self.obs.current_scene, "BRB")
        self.feed(3000, 15)
        self.assertEqual(self.obs.current_scene, "Gaming")

    def test_custom_event_and_chapter(self):
        self.cfg.set("integrations", "record_chapters", True)
        self.obs.recording = True
        self.feed(500, 4)
        ev = self.obs.events[-1]
        self.assertEqual((ev["event"], ev["state"], ev["previous"], ev["stream"], ev["test"]),
                         ("state_changed", "LOW_BITRATE", "NORMAL", "Main", False))
        self.assertEqual(self.obs.chapters, ["Main: LOW_BITRATE"])
        self.eng.simulate("disconnect")          # tests: event yes, chapter no
        self.assertTrue(self.obs.events[-1]["test"])
        self.assertEqual(len(self.obs.chapters), 1)

    def test_custom_event_can_be_turned_off(self):
        self.cfg.set("integrations", "obs_custom_event", False)
        self.feed(500, 4)
        self.assertIsNone(self.obs.events)

    def test_stream_placeholder_in_webhook(self):
        with self.cfg.batch():
            self.cfg.set("notifications", "webhook_enabled", True)
            self.cfg.set("notifications", "webhook_url", "https://example.invalid/hook")
            self.cfg.set("notifications", "webhook_low_bitrate_msg", "{stream} low at {bitrate}")
        self.feed(500, 4)
        self.assertEqual(self.eng.webhooks, ["Main low at 500"])
