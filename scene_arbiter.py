"""
Scene arbitration shared by every monitored stream (Stream Guardian 2.0).

With one stream, an engine could remember "the scene to go back to" itself.
With several, two engines doing that independently can leave OBS stuck: if
stream A switches Main -> BRB and stream B then switches BRB -> BRB 2, B's
"scene to go back to" is BRB. The arbiter keeps a single original scene for
all streams, shows the most severe active request (DISCONNECTED over
LOW_BITRATE; ties go to the earliest request), and restores the original only
once every stream has released.

Manual changes win: if OBS is no longer on the scene the arbiter put there,
the streamer's choice becomes the scene to return to. A release then leaves
it alone; a new or more severe request still switches (a disconnect must
not stay hidden behind a scene the streamer picked during low bitrate).

All engines live on one thread, so no locking is needed.
"""
from PyQt6.QtCore import QObject, pyqtSignal

SEVERITY = {"low_bitrate": 1, "disconnect": 2}


class SceneArbiter(QObject):
    log_entry = pyqtSignal(str)
    # (scene, reason) for every switch the arbiter makes
    scene_switched = pyqtSignal(str, str)

    def __init__(self, obs_client, parent=None):
        super().__init__(parent)
        self.obs = obs_client
        self._requests = {}  # owner -> (scene, severity, sequence, label)
        self._seq = 0
        self.original_scene = ""  # where to go back to when everything clears
        self.active_scene = ""    # the scene the arbiter itself put on program
        self.pending = False      # a switch failed; reconcile() retries

    @property
    def active(self) -> bool:
        return bool(self.active_scene)

    def holds(self, owner: str) -> bool:
        return owner in self._requests

    def request(self, owner: str, scene: str, severity: int, label: str = "") -> bool:
        """Ask for `scene` to be shown. False if OBS refused (kept for retry)."""
        prev = self._requests.get(owner)
        if prev and prev[0] == scene and prev[1] == severity:
            seq = prev[2]
        else:
            self._seq += 1
            seq = self._seq
        self._requests[owner] = (scene, severity, seq, label)
        return self._apply("request")

    def release(self, owner: str) -> bool:
        """Withdraw a request. False if a needed switch failed (kept for retry)."""
        had = self._requests.pop(owner, None) is not None
        if not had and not self.pending:
            return True
        return self._apply("release")

    def reconcile(self) -> bool:
        return self._apply("retry") if self.pending else True

    def _apply(self, trigger: str) -> bool:
        if not self.obs.is_connected:
            self.pending = True
            return False
        current = self.obs.current_scene

        manual = False
        if self.active_scene and current != self.active_scene:
            manual = True
            if self._requests:
                self.log_entry.emit(
                    f"Scene changed manually to '{current}' during protection; "
                    f"returning there on recovery"
                )
            else:
                self.log_entry.emit(
                    f"Scene changed manually to '{current}' during protection; "
                    f"not switching back to '{self.original_scene}'"
                )
            self.original_scene = current
            self.active_scene = ""

        if self._requests:
            target, _, _, label = max(self._requests.values(), key=lambda r: (r[1], -r[2]))
            if manual and trigger != "request":
                self.pending = False
                return True
            if current == target:
                self.pending = False
                return True
            if not self.obs.switch_scene(target):
                self.pending = True
                return False
            if not self.active_scene:
                self.original_scene = current
            self.active_scene = target
            self.pending = False
            self.scene_switched.emit(target, label or "Protection")
            return True

        # No requests left: restore if the arbiter's scene is still showing
        if self.active_scene:
            if not self.obs.switch_scene(self.original_scene):
                self.pending = True
                return False
            self.log_entry.emit(f"Scene restored -> '{self.original_scene}'")
            self.scene_switched.emit(self.original_scene, "Recovered")
        self.active_scene = ""
        self.original_scene = ""
        self.pending = False
        return True
