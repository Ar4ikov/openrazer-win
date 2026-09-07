# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the standalone Windows executables.

Produces two one-file binaries:

* ``openrazer-win.exe``     -- the CLI, which also starts and stops the daemon
* ``openrazer-win-gui.exe`` -- the Tk control panel, without a console window

Build with::

    pyinstaller --clean --noconfirm packaging/openrazer-win.spec
"""
import os

from PyInstaller.utils.hooks import collect_data_files

# The generated device database and recipes must travel with the binary.
datas = collect_data_files('openrazer_win', includes=['devices/data/*.json'])

# ctypes reaches hid.dll and setupapi.dll at runtime, so there is nothing to
# bundle for the HID backend -- they are Windows components.
hidden_imports = [
    'openrazer_win.hid.win32',
    'openrazer_win.hid.fake',
    'openrazer_win.daemon.main',
    'openrazer_win.daemon.demo',
    'openrazer_win.tray.gui',
]

EXCLUDES = [
    'numpy', 'PIL', 'matplotlib', 'pandas', 'scipy', 'pytest', 'setuptools',
    'pip', 'IPython', 'sqlite3', 'test', 'unittest', 'pydoc_data',
]

# SPECPATH is injected by PyInstaller and points at this file's directory;
# relative paths in a spec resolve against it, so build absolute ones.
cli_analysis = Analysis(
    [os.path.join(SPECPATH, 'entry_cli.py')],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES + ['tkinter'],
    noarchive=False,
)

gui_analysis = Analysis(
    [os.path.join(SPECPATH, 'entry_gui.py')],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

cli_pyz = PYZ(cli_analysis.pure)
gui_pyz = PYZ(gui_analysis.pure)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    cli_analysis.binaries,
    cli_analysis.datas,
    [],
    name='openrazer-win',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    gui_analysis.binaries,
    gui_analysis.datas,
    [],
    name='openrazer-win-gui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
