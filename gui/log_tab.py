"""
Log tab: scrolling event log with export capability.
"""
from datetime import datetime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QPlainTextEdit, QFileDialog, QGroupBox, QLabel, QCheckBox,
)
from PyQt6.QtCore import Qt


class LogTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._max_lines = 5000
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Controls
        ctrl_row = QHBoxLayout()
        self.chk_autoscroll = QCheckBox("Auto-scroll")
        self.chk_autoscroll.setChecked(True)
        self.chk_timestamps = QCheckBox("Show timestamps")
        self.chk_timestamps.setChecked(True)

        btn_clear = QPushButton("Clear Log")
        btn_clear.clicked.connect(self._clear)
        btn_export = QPushButton("Export Log...")
        btn_export.clicked.connect(self._export)

        ctrl_row.addWidget(self.chk_autoscroll)
        ctrl_row.addWidget(self.chk_timestamps)
        ctrl_row.addStretch()
        ctrl_row.addWidget(btn_clear)
        ctrl_row.addWidget(btn_export)
        layout.addLayout(ctrl_row)

        # Log text
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(self._max_lines)
        layout.addWidget(self.log_text)

        # Stats at bottom
        self._line_count = QLabel("0 entries")
        self._line_count.setProperty("dim", True)
        layout.addWidget(self._line_count)

    def append(self, message: str):
        if self.chk_timestamps.isChecked():
            ts = datetime.now().strftime("%H:%M:%S")
            line = f"[{ts}] {message}"
        else:
            line = message

        self.log_text.appendPlainText(line)
        self._line_count.setText(f"{self.log_text.blockCount()} entries")

        if self.chk_autoscroll.isChecked():
            self.log_text.verticalScrollBar().setValue(
                self.log_text.verticalScrollBar().maximum()
            )

    def _clear(self):
        self.log_text.clear()
        self._line_count.setText("0 entries")

    def _export(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Log", f"bitrate_log_{ts}.txt", "Text Files (*.txt)"
        )
        if path:
            with open(path, "w") as f:
                f.write(self.log_text.toPlainText())
