"""
Update check against GitHub Releases (notice only, never downloads or installs).
"""
import re
import threading

import requests
from PyQt6.QtCore import QObject, pyqtSignal

RELEASES_API = "https://api.github.com/repos/PoizenJam/Stream-Guardian/releases/latest"
RELEASES_PAGE = "https://github.com/PoizenJam/Stream-Guardian/releases/latest"


def parse_version(text: str):
    """'v2.0.1' -> (2, 0, 1). None if it doesn't look like a version."""
    m = re.match(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


class UpdateChecker(QObject):
    # (latest_tag, page_url) when a newer release exists; ("", "") when up to date
    result = pyqtSignal(str, str)
    failed = pyqtSignal(str)

    def __init__(self, current_version: str, parent=None):
        super().__init__(parent)
        self.current = current_version

    def check(self):
        """Run in a background thread; results arrive on the signals."""
        threading.Thread(target=self._run, daemon=True, name="StreamGuardianUpdate").start()

    def _run(self):
        try:
            resp = requests.get(RELEASES_API, timeout=8,
                                headers={"Accept": "application/vnd.github+json"})
            if resp.status_code == 404:
                self.result.emit("", "")  # no published release yet
                return
            resp.raise_for_status()
            data = resp.json()
            tag = data.get("tag_name", "")
            if data.get("draft") or data.get("prerelease"):
                self.result.emit("", "")
            elif is_newer(tag, self.current):
                self.result.emit(tag, data.get("html_url") or RELEASES_PAGE)
            else:
                self.result.emit("", "")
        except Exception as e:
            self.failed.emit(str(e)[:120])
