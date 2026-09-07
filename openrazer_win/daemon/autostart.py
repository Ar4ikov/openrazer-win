"""Starting the daemon with Windows.

Uses the per-user ``Run`` key rather than a service or a scheduled task: it
needs no administrator rights, survives Windows upgrades, and the user can see
and remove it from Task Manager's Startup tab like anything else.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
VALUE_NAME = 'OpenRazerWin'


class AutostartError(Exception):
    """The autostart entry could not be read or changed."""


def _winreg():
    try:
        import winreg
    except ImportError as error:  # pragma: no cover - non-Windows
        raise AutostartError('autostart is a Windows feature') from error
    return winreg


def daemon_launch_command(no_effects: bool = False) -> str:
    """The command line Windows should run at logon, ready to quote."""
    if getattr(sys, 'frozen', False):
        parts = [sys.executable, 'daemon', 'run']
    else:
        # pythonw.exe keeps the daemon from flashing a console window.
        interpreter = sys.executable
        windowless = os.path.join(os.path.dirname(interpreter), 'pythonw.exe')
        if os.path.exists(windowless):
            interpreter = windowless
        parts = [interpreter, '-m', 'openrazer_win.daemon.main']
    if no_effects:
        parts.append('--no-effects')
    return subprocess.list2cmdline(parts)


def status() -> Optional[str]:
    """The command currently registered, or ``None`` if autostart is off."""
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return value
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AutostartError('cannot read the Run key: {0}'.format(error)) from error


def enable(no_effects: bool = False) -> str:
    """Register the daemon to start at logon.  Returns the command written."""
    winreg = _winreg()
    command = daemon_launch_command(no_effects)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    except OSError as error:
        raise AutostartError('cannot write the Run key: {0}'.format(error)) from error
    return command


def disable() -> bool:
    """Remove the entry.  Returns whether there was one to remove."""
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise AutostartError('cannot update the Run key: {0}'.format(error)) from error
    return True
