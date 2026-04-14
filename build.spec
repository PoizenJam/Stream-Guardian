# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file.
Build with: pyinstaller build.spec
App name/version are defined in config_manager.py — change them there.
"""
import importlib.util, os, sys

# Import APP_INTERNAL_NAME from config_manager without running the whole app
_cm_path = os.path.join(SPECPATH, "config_manager.py")
_cm_spec = importlib.util.spec_from_file_location("config_manager", _cm_path)
_mod = importlib.util.module_from_spec(_cm_spec)
_cm_spec.loader.exec_module(_mod)
_EXE_NAME = _mod.APP_INTERNAL_NAME

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.png', '.'), ('icon.ico', '.')],
    hiddenimports=[
        'obsws_python',
        'pyqtgraph',
        'numpy',
        'PyQt6.sip',
        'PyQt6.QtMultimedia',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'scipy',
        'PIL',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=_EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',          # App icon (multi-resolution)
)
