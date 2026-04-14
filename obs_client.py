"""
OBS WebSocket client for scene management.
Uses obs-websocket built into OBS 28+ (WebSocket 5.x protocol).
"""
import threading
import obsws_python as obs
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, QThread
import time


class OBSClient(QThread):
    """Manages connection to OBS and provides scene control."""

    connected = pyqtSignal(bool, str)  # is_connected, message
    scene_changed = pyqtSignal(str)  # scene_name
    scene_list_updated = pyqtSignal(list)  # [scene_names]
    transition_list_updated = pyqtSignal(list)  # [transition_names]
    source_items_updated = pyqtSignal(str, list)  # scene_name, [(id, name, visible)]
    error = pyqtSignal(str)

    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.cfg = config_manager
        self._ws = None
        self._running = False
        self._connected = False
        self._current_scene = ""
        self._scene_list = []
        self._transition_list = []
        self._lock = threading.Lock()  # Protects all _ws access

    @property
    def is_connected(self):
        return self._connected

    @property
    def current_scene(self):
        return self._current_scene

    @property
    def scenes(self):
        return list(self._scene_list)

    def run(self):
        self._running = True
        while self._running:
            if not self._connected:
                self._try_connect()
            else:
                self._poll_state()

            interval = self.cfg.get("obs", "reconnect_interval_s") if not self._connected else 1
            self.msleep(int(interval * 1000))

        self._disconnect()

    def _try_connect(self):
        try:
            host = self.cfg.get("obs", "host")
            port = self.cfg.get("obs", "port")
            password = self.cfg.get("obs", "password")

            ws = obs.ReqClient(
                host=host,
                port=port,
                password=password if password else None,
                timeout=5,
            )

            with self._lock:
                self._ws = ws
                self._connected = True

            self.connected.emit(True, f"Connected to OBS at {host}:{port}")
            self._refresh_scene_list()
            self._refresh_transitions()
            self._poll_current_scene()

        except Exception as e:
            with self._lock:
                self._connected = False
                self._ws = None
            self.connected.emit(False, f"OBS: {str(e)[:80]}")

    def _disconnect(self):
        with self._lock:
            self._connected = False
            if self._ws:
                try:
                    del self._ws
                except Exception:
                    pass
                self._ws = None

    def _poll_state(self):
        try:
            self._poll_current_scene()
        except Exception:
            with self._lock:
                self._connected = False
                self._ws = None
            self.connected.emit(False, "OBS connection lost")

    def _poll_current_scene(self):
        with self._lock:
            ws = self._ws
        if not ws:
            return
        try:
            resp = ws.get_current_program_scene()
            scene = resp.scene_name if hasattr(resp, 'scene_name') else \
                    resp.current_program_scene_name if hasattr(resp, 'current_program_scene_name') else ""
            if scene != self._current_scene:
                self._current_scene = scene
                self.scene_changed.emit(scene)
        except Exception as e:
            raise

    def _refresh_scene_list(self):
        with self._lock:
            ws = self._ws
        if not ws:
            return
        try:
            resp = ws.get_scene_list()
            scenes = resp.scenes if hasattr(resp, 'scenes') else []
            self._scene_list = [s.get("sceneName", s.get("name", "")) for s in scenes]
            self._scene_list.reverse()  # OBS returns in reverse order
            self.scene_list_updated.emit(self._scene_list)
        except Exception as e:
            self.error.emit(f"Failed to get scene list: {e}")

    def _refresh_transitions(self):
        with self._lock:
            ws = self._ws
        if not ws:
            return
        try:
            resp = ws.get_scene_transition_list()
            transitions = resp.transitions if hasattr(resp, 'transitions') else []
            self._transition_list = [t.get("transitionName", t.get("name", "")) for t in transitions]
            self.transition_list_updated.emit(self._transition_list)
        except Exception:
            pass

    # --- Public scene control methods (called from main thread) ---

    def switch_scene(self, scene_name: str, transition: str = None, duration_ms: int = None):
        """Switch OBS to a specific scene, optionally with a custom transition."""
        with self._lock:
            ws = self._ws
            connected = self._connected

        if not ws or not connected:
            self.error.emit(f"Scene switch to '{scene_name}' failed: not connected (ws={ws is not None}, connected={connected})")
            return False
        try:
            use_custom = self.cfg.get("scenes", "use_custom_transition")
            if use_custom and transition is None:
                transition = self.cfg.get("scenes", "transition_name")
                duration_ms = self.cfg.get("scenes", "transition_duration_ms")

            if transition:
                try:
                    ws.set_current_scene_transition(transition_name=transition)
                    if duration_ms:
                        ws.set_current_scene_transition_duration(
                            transition_duration=duration_ms
                        )
                except Exception:
                    pass  # Non-fatal: use whatever transition is set

            ws.set_current_program_scene(scene_name=scene_name)
            self._current_scene = scene_name
            self.scene_changed.emit(scene_name)
            return True
        except TypeError as te:
            # Some obsws_python versions use different param names
            try:
                ws.set_current_program_scene(name=scene_name)
                self._current_scene = scene_name
                self.scene_changed.emit(scene_name)
                return True
            except Exception as e2:
                self.error.emit(f"Scene switch to '{scene_name}' failed (fallback): {e2}")
                return False
        except Exception as e:
            self.error.emit(f"Scene switch to '{scene_name}' failed: {e}")
            return False

    def get_scenes_sync(self) -> list:
        """Synchronous scene list fetch (for settings UI)."""
        return list(self._scene_list)

    def get_transitions_sync(self) -> list:
        return list(self._transition_list)

    def get_scene_items(self, scene_name: str) -> list:
        """Get list of source items in a scene. Returns [(id, name, visible), ...]"""
        with self._lock:
            ws = self._ws
            connected = self._connected
        if not ws or not connected:
            return []
        try:
            resp = ws.get_scene_item_list(scene_name=scene_name)
            items = resp.scene_items if hasattr(resp, 'scene_items') else []
            result = []
            for item in items:
                item_id = item.get("sceneItemId", 0)
                name = item.get("sourceName", "")
                enabled = item.get("sceneItemEnabled", True)
                result.append((item_id, name, enabled))
            return result
        except Exception as e:
            self.error.emit(f"Failed to get scene items: {e}")
            return []

    def set_source_visibility(self, scene_name: str, source_name: str, visible: bool) -> bool:
        """Toggle a specific source's visibility within a scene."""
        with self._lock:
            ws = self._ws
            connected = self._connected
        if not ws or not connected:
            return False
        try:
            # Find the scene item ID for this source
            items = self.get_scene_items(scene_name)
            target_id = None
            for item_id, name, _ in items:
                if name == source_name:
                    target_id = item_id
                    break

            if target_id is None:
                self.error.emit(f"Source '{source_name}' not found in scene '{scene_name}'")
                return False

            ws.set_scene_item_enabled(
                scene_name=scene_name,
                scene_item_id=target_id,
                scene_item_enabled=visible,
            )
            return True
        except Exception as e:
            self.error.emit(f"Source visibility toggle failed: {e}")
            return False

    def get_source_visibility(self, scene_name: str, source_name: str) -> bool | None:
        """Check if a source is currently visible in a scene."""
        items = self.get_scene_items(scene_name)
        for item_id, name, enabled in items:
            if name == source_name:
                return enabled
        return None

    def refresh(self):
        """Force refresh scene and transition lists."""
        with self._lock:
            connected = self._connected
        if connected:
            self._refresh_scene_list()
            self._refresh_transitions()

    def stop(self):
        self._running = False
        self.wait(3000)
