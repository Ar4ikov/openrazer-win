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

#: The windowless build that sits beside the console one in a release.
WINDOWED_EXE = 'openrazer-win-gui.exe'


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
        executable = sys.executable
        # The console build opens a console window at logon, and closing that
        # window -- the obvious thing to do with it -- takes the daemon down
        # with it.  The windowed build accepts the same command line.
        windowed = os.path.join(os.path.dirname(executable), WINDOWED_EXE)
        if os.path.basename(executable).lower() != WINDOWED_EXE and os.path.exists(windowed):
            executable = windowed
        parts = [executable, 'daemon', 'run']
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


def command_executable(command: str) -> str:
    """The program a registered command line starts."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end > 0 else command[1:]
    return command.split(' ', 1)[0]


def refresh() -> Optional[str]:
    """Re-point a registered entry whose program no longer exists.

    The entry stores an absolute path, so moving or replacing the executable
    -- downloading a new release somewhere else, rebuilding a virtualenv --
    left Windows starting nothing at logon, or an old copy of the daemon that
    no longer matches the client.  Returns the new command if it changed.
    """
    try:
        current = status()
    except AutostartError:
        return None
    if current is None or os.path.exists(command_executable(current)):
        return None
    no_effects = '--no-effects' in current
    return enable(no_effects)


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
