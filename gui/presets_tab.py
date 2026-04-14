"""
Presets tab: save, load, import, export configuration presets.
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QPushButton, QListWidget, QLineEdit, QFileDialog, QMessageBox,
)
from PyQt6.QtCore import pyqtSignal


class PresetsTab(QWidget):
    preset_loaded = pyqtSignal()

    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.cfg = config_manager
        self._build_ui()
        self._refresh_list()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(16)

        # Save new preset
        save_group = QGroupBox("Save Current Settings as Preset")
        save_layout = QHBoxLayout()
        self.preset_name_input = QLineEdit()
        self.preset_name_input.setPlaceholderText("Enter preset name...")
        btn_save = QPushButton("Save Preset")
        btn_save.setProperty("accent", True)
        btn_save.clicked.connect(self._save_preset)
        save_layout.addWidget(self.preset_name_input, stretch=1)
        save_layout.addWidget(btn_save)
        save_group.setLayout(save_layout)
        layout.addWidget(save_group)

        # Preset list
        list_group = QGroupBox("Saved Presets")
        list_layout = QVBoxLayout()

        self.preset_list = QListWidget()
        self.preset_list.setMinimumHeight(200)
        list_layout.addWidget(self.preset_list)

        btn_row = QHBoxLayout()
        btn_load = QPushButton("Load Selected")
        btn_load.setProperty("accent", True)
        btn_load.clicked.connect(self._load_preset)
        btn_delete = QPushButton("Delete")
        btn_delete.setProperty("danger", True)
        btn_delete.clicked.connect(self._delete_preset)

        btn_row.addStretch()
        btn_row.addWidget(btn_delete)
        btn_row.addWidget(btn_load)
        list_layout.addLayout(btn_row)

        list_group.setLayout(list_layout)
        layout.addWidget(list_group)

        # Import / Export
        io_group = QGroupBox("Import / Export")
        io_layout = QHBoxLayout()
        btn_import = QPushButton("Import Preset...")
        btn_import.clicked.connect(self._import_preset)
        btn_export = QPushButton("Export Selected...")
        btn_export.clicked.connect(self._export_preset)
        io_layout.addWidget(btn_import)
        io_layout.addWidget(btn_export)
        io_layout.addStretch()
        io_group.setLayout(io_layout)
        layout.addWidget(io_group)

        layout.addStretch()

    def _refresh_list(self):
        self.preset_list.clear()
        for name in self.cfg.list_presets():
            self.preset_list.addItem(name)

    def _save_preset(self):
        name = self.preset_name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Save Preset", "Please enter a preset name.")
            return

        existing = self.cfg.list_presets()
        if name in existing:
            reply = QMessageBox.question(
                self, "Overwrite Preset",
                f"Preset '{name}' already exists. Overwrite?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self.cfg.save_preset(name)
        self._refresh_list()
        self.preset_name_input.clear()
        QMessageBox.information(self, "Preset Saved", f"Preset '{name}' saved successfully.")

    def _load_preset(self):
        item = self.preset_list.currentItem()
        if not item:
            return
        name = item.text()
        reply = QMessageBox.question(
            self, "Load Preset",
            f"Load preset '{name}'? This will overwrite current settings.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            if self.cfg.load_preset(name):
                self.preset_loaded.emit()
                QMessageBox.information(self, "Preset Loaded", f"Preset '{name}' loaded.")
            else:
                QMessageBox.warning(self, "Error", f"Failed to load preset '{name}'.")

    def _delete_preset(self):
        item = self.preset_list.currentItem()
        if not item:
            return
        name = item.text()
        reply = QMessageBox.question(
            self, "Delete Preset",
            f"Delete preset '{name}'? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.cfg.delete_preset(name)
            self._refresh_list()

    def _import_preset(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Preset", "", "JSON Files (*.json)"
        )
        if path:
            name = None  # Use filename
            self.cfg.import_preset(path, name)
            self._refresh_list()
            QMessageBox.information(self, "Imported", "Preset imported successfully.")

    def _export_preset(self):
        item = self.preset_list.currentItem()
        if not item:
            QMessageBox.warning(self, "Export", "Select a preset to export.")
            return
        name = item.text()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Preset", f"{name}.json", "JSON Files (*.json)"
        )
        if path:
            self.cfg.export_preset(name, path)
            QMessageBox.information(self, "Exported", f"Preset '{name}' exported.")
