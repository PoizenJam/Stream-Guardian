"""
Tests for config persistence, the OBS client's request calls, hotkey parsing,
and the single-instance guard. Run: python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import obsws_python as obs
from PyQt6.QtWidgets import QApplication

from config_manager import ConfigManager
from obs_client import OBSClient

_app = QApplication.instance() or QApplication([])


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_saves_once_and_keeps_backup(self):
        cfg = ConfigManager(config_dir=self.dir)
        cfg.set("obs", "port", 1111)
        with cfg.batch():
            cfg.set("obs", "port", 2222)
            cfg.set("obs", "host", "studio")
            self.assertEqual(json.load(open(cfg.config_path))["obs"]["port"], 1111)
        self.assertEqual(json.load(open(cfg.config_path))["obs"]["port"], 2222)
        self.assertEqual(json.load(open(cfg.backup_path))["obs"]["port"], 1111)

    def test_corrupt_config_recovers_from_backup(self):
        cfg = ConfigManager(config_dir=self.dir)
        cfg.set("obs", "port", 1111)
        cfg.set("obs", "port", 2222)  # backup now holds 1111
        with open(cfg.config_path, "w") as f:
            f.write('{"obs": {"port": 22')  # truncated write
        cfg2 = ConfigManager(config_dir=self.dir)
        self.assertEqual(cfg2.get("obs", "port"), 1111)
        self.assertIn("restored from backup", cfg2.load_message)
        self.assertTrue(any(n.startswith("config.corrupt-") for n in os.listdir(self.dir)))

    def test_old_override_enabled_flag_is_ignored(self):
        with open(os.path.join(self.dir, "config.json"), "w") as f:
            json.dump({"override": {"enabled": True, "hotkey": "Alt+F9"}}, f)
        cfg = ConfigManager(config_dir=self.dir)
        self.assertNotIn("enabled", cfg.get("override"))
        self.assertEqual(cfg.get("override", "hotkey"), "Alt+F9")


class FakeReq(obs.ReqClient):
    """Real ReqClient methods (so signatures are the library's own), with
    send() replaced to record the request instead of using a socket."""

    def __init__(self):
        self.sent = []

    def send(self, param, data=None, raw=False):
        self.sent.append((param, data))
        if param == "GetCurrentSceneTransition":
            return SimpleNamespace(transition_name="Cut", transition_duration=None)
        if param == "GetSceneItemList":
            return SimpleNamespace(scene_items=[
                {"sceneItemId": 7, "sourceName": "Cam", "sceneItemEnabled": True},
            ])
        return None

    def disconnect(self):
        pass


class TestOBSClientRequests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = ConfigManager(config_dir=self.tmp.name)
        self.client = OBSClient(self.cfg)
        self.ws = FakeReq()
        self.client._ws = self.ws
        self.client._connected = True
        self.errors = []
        self.client.error.connect(self.errors.append)

    def tearDown(self):
        self.tmp.cleanup()

    def test_switch_scene_sends_correct_request(self):
        self.assertTrue(self.client.switch_scene("BRB"))
        self.assertEqual(self.ws.sent, [("SetCurrentProgramScene", {"sceneName": "BRB"})])
        self.assertEqual(self.errors, [])

    def test_custom_transition_applied_then_restored(self):
        with self.cfg.batch():
            self.cfg.set("scenes", "use_custom_transition", True)
            self.cfg.set("scenes", "transition_name", "Fade")
            self.cfg.set("scenes", "transition_duration_ms", 300)
        self.assertTrue(self.client.switch_scene("BRB"))
        self.assertEqual([p for p, _ in self.ws.sent], [
            "GetCurrentSceneTransition", "SetCurrentSceneTransition",
            "SetCurrentSceneTransitionDuration", "SetCurrentProgramScene",
        ])
        self.ws.sent.clear()
        self.client._restore_user_transition()
        self.assertEqual(self.ws.sent, [("SetCurrentSceneTransition", {"transitionName": "Cut"})])
        self.assertEqual(self.errors, [])

    def test_source_visibility_round_trip(self):
        self.assertTrue(self.client.get_source_visibility("Main", "Cam"))
        self.assertTrue(self.client.set_source_visibility("Main", "Cam", False))
        self.assertIn(("SetSceneItemEnabled",
                       {"sceneName": "Main", "sceneItemId": 7, "sceneItemEnabled": False}),
                      self.ws.sent)
        self.assertEqual(self.client.get_scene_items("Main"), [(7, "Cam", True)])
        self.assertIsNone(self.client.get_source_visibility("Main", "Missing"))

    def test_socket_failure_marks_disconnected(self):
        def boom(*a, **k):
            raise OSError("socket closed")
        self.ws.send = boom
        states = []
        self.client.connected.connect(lambda ok, msg: states.append(ok))
        self.assertFalse(self.client.switch_scene("BRB"))
        self.assertFalse(self.client.is_connected)
        self.assertEqual(states, [False])


class TestHotkeyParsing(unittest.TestCase):
    def test_parse(self):
        from gui.global_hotkey import qt_sequence_to_win, MOD_CONTROL, MOD_SHIFT, MOD_ALT, MOD_NOREPEAT
        self.assertEqual(qt_sequence_to_win("Ctrl+Shift+F12"), (MOD_NOREPEAT | MOD_CONTROL | MOD_SHIFT, 0x7B))
        self.assertEqual(qt_sequence_to_win("Alt+B"), (MOD_NOREPEAT | MOD_ALT, ord("B")))
        self.assertEqual(qt_sequence_to_win("Ctrl+Pause")[1], 0x13)
        self.assertIsNone(qt_sequence_to_win(""))


class TestSingleInstance(unittest.TestCase):
    def test_second_acquire_fails_until_release(self):
        from single_instance import SingleInstance
        a = SingleInstance("SGUnitTest")
        b = SingleInstance("SGUnitTest")
        self.assertTrue(a.acquire())
        try:
            self.assertFalse(b.acquire())
        finally:
            a.release()
        self.assertTrue(b.acquire())
        b.release()


if __name__ == "__main__":
    unittest.main()
