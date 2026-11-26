"""
Tests for the v2.0 helpers: update check, Start with Windows, overlay server.
Run: python -m unittest discover -s tests
"""
import json
import os
import sys
import types
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication

_app = QCoreApplication.instance() or QCoreApplication([])


class TestUpdater(unittest.TestCase):
    def test_version_compare(self):
        from updater import is_newer, parse_version
        self.assertEqual(parse_version("v2.0.1"), (2, 0, 1))
        self.assertEqual(parse_version("2.1"), (2, 1, 0))
        self.assertIsNone(parse_version("latest"))
        self.assertTrue(is_newer("v2.0.1", "2.0.0"))
        self.assertTrue(is_newer("v10.0.0", "9.9.9"))
        self.assertFalse(is_newer("v2.0.0", "2.0.0"))
        self.assertFalse(is_newer("v1.9.9", "2.0.0"))

    def _run_with(self, payload, status=200):
        from updater import UpdateChecker
        resp = mock.Mock(status_code=status)
        resp.json.return_value = payload
        resp.raise_for_status = mock.Mock()
        got = []
        checker = UpdateChecker("2.0.0")
        checker.result.connect(lambda tag, url: got.append((tag, url)))
        with mock.patch("updater.requests.get", return_value=resp):
            checker._run()  # synchronous for the test
        return got

    def test_newer_release_reported(self):
        got = self._run_with({"tag_name": "v2.1.0", "html_url": "https://example.invalid/r"})
        self.assertEqual(got, [("v2.1.0", "https://example.invalid/r")])

    def test_same_or_prerelease_is_quiet(self):
        self.assertEqual(self._run_with({"tag_name": "v2.0.0"}), [("", "")])
        self.assertEqual(self._run_with({"tag_name": "v3.0.0", "prerelease": True}), [("", "")])
        self.assertEqual(self._run_with({}, status=404), [("", "")])


class FakeWinreg(types.ModuleType):
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    KEY_QUERY_VALUE = 1
    REG_SZ = 1

    def __init__(self):
        super().__init__("winreg")
        self.values = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path, *a):
        return self._Key()

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], 1

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class TestAutostart(unittest.TestCase):
    def test_enable_refresh_disable(self):
        import autostart
        fake = FakeWinreg()
        with mock.patch.dict(sys.modules, {"winreg": fake}), \
                mock.patch("autostart.supported", return_value=True):
            self.assertEqual(autostart.sync(True), "")
            cmd = fake.values["StreamGuardian"]
            self.assertTrue(cmd.endswith("--minimized"))
            self.assertIn("main.py", cmd)  # source checkout in tests
            fake.values["StreamGuardian"] = '"C:\\old\\place\\StreamGuardian.exe" --minimized'
            autostart.sync(True)                       # moved app folder: refreshed
            self.assertEqual(fake.values["StreamGuardian"], cmd)
            self.assertEqual(autostart.sync(False), "")
            self.assertNotIn("StreamGuardian", fake.values)
            self.assertEqual(autostart.sync(False), "")  # already off: no error

    def test_frozen_command(self):
        import autostart
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", "C:\\Apps\\StreamGuardian.exe"):
            self.assertEqual(autostart.launch_command(), '"C:\\Apps\\StreamGuardian.exe" --minimized')


class TestOverlayServer(unittest.TestCase):
    def test_serves_page_and_stats(self):
        from overlay_server import OverlayServer
        srv = OverlayServer()
        err = srv.start(18799)
        self.assertEqual(err, "")
        try:
            srv.set_order(["a", "b"])
            srv.update("b", {"name": "Cam 2", "state": "LOW_BITRATE", "average_kbps": 500})
            srv.update("a", {"name": "Main", "state": "NORMAL", "average_kbps": 3000})
            with urllib.request.urlopen("http://127.0.0.1:18799/stats.json", timeout=3) as r:
                data = json.loads(r.read())
            self.assertEqual([s["name"] for s in data["streams"]], ["Main", "Cam 2"])
            with urllib.request.urlopen("http://127.0.0.1:18799/?stream=Main", timeout=3) as r:
                self.assertIn("stats.json", r.read().decode("utf-8"))
            srv.set_order(["a"])  # removed stream disappears
            self.assertEqual(len(srv.payload()["streams"]), 1)
            self.assertEqual(srv.start(18799), "")  # restart on the same port works
        finally:
            srv.stop()
        self.assertFalse(srv.running)

    def test_port_in_use_reported(self):
        from overlay_server import OverlayServer
        a, b = OverlayServer(), OverlayServer()
        try:
            self.assertEqual(a.start(18798), "")
            self.assertIn("could not listen", b.start(18798))
        finally:
            a.stop()
            b.stop()


if __name__ == "__main__":
    unittest.main()
