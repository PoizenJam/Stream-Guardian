"""
Single-instance guard.

QLockFile is the authority on whether another instance is running: it is
released by the OS if the owner crashes, and tryLock() detects a lock left by
a dead PID. QLocalServer alone is not enough because on Windows several
processes can listen on the same named pipe, so two near-simultaneous
launches could both "win".

The local server is only used as a doorbell, so a second launch can ask the
running instance to bring its window forward (e.g. out of the tray).
"""
import getpass
import os
import sys

from PyQt6.QtCore import QObject, QDir, QLockFile, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstance(QObject):
    activation_requested = pyqtSignal()

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        # Scope to the user so two Windows accounts on one PC don't block each other
        try:
            user = getpass.getuser()
        except Exception:
            user = "user"
        self._name = f"{key}-{user}"
        self._lock = QLockFile(os.path.join(QDir.tempPath(), f"{self._name}.lock"))
        # 0 = never treat a lock as stale by age; staleness is decided by PID liveness only
        self._lock.setStaleLockTime(0)
        self._server = None

    def acquire(self) -> bool:
        if not self._lock.tryLock(200):
            return False
        # A crashed previous instance can leave a dead Unix socket behind
        QLocalServer.removeServer(self._name)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        self._server.listen(self._name)
        return True

    def notify_running_instance(self) -> bool:
        """Ask the running instance to show its window. Returns True if it answered."""
        if sys.platform == "win32":
            # We are the foreground process right now; let the other process take focus,
            # otherwise Windows only flashes its taskbar button.
            try:
                import ctypes
                ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
            except Exception:
                pass
        sock = QLocalSocket()
        sock.connectToServer(self._name)
        if not sock.waitForConnected(1000):
            return False
        sock.write(b"activate")
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        return True

    def _on_connection(self):
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            conn.disconnected.connect(conn.deleteLater)
            conn.close()
        self.activation_requested.emit()

    def release(self):
        if self._server:
            self._server.close()
        self._lock.unlock()
