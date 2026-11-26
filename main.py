#!/usr/bin/env python3
"""
Main entry point for the application.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtCore import Qt

from config_manager import APP_NAME, APP_INTERNAL_NAME, APP_VERSION
from single_instance import SingleInstance
from gui.main_window import MainWindow, make_icon


def main():
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_INTERNAL_NAME)
    app.setApplicationVersion(APP_VERSION)

    # Two instances fight over OBS scenes and both write config.json, so refuse to start
    # a second one and surface the existing window instead.
    instance = SingleInstance(APP_INTERNAL_NAME)
    if not instance.acquire():
        app.setWindowIcon(make_icon())
        QMessageBox.warning(
            None,
            APP_NAME,
            f"{APP_NAME} is already running.\n\n"
            "Running two copies at once makes them fight over OBS scene switching. "
            "The existing window will be brought to the front (it may have been "
            "minimized to the system tray).",
        )
        instance.notify_running_instance()
        return 1

    app.setQuitOnLastWindowClosed(False)

    window = MainWindow()
    instance.activation_requested.connect(window._show_from_tray)
    app.aboutToQuit.connect(instance.release)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
