"""
Main window: ties together all components with tabs, menus, system tray, and global hotkey.

Stream Guardian 2.0 runs one IngestPoller (own thread) and one BitrateEngine
per configured stream. All engines live on a single engine thread and share
one SceneArbiter, so scene switches from different streams can't conflict.
The window only talks to engines through queued signals (EngineControl) and
reads their data from the stats_updated payload.
"""
import sys
import os
import threading
import webbrowser

from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QApplication, QSystemTrayIcon,
    QMenu, QMenuBar, QStatusBar, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer, QThread, QUrl, QObject, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

from config_manager import ConfigManager, APP_NAME, APP_INTERNAL_NAME, APP_VERSION
from ingest_client import IngestPoller
from obs_client import OBSClient
from bitrate_engine import BitrateEngine
from scene_arbiter import SceneArbiter
from overlay_server import OverlayServer
from updater import UpdateChecker
import autostart
from gui.dashboard import DashboardTab
from gui.settings_tab import SettingsTab
from gui.presets_tab import PresetsTab
from gui.log_tab import LogTab
from gui.global_hotkey import GlobalHotkey
from gui.themes import DARK_STYLESHEET, LIGHT_STYLESHEET, COLORS_DARK


def make_icon():
    """Load the app icon from file, falling back to a procedural one."""
    # Try to load icon.png / icon.ico from the app directory
    import sys
    search_dirs = [os.path.dirname(os.path.abspath(sys.argv[0]))]
    # PyInstaller single-file exe extracts data to _MEIPASS temp dir
    if getattr(sys, '_MEIPASS', None):
        search_dirs.insert(0, sys._MEIPASS)

    for base_dir in search_dirs:
        for name in ("icon.png", "icon.ico"):
            path = os.path.join(base_dir, name)
            if os.path.isfile(path):
                icon = QIcon(path)
                if not icon.isNull():
                    return icon

    # Fallback: procedural purple S-diamond
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    painter.setBrush(QColor("#9100ff"))
    painter.setPen(Qt.PenStyle.NoPen)
    from PyQt6.QtGui import QPolygon
    from PyQt6.QtCore import QPoint
    # Upper-right chevron
    painter.drawPolygon(QPolygon([
        QPoint(8, 28), QPoint(32, 4), QPoint(56, 28), QPoint(36, 28),
    ]))
    # Lower-left chevron
    painter.drawPolygon(QPolygon([
        QPoint(8, 36), QPoint(28, 36), QPoint(32, 60), QPoint(56, 36),
    ]))

    painter.end()
    return QIcon(pixmap)


class EngineControl(QObject):
    """Per-engine request signals. Emitting one queues the call into the
    engine thread (the engine's slots run there)."""
    sig_override = pyqtSignal(bool)
    sig_reset = pyqtSignal()
    sig_logging = pyqtSignal(bool)
    sig_simulate = pyqtSignal(str)
    sig_retire = pyqtSignal()


class StreamRuntime:
    """Everything running for one configured stream."""

    def __init__(self, stream_id, view, poller, engine, control):
        self.id = stream_id
        self.view = view
        self.poller = poller
        self.engine = engine
        self.control = control
        self.stats = {}
        self.srs_status = (False, "Disconnected")
        self.stream_status = (False, "No stream detected")
        self.test = ""

    @property
    def name(self) -> str:
        return self.view.name


class MainWindow(QMainWindow):
    # Background Fetch Sources result: scene, state_key, items
    _sources_ready = pyqtSignal(str, str, list)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(720, 480)
        self.resize(1100, 720)

        # Core components
        self.cfg = ConfigManager()
        self.obs = OBSClient(self.cfg)

        # Engine thread shared by every stream's engine, with one scene arbiter
        self.engine_thread = QThread(self)
        self.engine_thread.setObjectName("StreamGuardianEngine")
        self.arbiter = SceneArbiter(self.obs)
        self.arbiter.moveToThread(self.engine_thread)
        self.runtimes = {}   # stream id -> StreamRuntime, in config order
        self.selected_id = self.cfg.streams()[0]["id"]
        self._override = False

        # Alert sounds are played here: QMediaPlayer belongs on the GUI thread
        self._init_audio()

        self.overlay = OverlayServer()
        self.updater = UpdateChecker(APP_VERSION, parent=self)
        self._update_manual = False

        # Icon
        self._icon = make_icon()
        self.setWindowIcon(self._icon)

        # Build UI
        self._build_menus()
        self._build_tabs()
        self._build_statusbar()
        self._build_tray()
        self._build_hotkey()

        # Connect signals
        self._connect_signals()

        if self.cfg.load_message:
            self.tab_log.append(f"CONFIG: {self.cfg.load_message}")

        # Apply theme
        self._apply_theme()

        # Always-on-top flag (main.py decides whether to show the window)
        if self.cfg.get("gui", "always_on_top"):
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        # Start services: engine thread first so no sample arrives before it runs
        self.engine_thread.start()
        self._sync_streams()
        self.obs.start()
        self._apply_overlay()
        self._apply_autostart()
        if self.cfg.get("startup", "check_updates"):
            QTimer.singleShot(5000, self.updater.check)

    # --- Audio ---

    def _init_audio(self):
        try:
            self._audio_player = QMediaPlayer(self)
            self._audio_output = QAudioOutput(self)
            self._audio_player.setAudioOutput(self._audio_output)
        except Exception:
            self._audio_player = None
            self._audio_output = None

    def _play_audio(self, file_path: str, volume: int):
        if not self._audio_player:
            return
        try:
            self._audio_output.setVolume(max(0, min(volume, 100)) / 100.0)
            self._audio_player.setSource(QUrl.fromLocalFile(file_path))
            self._audio_player.play()
        except Exception as e:
            self.tab_log.append(f"Audio playback error: {e}")

    # --- UI construction ---

    def _build_menus(self):
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("&File")

        act_reset_session = QAction("Reset Session Stats", self)
        act_reset_session.triggered.connect(self._reset_session)
        file_menu.addAction(act_reset_session)

        file_menu.addSeparator()

        act_quit = QAction("E&xit", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self._quit)
        file_menu.addAction(act_quit)

        # View menu
        view_menu = menubar.addMenu("&View")

        self.act_dark = QAction("Dark Theme", self)
        self.act_dark.setCheckable(True)
        self.act_dark.triggered.connect(lambda: self._set_theme("dark"))

        self.act_light = QAction("Light Theme", self)
        self.act_light.setCheckable(True)
        self.act_light.triggered.connect(lambda: self._set_theme("light"))

        view_menu.addAction(self.act_dark)
        view_menu.addAction(self.act_light)
        view_menu.addSeparator()

        self.act_on_top = QAction("Always on Top", self)
        self.act_on_top.setCheckable(True)
        self.act_on_top.setChecked(self.cfg.get("gui", "always_on_top"))
        self.act_on_top.triggered.connect(self._toggle_on_top)
        view_menu.addAction(self.act_on_top)

        # Help menu
        help_menu = menubar.addMenu("&Help")
        act_update = QAction("Check for Updates...", self)
        act_update.triggered.connect(self._check_updates_manual)
        help_menu.addAction(act_update)
        act_about = QAction(f"About {APP_NAME}", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _build_tabs(self):
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.tab_dashboard = DashboardTab(self.cfg.stream_view(self.selected_id))
        self.tab_settings = SettingsTab(self.cfg)
        self.tab_presets = PresetsTab(self.cfg)
        self.tab_log = LogTab()

        self.tabs.addTab(self.tab_dashboard, "Dashboard")
        self.tabs.addTab(self.tab_settings, "Settings")
        self.tabs.addTab(self.tab_presets, "Presets")
        self.tabs.addTab(self.tab_log, "Log")

    def _build_statusbar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Starting up...")

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = None
            return

        self._tray = QSystemTrayIcon(self._icon, self)

        tray_menu = QMenu()
        act_show = tray_menu.addAction("Show")
        act_show.triggered.connect(self._show_from_tray)

        self._tray_override_action = tray_menu.addAction("Turn Auto-Switch OFF")
        self._tray_override_action.triggered.connect(self._toggle_override_from_tray)

        tray_menu.addSeparator()
        act_quit = tray_menu.addAction("Quit")
        act_quit.triggered.connect(self._quit)

        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    @property
    def tray_available(self) -> bool:
        return self._tray is not None

    def _build_hotkey(self):
        """Create the system-wide override hotkey and register it from config."""
        self._hotkey = GlobalHotkey(self, parent=self)
        self._hotkey.activated.connect(self._toggle_override_hotkey)
        self._register_hotkey()

    def _register_hotkey(self):
        try:
            self.tab_log.append(self._hotkey.register(self.cfg.get("override", "hotkey")))
        except Exception as e:
            self.tab_log.append(f"Failed to register hotkey: {e}")

    def _connect_signals(self):
        # OBS signals
        self.obs.connected.connect(self._on_obs_status)
        self.obs.scene_changed.connect(self._on_scene_changed)
        self.obs.scene_list_updated.connect(self._on_scene_list)
        self.obs.transition_list_updated.connect(self._on_transition_list)
        self.obs.error.connect(lambda msg: self.tab_log.append(f"OBS ERROR: {msg}"))

        # Scene arbiter (shared by all streams, lives on the engine thread)
        self.arbiter.log_entry.connect(self.tab_log.append)
        self.arbiter.scene_switched.connect(self._on_scene_switch)

        # Rehearsal buttons act on the stream shown on the dashboard
        self.tab_dashboard.btn_test_low.clicked.connect(lambda: self._simulate("low"))
        self.tab_dashboard.btn_test_disc.clicked.connect(lambda: self._simulate("disconnect"))
        self.tab_dashboard.btn_test_end.clicked.connect(lambda: self._simulate(""))
        self.tab_dashboard.stream_selected.connect(self._select_stream)

        # Settings
        self.tab_settings.settings_changed.connect(self._on_settings_changed)
        self.tab_settings.request_sources.connect(self._on_request_sources)
        self._sources_ready.connect(self._on_sources_ready)
        self.tab_presets.preset_loaded.connect(self._on_preset_loaded)

        # Override toggle
        self.tab_dashboard.btn_override.toggled.connect(self._on_override_toggle)

        # Log toggle from dashboard
        self.tab_dashboard.btn_log_toggle.toggled.connect(self._on_log_toggle)

        # Updates
        self.updater.result.connect(self._on_update_result)
        self.updater.failed.connect(self._on_update_failed)

    # --- Streams ---

    def _sync_streams(self):
        """Start runtimes for new streams, retire removed ones, keep order."""
        configured = [s["id"] for s in self.cfg.streams()]
        for sid in list(self.runtimes):
            if sid not in configured:
                self._stop_stream(sid)
        for sid in configured:
            if sid not in self.runtimes:
                self._start_stream(sid)
        self.runtimes = {sid: self.runtimes[sid] for sid in configured}

        multi = len(self.runtimes) > 1
        for rt in self.runtimes.values():
            rt.engine.prefix_logs = multi  # plain bool, read on the engine thread
        if self.selected_id not in self.runtimes:
            self.selected_id = configured[0]
        self.overlay.set_order(configured)
        self.tab_dashboard.set_streams([(sid, rt.name) for sid, rt in self.runtimes.items()],
                                       self.selected_id)
        self._select_stream(self.selected_id)

    def _start_stream(self, sid: str):
        view = self.cfg.stream_view(sid)
        poller = IngestPoller(view)
        engine = BitrateEngine(view, self.obs, arbiter=self.arbiter)
        engine.prefix_logs = len(self.cfg.streams()) > 1
        engine.moveToThread(self.engine_thread)
        control = EngineControl(self)
        rt = StreamRuntime(sid, view, poller, engine, control)
        self.runtimes[sid] = rt

        # Ingest data straight to the engine thread (queued)
        poller.bitrate_update.connect(engine.process_bitrate)
        poller.stream_online.connect(engine.set_stream_online)
        poller.link_stats.connect(engine.set_link_stats)
        # Window updates (bound methods of this window: delivered on the GUI thread)
        poller.connection_status.connect(self._on_srs_status)
        poller.stream_online.connect(self._on_stream_status)
        poller.error.connect(self._on_poller_error)
        engine.state_changed.connect(self._on_state_changed)
        engine.stats_updated.connect(self._on_stats)
        engine.source_toggle_triggered.connect(self._on_source_toggle)
        engine.log_entry.connect(self.tab_log.append)
        engine.play_audio.connect(self._play_audio)
        engine.test_mode_changed.connect(self._on_test_mode)

        control.sig_override.connect(engine.set_override)
        control.sig_reset.connect(engine.reset_session)
        control.sig_logging.connect(engine.set_logging)
        control.sig_simulate.connect(engine.simulate)
        control.sig_retire.connect(engine.retire)

        if self._override:
            control.sig_override.emit(True)
        if self.cfg.get("logging", "enabled"):
            control.sig_logging.emit(True)
        poller.start()

    def _stop_stream(self, sid: str):
        rt = self.runtimes.pop(sid, None)
        if not rt:
            return
        rt.poller.stop()
        # Undo its OBS actions and close its CSV on the engine thread, then free it
        rt.control.sig_retire.emit()
        rt.engine.deleteLater()
        self.tab_log.append(f"Stream '{rt.name}' removed")

    def _runtime_for_sender(self):
        src = self.sender()
        for rt in self.runtimes.values():
            if src is rt.engine or src is rt.poller:
                return rt
        return None

    def _select_stream(self, sid: str):
        if sid not in self.runtimes:
            return
        self.selected_id = sid
        rt = self.runtimes[sid]
        self.tab_dashboard.set_view(rt.view)
        self.tab_dashboard.srs_status.set_status(*rt.srs_status)
        self.tab_dashboard.stream_status.set_status(*rt.stream_status)
        self.tab_dashboard.set_test_mode(rt.test)
        if rt.stats:
            self._render_stats(rt.stats)

    def _simulate(self, kind: str):
        rt = self.runtimes.get(self.selected_id)
        if rt:
            rt.control.sig_simulate.emit(kind)

    # --- Signal handlers ---

    def _on_stats(self, stats: dict):
        sid = stats.get("stream_id")
        rt = self.runtimes.get(sid)
        if not rt:
            return
        rt.stats = stats
        self.overlay.update(sid, {
            "name": rt.name,
            "state": stats.get("state", "NORMAL"),
            "test": stats.get("test", ""),
            "current_kbps": stats.get("current_kbps", 0),
            "average_kbps": stats.get("average_kbps", 0),
            "link": stats.get("link", {}),
        })
        self.tab_dashboard.update_stream_chip(sid, rt.name, stats.get("state", "NORMAL"),
                                              bool(stats.get("test")))
        if sid == self.selected_id:
            self._render_stats(stats)

    def _render_stats(self, stats: dict):
        self.tab_dashboard.update_stats(stats)
        self.tab_dashboard.update_graph(stats.get("graph", []), stats.get("average_kbps", 0))

    def _on_srs_status(self, connected: bool, message: str):
        rt = self._runtime_for_sender()
        if not rt:
            return
        rt.srs_status = (connected, message)
        if rt.id == self.selected_id:
            self.tab_dashboard.srs_status.set_status(connected, message)
        if connected:
            self.status_bar.showMessage("Ingest connected | Monitoring bitrate")

    def _on_stream_status(self, online: bool, stream_id: str):
        rt = self._runtime_for_sender()
        if not rt:
            return
        rt.stream_status = (True, stream_id or "online") if online else (False, "No stream detected")
        if rt.id == self.selected_id:
            self.tab_dashboard.stream_status.set_status(*rt.stream_status)

    def _on_poller_error(self, msg: str):
        rt = self._runtime_for_sender()
        prefix = f"[{rt.name}] " if rt and len(self.runtimes) > 1 else ""
        self.tab_log.append(f"{prefix}INGEST ERROR: {msg}")

    def _on_obs_status(self, connected: bool, message: str):
        self.tab_dashboard.obs_status.set_status(connected, message)
        if connected:
            self.tab_log.append(f"OBS: {message}")

    def _on_scene_changed(self, scene: str):
        self.tab_dashboard.update_scene(scene)

    def _on_scene_list(self, scenes: list):
        self.tab_settings.update_scene_combos(scenes)

    def _on_transition_list(self, transitions: list):
        self.tab_settings.update_transition_combos(transitions)

    def _on_request_sources(self, scene_name: str, state_key: str):
        """Fetch source items from OBS on a worker thread (an OBS request can
        take up to 3 s if OBS hangs) and deliver them to Settings."""
        def work():
            items = self.obs.get_scene_items(scene_name)
            self._sources_ready.emit(scene_name, state_key, items)
        threading.Thread(target=work, daemon=True, name="FetchSources").start()
        self.status_bar.showMessage(f"Fetching sources from '{scene_name}'...", 3000)

    def _on_sources_ready(self, scene_name: str, state_key: str, items: list):
        self.tab_settings.update_source_list(state_key, items)
        self.tab_log.append(f"Fetched {len(items)} sources from scene '{scene_name}'")

    def _on_state_changed(self, new_state: str, old_state: str):
        rt = self._runtime_for_sender()
        name = f"{rt.name}: " if rt and len(self.runtimes) > 1 else ""
        self.tab_log.append(f"State: {name}{old_state} → {new_state}")
        if self._tray:
            suffix = " (test)" if rt and rt.test else ""
            self._tray.showMessage(
                APP_NAME,
                f"{name}Stream state: {new_state.replace('_', ' ').title()}{suffix}",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    def _on_scene_switch(self, scene: str, reason: str):
        self.tab_log.append(f"AUTO-SWITCH → {scene} ({reason})")

    def _on_source_toggle(self, scene: str, source: str, visible: bool, reason: str):
        action = "shown" if visible else "hidden"
        self.tab_log.append(f"SOURCE TOGGLE: '{source}' in '{scene}' {action} ({reason})")

    def _on_log_toggle(self, enabled: bool):
        """Start or stop CSV bitrate logging for every stream."""
        for rt in self.runtimes.values():
            rt.control.sig_logging.emit(enabled)

    def _on_test_mode(self, state_name: str):
        rt = self._runtime_for_sender()
        if not rt:
            return
        rt.test = state_name
        if rt.id == self.selected_id:
            self.tab_dashboard.set_test_mode(state_name)

    # --- Override ---

    def _on_override_toggle(self, enabled: bool):
        # Engine-held, not saved to config: a restart always resumes protection
        self._override = enabled
        for rt in self.runtimes.values():
            rt.control.sig_override.emit(enabled)
        if self._tray:
            self._tray_override_action.setText(
                "Turn Auto-Switch ON" if enabled else "Turn Auto-Switch OFF"
            )
        btn = self.tab_dashboard.btn_override
        if enabled:
            btn.setText("⏸ Auto-Switch OFF")
            btn.setStyleSheet(
                "background-color: #e04040; color: white; border: none; "
                "border-radius: 4px; padding: 7px 18px;"
            )
            self.tab_log.append("Manual override ENABLED — auto-switching disabled")
        else:
            btn.setText("⏸ Auto-Switch ON")
            btn.setStyleSheet("")
            self.tab_log.append("Manual override DISABLED — auto-switching active")

    def _toggle_override_hotkey(self):
        """Called by global hotkey shortcut."""
        btn = self.tab_dashboard.btn_override
        btn.setChecked(not btn.isChecked())

    def _toggle_override_from_tray(self):
        """Called from tray context menu."""
        btn = self.tab_dashboard.btn_override
        btn.setChecked(not btn.isChecked())

    # --- Settings ---

    def _on_settings_changed(self):
        self.tab_log.append("Settings updated")
        self._report_save_error()
        self._apply_theme()

        # Streams added/removed/renamed in Settings
        self._sync_streams()

        # Refresh dashboard ingest label (SRS / MediaMTX / Ingest)
        self.tab_dashboard.refresh_ingest_label()

        # Re-register hotkey (takes effect immediately, no restart needed)
        self._register_hotkey()

        # Graph line visibility options
        self.tab_dashboard.refresh_graph_options()

        # Sync dashboard log button with settings
        log_enabled = self.cfg.get("logging", "enabled")
        self.tab_dashboard.btn_log_toggle.blockSignals(True)
        self.tab_dashboard.btn_log_toggle.setChecked(log_enabled)
        self.tab_dashboard._update_log_btn(log_enabled)
        self.tab_dashboard.btn_log_toggle.blockSignals(False)

        # Restart logging (new file) or stop it, on the engine thread
        for rt in self.runtimes.values():
            rt.control.sig_logging.emit(log_enabled)

        # Sync graph window spinner
        self.tab_dashboard.spin_graph_window.blockSignals(True)
        self.tab_dashboard.spin_graph_window.setValue(self.cfg.get("graph", "history_seconds"))
        self.tab_dashboard.spin_graph_window.blockSignals(False)

        # Update always-on-top
        on_top = self.cfg.get("gui", "always_on_top")
        self.act_on_top.setChecked(on_top)
        self._apply_on_top(on_top)

        self._apply_overlay()
        self._apply_autostart()

    def _report_save_error(self):
        if self.cfg.save_error:
            self.tab_log.append(f"CONFIG ERROR: {self.cfg.save_error}")

    def _on_preset_loaded(self):
        # Run the full settings-changed path so streams, logging, hotkey,
        # always-on-top and graph options follow the preset.
        self.tab_settings.reload_streams()
        self._on_settings_changed()
        self.tab_log.append("Preset loaded, settings refreshed")

    def _apply_overlay(self):
        want = self.cfg.get("overlay", "enabled")
        port = int(self.cfg.get("overlay", "port"))
        lan = bool(self.cfg.get("overlay", "allow_lan"))
        host = "0.0.0.0" if lan else "127.0.0.1"
        if want and (not self.overlay.running or self.overlay.bound != (host, port)):
            err = self.overlay.start(port, lan)
            if err:
                self.tab_log.append(f"OVERLAY ERROR: {err}")
            else:
                self.tab_log.append(f"Overlay running: http://127.0.0.1:{port}/"
                                    + (" (also reachable from your network)" if lan else ""))
        elif not want and self.overlay.running:
            self.overlay.stop()
            self.tab_log.append("Overlay stopped")

    def _apply_autostart(self):
        if not autostart.supported():
            return
        enabled = bool(self.cfg.get("startup", "start_with_windows"))
        err = autostart.sync(enabled)
        if err:
            self.tab_log.append(f"Start with Windows: {err}")

    # --- Updates ---

    def _check_updates_manual(self):
        self._update_manual = True
        self.status_bar.showMessage("Checking for updates...", 5000)
        self.updater.check()

    def _on_update_result(self, tag: str, url: str):
        manual, self._update_manual = self._update_manual, False
        if not tag:
            if manual:
                QMessageBox.information(self, APP_NAME, f"You have the latest version ({APP_VERSION}).")
            return
        msg = f"{APP_NAME} {tag} is available (you have {APP_VERSION})."
        self.tab_log.append(f"UPDATE: {msg} {url}")
        self.status_bar.showMessage(msg, 15000)
        if manual:
            if QMessageBox.question(self, APP_NAME, msg + "\n\nOpen the download page?") \
                    == QMessageBox.StandardButton.Yes:
                webbrowser.open(url)
        elif self._tray:
            self._tray.showMessage(APP_NAME, msg + " See Help > Check for Updates.",
                                   QSystemTrayIcon.MessageIcon.Information, 8000)

    def _on_update_failed(self, err: str):
        manual, self._update_manual = self._update_manual, False
        if manual:
            QMessageBox.warning(self, APP_NAME, f"Could not check for updates:\n{err}")

    # --- Theme ---

    def _apply_theme(self):
        theme = self.cfg.get("gui", "theme")
        if theme == "dark":
            QApplication.instance().setStyleSheet(DARK_STYLESHEET)
            self.act_dark.setChecked(True)
            self.act_light.setChecked(False)
        else:
            QApplication.instance().setStyleSheet(LIGHT_STYLESHEET)
            self.act_dark.setChecked(False)
            self.act_light.setChecked(True)
        self.tab_dashboard.refresh_theme()

    def _set_theme(self, theme: str):
        self.cfg.set("gui", "theme", theme)
        self.cfg.save()
        self._apply_theme()

    def _toggle_on_top(self, checked):
        self.cfg.set("gui", "always_on_top", checked)
        self.cfg.save()
        self._apply_on_top(checked)

    def _apply_on_top(self, on_top):
        flags = self.windowFlags()
        new_flags = (flags | Qt.WindowType.WindowStaysOnTopHint) if on_top \
            else (flags & ~Qt.WindowType.WindowStaysOnTopHint)
        if new_flags == flags:
            return
        # setWindowFlags hides the window; only re-show it if it was visible,
        # so applying settings doesn't pop the window out of the tray.
        was_visible = self.isVisible()
        self.setWindowFlags(new_flags)
        if was_visible:
            self.show()

    # --- Tray ---

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    def _show_from_tray(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def closeEvent(self, event):
        if self.cfg.get("gui", "minimize_to_tray") and self._tray:
            self.hide()
            self._tray.showMessage(
                APP_NAME,
                "Running in system tray. Right-click to quit.",
                QSystemTrayIcon.MessageIcon.Information,
                2000,
            )
            event.ignore()
        else:
            # Accepting the event closes the window but does NOT stop the
            # Qt event loop on its own — background QThreads (ingest, OBS)
            # keep the process alive. Explicitly quit the application so
            # the process actually exits.
            self._shutdown()
            event.accept()
            QApplication.quit()

    # --- Session ---

    def _reset_session(self):
        # Each engine logs a session summary before clearing
        for rt in self.runtimes.values():
            rt.control.sig_reset.emit()
        self.tab_log.append("Session stats reset")

    # --- Quit ---

    def _quit(self):
        self._shutdown()
        QApplication.quit()

    def _shutdown(self):
        # Reached twice on Exit (from _quit, then closeEvent as the app quits)
        if getattr(self, "_shut_down", False):
            return
        self._shut_down = True
        self.tab_log.append("Shutting down...")
        # Auto-save current UI settings so nothing is lost on close
        try:
            self.tab_settings._apply(emit=False)
        except Exception:
            pass
        self._hotkey.unregister()
        self.overlay.stop()
        # Stop the data sources, then the engine thread (lets any in-flight
        # OBS request finish), then it is safe to touch the engines directly.
        for rt in self.runtimes.values():
            rt.poller.stop()
        self.engine_thread.quit()
        self.engine_thread.wait(6000)
        for rt in self.runtimes.values():
            rt.engine.log_entry.disconnect()  # engine thread stopped; log directly below
            self.tab_log.append(rt.engine.log_summary())
            rt.engine.stop_logging()
        self.obs.stop()
        if self._tray:
            self._tray.hide()

    def _show_about(self):
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<h3>{APP_NAME}</h3>"
            "<p>Real-time stream bitrate monitor with OBS scene protection.</p>"
            "<p>Monitors one or more streams on your ingest server (Oryx SRS, MediaMTX, "
            "or a custom endpoint) and automatically switches OBS scenes "
            "or toggles source visibility when a stream drops or struggles.</p>"
            "<p><b>Features:</b></p>"
            "<ul>"
            "<li>Several streams at once, each with its own thresholds and actions</li>"
            "<li>Real-time bitrate graph with rolling average</li>"
            "<li>Per-state scene switch <i>or</i> source visibility toggle, with per-scene targets</li>"
            "<li>Grace period, recovery delay, and cooldown timers</li>"
            "<li>SRT packet loss and round-trip time limits (MediaMTX)</li>"
            "<li>Rehearse buttons to test your setup before going live</li>"
            "<li>Browser-source overlay, OBS custom events, recording chapters</li>"
            "<li>Audio alerts and Discord / Slack / custom webhooks</li>"
            "<li>System-wide hotkey for manual override toggle</li>"
            "<li>Presets, CSV logging and session summaries</li>"
            "<li>Start with Windows, update notices, system tray</li>"
            "</ul>"
            f"<p>Version {APP_VERSION}</p>",
        )
