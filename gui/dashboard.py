"""
Dashboard tab: real-time bitrate graph, stats display, connection indicators.
"""
import time
import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QGroupBox, QPushButton, QFrame, QSizePolicy, QSpinBox,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

from gui.themes import COLORS_DARK, COLORS_LIGHT


class StatusIndicator(QWidget):
    """Small colored dot + label for connection status."""
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._dot = QLabel("●")
        self._dot.setFixedWidth(16)
        self._dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label = QLabel(label)
        self._status = QLabel("Disconnected")
        self._status.setProperty("dim", True)

        layout.addWidget(self._dot)
        layout.addWidget(self._label)
        layout.addWidget(self._status)
        layout.addStretch()

        self.set_status(False, "Disconnected")

    def set_label(self, label: str):
        self._label.setText(label)

    def set_status(self, connected: bool, message: str = ""):
        if connected:
            self._dot.setStyleSheet("color: #3ec45c; font-size: 14px; background: transparent;")
            self._status.setText(message or "Connected")
            self._status.setProperty("status_good", True)
        else:
            self._dot.setStyleSheet("color: #e04040; font-size: 14px; background: transparent;")
            self._status.setText(message or "Disconnected")
            self._status.setProperty("status_bad", True)
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)


class StatCard(QFrame):
    """Individual stat display card."""
    def __init__(self, title: str, unit: str = "kbps", parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("""
            StatCard {
                border: 1px solid palette(mid);
                border-radius: 6px;
                padding: 8px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)

        self._title = QLabel(title)
        self._title.setProperty("dim", True)
        self._title.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self._value = QLabel("—")
        font = QFont()
        font.setPointSize(20)
        font.setWeight(QFont.Weight.Bold)
        self._value.setFont(font)
        self._value.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self._unit = QLabel(unit)
        self._unit.setProperty("dim", True)

        val_row = QHBoxLayout()
        val_row.addWidget(self._value)
        val_row.addWidget(self._unit)
        val_row.addStretch()

        layout.addWidget(self._title)
        layout.addLayout(val_row)

    def set_value(self, value: str, color: str = None):
        self._value.setText(value)
        if color:
            self._value.setStyleSheet(f"color: {color}; background: transparent;")
        else:
            self._value.setStyleSheet("background: transparent;")


class DashboardTab(QWidget):
    INGEST_LABELS = {"oryx": "SRS", "mediamtx": "MediaMTX", "generic": "Ingest"}

    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.cfg = config_manager
        self._colors = COLORS_DARK if self.cfg.get("gui", "theme") == "dark" else COLORS_LIGHT
        self._build_ui()

    def _ingest_label(self) -> str:
        backend = self.cfg.get("srs", "backend")
        return self.INGEST_LABELS.get(backend, "Ingest")

    def refresh_ingest_label(self):
        """Called after settings are applied so the connection-row label
        reflects the currently selected backend (Oryx / MediaMTX / generic)."""
        self.srs_status.set_label(self._ingest_label())

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # --- Connection status bar ---
        status_row = QHBoxLayout()
        self.srs_status = StatusIndicator(self._ingest_label())
        self.obs_status = StatusIndicator("OBS")
        self.stream_status = StatusIndicator("Stream")
        status_row.addWidget(self.srs_status)
        status_row.addSpacing(24)
        status_row.addWidget(self.obs_status)
        status_row.addSpacing(24)
        status_row.addWidget(self.stream_status)
        status_row.addStretch()

        # Override button
        self.btn_override = QPushButton("⏸ Auto-Switch ON")
        self.btn_override.setCheckable(True)
        self.btn_override.setToolTip("Toggle automatic scene switching")
        self.btn_override.setFixedWidth(160)
        status_row.addWidget(self.btn_override)

        layout.addLayout(status_row)

        # --- Graph controls row ---
        graph_ctrl_row = QHBoxLayout()
        graph_ctrl_row.addWidget(QLabel("Graph Window:"))
        self.spin_graph_window = QSpinBox()
        self.spin_graph_window.setRange(10, 600)
        self.spin_graph_window.setValue(self.cfg.get("graph", "history_seconds"))
        self.spin_graph_window.setSuffix(" s")
        self.spin_graph_window.setSingleStep(10)
        self.spin_graph_window.setFixedWidth(100)
        self.spin_graph_window.setToolTip("How many seconds of history to show on the graph")
        self.spin_graph_window.valueChanged.connect(self._on_graph_window_changed)
        graph_ctrl_row.addWidget(self.spin_graph_window)

        graph_ctrl_row.addSpacing(20)

        # Quick-toggle for CSV bitrate logging
        self.btn_log_toggle = QPushButton("● Log: OFF")
        self.btn_log_toggle.setCheckable(True)
        self.btn_log_toggle.setFixedWidth(120)
        self.btn_log_toggle.setToolTip(
            "Toggle CSV bitrate logging for diagnosing\n"
            "connection issues and dropouts.\n"
            "Logs: timestamp, bitrate, average, state"
        )
        self.btn_log_toggle.setChecked(self.cfg.get("logging", "enabled"))
        self._update_log_btn(self.cfg.get("logging", "enabled"))
        self.btn_log_toggle.toggled.connect(self._on_log_toggle)
        graph_ctrl_row.addWidget(self.btn_log_toggle)

        graph_ctrl_row.addStretch()
        layout.addLayout(graph_ctrl_row)

        # --- Main content: graph + stats ---
        content = QHBoxLayout()

        # Graph
        self.graph_widget = self._create_graph()
        content.addWidget(self.graph_widget, stretch=3)

        # Stats panel
        stats_panel = QVBoxLayout()
        stats_panel.setSpacing(8)

        self.card_current = StatCard("Current Bitrate")
        self.card_average = StatCard("Average Bitrate")
        self.card_scene = StatCard("Current Scene", unit="")
        self.card_state = StatCard("Stream State", unit="")

        stats_panel.addWidget(self.card_current)
        stats_panel.addWidget(self.card_average)
        stats_panel.addWidget(self.card_scene)
        stats_panel.addWidget(self.card_state)

        # Session stats group
        session_group = QGroupBox("Session Stats")
        session_layout = QGridLayout()
        session_layout.setSpacing(4)

        self.lbl_peak = QLabel("—")
        self.lbl_min = QLabel("—")
        self.lbl_session_avg = QLabel("—")
        self.lbl_drops = QLabel("0")
        self.lbl_uptime = QLabel("00:00:00")

        labels = [
            ("Peak:", self.lbl_peak),
            ("Min:", self.lbl_min),
            ("Session Avg:", self.lbl_session_avg),
            ("Drops:", self.lbl_drops),
            ("Uptime:", self.lbl_uptime),
        ]
        for i, (name, widget) in enumerate(labels):
            lbl = QLabel(name)
            lbl.setProperty("dim", True)
            session_layout.addWidget(lbl, i, 0)
            session_layout.addWidget(widget, i, 1)

        session_group.setLayout(session_layout)
        stats_panel.addWidget(session_group)
        stats_panel.addStretch()

        stats_widget = QWidget()
        stats_widget.setLayout(stats_panel)
        stats_widget.setMinimumWidth(180)
        stats_widget.setMaximumWidth(240)
        content.addWidget(stats_widget)

        layout.addLayout(content, stretch=1)

    def _create_graph(self):
        """Create pyqtgraph plot widget styled to match OBS."""
        c = self._colors

        pg.setConfigOptions(antialias=True)
        plot_widget = pg.PlotWidget()
        plot_widget.setBackground(c["graph_bg"])
        plot_widget.showGrid(x=True, y=True, alpha=0.15)
        plot_widget.setLabel("left", "Bitrate", units="kbps")
        plot_widget.setLabel("bottom", "Time", units="s")
        plot_widget.setMouseEnabled(x=False, y=True)
        # Lock Y minimum at 0, auto-range upper only
        plot_widget.enableAutoRange(axis="y", enable=False)
        plot_widget.setYRange(0, 10000)
        plot_widget.getViewBox().setLimits(yMin=0)

        # Bitrate line
        self.curve_bitrate = plot_widget.plot(
            pen=pg.mkPen(color=c["graph_bitrate"], width=2),
            name="Bitrate",
        )

        # Average line
        self.curve_average = plot_widget.plot(
            pen=pg.mkPen(color=c["graph_average"], width=2, style=Qt.PenStyle.DashLine),
            name="Average",
        )

        # Threshold lines
        self.line_low = pg.InfiniteLine(
            pos=self.cfg.get("thresholds", "low_bitrate_kbps"),
            angle=0,
            pen=pg.mkPen(color=c["graph_low"], width=1, style=Qt.PenStyle.DotLine),
            label="Low",
            labelOpts={"color": c["graph_low"], "position": 0.05},
        )
        self.line_disc = pg.InfiniteLine(
            pos=self.cfg.get("thresholds", "disconnect_kbps"),
            angle=0,
            pen=pg.mkPen(color=c["graph_disc"], width=1, style=Qt.PenStyle.DotLine),
            label="Disconnect",
            labelOpts={"color": c["graph_disc"], "position": 0.05},
        )
        plot_widget.addItem(self.line_low)
        plot_widget.addItem(self.line_disc)

        # Legend
        legend = plot_widget.addLegend(offset=(10, 10))
        legend.setBrush(pg.mkBrush(c["bg_panel"] + "cc"))

        self._plot_widget = plot_widget
        return plot_widget

    def update_graph(self, graph_data: list, avg_kbps: float):
        """Update the rolling graph with new data."""
        if not graph_data:
            return

        now = time.time()
        times = [t - now for t, _ in graph_data]
        values = [v for _, v in graph_data]

        self.curve_bitrate.setData(times, values)

        # Rolling average curve: compute windowed average at each point
        if len(values) >= 2:
            window_s = self.cfg.get("thresholds", "averaging_window_s")
            poll_ms = self.cfg.get("srs", "poll_interval_ms")
            # Estimate samples per window
            samples_per_sec = 1000.0 / max(poll_ms, 100)
            kernel_size = max(int(window_s * samples_per_sec), 1)
            kernel_size = min(kernel_size, len(values))

            if kernel_size >= 2:
                vals = np.array(values, dtype=float)
                # Causal rolling average (no future peeking)
                kernel = np.ones(kernel_size) / kernel_size
                # Pad front to keep same length
                padded = np.concatenate([np.full(kernel_size - 1, vals[0]), vals])
                avg_curve = np.convolve(padded, kernel, mode='valid')
                self.curve_average.setData(times, avg_curve.tolist())
            else:
                self.curve_average.setData(times, values)
        else:
            self.curve_average.setData([], [])

        # Update threshold lines
        self.line_low.setValue(self.cfg.get("thresholds", "low_bitrate_kbps"))
        self.line_disc.setValue(self.cfg.get("thresholds", "disconnect_kbps"))

        # X axis: use dashboard spinner value
        history_s = self.spin_graph_window.value()
        self._plot_widget.setXRange(-history_s, 0)

        # Y axis: auto-scale max from data, keep min at 0
        if values:
            peak = max(values)
            low_thresh = self.cfg.get("thresholds", "low_bitrate_kbps")
            y_max = max(peak, low_thresh, avg_kbps) * 1.15
            y_max = max(y_max, 100)  # minimum visible range
            self._plot_widget.setYRange(0, y_max)

    def update_stats(self, stats: dict):
        """Update all stat cards from engine stats dict."""
        current = stats.get("current_kbps", 0)
        avg = stats.get("average_kbps", 0)
        state = stats.get("state", "NORMAL")

        low_thresh = self.cfg.get("thresholds", "low_bitrate_kbps")
        disc_thresh = self.cfg.get("thresholds", "disconnect_kbps")

        # Color current bitrate by threshold
        if current < disc_thresh:
            color = self._colors["danger"]
        elif current < low_thresh:
            color = self._colors["warning"]
        else:
            color = self._colors["success"]

        self.card_current.set_value(f"{current:.0f}", color)
        self.card_average.set_value(f"{avg:.0f}")

        # State card
        state_colors = {
            "NORMAL": self._colors["success"],
            "LOW_BITRATE": self._colors["warning"],
            "DISCONNECTED": self._colors["danger"],
        }
        self.card_state.set_value(state.replace("_", " ").title(), state_colors.get(state))

        # Session stats
        self.lbl_peak.setText(f"{stats.get('session_peak_kbps', 0):.0f} kbps")
        self.lbl_min.setText(f"{stats.get('session_min_kbps', 0):.0f} kbps")
        self.lbl_session_avg.setText(f"{stats.get('session_avg_kbps', 0):.0f} kbps")
        self.lbl_drops.setText(str(stats.get("session_drops", 0)))

        uptime = int(stats.get("uptime_s", 0))
        h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
        self.lbl_uptime.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def update_scene(self, scene_name: str):
        self.card_scene.set_value(scene_name or "—")

    def refresh_theme(self):
        self._colors = COLORS_DARK if self.cfg.get("gui", "theme") == "dark" else COLORS_LIGHT
        c = self._colors
        self._plot_widget.setBackground(c["graph_bg"])
        self.curve_bitrate.setPen(pg.mkPen(color=c["graph_bitrate"], width=2))
        self.curve_average.setPen(pg.mkPen(color=c["graph_average"], width=2, style=Qt.PenStyle.DashLine))
        self.line_low.setPen(pg.mkPen(color=c["graph_low"], width=1, style=Qt.PenStyle.DotLine))
        self.line_disc.setPen(pg.mkPen(color=c["graph_disc"], width=1, style=Qt.PenStyle.DotLine))

    def _on_graph_window_changed(self, value: int):
        """Save graph window to config when spinner changes."""
        self.cfg.set("graph", "history_seconds", value)
        self.cfg.save()

    def _on_log_toggle(self, enabled: bool):
        """Toggle CSV logging from dashboard."""
        self.cfg.set("logging", "enabled", enabled)
        self.cfg.save()
        self._update_log_btn(enabled)

    def _update_log_btn(self, enabled: bool):
        if enabled:
            self.btn_log_toggle.setText("● Log: ON")
            self.btn_log_toggle.setStyleSheet(
                "background-color: #3ec45c; color: white; border: none; "
                "border-radius: 4px; padding: 7px 12px;"
            )
        else:
            self.btn_log_toggle.setText("● Log: OFF")
            self.btn_log_toggle.setStyleSheet("")
