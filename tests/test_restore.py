"""Restoring only what was asked for.

The daemon replays saved lighting when a device appears.  Anything it replays
that the user never chose -- a default colour, a default effect -- overwrites
whatever the device was happily showing, so these tests pin down that a
default which was only *read*, or a zone that was only partly set, never turns
into an effect on the wire.
"""
from __future__ import annotations

import pytest

from openrazer_win.core.persistence import DEFAULT_ZONE_STATE, Persistence

from .conftest import KEYBOARD_PID, make_device
from .test_kraken import KITTY_V2, build


def test_reading_a_zone_stores_nothing(persistence):
    before = persistence.as_dict()
    state = persistence.zone('SERIAL', 'backlight')
    assert state == DEFAULT_ZONE_STATE
    state['effect'] = 'static'
    assert persistence.saved_zone('SERIAL', 'backlight') is None
    assert persistence.as_dict() == before


def test_setting_one_key_does_not_save_the_defaults_beside_it(persistence):
    persistence.set('SERIAL', 'backlight', 'brightness', 40)
    assert persistence.saved_zone('SERIAL', 'backlight') == {'brightness': 40}
    # Reads still see a complete state.
    assert persistence.zone('SERIAL', 'backlight')['effect'] == DEFAULT_ZONE_STATE['effect']
    assert persistence.get('SERIAL', 'backlight', 'brightness') == 40


def test_a_brightness_change_alone_does_not_restore_an_effect(persistence):
    device, fake = make_device(KEYBOARD_PID, persistence)
    try:
        device.set_brightness(40)
        fake.state.led_effect.clear()
        fake.state.sent.clear()
        device.restore()
        assert fake.state.led_effect == {}
        # Brightness is all that was set, so it is all that goes back out.
        assert {(r.command_class, r.command_id) for r in fake.sent_reports} <= {
            (0x00, 0x82), (0x0F, 0x04), (0x03, 0x03)}
    finally:
        device.close()


def test_a_partial_zone_survives_a_reload(tmp_path):
    path = str(tmp_path / 'persistence.json')
    first = Persistence(path)
    first.set('SERIAL', 'logo', 'effect', 'static')
    first.save()
    assert Persistence(path).saved_zone('SERIAL', 'logo') == {'effect': 'static'}


def test_a_kraken_that_was_never_set_is_left_alone(persistence):
    device, fake = build(KITTY_V2, persistence)
    try:
        fake.kraken_writes.clear()
        device.restore()
        assert fake.kraken_writes == []
    finally:
        device.close()


def test_a_kraken_still_restores_what_was_set(persistence):
    device, fake = build(KITTY_V2, persistence)
    try:
        device.set_static(1, 2, 3)
        fake.kraken_writes.clear()
        device.restore()
        assert fake.kraken_writes
    finally:
        device.close()


def test_a_bluetooth_headset_that_was_never_set_is_left_alone(headset):
    device, transport = headset
    device.restore()
    assert transport.colour_writes == []


@pytest.mark.parametrize('effect, args, colours', [
    ('breath_dual', (1, 2, 3, 4, 5, 6), [1, 2, 3, 4, 5, 6]),
    ('breath_single', (7, 8, 9), [7, 8, 9]),
    ('reactive', (10, 11, 12, 2), [10, 11, 12]),
])
def test_every_coloured_effect_saves_its_colours(persistence, effect, args, colours):
    device, fake = make_device(KEYBOARD_PID, persistence)
    try:
        getattr(device, 'set_' + effect)(*args)
        saved = persistence.saved_zone(device.serial, 'backlight')
        assert saved['effect'] == effect
        assert saved['colors'][:len(colours)] == colours
        # ...and puts them back: the last report on the wire carries them.
        fake.state.sent.clear()
        device.restore()
        sent = [bytes(r.arguments) for r in fake.sent_reports if r.command_class == 0x03]
        assert any(bytes(colours[:3]) in payload for payload in sent)
    finally:
        device.close()


def test_the_unused_colour_slots_keep_what_they_held(persistence):
    device, _ = make_device(KEYBOARD_PID, persistence)
    try:
        device.set_breath_dual(1, 2, 3, 4, 5, 6)
        device.set_static(9, 9, 9)
        assert persistence.saved_zone(device.serial, 'backlight')['colors'][:6] == [9, 9, 9, 4, 5, 6]
    finally:
        device.close()
