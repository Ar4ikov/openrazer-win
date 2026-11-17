"""Macros stored in a Razer mouse's onboard memory (class 0x06), read only."""
from __future__ import annotations

import pytest

from openrazer_win.core.device import DeviceError
from openrazer_win.devices.recipes import NotSupported
from openrazer_win.protocol.macros import decode_events, summarize

from .conftest import make_device

#: Byte strings read from a real Basilisk V3 on 2026-11-16.
DESKTOP_LEFT = bytes.fromhex('01e001e30150025002e3110302e01114')
TASK_VIEW = bytes.fromhex('01e3012b022b02e31114')


def test_events_are_decoded():
    assert decode_events(TASK_VIEW) == [
        {'key': 'lwin', 'down': True}, {'key': 'tab', 'down': True},
        {'key': 'tab', 'down': False}, {'key': 'lwin', 'down': False},
        {'delay': 20},
    ]


def test_every_event_kind():
    data = bytes([0x08, 0x01, 0x08, 0x00, 0x0A, 0x01, 0x0A, 0xFF,
                  0x12, 0x01, 0xF4, 0x15, 0x00, 0x05, 0xFF, 0xFB])
    assert decode_events(data) == [
        {'buttons': ['left']}, {'buttons': []}, {'scroll': 1}, {'scroll': -1},
        {'delay': 500}, {'move': [5, -5]},
    ]


def test_an_event_this_port_cannot_read_stops_the_decode():
    assert decode_events(bytes([0x01, 0x04, 0x7F, 0x00])) == [
        {'key': 'a', 'down': True}, {'raw': '7f 00'}]


@pytest.mark.parametrize('data, summary', [
    (TASK_VIEW, 'Win + Tab'),
    (DESKTOP_LEFT, 'Ctrl + Win + Left'),
    (bytes.fromhex('01e0010602e002061114'), 'Ctrl + C'),
    (bytes.fromhex('010402041201f401050205'), 'A, wait 500 ms, B'),
    (bytes([0x08, 0x01, 0x08, 0x00]), 'Left click'),
    (bytes([0x7F]), '(not fully decoded)'),
])
def test_summaries(data, summary):
    assert summarize(decode_events(data)) == summary


# -- from the device -----------------------------------------------------------

@pytest.fixture
def basilisk(persistence):
    device, fake = make_device(0x0099, persistence)
    yield device, fake
    device.transport.close()


def test_macros_are_read_from_the_mouse(basilisk):
    device, fake = basilisk
    macros = {m['id']: m for m in device.get_onboard_macros()}
    assert set(macros) == {0x1615, 0x3CAE, 0x036D}
    assert macros[0x1615]['name'] == 'Macro 6'
    assert macros[0x1615]['summary'] == 'Win + Tab'
    assert macros[0x3CAE]['summary'] == 'Ctrl + Win + Left'
    assert all(r.command_id & 0x80 for r in fake.sent_reports if r.command_class == 0x06)


def test_a_busy_macro_read_is_an_error(basilisk):
    device, fake = basilisk
    fake.state.busy_reads = 1
    with pytest.raises(DeviceError):
        device.get_onboard_macros()


def test_a_mouse_that_repeats_its_macro_list_is_not_read_for_ever(basilisk, monkeypatch):
    device, fake = basilisk
    fake.state.onboard['macros'] = {0x0100 + i: ('M{0}'.format(i), b'') for i in range(32)}
    answer = fake._respond_macro

    def first_page_always(cmd, args, response):
        if cmd == 0x8B:
            args = bytes((0, 0)) + bytes(args[2:])
        answer(cmd, args, response)

    monkeypatch.setattr(fake, '_respond_macro', first_page_always)
    assert len(device.get_onboard_macros()) == 32
    pages = [r for r in fake.sent_reports if (r.command_class, r.command_id) == (0x06, 0x8B)]
    assert len(pages) == 2


def test_forty_macros_take_two_pages(basilisk):
    device, fake = basilisk
    fake.state.onboard['macros'] = {0x0100 + i: ('M{0}'.format(i), b'') for i in range(40)}
    assert [m['id'] for m in device.get_onboard_macros()] == [0x0100 + i for i in range(40)]


def test_a_macro_bigger_than_believable_is_read_only_so_far(basilisk):
    device, fake = basilisk
    fake.state.onboard['macros'] = {0x0100: ('Long', bytes((0x01, 0x04, 0x02, 0x04)) * 0x2000)}
    device.get_onboard_macros()
    reads = [r for r in fake.sent_reports if (r.command_class, r.command_id) == (0x06, 0x89)]
    assert len(reads) == -(-0x4000 // 72)


def test_other_mice_are_not_offered_macros(persistence):
    other, _ = make_device(0x0078, persistence)
    try:
        with pytest.raises(NotSupported):
            other.get_onboard_macros()
    finally:
        other.transport.close()
