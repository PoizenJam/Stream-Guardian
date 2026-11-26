"""
Start with Windows: a per-user entry under
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run that launches Stream
Guardian minimized to the tray. No admin rights needed. The entry lives only
in the user's registry; nothing is written to the app folder.
"""
import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "StreamGuardian"


def supported() -> bool:
    return sys.platform == "win32"


def launch_command() -> str:
    """Command line for the Run entry, for the packaged EXE or a source checkout."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(pythonw):
        exe = pythonw  # no console window at login
    return f'"{exe}" "{main_py}" --minimized'


def current_entry() -> str:
    if not supported():
        return ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except OSError:
        return ""


def set_enabled(enabled: bool) -> str:
    """Create, refresh or remove the Run entry. Returns "" or an error message."""
    if not supported():
        return "Start with Windows is only available on Windows"
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return ""
    except OSError as e:
        return str(e)


def sync(enabled: bool) -> str:
    """Make the registry match the setting. Also refreshes the path if the
    app folder was moved since the entry was written."""
    want = launch_command() if enabled else ""
    if current_entry() == want:
        return ""
    return set_enabled(enabled)
