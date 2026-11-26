"""
OBS WebSocket client for scene management.
Uses obs-websocket built into OBS 28+ (WebSocket 5.x protocol) via obsws-python.

Threading: the OBS poll loop runs on this QThread, while scene switches and
source toggles are requested from the main (GUI) thread. obsws-python's
ReqClient is a single websocket that sends a request and then reads "the next"
response, so two threads using it at once can read each other's replies.
Every request therefore goes through _req(), which holds an RLock for the
whole send/receive.

Events (v1.2.0): a second websocket (obsws-python EventClient, its own
thread) delivers CurrentProgramSceneChanged and scene list changes the moment
they happen, so a manual scene change during an outage is seen immediately.
The request connection is then only polled every KEEPALIVE_POLL_S as a health
check. If the event connection can't be opened, the 1-second polling of
v1.1.0 is used instead.
"""
import threading

import obsws_python as obs
from obsws_python.error import OBSSDKRequestError
from obsws_python.subs import Subs
from PyQt6.QtCore import QThread, QTimer, pyqtSignal

# Requests that take longer than this mark the connection as lost. Kept short
# because main-thread calls block the UI for up to this long if OBS hangs.
REQUEST_TIMEOUT_S = 3

# Refresh the scene list every N one-second polls so scenes added in OBS
# after connecting show up in the settings combos without a restart.
SCENE_LIST_REFRESH_POLLS = 15

# Poll interval while the event connection is up (health check only)
KEEPALIVE_POLL_S = 5


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
        # RLock: set_source_visibility calls get_scene_items while holding it
        self._lock = threading.RLock()
        self._polls_since_scene_refresh = 0
        # (name, duration_ms) of the user's own transition while a custom one is
        # temporarily active for an auto-switch; None when nothing to restore.
        self._transition_restore = None
        # Event connection (None when unavailable -> 1 s polling)
        self._events = None
        self._scene_list_dirty = False
        self._obs_exiting = False

    @property
    def is_connected(self):
        return self._connected

    @property
    def current_scene(self):
        return self._current_scene

    @property
    def scenes(self):
        return list(self._scene_list)

    # --- Thread loop ---

    def run(self):
        self._running = True
        while self._running:
            if not self._connected:
                self._try_connect()
            else:
                self._poll_state()

            if not self._connected:
                interval_s = self.cfg.get("obs", "reconnect_interval_s")
            else:
                interval_s = KEEPALIVE_POLL_S if self._events_alive() else 1
            self._interruptible_sleep(interval_s)

        self._disconnect()

    def _interruptible_sleep(self, seconds: float):
        # Reconnect interval can be up to 300 s; sleeping it in one go made stop()
        # time out and left the thread running at process exit.
        remaining_ms = int(seconds * 1000)
        # Also wake early when an event asks for work (scene list change, OBS exiting)
        while self._running and remaining_ms > 0 and not self._scene_list_dirty \
                and not self._obs_exiting:
            step = min(100, remaining_ms)
            self.msleep(step)
            remaining_ms -= step

    def _try_connect(self):
        try:
            host = self.cfg.get("obs", "host")
            port = self.cfg.get("obs", "port")
            password = self.cfg.get("obs", "password")

            ws = obs.ReqClient(
                host=host,
                port=port,
                password=password if password else None,
                timeout=REQUEST_TIMEOUT_S,
            )

            with self._lock:
                self._ws = ws
                self._connected = True

            events_ok = self._start_events(host, port, password)
            self.connected.emit(
                True,
                f"Connected to OBS at {host}:{port}" + ("" if events_ok else " (polling, no events)"),
            )
            self._refresh_scene_list(force_emit=True)
            self._refresh_transitions()
            self._poll_current_scene()

        except Exception as e:
            with self._lock:
                self._connected = False
                self._ws = None
            self.connected.emit(False, f"OBS: {str(e)[:80]}")

    # --- Event connection ---

    def _start_events(self, host, port, password) -> bool:
        """Open the event websocket. Callbacks run on obsws-python's event
        thread; obsws matches them by function name, hence the on_* names."""
        self._stop_events()
        client = self

        def on_current_program_scene_changed(data):
            try:
                scene = getattr(data, "scene_name", "") or ""
                if scene and scene != client._current_scene:
                    client._current_scene = scene
                    client.scene_changed.emit(scene)
            except Exception:
                pass  # an exception here would kill obsws's event thread

        def on_scene_created(data):
            client._scene_list_dirty = True

        def on_scene_removed(data):
            client._scene_list_dirty = True

        def on_scene_name_changed(data):
            client._scene_list_dirty = True

        def on_exit_started(data):
            # OBS is closing; the poll thread drops the connection (never call
            # EventClient.disconnect from its own thread: it joins itself)
            client._obs_exiting = True

        try:
            ev = obs.EventClient(
                host=host, port=port, password=password if password else None,
                timeout=REQUEST_TIMEOUT_S, subs=Subs.GENERAL | Subs.SCENES,
            )
            ev.callback.register([
                on_current_program_scene_changed, on_scene_created, on_scene_removed,
                on_scene_name_changed, on_exit_started,
            ])
            self._events = ev
            return True
        except Exception as e:
            self._events = None
            self.error.emit(f"OBS events unavailable, using 1 s polling: {str(e)[:80]}")
            return False

    def _events_alive(self) -> bool:
        ev = self._events
        worker = getattr(ev, "worker", None) if ev else None
        return bool(worker and worker.is_alive())

    def _stop_events(self):
        # Flags only mean something for a live connection; leaving them set
        # would make the reconnect sleep return immediately (busy loop).
        self._scene_list_dirty = False
        self._obs_exiting = False
        ev, self._events = self._events, None
        if ev is None:
            return
        try:
            ev.base_client.ws.close()
            if threading.current_thread() is not ev.worker:
                ev.worker.join(timeout=2)
        except Exception:
            pass

    def _disconnect(self):
        with self._lock:
            self._connected = False
            ws, self._ws = self._ws, None
        self._stop_events()
        if ws:
            try:
                ws.disconnect()
            except Exception:
                pass

    def _mark_lost(self, reason: str):
        with self._lock:
            was_connected = self._connected
            self._connected = False
            ws, self._ws = self._ws, None
        self._stop_events()
        if ws:
            try:
                ws.disconnect()
            except Exception:
                pass
        if was_connected:
            self.connected.emit(False, reason)

    def _req(self, method: str, *args):
        """Run one ReqClient call under the lock.

        A request error (e.g. scene not found) is re-raised and leaves the
        connection up. Anything else (socket closed, timeout) means the
        connection is unusable: after a timeout the late reply would be read as
        the answer to the next request, so the socket is dropped and the poll
        loop reconnects.
        """
        with self._lock:
            ws = self._ws
            if ws is None or not self._connected:
                raise ConnectionError("not connected to OBS")
            try:
                return getattr(ws, method)(*args)
            except OBSSDKRequestError:
                raise
            except Exception as e:
                lost = e
        self._mark_lost(f"OBS connection lost ({type(lost).__name__})")
        raise ConnectionError(str(lost))

    def _poll_state(self):
        if self._obs_exiting:
            self._mark_lost("OBS is closing")
            return
        try:
            self._poll_current_scene()
            if self._scene_list_dirty:
                self._scene_list_dirty = False
                self._polls_since_scene_refresh = 0
                self._refresh_scene_list()
            elif not self._events_alive():
                # No events: refresh the list periodically instead
                self._polls_since_scene_refresh += 1
                if self._polls_since_scene_refresh >= SCENE_LIST_REFRESH_POLLS:
                    self._polls_since_scene_refresh = 0
                    self._refresh_scene_list()
        except ConnectionError:
            pass  # _req already marked the connection lost and emitted
        except Exception:
            self._mark_lost("OBS connection lost")

    def _poll_current_scene(self):
        resp = self._req("get_current_program_scene")
        scene = getattr(resp, "scene_name", None) or \
            getattr(resp, "current_program_scene_name", "") or ""
        if scene != self._current_scene:
            self._current_scene = scene
            self.scene_changed.emit(scene)

    def _refresh_scene_list(self, force_emit: bool = False):
        try:
            resp = self._req("get_scene_list")
            scenes = getattr(resp, "scenes", []) or []
            names = [s.get("sceneName", s.get("name", "")) for s in scenes]
            names.reverse()  # OBS returns bottom-to-top
            if force_emit or names != self._scene_list:
                self._scene_list = names
                self.scene_list_updated.emit(list(names))
        except ConnectionError:
            raise
        except Exception as e:
            self.error.emit(f"Failed to get scene list: {e}")

    def _refresh_transitions(self):
        try:
            resp = self._req("get_scene_transition_list")
            transitions = getattr(resp, "transitions", []) or []
            self._transition_list = [t.get("transitionName", t.get("name", "")) for t in transitions]
            self.transition_list_updated.emit(list(self._transition_list))
        except Exception:
            pass

    # --- Public scene control methods (called from main thread) ---

    def switch_scene(self, scene_name: str) -> bool:
        """Switch OBS to a scene, using the custom transition if configured.

        The user's own transition is put back once the switch has finished,
        so auto-switches no longer permanently change it.
        """
        if not self._connected:
            self.error.emit(f"Scene switch to '{scene_name}' failed: not connected to OBS")
            return False
        try:
            use_custom = self.cfg.get("scenes", "use_custom_transition")
            custom_name = self.cfg.get("scenes", "transition_name") if use_custom else ""
            custom_ms = self.cfg.get("scenes", "transition_duration_ms") if use_custom else 0

            if custom_name:
                self._activate_custom_transition(custom_name, custom_ms)

            # obsws-python signatures are positional-name based: (name), not (scene_name=)
            self._req("set_current_program_scene", scene_name)
            self._current_scene = scene_name
            self.scene_changed.emit(scene_name)

            if custom_name and self._transition_restore:
                # Restore after the transition has played. Changing the current
                # transition mid-transition is avoided on purpose.
                QTimer.singleShot(int(custom_ms or 0) + 500, self._restore_user_transition)
            return True
        except Exception as e:
            self.error.emit(f"Scene switch to '{scene_name}' failed: {e}")
            return False

    def _activate_custom_transition(self, name: str, duration_ms: int):
        try:
            if self._transition_restore is None:
                cur = self._req("get_current_scene_transition")
                cur_name = getattr(cur, "transition_name", "") or ""
                cur_ms = getattr(cur, "transition_duration", None)
                if cur_name and (cur_name != name or (duration_ms and cur_ms != duration_ms)):
                    self._transition_restore = (cur_name, cur_ms)
            self._req("set_current_scene_transition", name)
            if duration_ms:
                self._req("set_current_scene_transition_duration", int(duration_ms))
        except Exception as e:
            # Non-fatal: the switch still happens with whatever transition is active
            self.error.emit(f"Custom transition '{name}' not applied: {e}")

    def _restore_user_transition(self):
        restore, self._transition_restore = self._transition_restore, None
        if not restore or not self._connected:
            return
        name, duration_ms = restore
        try:
            self._req("set_current_scene_transition", name)
            if duration_ms:
                self._req("set_current_scene_transition_duration", int(duration_ms))
        except Exception as e:
            self.error.emit(f"Could not restore transition '{name}': {e}")

    def get_scenes_sync(self) -> list:
        """Synchronous scene list fetch (for settings UI)."""
        return list(self._scene_list)

    def get_transitions_sync(self) -> list:
        return list(self._transition_list)

    def get_scene_items(self, scene_name: str) -> list:
        """Get list of source items in a scene. Returns [(id, name, visible), ...]"""
        if not self._connected:
            return []
        try:
            resp = self._req("get_scene_item_list", scene_name)
            items = getattr(resp, "scene_items", []) or []
            return [
                (item.get("sceneItemId", 0), item.get("sourceName", ""), item.get("sceneItemEnabled", True))
                for item in items
            ]
        except Exception as e:
            self.error.emit(f"Failed to get scene items for '{scene_name}': {e}")
            return []

    def _find_item(self, scene_name: str, source_name: str):
        """Return (item_id, enabled) for a source in a scene, or None."""
        for item_id, name, enabled in self.get_scene_items(scene_name):
            if name == source_name:
                return item_id, enabled
        return None

    def set_source_visibility(self, scene_name: str, source_name: str, visible: bool) -> bool:
        """Show or hide a source within a scene."""
        if not self._connected:
            return False
        found = self._find_item(scene_name, source_name)
        if found is None:
            self.error.emit(f"Source '{source_name}' not found in scene '{scene_name}'")
            return False
        try:
            self._req("set_scene_item_enabled", scene_name, found[0], bool(visible))
            return True
        except Exception as e:
            self.error.emit(f"Source visibility toggle failed: {e}")
            return False

    def get_source_visibility(self, scene_name: str, source_name: str) -> bool | None:
        """Current visibility of a source in a scene, or None if not found."""
        found = self._find_item(scene_name, source_name)
        return None if found is None else bool(found[1])

    def refresh(self):
        """Force refresh scene and transition lists."""
        if self._connected:
            try:
                self._refresh_scene_list(force_emit=True)
            except ConnectionError:
                return
            self._refresh_transitions()

    def stop(self):
        self._running = False
        # Loop sleeps in 100 ms slices; one in-flight request can take up to
        # REQUEST_TIMEOUT_S, so this wait covers the worst case.
        self.wait((REQUEST_TIMEOUT_S + 2) * 1000)
