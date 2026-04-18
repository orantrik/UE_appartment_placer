# -*- mode: python ; coding: utf-8 -*-
import os

# Path to the built application exe (sibling dist/ folder)
_root = os.path.dirname(os.path.abspath(SPEC))
_app_exe = os.path.join(_root, "..", "dist", "UE-Apartment-Placer.exe")

a = Analysis(
    [os.path.join(_root, 'installer.py')],
    pathex=[_root],
    binaries=[],
    datas=[
        (_app_exe, '.'),   # bundle the app exe alongside the installer
    ],
    hiddenimports=['tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='UE-Apartment-Placer-Setup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,   # request elevation automatically
)
