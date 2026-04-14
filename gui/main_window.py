"""
Main window: ties together all components with tabs, menus, system tray, and global hotkey.
"""
import sys
import os

from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QApplication, QSystemTrayIcon,
    QMenu, QMenuBar, QStatusBar, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor, QKeySequence, QShortcut

from config_manager import ConfigManager, APP_NAME, APP_INTERNAL_NAME, APP_VERSION
from ingest_client import IngestPoller
from obs_client import OBSClient
from bitrate_engine import BitrateEngine
from gui.dashboard import DashboardTab
from gui.settings_tab import SettingsTab
from gui.presets_tab import PresetsTab
from gui.log_tab import LogTab
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(720, 480)
        self.resize(1100, 720)

        # Core components
        self.cfg = ConfigManager()
        self.srs = IngestPoller(self.cfg)
        self.obs = OBSClient(self.cfg)
        self.engine = BitrateEngine(self.cfg, self.obs)

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

        # Apply theme
        self._apply_theme()

        # Apply always-on-top
        if self.cfg.get("gui", "always_on_top"):
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            self.show()

        # Start services
        self.srs.start()
        self.obs.start()

        # Start logging if enabled
        if self.cfg.get("logging", "enabled"):
            self.engine.start_logging()

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
        act_about = QAction(f"About {APP_NAME}", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _build_tabs(self):
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.tab_dashboard = DashboardTab(self.cfg)
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

        self._tray_override_action = tray_menu.addAction("Toggle Auto-Switch")
        self._tray_override_action.triggered.connect(self._toggle_override_from_tray)

        tray_menu.addSeparator()
        act_quit = tray_menu.addAction("Quit")
        act_quit.triggered.connect(self._quit)

        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _build_hotkey(self):
        """Set up the global override hotkey from config."""
        hotkey_str = self.cfg.get("override", "hotkey")
        self._hotkey_shortcut = None
        if hotkey_str:
            try:
                seq = QKeySequence(hotkey_str)
                if not seq.isEmpty():
                    self._hotkey_shortcut = QShortcut(seq, self)
                    self._hotkey_shortcut.setContext(
                        Qt.ShortcutContext.ApplicationShortcut
                    )
                    self._hotkey_shortcut.activated.connect(self._toggle_override_hotkey)
                    self.tab_log.append(f"Override hotkey registered: {hotkey_str}")
            except Exception as e:
                self.tab_log.append(f"Failed to register hotkey '{hotkey_str}': {e}")

    def _connect_signals(self):
        # SRS signals
        self.srs.bitrate_update.connect(self._on_bitrate)
        self.srs.connection_status.connect(self._on_srs_status)
        self.srs.stream_online.connect(self._on_stream_status)
        self.srs.error.connect(lambda msg: self.tab_log.append(f"SRS ERROR: {msg}"))

        # OBS signals
        self.obs.connected.connect(self._on_obs_status)
        self.obs.scene_changed.connect(self._on_scene_changed)
        self.obs.scene_list_updated.connect(self._on_scene_list)
        self.obs.transition_list_updated.connect(self._on_transition_list)
        self.obs.error.connect(lambda msg: self.tab_log.append(f"OBS ERROR: {msg}"))

        # Engine signals
        self.engine.state_changed.connect(self._on_state_changed)
        self.engine.stats_updated.connect(self._on_stats)
        self.engine.scene_switch_triggered.connect(self._on_scene_switch)
        self.engine.source_toggle_triggered.connect(self._on_source_toggle)
        self.engine.log_entry.connect(self.tab_log.append)

        # Settings
        self.tab_settings.settings_changed.connect(self._on_settings_changed)
        self.tab_settings.request_sources.connect(self._on_request_sources)
        self.tab_presets.preset_loaded.connect(self._on_preset_loaded)

        # Override toggle
        self.tab_dashboard.btn_override.toggled.connect(self._on_override_toggle)

        # Log toggle from dashboard
        self.tab_dashboard.btn_log_toggle.toggled.connect(self._on_log_toggle)

    def _on_bitrate(self, total: float, video: float, audio: float):
        self.engine.process_bitrate(total, video, audio)

    def _on_stats(self, stats: dict):
        self.tab_dashboard.update_stats(stats)
        self.tab_dashboard.update_graph(
            self.engine.graph_data,
            stats.get("average_kbps", 0),
        )

    def _on_srs_status(self, connected: bool, message: str):
        self.tab_dashboard.srs_status.set_status(connected, message)
        if connected:
            self.status_bar.showMessage("SRS connected | Monitoring bitrate")

    def _on_stream_status(self, online: bool, stream_id: str):
        if online:
            self.tab_dashboard.stream_status.set_status(True, f"Stream: {stream_id}")
        else:
            self.tab_dashboard.stream_status.set_status(False, "No stream detected")

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
        """Fetch source items from OBS for a scene and route to settings."""
        items = self.obs.get_scene_items(scene_name)
        self.tab_settings.update_source_list(state_key, items)
        count = len(items)
        self.tab_log.append(f"Fetched {count} sources from scene '{scene_name}'")

    def _on_state_changed(self, new_state: str, old_state: str):
        self.tab_log.append(f"State: {old_state} → {new_state}")
        if self._tray:
            self._tray.showMessage(
                APP_NAME,
                f"Stream state: {new_state.replace('_', ' ').title()}",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    def _on_scene_switch(self, scene: str, reason: str):
        self.tab_log.append(f"AUTO-SWITCH → {scene} ({reason})")

    def _on_source_toggle(self, scene: str, source: str, visible: bool, reason: str):
        action = "shown" if visible else "hidden"
        self.tab_log.append(f"SOURCE TOGGLE: '{source}' in '{scene}' {action} ({reason})")

    def _on_log_toggle(self, enabled: bool):
        """Start or stop CSV bitrate logging from dashboard toggle."""
        if enabled:
            self.engine.stop_logging()
            self.engine.start_logging()
        else:
            self.engine.stop_logging()

    # --- Override ---

    def _on_override_toggle(self, enabled: bool):
        self.cfg.set("override", "enabled", enabled)
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
        self._apply_theme()

        # Rebuild hotkey
        if self._hotkey_shortcut:
            self._hotkey_shortcut.setEnabled(False)
            self._hotkey_shortcut = None
        self._build_hotkey()

        # Sync dashboard log button with settings
        log_enabled = self.cfg.get("logging", "enabled")
        self.tab_dashboard.btn_log_toggle.blockSignals(True)
        self.tab_dashboard.btn_log_toggle.setChecked(log_enabled)
        self.tab_dashboard._update_log_btn(log_enabled)
        self.tab_dashboard.btn_log_toggle.blockSignals(False)

        # Restart logging if needed
        self.engine.stop_logging()
        if log_enabled:
            self.engine.start_logging()

        # Sync graph window spinner
        self.tab_dashboard.spin_graph_window.blockSignals(True)
        self.tab_dashboard.spin_graph_window.setValue(self.cfg.get("graph", "history_seconds"))
        self.tab_dashboard.spin_graph_window.blockSignals(False)

        # Update always-on-top
        on_top = self.cfg.get("gui", "always_on_top")
        self.act_on_top.setChecked(on_top)
        self._apply_on_top(on_top)

    def _on_preset_loaded(self):
        self.tab_settings._load_from_config()
        self._apply_theme()
        self.tab_log.append("Preset loaded — settings refreshed")

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
        if on_top:
            self.setWindowFlags(flags | Qt.WindowType.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(flags & ~Qt.WindowType.WindowStaysOnTopHint)
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
            self._shutdown()
            event.accept()

    # --- Session ---

    def _reset_session(self):
        self.engine.reset_session()
        self.tab_log.append("Session stats reset")

    # --- Quit ---

    def _quit(self):
        self._shutdown()
        QApplication.quit()

    def _shutdown(self):
        self.tab_log.append("Shutting down...")
        # Auto-save current UI settings so nothing is lost on close
        try:
            self.tab_settings._apply()
        except Exception:
            pass
        self.engine.stop_logging()
        self.srs.stop()
        self.obs.stop()
        if self._tray:
            self._tray.hide()

    def _show_about(self):
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<h3>{APP_NAME}</h3>"
            "<p>Real-time stream bitrate monitor with OBS scene protection.</p>"
            "<p>Monitors your ingest server (Oryx SRS, MediaMTX, or custom endpoint) "
            "and automatically switches OBS scenes "
            "or toggles source visibility when bitrate drops below configurable thresholds.</p>"
            "<p><b>Features:</b></p>"
            "<ul>"
            "<li>Real-time bitrate graph with rolling average</li>"
            "<li>Configurable low-bitrate and disconnect thresholds</li>"
            "<li>Choose per-state: scene switch <i>or</i> source visibility toggle</li>"
            "<li>Grace period, recovery delay, and cooldown timers</li>"
            "<li>Scene whitelist for override protection</li>"
            "<li>Custom OBS transitions for auto-switches</li>"
            "<li>Customizable audio alerts per event</li>"
            "<li>Discord / Slack / custom webhook notifications</li>"
            "<li>Global hotkey for manual override toggle</li>"
            "<li>Persistent settings with save/load presets</li>"
            "<li>CSV bitrate logging</li>"
            "<li>System tray support</li>"
            "</ul>"
            f"<p>Version {APP_VERSION}</p>",
        )
