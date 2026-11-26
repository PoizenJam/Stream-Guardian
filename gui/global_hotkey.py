"""
System-wide hotkey for the manual override toggle.

The old implementation used QShortcut with ApplicationShortcut context, which
only fires while a Stream Guardian window has keyboard focus, so it did
nothing while the streamer was in OBS or a game.

On Windows this registers the key with RegisterHotKey (thread-level, hwnd
NULL, so it survives the main window's native handle being recreated when
"Always on top" is toggled) and picks up WM_HOTKEY through a Qt native event
filter. If registration fails (another app owns the combo, or the key has no
mapping) it falls back to the old focus-only QShortcut and reports why.
"""
import sys
import time

from PyQt6.QtCore import QObject, Qt, QAbstractNativeEventFilter, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
HOTKEY_ID = 0xB16B

# Qt key -> Windows virtual-key code for keys outside A-Z / 0-9 / F1-F24
_VK_SPECIAL = {
    Qt.Key.Key_Space: 0x20, Qt.Key.Key_Insert: 0x2D, Qt.Key.Key_Delete: 0x2E,
    Qt.Key.Key_Home: 0x24, Qt.Key.Key_End: 0x23, Qt.Key.Key_PageUp: 0x21,
    Qt.Key.Key_PageDown: 0x22, Qt.Key.Key_Left: 0x25, Qt.Key.Key_Up: 0x26,
    Qt.Key.Key_Right: 0x27, Qt.Key.Key_Down: 0x28, Qt.Key.Key_Pause: 0x13,
    Qt.Key.Key_Print: 0x2C, Qt.Key.Key_ScrollLock: 0x91, Qt.Key.Key_Escape: 0x1B,
    Qt.Key.Key_Tab: 0x09, Qt.Key.Key_Backspace: 0x08, Qt.Key.Key_Return: 0x0D,
    Qt.Key.Key_Enter: 0x0D, Qt.Key.Key_Minus: 0xBD, Qt.Key.Key_Equal: 0xBB,
    Qt.Key.Key_Comma: 0xBC, Qt.Key.Key_Period: 0xBE, Qt.Key.Key_Slash: 0xBF,
    Qt.Key.Key_Semicolon: 0xBA, Qt.Key.Key_BracketLeft: 0xDB,
    Qt.Key.Key_BracketRight: 0xDD, Qt.Key.Key_Backslash: 0xDC,
    Qt.Key.Key_Apostrophe: 0xDE, Qt.Key.Key_QuoteLeft: 0xC0,
}


def qt_sequence_to_win(seq_str: str):
    """Parse e.g. "Ctrl+Shift+F12" into (modifiers, vk). None if unsupported.
    Only the first chord of a multi-chord sequence is used."""
    seq = QKeySequence(seq_str)
    if seq.isEmpty():
        return None
    combo = seq[0]
    key = combo.key()
    qmods = combo.keyboardModifiers()

    kv = key.value
    if Qt.Key.Key_F1.value <= kv <= Qt.Key.Key_F24.value:
        vk = 0x70 + (kv - Qt.Key.Key_F1.value)
    elif Qt.Key.Key_A.value <= kv <= Qt.Key.Key_Z.value or Qt.Key.Key_0.value <= kv <= Qt.Key.Key_9.value:
        vk = kv  # Qt uses the ASCII code, same as the VK code for these
    elif key in _VK_SPECIAL:
        vk = _VK_SPECIAL[key]
    else:
        return None

    mods = MOD_NOREPEAT
    if qmods & Qt.KeyboardModifier.ControlModifier:
        mods |= MOD_CONTROL
    if qmods & Qt.KeyboardModifier.AltModifier:
        mods |= MOD_ALT
    if qmods & Qt.KeyboardModifier.ShiftModifier:
        mods |= MOD_SHIFT
    if qmods & Qt.KeyboardModifier.MetaModifier:
        mods |= MOD_WIN
    return mods, vk


class _WinHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, event_type, message):
        # Thread-level hotkeys (hwnd NULL) arrive as "windows_dispatcher_MSG";
        # the generic type is checked too in case Qt routes it that way.
        if bytes(event_type) in (b"windows_dispatcher_MSG", b"windows_generic_MSG"):
            import ctypes.wintypes
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self._callback()
                return True, 0
        return False, 0


class GlobalHotkey(QObject):
    activated = pyqtSignal()

    def __init__(self, focus_widget, parent=None):
        super().__init__(parent)
        self._focus_widget = focus_widget  # owner for the QShortcut fallback
        self._registered_win = False
        self._filter = None
        self._shortcut = None
        self._last_fire = 0.0

    def register(self, seq_str: str) -> str:
        """(Re)register the hotkey. Returns a log line describing the result."""
        self.unregister()
        if not seq_str or QKeySequence(seq_str).isEmpty():
            return "Override hotkey: none set"

        if sys.platform == "win32":
            parsed = qt_sequence_to_win(seq_str)
            if parsed is None:
                reason = "key not supported for system-wide use"
            else:
                import ctypes
                mods, vk = parsed
                if ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, mods, vk):
                    self._registered_win = True
                    self._filter = _WinHotkeyFilter(self._fire)
                    QApplication.instance().installNativeEventFilter(self._filter)
                    return f"Override hotkey registered system-wide: {seq_str}"
                reason = "already in use by another application"
            self._register_fallback(seq_str)
            return (f"Override hotkey '{seq_str}' could not be registered system-wide ({reason}); "
                    "it will only work while Stream Guardian has focus")

        self._register_fallback(seq_str)
        return f"Override hotkey registered (works while Stream Guardian has focus): {seq_str}"

    def _register_fallback(self, seq_str: str):
        self._shortcut = QShortcut(QKeySequence(seq_str), self._focus_widget)
        self._shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._shortcut.activated.connect(self._fire)

    def _fire(self):
        # Guard against a double delivery of one key press
        now = time.monotonic()
        if now - self._last_fire < 0.25:
            return
        self._last_fire = now
        self.activated.emit()

    def unregister(self):
        if self._registered_win:
            import ctypes
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
            self._registered_win = False
        if self._filter is not None:
            QApplication.instance().removeNativeEventFilter(self._filter)
            self._filter = None
        if self._shortcut is not None:
            self._shortcut.setEnabled(False)
            self._shortcut.deleteLater()
            self._shortcut = None
