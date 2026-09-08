# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the standalone Windows executables.

Produces two one-file binaries:

* ``openrazer-win.exe``     -- the CLI, which also starts and stops the daemon
* ``openrazer-win-gui.exe`` -- the Tk control panel, without a console window

Build with::

    pyinstaller --clean --noconfirm packaging/openrazer-win.spec
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT_ROOT = os.path.dirname(SPECPATH)

# The generated device database, recipes and key map must travel with the
# binary; they are read from disk at runtime.
datas = collect_data_files('openrazer_win', includes=['devices/data/*.json'])
if not datas:  # e.g. an editable install PyInstaller cannot introspect
    data_dir = os.path.join(PROJECT_ROOT, 'openrazer_win', 'devices', 'data')
    datas = [(os.path.join(data_dir, name), 'openrazer_win/devices/data')
             for name in os.listdir(data_dir) if name.endswith('.json')]

# Every submodule, enumerated rather than inferred: the CLI reaches its
# subpackages through plain imports that PyInstaller's graph walk misses under
# an editable install, which produced a binary that could not find
# openrazer_win.cli at all.
hidden_imports = collect_submodules('openrazer_win')

# The Bluetooth path reaches WinRT through PyWinRT, whose namespace packages are
# imported lazily by name and so never appear in the import graph.  They are
# optional: a build without the [ble] extra installed still produces a working
# binary, minus Bluetooth.
WINRT_MODULES = [
    'winrt.system',
    'winrt.windows.devices.bluetooth',
    'winrt.windows.devices.bluetooth.advertisement',
    'winrt.windows.devices.bluetooth.genericattributeprofile',
    'winrt.windows.foundation',
    'winrt.windows.foundation.collections',
    'winrt.windows.storage.streams',
]
for module in WINRT_MODULES:
    try:
        __import__(module)
    except ImportError:
        continue
    hidden_imports.append(module)

EXCLUDES = [
    'numpy', 'PIL', 'matplotlib', 'pandas', 'scipy', 'pytest', 'setuptools',
    'pip', 'IPython', 'sqlite3', 'test', 'unittest', 'pydoc_data',
]

# SPECPATH is injected by PyInstaller and points at this file's directory;
# relative paths in a spec resolve against it, so build absolute ones.
cli_analysis = Analysis(
    [os.path.join(SPECPATH, 'entry_cli.py')],
    pathex=[PROJECT_ROOT],
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
    pathex=[PROJECT_ROOT],
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
