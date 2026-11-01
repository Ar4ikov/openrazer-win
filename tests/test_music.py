"""The effect that follows whatever the PC is playing."""
from __future__ import annotations

import sys
import threading
import time

import pytest

from openrazer_win.core.persistence import Persistence
from openrazer_win.devices.recipes import NotSupported
from openrazer_win.effects import audio
from openrazer_win.effects.engine import (
    MUSIC, MUSIC_FLOOR, SOFTWARE_EFFECTS, EffectEngine, EffectThread, KeyPressSource,
)

from .conftest import make_device

BLACKWIDOW = 0x0203


class Meter:
    def __init__(self, *levels):
        self.levels = list(levels)
        self.closed = threading.Event()

    def peak(self):
        return self.levels.pop(0) if len(self.levels) > 1 else self.levels[0]

    def close(self):
        self.closed.set()


def thread_for(device, meter, colour=(0, 200, 100)):
    thread = EffectThread(device, MUSIC, {'colour': list(colour)}, KeyPressSource())
    thread.meter = meter
    return thread


@pytest.fixture
def device(tmp_path):
    device, _fake = make_device(BLACKWIDOW, Persistence(str(tmp_path / 'p.json')))
    return device


def lit(thread, row=0):
    return [thread.frame.get(row, column)[1] for column in range(thread.frame.columns)]


def test_it_is_an_effect_the_engine_keeps_and_brings_back():
    assert MUSIC in SOFTWARE_EFFECTS


def test_loud_lights_the_whole_device_in_its_colour(device):
    thread = thread_for(device, Meter(1.0))
    assert thread._render_music()
    assert thread.frame.get(0, 0) == (0, 200, 100)
    assert set(lit(thread)) == {200}


def test_quiet_glows_at_the_middle(device):
    thread = thread_for(device, Meter(1.0, 0.5))
    thread._render_music()
    thread.level = 0.0
    thread._render_music()
    row = lit(thread)
    middle = len(row) // 2
    assert row[middle] > row[0] > 0
    assert row[middle] > row[-1] > 0


def test_the_beat_shows_at_any_volume(device):
    thread = thread_for(device, Meter(0.2, 0.05, 0.2))
    thread._render_music()
    assert max(lit(thread)) == 200
    thread.level = 0.0
    thread._render_music()
    assert max(lit(thread)) < 50
    thread._render_music()
    assert max(lit(thread)) == 200


def test_a_whisper_lights_nothing(device):
    thread = thread_for(device, Meter(0.002))
    thread._render_music()
    assert max(lit(thread)) <= 200 * MUSIC_FLOOR


def test_silence_leaves_a_glow_and_then_nothing_to_send(device):
    thread = thread_for(device, Meter(0.0))
    assert thread._render_music()
    assert 0 < max(lit(thread)) <= 200 * MUSIC_FLOOR
    assert not thread._render_music()


def test_it_rises_at_once_and_falls_slowly(device):
    thread = thread_for(device, Meter(1.0, 0.0))
    thread._render_music()
    assert max(lit(thread)) == 200
    thread._render_music()
    assert 100 < max(lit(thread)) < 200


def test_a_strange_reading_does_not_stop_it(device):
    thread = thread_for(device, Meter(-1.0))
    thread._render_music()
    assert 0 < max(lit(thread)) <= 200 * MUSIC_FLOOR


def test_two_cells_pulse_together(tmp_path):
    from openrazer_win.effects.frame import Frame
    frame = Frame(1, 2)

    class Thread(EffectThread):
        def __init__(self):     # no device needed for the drawing
            self.frame = frame
            self.meter = Meter(0.25)
            self.level = 0.0
            self.colour = (0, 255, 0)

    thread = Thread()
    thread.recent_peak = 1.0
    thread._render_music()
    assert frame.get(0, 0) == frame.get(0, 1)
    assert frame.get(0, 0)[1] > 0


def test_the_engine_runs_it_and_closes_the_meter(device, monkeypatch):
    meter = Meter(0.5)
    monkeypatch.setattr('openrazer_win.effects.engine.open_meter', lambda: meter)
    pushed = threading.Event()
    device_set_key_row = device.set_key_row
    device.set_key_row = lambda payload: (pushed.set(), device_set_key_row(payload))
    manager = type('M', (), {'persistence': device.persistence})()
    engine = EffectEngine(manager)
    try:
        started = engine.set_effect(device, MUSIC, {'colour': [255, 0, 0], 'refresh_rate': 0.005})
        assert started['effect'] == MUSIC
        assert pushed.wait(2.0)
        assert engine.status()['active'] == {device.serial: MUSIC}
        assert engine.saved(device.serial)['effect'] == MUSIC
        engine.set_effect(device, 'none', {})
        assert meter.closed.wait(2.0)
    finally:
        engine.stop()


def test_a_device_with_nothing_to_draw_on_is_refused(tmp_path):
    from openrazer_win.protocol.blackshark import PID_WIRELESS
    headset, _fake = make_device(PID_WIRELESS, Persistence(str(tmp_path / 'p.json')))
    manager = type('M', (), {'persistence': headset.persistence})()
    engine = EffectEngine(manager)
    with pytest.raises(NotSupported):
        engine.set_effect(headset, MUSIC, {})


def test_the_silent_meter_is_silent():
    meter = audio.SilentMeter()
    assert meter.peak() == 0.0
    meter.close()


def test_off_windows_there_is_only_silence(monkeypatch):
    monkeypatch.setattr(audio.sys, 'platform', 'linux')
    assert isinstance(audio.open_meter(), audio.SilentMeter)


@pytest.mark.skipif(sys.platform != 'win32', reason='asks Windows')
def test_windows_gives_a_reading_or_silence():
    """Whatever this PC has: a number from 0 to 1, and no crash without an output."""
    meter = audio.open_meter()
    try:
        first = meter.peak()
        time.sleep(0.01)
        second = meter.peak()
        assert 0.0 <= first <= 1.0 and 0.0 <= second <= 1.0
    finally:
        meter.close()


@pytest.mark.skipif(sys.platform != 'win32', reason='asks Windows')
def test_the_meter_reopens_when_it_is_time(monkeypatch):
    monkeypatch.setattr(audio, 'REOPEN_SECONDS', 0.0)
    meter = audio.open_meter()
    try:
        assert meter.peak() >= 0.0
        assert meter.peak() >= 0.0
    finally:
        meter.close()
