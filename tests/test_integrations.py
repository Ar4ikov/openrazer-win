"""Lighting that follows the Windows accent colour, and the new host-drawn effects."""
from __future__ import annotations

import sys
import time

import pytest

from openrazer_win.core.ble_device import BleDevice
from openrazer_win.core.persistence import Persistence
from openrazer_win.daemon.demo import build_demo_backend
from openrazer_win.daemon.server import DaemonService
from openrazer_win.devices import get_database
from openrazer_win.effects.engine import (
    ACCENT, BREATH_SOFT, STARLIGHT_SOFT, ZONES, EffectThread, KeyPressSource, clean_zones,
)
from openrazer_win.integrations import AccentWatch, IntegrationSettings, accent

from .conftest import KITTY_V2_BT, FakeBleTransport

RED = (255, 0, 0)
TEAL = (0, 128, 128)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr('openrazer_win.daemon.server.read_accent', lambda: TEAL)
    service = DaemonService(backend=build_demo_backend(),
                            persistence=Persistence(str(tmp_path / 'p.json')),
                            integrations=IntegrationSettings(str(tmp_path / 'i.json')),
                            chroma_port=0)
    service.accent._read = lambda: TEAL
    service.start()
    yield service
    service.stop()


def ears(persistence):
    return BleDevice(get_database().get(0x1532, KITTY_V2_BT), FakeBleTransport(), persistence)


# -- reading the colour ---------------------------------------------------

def test_the_registry_holds_red_in_the_low_byte(monkeypatch):
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    winreg = type(sys)('winreg')
    winreg.HKEY_CURRENT_USER = 0
    winreg.OpenKey = lambda *a: Key()
    winreg.QueryValueEx = lambda key, name: (0xFFD47800, 4)     # Windows' own blue
    monkeypatch.setitem(sys.modules, 'winreg', winreg)
    monkeypatch.setattr(accent.sys, 'platform', 'win32')
    assert accent.read_accent() == (0x00, 0x78, 0xD4)


def test_off_windows_there_is_no_accent(monkeypatch):
    monkeypatch.setattr(accent.sys, 'platform', 'linux')
    assert accent.read_accent() is None


@pytest.mark.skipif(sys.platform != 'win32', reason='asks Windows')
def test_this_pc_has_an_accent_colour():
    colour = accent.read_accent()
    assert colour is None or (len(colour) == 3 and all(0 <= c <= 255 for c in colour))


def test_the_watch_passes_on_a_change_and_only_a_change():
    colours = [RED, RED, TEAL]
    seen = []
    watch = AccentWatch(seen.append, read=lambda: colours.pop(0))
    watch.check(force=True)
    watch.check()
    watch.check()
    assert seen == [RED, TEAL]


def test_the_watch_runs_until_stopped():
    seen = []
    watch = AccentWatch(seen.append, read=lambda: RED, interval=0.01)
    watch.start()
    deadline = time.monotonic() + 2.0
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    watch.stop()
    assert seen == [RED] and not watch.running


def test_switches_are_off_until_turned_on_and_are_remembered(tmp_path):
    path = str(tmp_path / 'i.json')
    settings = IntegrationSettings(path)
    assert settings.as_dict() == {'accent': False, 'chroma': False}
    settings.set('accent', True)
    assert IntegrationSettings(path).enabled('accent')
    with pytest.raises(ValueError):
        settings.set('hue', True)


# -- following it ----------------------------------------------------------

def test_turning_it_on_colours_every_device(service):
    status = service.rpc_integration_set('accent', True)
    assert status['accent']['enabled'] and status['accent']['colour'] == list(TEAL)
    deadline = time.monotonic() + 2.0
    keyboard = service.manager.find('BlackWidow')[0]
    while time.monotonic() < deadline and service.persistence.get(
            keyboard.serial, 'backlight', 'effect') != 'static':
        time.sleep(0.02)
    for device in service.manager.devices:
        zones = [z for z, c in device.capabilities()['zones'].items() if 'static' in c['effects']]
        for zone in zones:
            assert service.persistence.get(device.serial, zone, 'effect') == 'static'
            assert service.persistence.get(device.serial, zone, 'colors')[:3] == list(TEAL)


def test_a_host_drawn_effect_gives_way_to_it(service):
    keyboard = service.manager.find('BlackWidow')[0]
    service.rpc_effect(keyboard.serial, 'spectrum_soft', {})
    service._follow_accent(TEAL)
    assert not service.effects.drawing(keyboard)


def test_the_headset_s_ears_take_it_together(service, persistence):
    headset = ears(persistence)
    service._paint_accent(headset, TEAL)
    assert headset.zone_colours_now() == [TEAL, TEAL]
    assert headset.transport.held is not None


def test_turning_it_off_stops_the_watch(service):
    service.rpc_integration_set('accent', True)
    service.rpc_integration_set('accent', False)
    assert not service.accent.running
    assert not service.rpc_integration_status()['accent']['enabled']


def test_an_unknown_integration_is_refused(service):
    from openrazer_win.daemon.protocol import RpcError
    with pytest.raises(RpcError):
        service.rpc_integration_set('hue', True)


# -- the effects that came with it -----------------------------------------

def test_breathing_rises_and_falls(persistence):
    headset = ears(persistence)
    thread = EffectThread(headset, BREATH_SOFT, {'colour': [200, 0, 0]}, KeyPressSource())
    thread.started = time.monotonic()
    thread._render_cells(BREATH_SOFT, (200, 0, 0))
    dark = thread.frame.get(0, 0)[0]
    thread.started = time.monotonic() - 2.0          # half a breath later
    thread._render_cells(BREATH_SOFT, (200, 0, 0))
    assert dark < 20 and thread.frame.get(0, 0)[0] > 180


def test_starlight_twinkles_each_led_on_its_own(persistence):
    headset = ears(persistence)
    thread = EffectThread(headset, STARLIGHT_SOFT, {'colour': [0, 0, 255]}, KeyPressSource())
    seen = set()
    for step in range(60):
        thread.started = time.monotonic() - step * 0.05
        thread._render_cells(STARLIGHT_SOFT, (0, 0, 255))
        seen.add((thread.frame.get(0, 0), thread.frame.get(0, 1)))
    assert any(left != right for left, right in seen)


def test_the_accent_effect_is_the_accent_colour_and_held(persistence, monkeypatch):
    monkeypatch.setattr('openrazer_win.effects.engine.read_accent', lambda: TEAL)
    headset = ears(persistence)
    thread = EffectThread(headset, ACCENT, {}, KeyPressSource())
    assert thread._render_cells(ACCENT, (0, 0, 0))
    assert thread.frame.get(0, 1) == TEAL and thread.still
    thread._push()
    assert headset.transport.held is not None


def test_an_ear_can_breathe_while_the_other_follows_windows(persistence, monkeypatch):
    monkeypatch.setattr('openrazer_win.effects.engine.read_accent', lambda: TEAL)
    headset = ears(persistence)
    zones = clean_zones(headset, {'left': {'effect': BREATH_SOFT, 'colour': list(RED)},
                                  'right': {'effect': ACCENT}})
    thread = EffectThread(headset, ZONES, {'zones': zones}, KeyPressSource())
    thread.started = time.monotonic() - 2.0
    thread._render_zones(0.0)
    assert thread.frame.get(0, 0)[0] > 180
    assert thread.frame.get(0, 1) == TEAL
    assert not thread.still
