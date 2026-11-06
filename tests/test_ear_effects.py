"""Each ear of the Bluetooth headset with an effect of its own."""
from __future__ import annotations

import time

import pytest

from openrazer_win.client import Fx
from openrazer_win.core.ble_device import BleDevice
from openrazer_win.daemon.server import DaemonService
from openrazer_win.devices import get_database
from openrazer_win.devices.recipes import NotSupported
from openrazer_win.effects.engine import (
    MUSIC, ZONES, EffectEngine, EffectThread, KeyPressSource, clean_zones,
)
from openrazer_win.hid.fake import FakeHidBackend

from .conftest import KITTY_V2_BT, FakeBleTransport

RED = [255, 0, 0]
BLUE = [0, 0, 255]


class Meter:
    def peak(self):
        return 0.5

    def close(self):
        pass


@pytest.fixture
def ears(persistence):
    return BleDevice(get_database().get(0x1532, KITTY_V2_BT), FakeBleTransport(), persistence)


@pytest.fixture
def engine(ears, monkeypatch):
    monkeypatch.setattr('openrazer_win.effects.engine.open_meter', Meter)
    engine = EffectEngine(type('M', (), {'persistence': ears.persistence})())
    yield engine
    engine.stop()


def thread_for(ears, zones, phase_meter=None):
    thread = EffectThread(ears, ZONES, {'zones': clean_zones(ears, zones)}, KeyPressSource())
    thread.meter = phase_meter
    return thread


def test_one_ear_gets_an_effect_and_the_other_keeps_its_colour(ears, engine):
    ears.set_zone_colours([RED, BLUE])
    started = engine.set_effect(ears, MUSIC, {'zone': 'left', 'colour': [0, 255, 0]})
    assert started['effect'] == ZONES
    zones = engine.status()['options'][ears.serial]['zones']
    assert zones['left'] == {'effect': MUSIC, 'colour': [0, 255, 0]}
    assert zones['right'] == {'effect': 'static', 'colour': BLUE}
    assert engine.saved(ears.serial)['effect'] == ZONES


def test_a_dark_ear_reads_as_off(ears, engine):
    ears.set_zone_colours([RED, [0, 0, 0]])
    engine.set_effect(ears, 'spectrum_soft', {'zone': 'left'})
    assert engine.zones_now(ears)['right']['effect'] == 'none'


def test_an_effect_on_both_ears_is_kept_when_one_ear_changes(ears, engine):
    engine.set_effect(ears, 'wave_soft', {'colour': RED})
    engine.set_effect(ears, 'static', {'zone': 'right', 'colour': BLUE})
    zones = engine.zones_now(ears)
    assert zones['left'] == {'effect': 'wave_soft', 'colour': RED}
    assert zones['right'] == {'effect': 'static', 'colour': BLUE}


def test_each_ear_is_drawn_in_its_own_column(ears):
    thread = thread_for(ears, {'left': {'effect': 'spectrum_soft'},
                               'right': {'effect': 'static', 'colour': BLUE}})
    assert thread._render_zones(0.0)
    first = thread.frame.get(0, 0)
    thread._render_zones(1.0)
    assert thread.frame.get(0, 0) != first           # the spectrum moves on
    assert thread.frame.get(0, 1) == tuple(BLUE)     # the static ear does not
    assert not thread._render_zones(1.0)             # nothing new to send


def test_a_wave_goes_from_one_ear_to_the_other(ears):
    thread = thread_for(ears, {'left': {'effect': 'wave_soft', 'colour': RED},
                               'right': {'effect': 'wave_soft', 'colour': RED}})
    thread._render_zones(0.0)
    left, right = thread.frame.get(0, 0)[0], thread.frame.get(0, 1)[0]
    assert left == 255 and right == 0


def test_music_on_one_ear_is_heard_and_the_other_stays_dark(ears):
    thread = thread_for(ears, {'left': {'effect': MUSIC, 'colour': RED}}, Meter())
    thread.recent_peak = 1.0
    thread._render_zones(0.0)
    assert thread.frame.get(0, 0)[0] > 0
    assert thread.frame.get(0, 1) == (0, 0, 0)


def test_the_running_effect_draws_frames_on_the_headset(ears, engine):
    transport = ears.transport
    engine.set_effect(ears, 'spectrum_soft', {'zone': 'left', 'refresh_rate': 0.005})
    deadline = time.monotonic() + 2.0
    while len(transport.colour_writes) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(transport.colour_writes) >= 3


def test_ears_that_need_nothing_drawn_are_given_to_the_headset_to_hold(ears, engine):
    engine.set_effect(ears, MUSIC, {'zone': 'left', 'colour': RED})
    result = engine.set_effect(ears, ZONES, {'zones': {
        'left': {'effect': 'static', 'colour': RED}, 'right': {'effect': 'none'}}})
    assert result['effect'] is None
    assert not engine.drawing(ears)
    assert engine.saved(ears.serial) is None
    assert ears.zone_colours_now() == [tuple(RED), (0, 0, 0)]
    assert ears.transport.held is not None


def test_the_ears_come_back_with_the_daemon(ears, engine):
    engine.set_effect(ears, MUSIC, {'zone': 'right', 'colour': BLUE})
    engine.clear(ears)
    assert engine.restore(ears)
    assert engine.zones_now(ears)['right'] == {'effect': MUSIC, 'colour': BLUE}


@pytest.mark.parametrize('zones', [
    {'left': {'effect': 'ripple'}},
    {'middle': {'effect': 'static'}},
    {'left': 'red'},
])
def test_what_an_ear_cannot_show_is_refused(ears, zones):
    with pytest.raises(ValueError):
        clean_zones(ears, zones)


def test_a_whole_device_effect_cannot_go_on_one_ear(ears, engine):
    with pytest.raises(NotSupported):
        engine.set_effect(ears, 'ripple', {'zone': 'left'})


def test_a_device_without_ears_has_no_zones_effect(persistence):
    from .conftest import make_device
    keyboard, _fake = make_device(0x0203, persistence)
    with pytest.raises(NotSupported):
        clean_zones(keyboard, {})


def test_a_colour_for_one_ear_joins_what_the_other_is_drawing(ears, persistence, monkeypatch):
    monkeypatch.setattr('openrazer_win.effects.engine.open_meter', Meter)
    service = DaemonService(backend=FakeHidBackend(), persistence=persistence)
    service.manager._devices['ble'] = ears
    try:
        service.rpc_effect(ears.serial, MUSIC, {'zone': 'left', 'colour': RED})
        service.rpc_call(ears.serial, 'set_static', [0, 0, 255], {'zone': 'right'})
        zones = service.effects.zones_now(ears)
        assert zones['left']['effect'] == MUSIC
        assert zones['right'] == {'effect': 'static', 'colour': BLUE}
        # Both ears the device's own again: nothing left to draw.
        service.rpc_call(ears.serial, 'set_none', [], {'zone': 'left'})
        assert not service.effects.drawing(ears)
        assert ears.zone_colours_now() == [(0, 0, 0), tuple(BLUE)]
    finally:
        service.effects.stop()


def test_the_client_sends_an_ear_s_effect_with_its_zone():
    sent = []

    class Device:
        capabilities = {'zone_order': ['left', 'right']}

        def _software_effect(self, effect, options):
            sent.append((effect, options))

    Fx(Device(), 'left').music(1, 2, 3)
    Fx(Device(), 'backlight').music(1, 2, 3)
    assert sent[0][1]['zone'] == 'left'
    assert 'zone' not in sent[1][1]
