"""Behaviour that only differs in a PyInstaller build.

Both bugs covered here were reported from the shipped executables and could not
happen when running from source, so they need explicit tests.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from openrazer_win.cli import _PYINSTALLER_ENV, child_environment, daemon_command

PACKAGING = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'packaging')


def test_the_daemon_child_does_not_inherit_pyinstaller_state(monkeypatch):
    """``_MEIPASS2`` and friends must not reach the spawned daemon.

    A onefile child that inherits them reuses the parent's extraction
    directory, so the parent cannot delete it and prints
    "Failed to remove temporary directory" on exit.
    """
    for name in _PYINSTALLER_ENV:
        monkeypatch.setenv(name, r'C:\Temp\_MEI12345')
    monkeypatch.setenv('PATH_SHOULD_SURVIVE', 'yes')

    environment = child_environment()
    assert not [name for name in _PYINSTALLER_ENV if name in environment]
    assert environment['PATH_SHOULD_SURVIVE'] == 'yes'
    # The caller's own environment is untouched.
    assert os.environ['_MEIPASS2'] == r'C:\Temp\_MEI12345'


def test_a_frozen_build_relaunches_itself_with_cli_arguments(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', r'C:\apps\openrazer-win-gui.exe')
    assert daemon_command() == [r'C:\apps\openrazer-win-gui.exe', 'daemon', 'run']


def test_running_from_source_uses_the_module_entry_point(monkeypatch):
    monkeypatch.delattr(sys, 'frozen', raising=False)
    assert daemon_command()[1:] == ['-m', 'openrazer_win.daemon.main']


def test_the_gui_entry_point_dispatches_arguments_to_the_cli():
    """``openrazer-win-gui.exe daemon run`` must run a daemon, not a window.

    When it opened the control panel instead, the panel found no daemon,
    offered to start one, relaunched itself, and looped forever.
    """
    source = open(os.path.join(PACKAGING, 'entry_gui.py'), encoding='utf-8').read()
    assert 'len(sys.argv) > 1' in source
    assert 'openrazer_win.cli' in source
    # The GUI import must be reached only in the no-arguments branch.
    cli_at = source.index('openrazer_win.cli')
    gui_at = source.index('openrazer_win.tray.gui')
    assert cli_at < gui_at


def test_both_entry_points_compile():
    for name in ('entry_cli.py', 'entry_gui.py'):
        path = os.path.join(PACKAGING, name)
        subprocess.run([sys.executable, '-m', 'py_compile', path], check=True)


def test_the_spec_enumerates_submodules_rather_than_inferring_them():
    """collect_submodules, not graph inference.

    PyInstaller cannot follow imports through a PEP 660 editable install, which
    produced an exe that could not import openrazer_win.cli at all.
    """
    source = open(os.path.join(PACKAGING, 'openrazer-win.spec'), encoding='utf-8').read()
    assert 'collect_submodules' in source
    assert re.search(r'hidden_imports\s*=\s*collect_submodules', source)


def test_daemon_logging_survives_a_missing_stderr(tmp_path, monkeypatch):
    """A windowed build has ``sys.stderr is None``.

    Adding a StreamHandler on None makes every later log call fail inside
    logging itself, so the daemon has to notice and use the file only.
    """
    import logging

    from openrazer_win.daemon import logs

    monkeypatch.setattr(sys, 'stderr', None)
    path = str(tmp_path / 'daemon.log')
    assert logs.configure(verbose=False, log_file=path) == path

    handlers = logging.getLogger().handlers
    assert handlers, 'the daemon must still log somewhere'
    assert all(getattr(h, 'stream', None) is not None for h in handlers)

    logging.getLogger('test').info('hello from a windowed build')
    for handler in handlers:
        handler.flush()
    assert 'hello from a windowed build' in open(path, encoding='utf-8').read()


def test_daemon_logging_keeps_stderr_when_there_is_one(tmp_path):
    import logging

    from openrazer_win.daemon import logs

    path = str(tmp_path / 'daemon.log')
    logs.configure(verbose=True, log_file=path)
    streams = [getattr(h, 'stream', None) for h in logging.getLogger().handlers]
    assert sys.stderr in streams
    assert logging.getLogger().level == logging.DEBUG


@pytest.fixture(autouse=True)
def _restore_logging():
    """Leave the root logger as the rest of the suite expects it."""
    import logging

    root = logging.getLogger()
    saved = list(root.handlers), root.level
    yield
    root.handlers = saved[0]
    root.setLevel(saved[1])
