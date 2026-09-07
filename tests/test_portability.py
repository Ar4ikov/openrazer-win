"""Every module must import where Windows-only ctypes features do not exist.

The package is developed and used on Windows, but the tests, the documentation
tooling and the fleet simulator all run on Linux CI against the emulator. A
module-level ``ctypes.WINFUNCTYPE`` or ``ctypes.wintypes`` reference breaks that
without anyone noticing until CI turns red, so this reproduces the condition on
any platform: it strips the Windows-only pieces out of ``ctypes`` in a
subprocess, then imports everything.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

MODULES = [
    'openrazer_win',
    'openrazer_win.protocol.chroma',
    'openrazer_win.protocol.report',
    'openrazer_win.hid',
    'openrazer_win.hid.base',
    'openrazer_win.hid.fake',
    'openrazer_win.hid.win32',
    'openrazer_win.devices',
    'openrazer_win.core',
    'openrazer_win.daemon',
    'openrazer_win.daemon.demo',
    'openrazer_win.effects',
    'openrazer_win.effects.keyboard_hook',
    'openrazer_win.client',
    'openrazer_win.cli',
]

SCRIPT = textwrap.dedent('''
    import ctypes, sys

    # Look like Linux to the platform guards, then take away the ctypes
    # features that genuinely do not exist there.
    sys.platform = 'linux'
    for attribute in ('WINFUNCTYPE', 'WinDLL', 'OleDLL', 'HRESULT',
                      'get_last_error', 'set_last_error', 'WinError'):
        if hasattr(ctypes, attribute):
            delattr(ctypes, attribute)
    sys.modules.pop('ctypes.wintypes', None)

    class _BlockWintypes:
        def find_module(self, name, path=None):
            return self if name == 'ctypes.wintypes' else None

        def load_module(self, name):
            raise ImportError('ctypes.wintypes is unavailable on this platform')

    sys.meta_path.insert(0, _BlockWintypes())

    failures = []
    for module in {modules!r}:
        try:
            __import__(module)
        except Exception as error:
            failures.append('{{0}}: {{1}}: {{2}}'.format(
                module, type(error).__name__, error))

    if failures:
        print('\\n'.join(failures))
        raise SystemExit(1)
    print('ok')
''')


def test_all_modules_import_without_windows_only_ctypes():
    result = subprocess.run(
        [sys.executable, '-c', SCRIPT.format(modules=MODULES)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (
        'these modules touch Windows-only ctypes at import time:\n'
        + result.stdout + result.stderr)


def test_the_windows_backend_reports_a_clear_error_off_windows(monkeypatch):
    from openrazer_win import hid

    monkeypatch.setattr(hid, 'is_supported_platform', lambda: False)
    # Asking for it explicitly is an error; asking for "whatever works" is not.
    with __import__('pytest').raises(hid.HidError, match='requires Windows'):
        hid.get_backend('win32')
    assert hid.get_backend().name == 'fake'


def test_the_keyboard_hook_declines_gracefully_off_windows(monkeypatch):
    from openrazer_win.effects import keyboard_hook

    monkeypatch.setattr(keyboard_hook, '_IS_WINDOWS', False)
    hook = keyboard_hook.KeyboardHook(lambda name: None)
    assert hook.start() is False
    assert hook.running is False
    hook.stop()          # must not raise even though nothing was started
