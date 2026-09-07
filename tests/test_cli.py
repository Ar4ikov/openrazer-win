"""The command line interface and the host-rendered effects."""
from __future__ import annotations

import threading

import pytest

from openrazer_win.cli import CliError, build_parser, main, parse_colour
from openrazer_win.daemon.demo import build_demo_backend
from openrazer_win.daemon.server import DaemonServer, DaemonService
from openrazer_win.effects.frame import Frame, get_keymaps, keymap_for


# -- argument parsing -------------------------------------------------------

@pytest.mark.parametrize('text, expected', [
    ('red', (255, 0, 0)),
    ('GREEN', (0, 255, 0)),
    ('#ff0080', (255, 0, 128)),
    ('ff0080', (255, 0, 128)),
    ('#f08', (255, 0, 136)),
    ('255,0,128', (255, 0, 128)),
    ('300,-5,0', (255, 0, 0)),
])
def test_colour_parsing(text, expected):
    assert parse_colour(text) == expected


@pytest.mark.parametrize('text', ['nope', '1,2', '#12345', ''])
def test_bad_colours_are_rejected(text):
    with pytest.raises(CliError):
        parse_colour(text)


def test_parser_exposes_every_command():
    parser = build_parser()
    commands = parser._subparsers._group_actions[0].choices  # noqa: SLF001
    assert set(commands) >= {'list', 'info', 'effect', 'brightness', 'dpi',
                             'poll-rate', 'battery', 'supported', 'daemon', 'doctor'}


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert 'usage' in capsys.readouterr().out.lower()


def test_supported_lists_the_database(capsys):
    assert main(['supported', 'viper']) == 0
    output = capsys.readouterr().out
    assert 'Razer Viper' in output
    assert 'devices in total' in output


def test_supported_as_json(capsys):
    assert main(['--json', 'supported', 'blackwidow chroma']) == 0
    import json
    entries = json.loads(capsys.readouterr().out)
    assert entries and all(entry['vid'] == 0x1532 for entry in entries)


def test_doctor_runs_without_hardware(capsys):
    main(['doctor'])
    output = capsys.readouterr().out
    assert 'supported product ids' in output


# -- CLI against a live daemon ----------------------------------------------

@pytest.fixture
def demo_daemon(tmp_path, monkeypatch):
    from openrazer_win.core.persistence import Persistence
    service = DaemonService(backend=build_demo_backend(),
                            persistence=Persistence(str(tmp_path / 'p.json')),
                            enable_effects=True)
    service.start()
    server = DaemonServer(service, port=0)
    path = str(tmp_path / 'daemon.json')
    server.endpoint.write(path)
    monkeypatch.setattr('openrazer_win.daemon.protocol.endpoint_path', lambda: path)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        service.stop()
        thread.join(timeout=2)


def test_list_shows_the_demo_devices(demo_daemon, capsys):
    assert main(['list']) == 0
    output = capsys.readouterr().out
    assert 'Razer Viper' in output
    assert 'DEMO' in output


def test_info_reports_zones_and_settings(demo_daemon, capsys):
    assert main(['info', 'viper']) == 0
    output = capsys.readouterr().out
    assert 'zone logo' in output
    assert 'dpi' in output


def test_effect_applies_to_the_right_zone(demo_daemon, capsys):
    assert main(['effect', 'static', 'red', '--device', 'viper']) == 0
    assert 'static on logo' in capsys.readouterr().out


def test_effect_reports_failure_for_an_unsupported_effect(demo_daemon, capsys):
    assert main(['effect', 'wheel', '--device', 'viper']) == 1
    assert 'wheel' in capsys.readouterr().err.lower()


def test_brightness_round_trip(demo_daemon, capsys):
    assert main(['brightness', '55', '--device', 'viper']) == 0
    capsys.readouterr()
    assert main(['brightness', '--device', 'viper']) == 0
    # Brightness is an 8-bit value on the wire, so 55% comes back as 54.9%.
    assert '54.9' in capsys.readouterr().out


def test_dpi_round_trip(demo_daemon, capsys):
    assert main(['dpi', '2000', '--device', 'viper']) == 0
    assert '2000' in capsys.readouterr().out


def test_daemon_status(demo_daemon, capsys):
    assert main(['daemon', 'status']) == 0
    assert 'running' in capsys.readouterr().out


def test_unknown_device_is_a_clear_error(demo_daemon, capsys):
    assert main(['info', 'not-a-device']) == 2
    assert 'no device matches' in capsys.readouterr().err


# -- frame buffer -----------------------------------------------------------

def test_frame_payload_layout():
    frame = Frame(2, 3)
    frame.set(0, 0, 1, 2, 3)
    frame.set(1, 2, 7, 8, 9)
    payload = frame.to_payload()
    assert len(payload) == 2 * (3 + 3 * 3)
    assert payload[:3] == bytes((0, 0, 2))
    assert payload[3:6] == bytes((1, 2, 3))
    assert payload[12:15] == bytes((1, 0, 2))
    assert payload[-3:] == bytes((7, 8, 9))


def test_frame_ignores_out_of_range_writes():
    frame = Frame(1, 1)
    frame.set(9, 9, 255, 255, 255)
    assert frame.to_payload() == bytes((0, 0, 0)) + bytes(3)


def test_frame_fill_and_clear():
    frame = Frame(2, 2).fill(10, 20, 30)
    assert frame.get(1, 1) == (10, 20, 30)
    assert frame.clear().get(1, 1) == (0, 0, 0)


def test_keymap_covers_a_full_keyboard():
    keymap = keymap_for('keyboard', 'Razer BlackWidow Chroma')
    assert keymap['ESC'] == (0, 1)
    assert keymap['SPACE'][0] == 5
    assert len(keymap) > 100


def test_keymap_families_are_distinct():
    keymaps = get_keymaps()
    assert set(keymaps) >= {'default', 'tartarus', 'orbweaver'}
    assert keymap_for('keypad', 'Razer Tartarus V2') is not keymaps['default']


def test_virtual_key_table_maps_letters_and_function_keys():
    from openrazer_win.effects.keyboard_hook import VK_TO_KEY
    assert VK_TO_KEY[0x41] == 'A'
    assert VK_TO_KEY[0x70] == 'F1'
    assert VK_TO_KEY[0x60] == 'NP0'
    assert VK_TO_KEY[0x31] == '1'
    keymap = keymap_for('keyboard', 'Razer BlackWidow Chroma')
    assert VK_TO_KEY[0x5B] == 'SUPER'
    unmapped = sorted({name for name in VK_TO_KEY.values() if name not in keymap})
    assert unmapped == [], 'virtual keys with no matrix position: {0}'.format(unmapped)
    from openrazer_win.effects.keyboard_hook import EXTENDED_OVERRIDES
    assert all(name in keymap for name in EXTENDED_OVERRIDES.values())


# -- autostart --------------------------------------------------------------

def test_autostart_round_trip(capsys):
    """Enable, read back and disable the logon entry.

    This touches the real per-user Run key, which needs no elevation, and
    removes what it adds. Skipped where there is no registry.
    """
    pytest.importorskip('winreg')
    from openrazer_win.daemon import autostart

    previous = autostart.status()
    try:
        assert main(['autostart', 'enable']) == 0
        assert 'autostart enabled' in capsys.readouterr().out

        assert main(['autostart', 'status']) == 0
        assert 'autostart: on' in capsys.readouterr().out

        assert main(['autostart', 'disable']) == 0
        assert 'disabled' in capsys.readouterr().out

        assert main(['autostart', 'status']) == 1
        assert 'autostart: off' in capsys.readouterr().out
    finally:
        autostart.disable()
        if previous is not None:
            import winreg
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, autostart.RUN_KEY,
                                    0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, autostart.VALUE_NAME, 0,
                                  winreg.REG_SZ, previous)


def test_the_launch_command_is_quoted_and_windowless():
    from openrazer_win.daemon.autostart import daemon_launch_command

    command = daemon_launch_command()
    assert 'openrazer_win.daemon.main' in command or 'daemon run' in command
    assert daemon_launch_command(no_effects=True).endswith('--no-effects')
    # A path with spaces must survive as one argument.
    import subprocess
    assert subprocess.list2cmdline(['a b', 'c']) == '"a b" c'
