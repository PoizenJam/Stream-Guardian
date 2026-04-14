#!/usr/bin/env python3
"""
Main entry point for the application.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from config_manager import APP_NAME, APP_INTERNAL_NAME, APP_VERSION
from gui.main_window import MainWindow


def main():
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_INTERNAL_NAME)
    app.setApplicationVersion(APP_VERSION)

    app.setQuitOnLastWindowClosed(False)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
