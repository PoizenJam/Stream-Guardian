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


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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
            self.assertEqual(read_json(cfg.config_path)["obs"]["port"], 1111)
        self.assertEqual(read_json(cfg.config_path)["obs"]["port"], 2222)
        self.assertEqual(read_json(cfg.backup_path)["obs"]["port"], 1111)

    def test_config_file_is_ascii(self):
        # Default webhook messages contain emoji; the file must still read
        # under any encoding (Windows cp1252 default, older versions).
        cfg = ConfigManager(config_dir=self.dir)
        cfg.save()
        with open(cfg.config_path, "rb") as f:
            f.read().decode("ascii")  # raises if any non-ASCII byte was written
        with open(cfg.config_path, encoding="cp1252") as f:
            self.assertIn(r"\u2705 Stream recovered", f.read())

    def test_locked_config_does_not_raise(self):
        # Windows: os.replace fails while another program holds the file
        from unittest import mock
        cfg = ConfigManager(config_dir=self.dir)
        cfg.set("obs", "port", 1111)
        with mock.patch("config_manager.os.replace", side_effect=PermissionError("in use")), \
                mock.patch("config_manager.time.sleep"):
            self.assertTrue(cfg.save())  # falls back to writing in place
        self.assertEqual(read_json(cfg.config_path)["obs"]["port"], 1111)
        self.assertFalse(os.path.exists(str(cfg.config_path) + ".tmp"))
        with mock.patch("config_manager.os.replace", side_effect=PermissionError("in use")), \
                mock.patch("config_manager.time.sleep"), \
                mock.patch("builtins.open", side_effect=PermissionError("locked")):
            self.assertFalse(cfg.save())
        self.assertIn("could not save settings", cfg.save_error)

    def test_corrupt_config_recovers_from_backup(self):
        cfg = ConfigManager(config_dir=self.dir)
        cfg.set("obs", "port", 1111)
        cfg.set("obs", "port", 2222)  # backup now holds 1111
        with open(cfg.config_path, "w", encoding="utf-8") as f:
            f.write('{"obs": {"port": 22')  # truncated write
        cfg2 = ConfigManager(config_dir=self.dir)
        self.assertEqual(cfg2.get("obs", "port"), 1111)
        self.assertIn("restored from backup", cfg2.load_message)
        self.assertTrue(any(n.startswith("config.corrupt-") for n in os.listdir(self.dir)))

    def test_old_override_enabled_flag_is_ignored(self):
        with open(os.path.join(self.dir, "config.json"), "w", encoding="utf-8") as f:
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


class TestIngestParsing(unittest.TestCase):
    def setUp(self):
        from ingest_client import IngestPoller
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = ConfigManager(config_dir=self.tmp.name)
        self.cfg.set("srs", "backend", "mediamtx")
        self.cfg.set("srs", "stream_name", "cam")
        self.cfg.set("srs", "link_stats_enabled", False)
        self.poller = IngestPoller(self.cfg)
        self.IngestPoller = IngestPoller

    def tearDown(self):
        self.tmp.cleanup()

    def test_srt_deltas_and_reset(self):
        parse = self.IngestPoller.parse_srt_conn
        a = parse({"id": "x", "packetsReceived": 1000, "packetsReceivedLoss": 10,
                   "packetsReceivedDrop": 1, "msRTT": 42.5}, None)
        self.assertEqual((a["received"], a["lost"], a["dropped"], a["rtt_ms"]), (0, 0, 0, 42.5))
        b = parse({"id": "x", "packetsReceived": 1900, "packetsReceivedLoss": 40,
                   "packetsReceivedDrop": 3, "msRTT": 50}, a["_counters"])
        self.assertEqual((b["received"], b["lost"], b["dropped"]), (900, 30, 2))
        c = parse({"id": "y", "packetsReceived": 5, "packetsReceivedLoss": 0,
                   "packetsReceivedDrop": 0, "msRTT": 20}, b["_counters"])
        self.assertEqual((c["received"], c["lost"]), (0, 0))  # new connection

    def test_mediamtx_online_preferred_over_ready(self):
        # alwaysAvailable: path still "ready" with fallback content, publisher gone
        found, _, _ = self.poller._poll_mediamtx({"name": "cam", "ready": True, "online": False,
                                                  "inboundBytes": 1000})
        self.assertFalse(found)
        found, sid, total = self.poller._poll_mediamtx({"name": "cam", "online": True,
                                                        "inboundBytes": 1000})
        self.assertTrue(found)
        self.assertIsNone(total)  # first sample: no rate yet

    def test_mediamtx_old_server_fields(self):
        found, _, _ = self.poller._poll_mediamtx({"name": "cam", "ready": True, "bytesReceived": 10})
        self.assertTrue(found)


class TestConfigV2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_v1_config_migrates_to_one_stream(self):
        v1 = {"srs": {"backend": "mediamtx", "stream_name": "PixieMainCam", "port": 9997},
              "thresholds": {"low_bitrate_kbps": 1500},
              "scenes": {"disconnect_scene": "BRB", "whitelist": ["Starting Soon"],
                         "transition_name": "Stinger"},
              "obs": {"port": 4456}}
        with open(os.path.join(self.dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump(v1, f)
        cfg = ConfigManager(config_dir=self.dir)
        self.assertIn("upgraded", cfg.load_message)
        self.assertEqual(read_json(os.path.join(self.dir, "config.v1-backup.json"))["obs"]["port"], 4456)
        streams = cfg.streams()
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["name"], "Main")
        self.assertEqual(streams[0]["srs"]["stream_name"], "PixieMainCam")
        self.assertEqual(streams[0]["srs"]["poll_interval_ms"], 1000)  # default filled in
        self.assertEqual(streams[0]["thresholds"]["low_bitrate_kbps"], 1500)
        self.assertEqual(streams[0]["scenes"]["disconnect_scene"], "BRB")
        self.assertEqual(cfg.get("scenes", "whitelist"), ["Starting Soon"])  # stays global
        self.assertEqual(cfg.get("scenes", "transition_name"), "Stinger")
        self.assertEqual(cfg.get("obs", "port"), 4456)
        on_disk = read_json(cfg.config_path)
        self.assertEqual(on_disk["config_version"], 2)
        self.assertNotIn("srs", on_disk)

    def test_v1_preset_loads(self):
        cfg = ConfigManager(config_dir=self.dir)
        with open(os.path.join(self.dir, "presets", "Old.json"), "w", encoding="utf-8") as f:
            json.dump({"srs": {"host": "mainframe"}, "scenes": {"low_bitrate_scene": "Potato"}}, f)
        self.assertTrue(cfg.load_preset("Old"))
        self.assertEqual(cfg.get("srs", "host"), "mainframe")
        self.assertEqual(cfg.stream_view(cfg.streams()[0]["id"]).get("scenes", "low_bitrate_scene"), "Potato")

    def test_add_rename_remove_streams(self):
        cfg = ConfigManager(config_dir=self.dir)
        main = cfg.streams()[0]["id"]
        cfg.stream_view(main).set("srs", "host", "studio")
        b = cfg.add_stream("Cam 2", copy_from=main)
        self.assertEqual(cfg.stream_view(b).get("srs", "host"), "studio")  # copied
        c = cfg.add_stream("Cam 2")                                        # duplicate name
        self.assertEqual(cfg.stream_view(c).name, "Cam 2 2")
        cfg.rename_stream(b, "Side")
        self.assertEqual([s["name"] for s in cfg.streams()], ["Main", "Side", "Cam 2 2"])
        self.assertTrue(cfg.remove_stream(c))
        self.assertTrue(cfg.remove_stream(b))
        self.assertFalse(cfg.remove_stream(main))  # last one stays
        reloaded = ConfigManager(config_dir=self.dir)
        self.assertEqual([s["name"] for s in reloaded.streams()], ["Main"])


class TestStreamViewAfterDelete(unittest.TestCase):
    def test_deleted_stream_view_keeps_its_own_settings(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            cfg = ConfigManager(config_dir=tmp.name)
            b = cfg.add_stream("Side")
            view = cfg.stream_view(b)
            view.set("scenes", "disconnect_scene", "Side BRB")
            self.assertEqual(view.name, "Side")
            cfg.remove_stream(b)
            self.assertEqual(view.name, "Side")
            self.assertEqual(view.get("scenes", "disconnect_scene"), "Side BRB")
        finally:
            tmp.cleanup()
