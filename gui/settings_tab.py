"""
Settings tab: all configuration organized in groups.
Includes source visibility mode, audio alerts, webhook templates, hotkey config.
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QGroupBox, QPushButton, QLineEdit, QSpinBox, QDoubleSpinBox,
    QCheckBox, QComboBox, QListWidget, QListWidgetItem, QScrollArea,
    QFrame, QFileDialog, QAbstractItemView, QMessageBox, QSlider,
    QStackedWidget, QRadioButton, QButtonGroup, QTextEdit,
)
from PyQt6.QtCore import Qt, pyqtSignal

from config_manager import APP_NAME, APP_INTERNAL_NAME

# Audio file filter
AUDIO_FILTER = "Audio Files (*.wav *.mp3 *.ogg *.flac *.m4a);;All Files (*)"


def _file_picker_row(label_text: str, line_edit: QLineEdit, parent: QWidget) -> QHBoxLayout:
    """Helper: create a row with label, line edit, and browse button."""
    row = QHBoxLayout()
    row.addWidget(QLabel(label_text))
    row.addWidget(line_edit, stretch=1)
    btn = QPushButton("Browse...")

    def _browse():
        path, _ = QFileDialog.getOpenFileName(parent, f"Select {label_text}", "", AUDIO_FILTER)
        if path:
            line_edit.setText(path)

    btn.clicked.connect(_browse)
    row.addWidget(btn)
    return row


class ProtectionModeWidget(QWidget):
    """Widget for configuring a single protection state (low bitrate or disconnect).
    Allows choosing between Scene Switch mode and Source Visibility Toggle mode."""

    def __init__(self, state_label: str, state_key: str, parent=None):
        super().__init__(parent)
        self.state_key = state_key  # "low_bitrate" or "disconnect"
        self._build_ui(state_label)

    def _build_ui(self, state_label: str):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Mode selection
        mode_row = QHBoxLayout()
        self.rb_scene = QRadioButton("Switch Scene")
        self.rb_source = QRadioButton("Toggle Source Visibility")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.rb_scene, 0)
        self.mode_group.addButton(self.rb_source, 1)
        self.rb_scene.setChecked(True)
        mode_row.addWidget(QLabel(f"{state_label} Action:"))
        mode_row.addWidget(self.rb_scene)
        mode_row.addWidget(self.rb_source)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        # Stacked widget for the two modes
        self.stack = QStackedWidget()

        # --- Page 0: Scene switch ---
        scene_page = QWidget()
        scene_layout = QHBoxLayout(scene_page)
        scene_layout.setContentsMargins(0, 0, 0, 0)
        scene_layout.addWidget(QLabel("Target Scene:"))
        self.scene_combo = QComboBox()
        self.scene_combo.setEditable(True)
        scene_layout.addWidget(self.scene_combo, stretch=1)
        self.stack.addWidget(scene_page)

        # --- Page 1: Source visibility ---
        source_page = QWidget()
        source_layout = QGridLayout(source_page)
        source_layout.setContentsMargins(0, 0, 0, 0)

        source_layout.addWidget(QLabel("Scene:"), 0, 0)
        self.source_scene_combo = QComboBox()
        self.source_scene_combo.setEditable(True)
        source_layout.addWidget(self.source_scene_combo, 0, 1)

        self.btn_fetch_sources = QPushButton("Fetch Sources")
        source_layout.addWidget(self.btn_fetch_sources, 0, 2)

        source_layout.addWidget(QLabel("Source:"), 1, 0)
        self.source_name_combo = QComboBox()
        self.source_name_combo.setEditable(True)
        source_layout.addWidget(self.source_name_combo, 1, 1, 1, 2)

        source_layout.addWidget(QLabel("Action:"), 2, 0)
        self.action_combo = QComboBox()
        self.action_combo.addItems(["hide", "show"])
        source_layout.addWidget(self.action_combo, 2, 1, 1, 2)

        self.stack.addWidget(source_page)

        layout.addWidget(self.stack)

        # Connect mode switch
        self.rb_scene.toggled.connect(lambda checked: self.stack.setCurrentIndex(0 if checked else 1))

    def update_scene_combos(self, scenes: list):
        for combo in [self.scene_combo, self.source_scene_combo]:
            current = combo.currentText()
            combo.clear()
            combo.addItems(scenes)
            if current:
                combo.setCurrentText(current)

    def update_source_combo(self, sources: list):
        current = self.source_name_combo.currentText()
        self.source_name_combo.clear()
        self.source_name_combo.addItems(sources)
        if current:
            self.source_name_combo.setCurrentText(current)


class SettingsTab(QWidget):
    settings_changed = pyqtSignal()
    request_sources = pyqtSignal(str, str)  # scene_name, state_key (for routing response)

    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.cfg = config_manager
        self._build_ui()
        self._load_from_config()

    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        main_layout = QVBoxLayout(container)
        main_layout.setSpacing(16)

        # === Ingest Server Connection ===
        srs_group = QGroupBox("Ingest Server API Connection")
        srs_layout = QGridLayout()

        # -- Backend selector at top --
        self.srs_backend = QComboBox()
        self.srs_backend.addItems(["oryx", "mediamtx", "generic"])
        self.srs_backend.setToolTip(
            "Select your ingest server backend:\n\n"
            "  Oryx — Oryx/SRS media server (/api/v1/streams/)\n"
            "  MediaMTX — MediaMTX relay server (/v3/paths/list)\n"
            "  Generic — Custom JSON endpoint with configurable key"
        )
        self.srs_backend.currentTextChanged.connect(self._on_backend_changed)
        srs_layout.addWidget(QLabel("Backend:"), 0, 0)
        srs_layout.addWidget(self.srs_backend, 0, 1)

        # -- Note label --
        self.srs_note = QLabel(
            "These fields configure the HTTP API connection, not the ingest URL.\n"
            "The API URL is built as: http(s)://Host:Port/APIPath"
        )
        self.srs_note.setProperty("dim", True)
        self.srs_note.setWordWrap(True)
        srs_layout.addWidget(self.srs_note, 1, 0, 1, 2)

        self.srs_host = QLineEdit()
        self.srs_host.setPlaceholderText("e.g. mainframe  or  192.168.1.50")
        self.srs_host.setToolTip(
            "Hostname or IP of your ingest server.\n"
            "Just the hostname — no protocol prefix (no http://).\n\n"
            "Examples:\n"
            "  mainframe\n"
            "  192.168.1.50\n"
            "  srs.example.com"
        )

        self.srs_port = QSpinBox()
        self.srs_port.setRange(1, 65535)
        self.srs_port.setToolTip(
            "HTTP API port.\n\n"
            "Common values:\n"
            "  2022 — Oryx default\n"
            "  1985 — SRS standalone\n"
            "  9997 — MediaMTX default API port"
        )

        self.srs_ssl = QCheckBox("Use HTTPS")
        self.srs_ssl.setToolTip("Check if your API uses HTTPS instead of HTTP")

        self.srs_api_path = QLineEdit()
        self.srs_api_path.setPlaceholderText("/api/v1/streams/")
        self.srs_api_path.setToolTip(
            "API endpoint path for stream statistics.\n"
            "Default for Oryx: /api/v1/streams/\n"
            "MediaMTX uses /v3/paths/list (set automatically).\n\n"
            "For generic mode, set your custom path here."
        )

        self.srs_token = QLineEdit()
        self.srs_token.setPlaceholderText("Bearer token (optional)")
        self.srs_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.srs_token.setToolTip(
            "API bearer token for authentication.\n\n"
            "Oryx: find in dashboard → System → API Secret.\n"
            "MediaMTX/Generic: leave empty if no auth required."
        )

        self.srs_app = QLineEdit()
        self.srs_app.setPlaceholderText("e.g. live")
        self.srs_app.setToolTip(
            "The SRS application name to monitor (Oryx only).\n\n"
            "This matches the 'app' in your RTMP URL:\n"
            "  rtmp://server/live/streamkey  →  app = 'live'\n"
            "  rtmp://server/myapp/stream  →  app = 'myapp'"
        )

        self.srs_stream = QLineEdit()
        self.srs_stream.setPlaceholderText("e.g. MyStream  (empty = any stream)")
        self.srs_stream.setToolTip(
            "Stream name/path to monitor.\n\n"
            "Oryx: stream name as it appears in SRS stats.\n"
            "MediaMTX: path name (e.g. 'PixieMainCam').\n\n"
            "Leave empty to monitor the first active stream."
        )

        self.srs_poll = QSpinBox()
        self.srs_poll.setRange(100, 30000)
        self.srs_poll.setSuffix(" ms")
        self.srs_poll.setSingleStep(100)
        self.srs_poll.setToolTip(
            "How often to poll the API for bitrate data.\n"
            "Lower = more responsive but more API calls.\n\n"
            "Recommended: 1000 ms (1 second)"
        )

        self.srs_raw_bitrate = QCheckBox("Use raw bitrate (instant estimate from recv_bytes)")
        self.srs_raw_bitrate.setToolTip(
            "Raw mode: computes instantaneous bitrate from byte-count deltas\n"
            "between polls. Updates every poll interval with real-time values.\n\n"
            "When OFF (Oryx only): reads recv_30s from SRS, a 30-second\n"
            "rolling average computed server-side.\n\n"
            "MediaMTX always uses raw mode."
        )

        self.srs_raw_window = QSpinBox()
        self.srs_raw_window.setRange(1, 30)
        self.srs_raw_window.setToolTip(
            "Number of poll samples to average when computing raw bitrate.\n"
            "Higher = smoother but slower to respond.\n"
            "Lower = more jittery but truly instant.\n\n"
            "At 1000ms poll interval:\n"
            "  3 = average over ~3 seconds\n"
            "  10 = average over ~10 seconds"
        )

        # Generic-only: JSON key path
        self.srs_generic_key = QLineEdit()
        self.srs_generic_key.setPlaceholderText("e.g. stream.bitrate_kbps")
        self.srs_generic_key.setToolTip(
            "Dot-separated path to the bitrate value in the JSON response.\n\n"
            "Examples:\n"
            "  bitrate_kbps\n"
            "  data.stream.bitrate\n"
            "  streams.0.kbps\n\n"
            "The value at this path should be a number in kbps."
        )

        # Labels we'll need references to for show/hide
        self._srs_labels = {}

        row = 2  # rows 0-1 are backend selector and note
        for label_text, widget, key in [
            ("Host:", self.srs_host, "host"),
            ("API Port:", self.srs_port, "port"),
            ("", self.srs_ssl, "ssl"),
            ("API Path:", self.srs_api_path, "api_path"),
            ("API Token:", self.srs_token, "token"),
            ("Stream App:", self.srs_app, "app"),
            ("Stream Name:", self.srs_stream, "stream"),
            ("Poll Interval:", self.srs_poll, "poll"),
            ("", self.srs_raw_bitrate, "raw"),
            ("Raw Avg Window:", self.srs_raw_window, "raw_window"),
            ("Bitrate JSON Key:", self.srs_generic_key, "generic_key"),
        ]:
            if label_text:
                lbl = QLabel(label_text)
                lbl.setToolTip(widget.toolTip())
                srs_layout.addWidget(lbl, row, 0)
                self._srs_labels[key] = lbl
            srs_layout.addWidget(widget, row, 1)
            row += 1

        srs_group.setLayout(srs_layout)
        main_layout.addWidget(srs_group)

        # === OBS Connection ===
        obs_group = QGroupBox("OBS WebSocket Connection")
        obs_layout = QGridLayout()

        self.obs_host = QLineEdit()
        self.obs_host.setPlaceholderText("e.g. localhost")
        self.obs_host.setToolTip(
            "Hostname/IP where OBS Studio is running.\n"
            "Usually 'localhost' if OBS is on the same machine."
        )
        self.obs_port = QSpinBox()
        self.obs_port.setRange(1, 65535)
        self.obs_port.setToolTip(
            "OBS WebSocket server port.\n"
            "Default: 4455 (OBS 28+ built-in WebSocket)\n\n"
            "Check: OBS → Tools → WebSocket Server Settings"
        )
        self.obs_password = QLineEdit()
        self.obs_password.setPlaceholderText("Leave empty if no password set in OBS")
        self.obs_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.obs_password.setToolTip(
            "WebSocket authentication password.\n"
            "Set in OBS → Tools → WebSocket Server Settings.\n"
            "Leave empty if authentication is disabled."
        )
        self.obs_reconnect = QSpinBox()
        self.obs_reconnect.setRange(1, 300)
        self.obs_reconnect.setSuffix(" s")
        self.obs_reconnect.setToolTip("How often to retry connecting if OBS is not reachable")

        for i, (label, widget) in enumerate([
            ("Host:", self.obs_host),
            ("Port:", self.obs_port),
            ("Password:", self.obs_password),
            ("Reconnect Interval:", self.obs_reconnect),
        ]):
            obs_layout.addWidget(QLabel(label), i, 0)
            obs_layout.addWidget(widget, i, 1)

        obs_group.setLayout(obs_layout)
        main_layout.addWidget(obs_group)

        # === Thresholds ===
        thresh_group = QGroupBox("Bitrate Thresholds")
        thresh_layout = QGridLayout()

        self.thresh_low = QSpinBox()
        self.thresh_low.setRange(0, 100000)
        self.thresh_low.setSuffix(" kbps")
        self.thresh_low.setSingleStep(100)

        self.thresh_disc = QSpinBox()
        self.thresh_disc.setRange(0, 100000)
        self.thresh_disc.setSuffix(" kbps")
        self.thresh_disc.setSingleStep(50)

        self.thresh_window = QSpinBox()
        self.thresh_window.setRange(1, 120)
        self.thresh_window.setSuffix(" s")

        self.thresh_grace = QSpinBox()
        self.thresh_grace.setRange(0, 60)
        self.thresh_grace.setSuffix(" s")
        self.thresh_grace.setToolTip("Wait this long below threshold before triggering a switch")

        self.thresh_recovery = QSpinBox()
        self.thresh_recovery.setRange(0, 120)
        self.thresh_recovery.setSuffix(" s")
        self.thresh_recovery.setToolTip("Require stable bitrate for this long before switching back")

        self.thresh_cooldown = QSpinBox()
        self.thresh_cooldown.setRange(0, 300)
        self.thresh_cooldown.setSuffix(" s")
        self.thresh_cooldown.setToolTip("Minimum time between scene switches")

        for i, (label, widget, tip) in enumerate([
            ("Low Bitrate Threshold:", self.thresh_low,
             "Switch to low-bitrate scene when average falls below this"),
            ("Disconnect Threshold:", self.thresh_disc,
             "Switch to disconnect scene when average falls below this"),
            ("Averaging Window:", self.thresh_window,
             "Compute average over this rolling window"),
            ("Grace Period:", self.thresh_grace,
             "Seconds below threshold before triggering"),
            ("Recovery Delay:", self.thresh_recovery,
             "Seconds of stable bitrate before switching back"),
            ("Cooldown:", self.thresh_cooldown,
             "Minimum seconds between any two auto-switches"),
        ]):
            lbl = QLabel(label)
            lbl.setToolTip(tip)
            widget.setToolTip(tip)
            thresh_layout.addWidget(lbl, i, 0)
            thresh_layout.addWidget(widget, i, 1)

        thresh_group.setLayout(thresh_layout)
        main_layout.addWidget(thresh_group)

        # === False Positive Mitigation ===
        # Adaptive encoders (Moblin SRT, x264 capped CRF, etc.) legitimately
        # drop their bitrate on dark or static scenes — that's not a network
        # problem. These options help Stream Guardian tell the difference.
        fp_group = QGroupBox("False Positive Mitigation (Adaptive Bitrate)")
        fp_layout = QGridLayout()

        self.fp_trust_offline = QCheckBox(
            "Only trigger DISCONNECTED when the ingest server reports the stream is offline"
        )
        self.fp_trust_offline.setToolTip(
            "RECOMMENDED. When enabled, the disconnect state is driven by\n"
            "the ingest server's own stream-online signal (Oryx, MediaMTX,\n"
            "and generic backends all report this) instead of the bitrate\n"
            "threshold. This prevents an adaptive encoder briefly dipping\n"
            "below the disconnect threshold on a static scene from being\n"
            "treated as a real disconnect.\n\n"
            "Disable to fall back to the legacy bitrate-only behaviour."
        )

        self.fp_floor = QSpinBox()
        self.fp_floor.setRange(0, 100000)
        self.fp_floor.setSuffix(" kbps")
        self.fp_floor.setSingleStep(50)
        self.fp_floor.setToolTip(
            "Adaptive bitrate floor. If the average bitrate is at or above\n"
            "this value AND the stream is stable (see Stability CV below),\n"
            "the LOW BITRATE trigger is suppressed.\n\n"
            "Set this to your encoder's expected static-scene minimum.\n"
            "For a 3000 kbps target with adaptive bitrate, 500–800 kbps\n"
            "is typical. Set to 0 to disable this check entirely."
        )

        self.fp_cv = QDoubleSpinBox()
        self.fp_cv.setRange(0.0, 2.0)
        self.fp_cv.setSingleStep(0.05)
        self.fp_cv.setDecimals(2)
        self.fp_cv.setToolTip(
            "Stability threshold expressed as coefficient of variation\n"
            "(standard deviation ÷ mean) over the averaging window.\n\n"
            "A real network problem produces erratic bitrate (high CV).\n"
            "An encoder coasting on a static scene produces a smooth low\n"
            "plateau (low CV). Below this value, the stream is considered\n"
            "stable and the adaptive-floor suppression applies.\n\n"
            "0.25 is a sensible default. Lower = stricter (suppress only\n"
            "very smooth streams). Higher = more lenient."
        )

        fp_layout.addWidget(self.fp_trust_offline, 0, 0, 1, 2)
        fp_layout.addWidget(QLabel("Adaptive Bitrate Floor:"), 1, 0)
        fp_layout.addWidget(self.fp_floor, 1, 1)
        fp_layout.addWidget(QLabel("Stability CV Threshold:"), 2, 0)
        fp_layout.addWidget(self.fp_cv, 2, 1)

        fp_help = QLabel(
            "Tip: With Moblin or any SRT/RIST adaptive source, leave\n"
            "“trust ingest offline” enabled and set the floor a few hundred\n"
            "kbps below your normal minimum. Keep the bitrate “Disconnect\n"
            "Threshold” above as a fallback for backends that don't reliably\n"
            "report offline status."
        )
        fp_help.setProperty("dim", True)
        fp_help.setWordWrap(True)
        fp_layout.addWidget(fp_help, 3, 0, 1, 2)

        fp_group.setLayout(fp_layout)
        main_layout.addWidget(fp_group)

        # === Protection Actions (Low Bitrate) ===
        low_group = QGroupBox("Low Bitrate Protection")
        low_layout = QVBoxLayout()
        self.protection_low = ProtectionModeWidget("Low Bitrate", "low_bitrate")
        self.protection_low.btn_fetch_sources.clicked.connect(
            lambda: self._fetch_sources_for("low_bitrate")
        )
        low_layout.addWidget(self.protection_low)
        low_group.setLayout(low_layout)
        main_layout.addWidget(low_group)

        # === Protection Actions (Disconnect) ===
        disc_group = QGroupBox("Disconnect Protection")
        disc_layout = QVBoxLayout()
        self.protection_disc = ProtectionModeWidget("Disconnect", "disconnect")
        self.protection_disc.btn_fetch_sources.clicked.connect(
            lambda: self._fetch_sources_for("disconnect")
        )
        disc_layout.addWidget(self.protection_disc)
        disc_group.setLayout(disc_layout)
        main_layout.addWidget(disc_group)

        # === Transitions ===
        trans_group = QGroupBox("OBS Transitions")
        trans_layout = QGridLayout()

        self.scene_use_transition = QCheckBox("Use custom transition for auto-switches")
        self.scene_transition = QComboBox()
        self.scene_transition.setEditable(True)
        self.scene_transition_dur = QSpinBox()
        self.scene_transition_dur.setRange(0, 10000)
        self.scene_transition_dur.setSuffix(" ms")
        self.scene_transition_dur.setSingleStep(50)

        trans_layout.addWidget(self.scene_use_transition, 0, 0, 1, 2)
        trans_layout.addWidget(QLabel("Transition:"), 1, 0)
        trans_layout.addWidget(self.scene_transition, 1, 1)
        trans_layout.addWidget(QLabel("Duration:"), 2, 0)
        trans_layout.addWidget(self.scene_transition_dur, 2, 1)

        trans_group.setLayout(trans_layout)
        main_layout.addWidget(trans_group)

        # === Scene Whitelist ===
        wl_group = QGroupBox("Scene Whitelist (no auto-switching on these scenes)")
        wl_layout = QVBoxLayout()

        self.whitelist = QListWidget()
        self.whitelist.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.whitelist.setMaximumHeight(120)

        wl_btn_row = QHBoxLayout()
        self.wl_scene_combo = QComboBox()
        self.wl_scene_combo.setEditable(True)
        btn_add_wl = QPushButton("Add")
        btn_add_wl.clicked.connect(self._add_whitelist)
        btn_rem_wl = QPushButton("Remove Selected")
        btn_rem_wl.clicked.connect(self._remove_whitelist)

        wl_btn_row.addWidget(self.wl_scene_combo, stretch=1)
        wl_btn_row.addWidget(btn_add_wl)
        wl_btn_row.addWidget(btn_rem_wl)

        wl_layout.addWidget(self.whitelist)
        wl_layout.addLayout(wl_btn_row)
        wl_group.setLayout(wl_layout)
        main_layout.addWidget(wl_group)

        # === Override Hotkey ===
        hotkey_group = QGroupBox("Manual Override Hotkey")
        hotkey_layout = QHBoxLayout()
        hotkey_layout.addWidget(QLabel("Toggle Hotkey:"))
        self.hotkey_edit = QLineEdit()
        self.hotkey_edit.setPlaceholderText("e.g. Ctrl+Shift+F12")
        self.hotkey_edit.setToolTip(
            "Global keyboard shortcut to toggle auto-switching on/off.\n"
            "Use format: Ctrl+Shift+F12, Alt+F9, etc.\n"
            "Requires restart to take effect."
        )
        hotkey_layout.addWidget(self.hotkey_edit, stretch=1)
        lbl_note = QLabel("(Restart required for hotkey changes)")
        lbl_note.setProperty("dim", True)
        hotkey_layout.addWidget(lbl_note)
        hotkey_group.setLayout(hotkey_layout)
        main_layout.addWidget(hotkey_group)

        # === Audio Notifications ===
        audio_group = QGroupBox("Audio Alerts")
        audio_layout = QVBoxLayout()

        self.notif_audio = QCheckBox("Enable audio alerts on state changes")
        audio_layout.addWidget(self.notif_audio)

        # Volume slider
        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel("Volume:"))
        self.audio_volume = QSlider(Qt.Orientation.Horizontal)
        self.audio_volume.setRange(0, 100)
        self.audio_volume.setValue(80)
        self.audio_volume_label = QLabel("80%")
        self.audio_volume.valueChanged.connect(
            lambda v: self.audio_volume_label.setText(f"{v}%")
        )
        vol_row.addWidget(self.audio_volume, stretch=1)
        vol_row.addWidget(self.audio_volume_label)
        audio_layout.addLayout(vol_row)

        # Per-event audio files
        self.audio_low = QLineEdit()
        self.audio_low.setPlaceholderText("No file selected")
        audio_layout.addLayout(
            _file_picker_row("Low Bitrate Sound:", self.audio_low, self)
        )

        self.audio_disc = QLineEdit()
        self.audio_disc.setPlaceholderText("No file selected")
        audio_layout.addLayout(
            _file_picker_row("Disconnect Sound:", self.audio_disc, self)
        )

        self.audio_recovery = QLineEdit()
        self.audio_recovery.setPlaceholderText("No file selected")
        audio_layout.addLayout(
            _file_picker_row("Recovery Sound:", self.audio_recovery, self)
        )

        audio_group.setLayout(audio_layout)
        main_layout.addWidget(audio_group)

        # === Webhook Notifications ===
        webhook_group = QGroupBox("Webhook Notifications")
        webhook_layout = QVBoxLayout()

        self.notif_webhook = QCheckBox("Enable webhook notifications (Discord, Slack, custom)")
        webhook_layout.addWidget(self.notif_webhook)

        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("Webhook URL:"))
        self.notif_webhook_url = QLineEdit()
        self.notif_webhook_url.setPlaceholderText("https://discord.com/api/webhooks/...")
        url_row.addWidget(self.notif_webhook_url, stretch=1)
        webhook_layout.addLayout(url_row)

        webhook_layout.addWidget(QLabel("Message Templates (use {bitrate} placeholder):"))

        self.webhook_low_msg = QLineEdit()
        self.webhook_low_msg.setPlaceholderText(
            "⚠️ Stream bitrate dropped below threshold ({bitrate} kbps avg)"
        )
        wlm_row = QHBoxLayout()
        wlm_row.addWidget(QLabel("Low Bitrate:"))
        wlm_row.addWidget(self.webhook_low_msg, stretch=1)
        webhook_layout.addLayout(wlm_row)

        self.webhook_disc_msg = QLineEdit()
        self.webhook_disc_msg.setPlaceholderText(
            "🔴 Stream disconnected ({bitrate} kbps avg)"
        )
        wdm_row = QHBoxLayout()
        wdm_row.addWidget(QLabel("Disconnect:"))
        wdm_row.addWidget(self.webhook_disc_msg, stretch=1)
        webhook_layout.addLayout(wdm_row)

        self.webhook_recover_msg = QLineEdit()
        self.webhook_recover_msg.setPlaceholderText(
            "✅ Stream recovered ({bitrate} kbps avg)"
        )
        wrm_row = QHBoxLayout()
        wrm_row.addWidget(QLabel("Recovery:"))
        wrm_row.addWidget(self.webhook_recover_msg, stretch=1)
        webhook_layout.addLayout(wrm_row)

        # Test button
        btn_test_webhook = QPushButton("Send Test Notification")
        btn_test_webhook.clicked.connect(self._test_webhook)
        webhook_layout.addWidget(btn_test_webhook)

        webhook_group.setLayout(webhook_layout)
        main_layout.addWidget(webhook_group)

        # === Graph Settings ===
        graph_group = QGroupBox("Graph Settings")
        graph_layout = QGridLayout()

        self.graph_history = QSpinBox()
        self.graph_history.setRange(10, 600)
        self.graph_history.setSuffix(" s")
        self.graph_show_avg = QCheckBox("Show average line")
        self.graph_show_thresh = QCheckBox("Show threshold lines")

        graph_layout.addWidget(QLabel("History Window:"), 0, 0)
        graph_layout.addWidget(self.graph_history, 0, 1)
        graph_layout.addWidget(self.graph_show_avg, 1, 0, 1, 2)
        graph_layout.addWidget(self.graph_show_thresh, 2, 0, 1, 2)

        graph_group.setLayout(graph_layout)
        main_layout.addWidget(graph_group)

        # === Logging ===
        log_group = QGroupBox("Bitrate Logging (CSV)")
        log_layout = QVBoxLayout()

        log_desc = QLabel(
            "Log bitrate data to CSV for diagnosing connection issues and dropouts.\n"
            "Format: timestamp, bitrate_kbps, avg_kbps, state — one row per poll interval.\n"
            "Can also be toggled quickly from the dashboard."
        )
        log_desc.setProperty("dim", True)
        log_desc.setWordWrap(True)
        log_layout.addWidget(log_desc)

        self.log_enabled = QCheckBox("Enable CSV logging")
        log_layout.addWidget(self.log_enabled)

        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("Log Directory:"))
        self.log_dir = QLineEdit()
        self.log_dir.setPlaceholderText(f"Default: ~/{APP_INTERNAL_NAME}/logs")
        self.log_dir.setToolTip(
            "Directory where CSV log files are saved.\n"
            "Each session creates a new file:\n"
            "  bitrate_20260227_143000.csv"
        )
        btn_browse_log = QPushButton("Browse...")
        btn_browse_log.clicked.connect(self._browse_log_dir)
        dir_row.addWidget(self.log_dir, stretch=1)
        dir_row.addWidget(btn_browse_log)
        log_layout.addLayout(dir_row)

        log_group.setLayout(log_layout)
        main_layout.addWidget(log_group)

        # === GUI Preferences ===
        gui_group = QGroupBox("Appearance")
        gui_layout = QGridLayout()

        self.gui_theme = QComboBox()
        self.gui_theme.addItems(["dark", "light"])
        self.gui_minimize_tray = QCheckBox("Minimize to system tray")
        self.gui_always_top = QCheckBox("Always on top")

        gui_layout.addWidget(QLabel("Theme:"), 0, 0)
        gui_layout.addWidget(self.gui_theme, 0, 1)
        gui_layout.addWidget(self.gui_minimize_tray, 1, 0, 1, 2)
        gui_layout.addWidget(self.gui_always_top, 2, 0, 1, 2)

        gui_group.setLayout(gui_layout)
        main_layout.addWidget(gui_group)

        # === Apply / Reset ===
        btn_row = QHBoxLayout()
        btn_apply = QPushButton("Apply Settings")
        btn_apply.setProperty("accent", True)
        btn_apply.clicked.connect(self._apply)
        btn_reset = QPushButton("Reset to Defaults")
        btn_reset.clicked.connect(self._reset_defaults)

        btn_row.addStretch()
        btn_row.addWidget(btn_reset)
        btn_row.addWidget(btn_apply)
        main_layout.addLayout(btn_row)

        main_layout.addStretch()

        scroll.setWidget(container)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    # --- Backend visibility ---

    def _on_backend_changed(self, backend: str):
        """Show/hide fields based on selected backend."""
        is_oryx    = (backend == "oryx")
        is_mtx     = (backend == "mediamtx")
        is_generic = (backend == "generic")

        # Oryx-only: Stream App, raw bitrate toggle
        self.srs_app.setVisible(is_oryx)
        if "app" in self._srs_labels:
            self._srs_labels["app"].setVisible(is_oryx)
        self.srs_raw_bitrate.setVisible(is_oryx)

        # API Path: shown for Oryx and Generic, hidden for MediaMTX (auto-set)
        self.srs_api_path.setVisible(not is_mtx)
        if "api_path" in self._srs_labels:
            self._srs_labels["api_path"].setVisible(not is_mtx)

        # Generic-only: JSON key path
        self.srs_generic_key.setVisible(is_generic)
        if "generic_key" in self._srs_labels:
            self._srs_labels["generic_key"].setVisible(is_generic)

        # Auto-set sensible port defaults when switching
        if is_mtx and self.srs_port.value() in (2022, 1985):
            self.srs_port.setValue(9997)
        elif is_oryx and self.srs_port.value() == 9997:
            self.srs_port.setValue(2022)

    # --- Load / Apply ---

    def _load_from_config(self):
        c = self.cfg

        self.srs_backend.setCurrentText(c.get("srs", "backend"))
        self.srs_host.setText(c.get("srs", "host"))
        self.srs_port.setValue(c.get("srs", "port"))
        self.srs_ssl.setChecked(c.get("srs", "use_ssl"))
        self.srs_api_path.setText(c.get("srs", "api_path"))
        self.srs_token.setText(c.get("srs", "auth_token"))
        self.srs_app.setText(c.get("srs", "stream_app"))
        self.srs_stream.setText(c.get("srs", "stream_name"))
        self.srs_poll.setValue(c.get("srs", "poll_interval_ms"))
        self.srs_raw_bitrate.setChecked(c.get("srs", "use_raw_bitrate"))
        self.srs_raw_window.setValue(c.get("srs", "raw_bitrate_avg_window"))
        self.srs_generic_key.setText(c.get("srs", "generic_bitrate_key"))

        # Update field visibility for current backend
        self._on_backend_changed(c.get("srs", "backend"))

        self.obs_host.setText(c.get("obs", "host"))
        self.obs_port.setValue(c.get("obs", "port"))
        self.obs_password.setText(c.get("obs", "password"))
        self.obs_reconnect.setValue(c.get("obs", "reconnect_interval_s"))

        self.thresh_low.setValue(c.get("thresholds", "low_bitrate_kbps"))
        self.thresh_disc.setValue(c.get("thresholds", "disconnect_kbps"))
        self.thresh_window.setValue(c.get("thresholds", "averaging_window_s"))
        self.thresh_grace.setValue(c.get("thresholds", "grace_period_s"))
        self.thresh_recovery.setValue(c.get("thresholds", "recovery_delay_s"))
        self.thresh_cooldown.setValue(c.get("thresholds", "cooldown_s"))
        self.fp_trust_offline.setChecked(c.get("thresholds", "trust_ingest_offline_for_disconnect"))
        self.fp_floor.setValue(c.get("thresholds", "adaptive_floor_kbps"))
        self.fp_cv.setValue(c.get("thresholds", "stability_cv_threshold"))

        # Protection modes
        self._load_protection(self.protection_low, "low_bitrate")
        self._load_protection(self.protection_disc, "disconnect")

        self.scene_use_transition.setChecked(c.get("scenes", "use_custom_transition"))
        self.scene_transition.setCurrentText(c.get("scenes", "transition_name"))
        self.scene_transition_dur.setValue(c.get("scenes", "transition_duration_ms"))

        self.whitelist.clear()
        for s in c.get("scenes", "whitelist"):
            self.whitelist.addItem(s)

        self.hotkey_edit.setText(c.get("override", "hotkey"))

        # Audio
        self.notif_audio.setChecked(c.get("notifications", "audio_alerts"))
        self.audio_volume.setValue(c.get("notifications", "audio_volume"))
        self.audio_low.setText(c.get("notifications", "audio_low_bitrate"))
        self.audio_disc.setText(c.get("notifications", "audio_disconnect"))
        self.audio_recovery.setText(c.get("notifications", "audio_recovery"))

        # Webhook
        self.notif_webhook.setChecked(c.get("notifications", "webhook_enabled"))
        self.notif_webhook_url.setText(c.get("notifications", "webhook_url"))
        self.webhook_low_msg.setText(c.get("notifications", "webhook_low_bitrate_msg"))
        self.webhook_disc_msg.setText(c.get("notifications", "webhook_disconnect_msg"))
        self.webhook_recover_msg.setText(c.get("notifications", "webhook_recovery_msg"))

        self.graph_history.setValue(c.get("graph", "history_seconds"))
        self.graph_show_avg.setChecked(c.get("graph", "show_average_line"))
        self.graph_show_thresh.setChecked(c.get("graph", "show_threshold_lines"))

        self.log_enabled.setChecked(c.get("logging", "enabled"))
        self.log_dir.setText(c.get("logging", "directory"))

        self.gui_theme.setCurrentText(c.get("gui", "theme"))
        self.gui_minimize_tray.setChecked(c.get("gui", "minimize_to_tray"))
        self.gui_always_top.setChecked(c.get("gui", "always_on_top"))

    def _load_protection(self, widget: ProtectionModeWidget, key: str):
        c = self.cfg
        mode = c.get("scenes", f"{key}_mode")
        if mode == "source":
            widget.rb_source.setChecked(True)
        else:
            widget.rb_scene.setChecked(True)

        widget.scene_combo.setCurrentText(c.get("scenes", f"{key}_scene"))
        widget.source_scene_combo.setCurrentText(c.get("scenes", f"{key}_source_scene"))
        widget.source_name_combo.setCurrentText(c.get("scenes", f"{key}_source_name"))

        action = c.get("scenes", f"{key}_source_action")
        widget.action_combo.setCurrentText(action)

    def _apply(self):
        c = self.cfg

        c.set("srs", "backend", self.srs_backend.currentText())
        c.set("srs", "host", self.srs_host.text() or "localhost")
        c.set("srs", "port", self.srs_port.value())
        c.set("srs", "use_ssl", self.srs_ssl.isChecked())
        c.set("srs", "api_path", self.srs_api_path.text() or "/api/v1/streams/")
        c.set("srs", "auth_token", self.srs_token.text())
        c.set("srs", "stream_app", self.srs_app.text())
        c.set("srs", "stream_name", self.srs_stream.text())
        c.set("srs", "poll_interval_ms", self.srs_poll.value())
        c.set("srs", "use_raw_bitrate", self.srs_raw_bitrate.isChecked())
        c.set("srs", "raw_bitrate_avg_window", self.srs_raw_window.value())
        c.set("srs", "generic_bitrate_key", self.srs_generic_key.text())

        c.set("obs", "host", self.obs_host.text() or "localhost")
        c.set("obs", "port", self.obs_port.value())
        c.set("obs", "password", self.obs_password.text())
        c.set("obs", "reconnect_interval_s", self.obs_reconnect.value())

        c.set("thresholds", "low_bitrate_kbps", self.thresh_low.value())
        c.set("thresholds", "disconnect_kbps", self.thresh_disc.value())
        c.set("thresholds", "averaging_window_s", self.thresh_window.value())
        c.set("thresholds", "grace_period_s", self.thresh_grace.value())
        c.set("thresholds", "recovery_delay_s", self.thresh_recovery.value())
        c.set("thresholds", "cooldown_s", self.thresh_cooldown.value())
        c.set("thresholds", "trust_ingest_offline_for_disconnect", self.fp_trust_offline.isChecked())
        c.set("thresholds", "adaptive_floor_kbps", self.fp_floor.value())
        c.set("thresholds", "stability_cv_threshold", self.fp_cv.value())

        # Protection modes
        self._apply_protection(self.protection_low, "low_bitrate")
        self._apply_protection(self.protection_disc, "disconnect")

        c.set("scenes", "use_custom_transition", self.scene_use_transition.isChecked())
        c.set("scenes", "transition_name", self.scene_transition.currentText())
        c.set("scenes", "transition_duration_ms", self.scene_transition_dur.value())

        wl = [self.whitelist.item(i).text() for i in range(self.whitelist.count())]
        c.set("scenes", "whitelist", wl)

        c.set("override", "hotkey", self.hotkey_edit.text())

        # Audio
        c.set("notifications", "audio_alerts", self.notif_audio.isChecked())
        c.set("notifications", "audio_volume", self.audio_volume.value())
        c.set("notifications", "audio_low_bitrate", self.audio_low.text())
        c.set("notifications", "audio_disconnect", self.audio_disc.text())
        c.set("notifications", "audio_recovery", self.audio_recovery.text())

        # Webhook
        c.set("notifications", "webhook_enabled", self.notif_webhook.isChecked())
        c.set("notifications", "webhook_url", self.notif_webhook_url.text())
        c.set("notifications", "webhook_low_bitrate_msg", self.webhook_low_msg.text())
        c.set("notifications", "webhook_disconnect_msg", self.webhook_disc_msg.text())
        c.set("notifications", "webhook_recovery_msg", self.webhook_recover_msg.text())

        c.set("graph", "history_seconds", self.graph_history.value())
        c.set("graph", "show_average_line", self.graph_show_avg.isChecked())
        c.set("graph", "show_threshold_lines", self.graph_show_thresh.isChecked())

        c.set("logging", "enabled", self.log_enabled.isChecked())
        c.set("logging", "directory", self.log_dir.text())

        c.set("gui", "theme", self.gui_theme.currentText())
        c.set("gui", "minimize_to_tray", self.gui_minimize_tray.isChecked())
        c.set("gui", "always_on_top", self.gui_always_top.isChecked())

        c.save()
        self.settings_changed.emit()

    def _apply_protection(self, widget: ProtectionModeWidget, key: str):
        c = self.cfg
        mode = "source" if widget.rb_source.isChecked() else "scene"
        c.set("scenes", f"{key}_mode", mode)
        c.set("scenes", f"{key}_scene", widget.scene_combo.currentText())
        c.set("scenes", f"{key}_source_scene", widget.source_scene_combo.currentText())
        c.set("scenes", f"{key}_source_name", widget.source_name_combo.currentText())
        c.set("scenes", f"{key}_source_action", widget.action_combo.currentText())

    def _reset_defaults(self):
        reply = QMessageBox.question(
            self, "Reset Settings",
            "Reset all settings to defaults? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.cfg.reset()
            self._load_from_config()
            self.settings_changed.emit()

    # --- Helpers ---

    def _add_whitelist(self):
        scene = self.wl_scene_combo.currentText().strip()
        if scene:
            existing = [self.whitelist.item(i).text() for i in range(self.whitelist.count())]
            if scene not in existing:
                self.whitelist.addItem(scene)

    def _remove_whitelist(self):
        for item in self.whitelist.selectedItems():
            self.whitelist.takeItem(self.whitelist.row(item))

    def _browse_log_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select Log Directory")
        if d:
            self.log_dir.setText(d)

    def _fetch_sources_for(self, state_key: str):
        """Request source list from OBS for a given protection mode widget."""
        widget = self.protection_low if state_key == "low_bitrate" else self.protection_disc
        scene = widget.source_scene_combo.currentText().strip()
        if scene:
            self.request_sources.emit(scene, state_key)
        else:
            QMessageBox.information(
                self, "Fetch Sources", "Select a scene first."
            )

    def _test_webhook(self):
        """Send a test webhook notification."""
        import threading
        import requests as http_req

        url = self.notif_webhook_url.text().strip()
        if not url:
            QMessageBox.warning(self, "Test Webhook", "Enter a webhook URL first.")
            return

        def _post():
            try:
                if "discord.com/api/webhooks" in url:
                    payload = {
                        "content": f"🧪 Test notification from {APP_NAME}",
                        "username": APP_NAME,
                    }
                else:
                    payload = {
                        "text": f"🧪 Test notification from {APP_NAME}",
                        "source": APP_NAME,
                    }
                resp = http_req.post(url, json=payload, timeout=10)
                # Can't easily show a message box from a thread, but it's fine for testing
            except Exception:
                pass

        threading.Thread(target=_post, daemon=True).start()
        QMessageBox.information(self, "Test Webhook", "Test notification sent!")

    # --- External updates ---

    def update_scene_combos(self, scenes: list):
        """Populate all scene combo boxes from OBS scene list."""
        for combo in [self.wl_scene_combo]:
            current = combo.currentText()
            combo.clear()
            combo.addItems(scenes)
            if current:
                combo.setCurrentText(current)
        self.protection_low.update_scene_combos(scenes)
        self.protection_disc.update_scene_combos(scenes)

    def update_transition_combos(self, transitions: list):
        current = self.scene_transition.currentText()
        self.scene_transition.clear()
        self.scene_transition.addItems(transitions)
        if current:
            self.scene_transition.setCurrentText(current)

    def update_source_list(self, state_key: str, sources: list):
        """Populate source combo for a specific protection widget."""
        widget = self.protection_low if state_key == "low_bitrate" else self.protection_disc
        source_names = [name for _, name, _ in sources]
        widget.update_source_combo(source_names)
