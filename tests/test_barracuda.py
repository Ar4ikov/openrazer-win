"""The Barracuda X (2022): its tunnel, and the device built on it."""
from __future__ import annotations

import dataclasses
import sys
import threading

import pytest

from openrazer_win.cli import main
from openrazer_win.core.barracuda import IDLE_SECONDS, BarracudaDevice
from openrazer_win.core.device import DeviceError
from openrazer_win.core.manager import DeviceManager
from openrazer_win.core.persistence import Persistence
from openrazer_win.core.transport import TunnelChannel, select_barracuda_interface
from openrazer_win.daemon.demo import DEMO_DEVICES, build_demo_backend
from openrazer_win.daemon.protocol import RpcError
from openrazer_win.daemon.server import DEVICE_METHODS, DaemonServer, DaemonService
from openrazer_win.devices.database import get_database
from openrazer_win.devices.recipes import NotSupported
from openrazer_win.hid import win32
from openrazer_win.hid.base import HidDeviceInfo, HidError
from openrazer_win.hid.fake import FakeBarracuda, FakeHidBackend, FakeRazerDevice
from openrazer_win.protocol import barracuda as protocol
from openrazer_win.protocol.barracuda import Family, Link, Register

PID = protocol.PID_WIRELESS
SERIAL = 'BARX0552'

LINKED = (Family.LINK, bytes((Link.GET_LINK,)))
TRANSPORT = (Family.LINK, bytes((Link.GET_TRANSPORT,)))
ROUTE = (Family.LINK, bytes((Link.GET_ROUTE,)))
REMOTE = (Family.LINK, bytes((Link.SET_ROUTE, 1)))
LOCAL = (Family.LINK, bytes((Link.SET_ROUTE, 0)))
READ_LENGTH = (Family.DIAG, protocol.READ_MAX_LEN)
#: What goes before every question to the headset, and what comes after it.
BEFORE = [LINKED, TRANSPORT, ROUTE, REMOTE, ROUTE]
AFTER = [LOCAL, ROUTE]


def build(tmp_path, timeout=0.05):
    meta = get_database().get(0x1532, PID)
    fake = FakeRazerDevice(PID, meta.name, headset=True)
    channel = TunnelChannel(FakeHidBackend([fake]), fake.headset_info, timeout=timeout)
    return BarracudaDevice(meta, channel, Persistence(str(tmp_path / 'p.json'))), fake


@pytest.fixture
def headset(tmp_path):
    return build(tmp_path)


def asked(fake):
    """What the headset itself was asked, in the order it came."""
    return [payload for family, payload in fake.headset.requests
            if family == Family.CUSTOMER]


def routes(fake):
    """Where the receiver was told to send things, in the order it was told."""
    return [payload[1] for family, payload in fake.headset.requests
            if family == Family.LINK and payload[0] == Link.SET_ROUTE]


def frame(family, data, counter=0xC5):
    return bytes((0x50, 0x49, family, counter, 0, 0, 0, 0, len(data), 0)) + bytes(data)


def report(chunk):
    return FakeBarracuda.report(bytes(chunk))


# -- reports ------------------------------------------------------------------

def test_a_question_to_the_receiver_is_laid_out_as_documented():
    sent = protocol.request(Family.LINK, 0x61, (Link.GET_LINK,))
    assert len(sent) == protocol.REPORT_SIZE
    assert sent[:9] == bytes.fromhex('01 80 06 50 41 0e 61 01 e3')
    assert not any(sent[9:])


def test_reading_a_register_is_laid_out_as_documented():
    sent = protocol.request(Family.CUSTOMER, 0x62, protocol.get(Register.BATTERY))
    assert sent[:11] == bytes.fromhex('01 80 08 50 41 08 62 03 21 00 00')
    assert not any(sent[11:])
    cable = protocol.request(Family.CUSTOMER, 0x63, protocol.get(Register.CHARGING))
    assert cable[:11] == bytes.fromhex('01 80 08 50 41 08 63 03 2a 00 00')


def test_writing_a_register_is_reading_it_with_the_top_bit_set_and_a_length():
    assert protocol.put(Register.EQ_PRESET, (7,)) == bytes.fromhex('93 00 01 07')
    sent = protocol.request(Family.CUSTOMER, 0x60, protocol.put(Register.IDLE_MINUTES, (30,)))
    assert sent[:12] == bytes.fromhex('01 80 09 50 41 08 60 04 ac 00 01 1e')


def test_the_one_family_with_a_long_length_starts_a_byte_later():
    sent = protocol.request(Family.DIAG, 0x64, protocol.READ_MAX_LEN)
    assert sent[:22] == bytes.fromhex(
        '01 80 13 50 41 06 64 0d 00 25 34 12 5a 5a 01 00 00 00 f0 00 00 00')
    assert not any(sent[22:])


def test_a_request_cannot_overrun_the_report():
    with pytest.raises(ValueError):
        protocol.request(Family.CUSTOMER, 0x60, bytes(57))
    assert len(protocol.request(Family.CUSTOMER, 0x60, bytes(56))) == 64


def test_sequence_numbers_go_round():
    assert protocol.next_sequence(0) == 0x60
    assert protocol.next_sequence(0x60) == 0x61
    assert protocol.next_sequence(0x7E) == 0x7F
    assert protocol.next_sequence(0x7F) == 0x60


def test_a_frame_in_one_report_is_read_as_captured():
    """The receiver saying a headset is linked, as a capture of one has it."""
    stream = protocol.Reassembler()
    frames = stream.feed(bytes.fromhex('01 80 0C 50 49 0E F5 00 00 00 00 02 00 E3 01 00 00'))
    assert len(frames) == 1 and len(frames[0]) == 12
    assert protocol.link_reply(frames[0], Link.GET_LINK) == 1
    assert protocol.link_reply(frames[0], Link.GET_ROUTE) is None


def test_what_the_headset_says_unasked_is_told_from_a_reply():
    stream = protocol.Reassembler()
    said = stream.feed(bytes.fromhex(
        '01 80 0E 50 49 08 F7 0D 32 69 00 04 00 21 02 01 36').ljust(64, b'\x00'))[0]
    assert protocol.notification(said) == (Register.BATTERY, 54)
    assert protocol.register_reply(said, Register.BATTERY) is None
    answered = frame(Family.CUSTOMER, (0x21, 0x01, 0x01, 0x64))
    assert protocol.register_reply(answered, Register.BATTERY) == b'\x64'
    assert protocol.notification(answered) is None
    assert protocol.register_reply(answered, Register.CHARGING) is None


def test_a_reply_longer_or_shorter_than_it_says_is_not_one():
    bands = frame(Family.CUSTOMER, bytes((0x15, 0x01, 10)) + bytes(10))
    assert protocol.register_reply(bands, Register.EQ_BANDS) == bytes(10)
    assert protocol.register_reply(bands[:-1], Register.EQ_BANDS) is None
    wrong = bytearray(bands)
    wrong[12] = 9
    assert protocol.register_reply(bytes(wrong), Register.EQ_BANDS) is None


def test_a_frame_cut_in_two_is_put_back_together():
    whole = frame(Family.CUSTOMER, bytes((0x15, 0x01, 10)) + bytes(range(10)))
    stream = protocol.Reassembler()
    assert stream.feed(report(whole[:7])) == []
    # The play/pause button has a report of its own, and may come in between.
    assert stream.feed(FakeBarracuda.MEDIA_PRESS) == []
    assert stream.feed(report(whole[7:])) == [whole]


def test_two_frames_in_one_report_are_two_frames():
    first = frame(Family.ACK, (Family.LINK, 0xE1, 0x00))
    second = frame(Family.LINK, (Link.GET_ROUTE, 1))
    assert protocol.Reassembler().feed(report(first + second)) == [first, second]


@pytest.mark.parametrize('bad', [
    bytes.fromhex('01 80 3e') + bytes(61),                 # more than a report holds
    bytes.fromhex('01 7f 02 50 49'),                       # not the tunnel
    bytes.fromhex('01 80 08 50 49'),                       # shorter than it says
    bytes.fromhex('01 80'),
])
def test_a_report_that_cannot_be_right_drops_what_was_half_read(bad):
    whole = frame(Family.LINK, (Link.GET_LINK, 1))
    stream = protocol.Reassembler()
    assert stream.feed(report(whole[:5])) == []
    assert stream.feed(bad) == []
    # The next whole frame is read as one, not as the rest of the last.
    assert stream.feed(report(whole)) == [whole]


@pytest.mark.parametrize('chunk', [
    bytes.fromhex('de ad be ef'),                           # not the start of a frame
    bytes.fromhex('50 41 0e 60 01 e3'),                     # a frame of the host's
    bytes.fromhex('50 49 08 00 00 00 00 00 f8 00 01 02'),   # longer than a frame can be
])
def test_a_stream_that_cannot_be_right_starts_again_at_the_next_report(chunk):
    whole = frame(Family.LINK, (Link.GET_LINK, 1))
    stream = protocol.Reassembler()
    assert stream.feed(report(chunk)) == []
    assert stream.feed(report(whole)) == [whole]


def test_an_acknowledgment_is_for_one_request_only():
    good = frame(Family.ACK, (Family.DIAG, 0xE4, 0x00, 0xF0, 0, 0, 0))
    assert protocol.ack(good, Family.DIAG, 0x64) == protocol.READ_MAX_LEN_RESULT
    assert protocol.ack(good, Family.DIAG, 0x65) is None
    assert protocol.ack(good, Family.LINK, 0x64) is None
    assert protocol.ack(frame(Family.ACK, (Family.LINK, 0xE4, 0x00)), Family.LINK, 0x64) == b''
    with pytest.raises(protocol.ProtocolError):
        protocol.ack(frame(Family.ACK, (Family.LINK, 0xE4, 0x02)), Family.LINK, 0x64)


def test_bands_are_kept_five_above_what_they_are():
    gains = [-5, -1, 0, 1, 5, 0, 0, 0, 0, 0]
    encoded = protocol.encode_bands(gains)
    assert encoded[:5] == bytes((0, 4, 5, 6, 10))
    assert protocol.decode_bands(encoded) == gains


@pytest.mark.parametrize('gains', [[0] * 9, [0] * 11, [6] + [0] * 9, [-6] + [0] * 9])
def test_a_curve_it_cannot_hold_is_refused(gains):
    with pytest.raises(ValueError):
        protocol.encode_bands(gains)


def test_a_curve_it_could_not_have_sent_is_not_taken():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_bands(bytes((11,)) + bytes(9))
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_bands(bytes(9))


# -- finding it ---------------------------------------------------------------

def collection(interface, number, usage_page, size=64):
    return HidDeviceInfo(
        path='\\\\?\\hid#vid_1532&pid_0552&mi_{0:02d}&col{1:02d}#x'.format(interface, number),
        vendor_id=0x1532, product_id=PID, interface=interface, collection=number,
        usage_page=usage_page, usage=1, input_length=size, output_length=size)


def test_the_vendor_collection_on_interface_three_is_the_one():
    seen = [collection(3, 2, 0x000C, size=5), collection(2, 1, 0xFF01),
            collection(3, 1, 0xFF00), collection(3, 3, 0xFF02)]
    chosen = select_barracuda_interface(seen)
    assert (chosen.interface, chosen.collection) == (3, 1)


def test_failing_a_vendor_collection_any_with_reports_of_the_size_will_do():
    seen = [collection(3, 2, 0x000C, size=5), collection(3, 1, 0x000C)]
    assert select_barracuda_interface(seen).collection == 1
    # A vendor one elsewhere is still better than that.
    assert select_barracuda_interface(seen + [collection(0, 1, 0xFF00)]).interface == 0


def test_nothing_usable_is_nothing():
    assert select_barracuda_interface([collection(3, 2, 0x000C, size=5)]) is None
    one_way = dataclasses.replace(collection(3, 1, 0xFF00), output_length=0)
    assert select_barracuda_interface([one_way]) is None


def test_it_is_in_the_database():
    meta = get_database().get(0x1532, PID)
    assert meta.name == 'Razer Barracuda X (Wireless)'
    assert meta.type == 'headset' and not meta.has_matrix and not meta.is_bluetooth


def test_the_manager_builds_it(tmp_path):
    fake = FakeRazerDevice(PID, 'Razer Barracuda X 2.4', headset=True)
    backend = FakeHidBackend([fake])
    with DeviceManager(backend=backend, persistence=Persistence(str(tmp_path / 'p.json')),
                       bluetooth=False) as manager:
        device = manager.devices[0]
        assert isinstance(device, BarracudaDevice)
        assert isinstance(device.channel, TunnelChannel)
        assert manager.unsupported == []
        assert device.channel.info is fake.headset_info
        assert device.get_battery_level() == 85


def test_a_receiver_with_no_such_reports_is_left_out(tmp_path, caplog):
    fake = FakeRazerDevice(PID, 'Razer Barracuda X 2.4')
    manager = DeviceManager(backend=FakeHidBackend([fake]), bluetooth=False,
                            persistence=Persistence(str(tmp_path / 'p.json')))
    assert manager.scan() == []
    assert 'no 64-byte input and output reports' in caplog.text


def test_it_can_be_emulated_and_is_not_in_the_demo_for_all_that():
    backend = build_demo_backend((PID,))
    assert isinstance(backend.devices[0].headset, FakeBarracuda)
    assert PID not in DEMO_DEVICES


# -- the device ---------------------------------------------------------------

def test_nothing_is_asked_when_it_is_found(headset):
    device, fake = headset
    device.enter_driver_mode()
    device.restore()
    assert device.serial and device.capabilities()
    assert fake.headset.requests == []


def test_it_goes_by_a_fixed_name_or_by_the_serial_windows_read(headset, tmp_path):
    device, fake = headset
    fake.headset.switched_on = False
    assert device.serial == SERIAL
    other, fake = build(tmp_path)
    other.channel.info = dataclasses.replace(fake.headset_info, serial='PM22-05 1234')
    assert other.serial == 'PM22051234'


def test_nobody_knows_how_to_ask_for_its_firmware(headset):
    device, fake = headset
    with pytest.raises(NotSupported):
        _ = device.firmware_version
    assert fake.headset.requests == []


def test_it_has_nothing_to_light_and_says_what_it_can_be_set_to(headset):
    device, _fake = headset
    capabilities = device.capabilities()
    assert capabilities['zones'] == {}
    assert capabilities['battery'] and capabilities['idle_time']
    assert capabilities['protocol'] == 'barracuda'
    assert capabilities['idle_time_max'] == 3600
    assert capabilities['idle_time_choices'] == [0, 300, 900, 1800, 2700, 3600]
    audio = capabilities['audio']
    assert audio['equalizer_presets'] == ['flat', 'game', 'music', 'movie', 'custom']
    assert audio['equalizer_frequencies'][0] == 31 and len(audio['equalizer_frequencies']) == 10
    assert audio['equalizer_range'] == [-5, 5]
    assert audio['equalizer_switch'] is False and 'equalizer_curve_per_preset' not in audio
    assert audio['sidetone_max'] == 0 and audio['dongle_led_modes'] == []
    assert device.available_effects() == [] and not device.has_effect('static')
    with pytest.raises(NotSupported):
        device.run('read:device_serial')


def test_the_battery_is_asked_for_with_the_route_set_and_put_back(headset):
    device, fake = headset
    assert device.get_battery_level() == 85
    assert fake.headset.requests == (
        BEFORE + [(Family.CUSTOMER, bytes.fromhex('21 00 00'))] + AFTER)
    assert fake.headset.route == protocol.ROUTE_LOCAL
    fake.headset.registers[Register.BATTERY] = b'\x64'
    assert device.get_battery_level() == 100


def test_what_goes_out_is_numbered_in_turn(headset, monkeypatch):
    device, fake = headset
    written = []
    write = fake.headset.write
    monkeypatch.setattr(fake.headset, 'write',
                        lambda data: written.append(data) or write(data))
    device.get_battery_level()
    assert [data[6] for data in written] == list(range(0x60, 0x68))
    assert written[0][:9] == bytes.fromhex('01 80 06 50 41 0e 60 01 e3')
    assert written[5][:11] == bytes.fromhex('01 80 08 50 41 08 65 03 21 00 00')
    assert all(len(data) == 64 for data in written)


def test_the_cable_is_all_it_says_about_charging(headset):
    device, fake = headset
    assert device.is_charging() is False
    fake.headset.registers[Register.CHARGING] = b'\x01'
    assert device.is_charging() is True
    # Neither needs the preliminary that the settings do.
    assert READ_LENGTH not in fake.headset.requests


def test_a_setting_is_asked_for_after_the_read_length(headset):
    device, fake = headset
    assert device.get_idle_time() == 900
    assert fake.headset.requests == (
        BEFORE + [READ_LENGTH, (Family.CUSTOMER, bytes.fromhex('2c 00 00'))] + AFTER)


@pytest.mark.parametrize('seconds', IDLE_SECONDS)
def test_the_sleep_timer_takes_six_lengths_of_time(headset, seconds):
    device, fake = headset
    device.set_idle_time(seconds)
    assert fake.headset.registers[Register.IDLE_MINUTES] == bytes((seconds // 60,))
    assert device.get_idle_time() == seconds
    # Written, and then read back to see that it took.
    assert asked(fake)[:2] == [bytes((0xAC, 0x00, 0x01, seconds // 60)), b'\x2c\x00\x00']


@pytest.mark.parametrize('seconds', [-1, 60, 600, 1799, 3601, 7200])
def test_any_other_length_is_refused_with_the_ones_there_are(headset, seconds):
    device, fake = headset
    with pytest.raises(DeviceError, match='5, 15, 30, 45 or 60 minutes, or 0 for never'):
        device.set_idle_time(seconds)
    assert fake.headset.requests == []


def test_all_the_sound_settings_at_once_under_one_route(headset):
    device, fake = headset
    fake.headset.registers[Register.EQ_PRESET] = b'\x08'
    fake.headset.registers[Register.EQ_BANDS] = bytes((6, 5, 4, 5, 5, 5, 5, 5, 5, 10))
    fake.headset.registers[Register.DO_NOT_DISTURB] = b'\x01'
    assert device.get_audio() == {
        'connected': True, 'equalizer': True, 'equalizer_preset': 'music',
        'equalizer_bands': [1, 0, -1, 0, 0, 0, 0, 0, 0, 5], 'do_not_disturb': True,
    }
    assert fake.headset.requests == BEFORE + [READ_LENGTH] + [
        (Family.CUSTOMER, bytes((register, 0, 0)))
        for register in (Register.EQ_PRESET, Register.EQ_BANDS, Register.DO_NOT_DISTURB)
    ] + AFTER


def test_a_preset_is_chosen_by_name(headset):
    device, fake = headset
    for name, code in protocol.EQ_PRESETS.items():
        device.set_equalizer_preset(name)
        assert fake.headset.registers[Register.EQ_PRESET] == bytes((code,))
        assert device.get_equalizer_preset() == name
    assert asked(fake)[:2] == [bytes.fromhex('93 00 01 00'), bytes.fromhex('13 00 00')]
    with pytest.raises(DeviceError, match='no such preset'):
        device.set_equalizer_preset('default')


def test_a_curve_is_the_custom_preset_s_and_setting_it_switches_to_that(headset):
    device, fake = headset
    device.set_equalizer_preset('game')
    fake.headset.requests.clear()
    curve = [-5, -3, 0, 1, 2, 3, 4, 5, 0, 0]
    device.set_equalizer_bands(curve)
    assert asked(fake) == [bytes.fromhex('95 00 0a 00 02 05 06 07 08 09 0a 05 05'),
                           bytes.fromhex('15 00 00'),
                           bytes.fromhex('93 00 01 ff'), bytes.fromhex('13 00 00')]
    assert routes(fake) == [1, 0]
    assert device.get_equalizer_bands() == curve
    assert device.get_audio()['equalizer_preset'] == 'custom'


def test_a_curve_it_cannot_hold_sends_nothing(headset):
    device, fake = headset
    for gains in ([6] + [0] * 9, [0] * 9):
        with pytest.raises(DeviceError):
            device.set_equalizer_bands(gains)
    assert fake.headset.requests == []


def test_do_not_disturb(headset):
    device, fake = headset
    device.set_do_not_disturb(True)
    assert fake.headset.registers[Register.DO_NOT_DISTURB] == b'\x01'
    assert asked(fake) == [bytes.fromhex('a7 00 01 01'), bytes.fromhex('27 00 00')]
    assert device.get_audio()['do_not_disturb'] is True
    device.set_do_not_disturb(False)
    assert device.get_audio()['do_not_disturb'] is False


def test_what_only_the_blacksharks_have_is_not_supported(headset):
    device, fake = headset
    for call in (device.get_microphone_muted, lambda: device.set_sidetone(True),
                 lambda: device.set_sidetone_level(5), lambda: device.set_dongle_led('off'),
                 lambda: device.set_microphone_noise_cancelling(True),
                 lambda: device.set_power_saving(True), lambda: device.set_equalizer(False)):
        with pytest.raises(NotSupported):
            call()
    device.set_equalizer(True)      # it is on, always
    assert fake.headset.requests == []


def test_a_value_it_could_not_have_is_an_error(headset):
    device, fake = headset
    fake.headset.registers[Register.BATTERY] = b'\xff'
    with pytest.raises(DeviceError, match='unexpected battery level'):
        device.get_battery_level()
    fake.headset.registers[Register.EQ_PRESET] = b'\x03'
    with pytest.raises(DeviceError, match='unexpected equalizer preset'):
        device.get_audio()
    assert fake.headset.route == protocol.ROUTE_LOCAL


# -- writes that do not take ---------------------------------------------------

def test_a_write_the_headset_refuses_is_an_error(headset):
    """Ten minutes is not a sleep timer it has, whoever asks."""
    device, fake = headset
    with pytest.raises(DeviceError, match='refused the sleep timer'):
        device._with_headset(lambda: device._put(Register.IDLE_MINUTES, (10,), 'sleep timer'))
    assert fake.headset.registers[Register.IDLE_MINUTES] == b'\x0f'
    assert routes(fake) == [1, 0] and fake.headset.route == protocol.ROUTE_LOCAL


def test_a_write_that_reads_back_as_something_else_did_not_happen(headset):
    """It says yes to some writes and keeps what it had."""
    device, fake = headset
    fake.headset.forgetful = {Register.EQ_PRESET, Register.DO_NOT_DISTURB}
    with pytest.raises(DeviceError, match='did not keep the preset'):
        device.set_equalizer_preset('movie')
    with pytest.raises(DeviceError, match='did not keep the do-not-disturb mode'):
        device.set_do_not_disturb(True)
    # The curve went in; the switch to it is what failed, and is what is said.
    with pytest.raises(DeviceError, match='did not keep the preset'):
        device.set_equalizer_bands([1] * 10)
    assert fake.headset.registers[Register.EQ_BANDS] == bytes((6,)) * 10
    assert routes(fake) == [1, 0] * 3


# -- what arrives, and in what order --------------------------------------------

def everything(device):
    return (device.get_battery_level(), device.is_charging(), device.get_idle_time(),
            device.get_audio()['equalizer_bands'])


def test_replies_cut_in_two_are_read_all_the_same(headset):
    device, fake = headset
    fake.headset.split = True
    fake.headset.inside_reply = [FakeBarracuda.MEDIA_PRESS]
    assert everything(device) == (85, False, 900, [0] * 10)
    device.set_equalizer_bands([2] * 10)
    assert fake.headset.registers[Register.EQ_BANDS] == bytes((7,)) * 10


def test_what_the_headset_says_unasked_is_not_taken_for_an_answer(headset):
    device, fake = headset
    emulated = fake.headset
    emulated.before_reply = [
        report(emulated.notification(Register.BATTERY, 7)),
        report(emulated.notification(Register.CHARGING, 1)),
        report(emulated.notification(Register.LINK, 0)),
        FakeBarracuda.MEDIA_PRESS,
    ]
    assert everything(device) == (85, False, 900, [0] * 10)
    emulated.split = True
    assert everything(device) == (85, False, 900, [0] * 10)


def test_an_answer_counts_only_after_its_acknowledgment(headset):
    """The receiver numbers its answers itself, so an old one looks like a new one."""
    device, fake = headset
    emulated = fake.headset
    emulated.before_reply = [report(frame(Family.LINK, (Link.GET_LINK, 0))),
                             report(frame(Family.LINK, (Link.GET_ROUTE, 1))),
                             report(frame(Family.LINK, (Link.GET_TRANSPORT, 0)))]
    assert device.is_headset_connected() is True
    assert device.get_battery_level() == 85


def test_an_answer_with_no_acknowledgment_of_this_request_is_none(headset, monkeypatch):
    device, fake = headset
    emulated = fake.headset
    acknowledge = emulated._ack
    # Acknowledged, but as the request before would have been.
    monkeypatch.setattr(emulated, '_ack', lambda family, sequence, *rest: acknowledge(
        family, sequence - 1, *rest))
    with pytest.raises(DeviceError, match='did not answer'):
        device.get_battery_level()
    assert fake.headset.requests == [LINKED]


def test_what_is_left_over_from_before_is_thrown_away(headset):
    device, fake = headset
    assert device.get_battery_level() == 85
    # A reply that came too late for whoever asked, and half of another.
    fake.input_reports.put(report(frame(Family.CUSTOMER, (0x21, 0x01, 0x01, 0x07))))
    fake.input_reports.put(report(frame(Family.LINK, (Link.GET_LINK, 0))[:6]))
    assert device.get_battery_level() == 85
    assert fake.input_reports.empty()


def test_a_request_the_receiver_refuses_is_an_error(headset):
    device, fake = headset
    fake.headset.force_status = 0x02
    with pytest.raises(DeviceError, match='refused the request'):
        device.get_battery_level()
    assert device.is_headset_connected() is False


# -- the route ----------------------------------------------------------------

def test_the_headset_only_answers_while_the_receiver_passes_things_on(headset):
    device, fake = headset
    with pytest.raises(DeviceError, match='did not answer'):
        device._get(Register.BATTERY)
    assert fake.headset.requests == [(Family.CUSTOMER, b'\x21\x00\x00')]


def test_the_route_is_put_back_when_the_headset_does_not_answer(headset):
    device, fake = headset
    fake.headset.silent = {(Family.CUSTOMER, Register.BATTERY)}
    with pytest.raises(DeviceError, match='did not answer'):
        device.get_battery_level()
    assert fake.headset.requests[-2:] == AFTER
    assert fake.headset.route == protocol.ROUTE_LOCAL
    # Silence once is not the headset being off: it is asked again next time.
    fake.headset.silent = set()
    assert device.get_battery_level() == 85


def test_the_route_is_put_back_whatever_goes_wrong(headset):
    device, fake = headset

    def work():
        assert fake.headset.route == protocol.ROUTE_REMOTE
        raise RuntimeError('anything at all')

    with pytest.raises(RuntimeError):
        device._with_headset(work)
    assert routes(fake) == [1, 0] and fake.headset.route == protocol.ROUTE_LOCAL
    fake.headset.silent = {(Family.DIAG, protocol.READ_MAX_LEN[0])}
    with pytest.raises(DeviceError, match='did not answer'):
        device.get_idle_time()
    assert routes(fake) == [1, 0, 1, 0] and asked(fake) == []


def test_a_route_that_will_not_change_is_an_error_and_nothing_is_asked(headset):
    device, fake = headset
    fake.headset.stuck_route = True
    with pytest.raises(DeviceError, match='did not change its route'):
        device.get_battery_level()
    assert asked(fake) == []
    assert device._route_failed is False     # it never left where it should be


def test_a_route_that_will_not_go_back_is_tried_twice_and_then_left_alone(headset):
    device, fake = headset

    def work():
        fake.headset.stuck_route = True

    with pytest.raises(DeviceError, match='would not go back'):
        device._with_headset(work)
    assert routes(fake) == [1, 0, 0] and device._route_failed is True
    # Nothing more is passed on until the receiver itself says it is back.
    fake.headset.requests.clear()
    with pytest.raises(DeviceError, match='still set to pass requests on'):
        device.get_battery_level()
    assert fake.headset.requests == [LINKED, ROUTE]
    fake.headset.stuck_route = False
    fake.headset.route = protocol.ROUTE_LOCAL
    assert device.get_battery_level() == 85
    assert device._route_failed is False


def test_a_failure_is_not_hidden_by_the_route_going_back(headset):
    device, fake = headset

    def work():
        fake.headset.stuck_route = True
        raise DeviceError('the first thing that went wrong')

    with pytest.raises(DeviceError, match='the first thing'):
        device._with_headset(work)
    assert device._route_failed is True


def test_a_receiver_left_passing_things_on_is_put_back_and_used(headset):
    # Left so by a daemon stopped half way.  The receiver keeps it until it is
    # unplugged, so refusing would shut the headset out for as long.
    device, fake = headset
    fake.headset.route = protocol.ROUTE_REMOTE
    assert device.get_battery_level() == fake.headset.registers[Register.BATTERY][0]
    assert routes(fake) == [protocol.ROUTE_LOCAL, protocol.ROUTE_REMOTE, protocol.ROUTE_LOCAL]
    assert fake.headset.route == protocol.ROUTE_LOCAL


def test_a_receiver_that_will_not_be_put_back_is_left_alone(headset):
    device, fake = headset
    fake.headset.route = protocol.ROUTE_REMOTE
    fake.headset.stuck_route = True
    with pytest.raises(DeviceError, match='another program'):
        device.get_battery_level()
    assert protocol.ROUTE_REMOTE not in routes(fake)


def test_a_receiver_that_cannot_pass_things_on_is_not_told_to(headset, monkeypatch):
    device, fake = headset
    monkeypatch.setattr(fake.headset, 'TRANSPORT', 0x13)
    with pytest.raises(DeviceError, match='cannot pass requests on'):
        device.get_battery_level()
    assert fake.headset.requests == [LINKED, TRANSPORT]


def test_one_at_a_time_from_start_to_finish(headset):
    """Two callers at once must not find the route where the other left it."""
    device, fake = headset
    failures = []

    def read():
        try:
            for _ in range(5):
                assert device.get_battery_level() == 85
                assert device.get_idle_time() == 900
        except Exception as error:  # noqa: BLE001 - collected for the assertion
            failures.append(error)

    threads = [threading.Thread(target=read) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert failures == []
    assert routes(fake) == [1, 0] * 40


# -- a headset that is off ----------------------------------------------------

def test_switching_off_is_said_by_the_receiver_at_once(headset):
    device, fake = headset
    fake.headset.switched_on = False
    said = protocol.Reassembler()
    frames = [said.feed(fake.input_reports.get_nowait())[0] for _ in range(2)]
    assert protocol.link_reply(frames[0], Link.GET_LINK) == 0
    assert protocol.notification(frames[1]) == (Register.LINK, 0)


def test_a_headset_that_is_off_is_not_there(headset):
    device, fake = headset
    fake.headset.switched_on = False
    assert device.get_audio() == {'connected': False}
    assert device.is_headset_connected() is False
    with pytest.raises(DeviceError, match='switched off'):
        device.get_battery_level()
    # The receiver said so; nothing was passed on, nor was it told to.
    assert fake.headset.requests == [LINKED]


def test_nothing_is_set_on_a_headset_that_is_off(headset):
    device, fake = headset
    fake.headset.switched_on = False
    before = dict(fake.headset.registers)
    for change in (lambda: device.set_equalizer_preset('game'),
                   lambda: device.set_equalizer_bands([1] * 10),
                   lambda: device.set_do_not_disturb(True),
                   lambda: device.set_idle_time(900)):
        with pytest.raises(DeviceError, match='switched off'):
            change()
    assert fake.headset.registers == before and routes(fake) == []


def test_a_headset_that_is_off_is_asked_once_and_then_left_alone(headset):
    device, fake = headset
    fake.headset.switched_on = False
    assert device.is_headset_connected() is False
    for read in (device.get_battery_level, device.is_charging, device.get_idle_time,
                 device.get_equalizer_bands, device.get_equalizer_preset):
        with pytest.raises(DeviceError, match='switched off'):
            read()
    assert device.get_audio() == {'connected': False}
    assert fake.headset.requests == [LINKED]


def test_it_is_noticed_coming_back(headset):
    device, fake = headset
    fake.headset.switched_on = False
    assert device.get_audio() == {'connected': False}
    fake.headset.switched_on = True
    device._quiet_until = 0.0       # the quiet spell is over
    assert device.is_headset_connected() is True
    assert device.get_audio()['connected'] is True


def test_who_it_is_needs_no_headset(headset):
    device, fake = headset
    fake.headset.switched_on = False
    assert device.get_audio() == {'connected': False}
    assert device.serial == SERIAL
    assert device.capabilities()['audio']


def test_silence_from_the_receiver_is_not_the_headset_being_off(headset):
    device, fake = headset
    fake.headset.silent = {(Family.LINK, Link.GET_LINK)}
    with pytest.raises(DeviceError, match='did not answer'):
        device.get_battery_level()
    assert device.is_headset_connected() is False
    assert device._quiet_until == 0.0


# -- when things go wrong -----------------------------------------------------

def test_requests_do_not_go_out_as_set_report(headset):
    """Checked on the hardware: only the interrupt endpoint is listened to."""
    device, fake = headset
    handle = FakeHidBackend([fake]).open(fake.headset_info)
    with pytest.raises(HidError, match='interrupt endpoint'):
        handle.send_output_report(protocol.request(Family.LINK, 0x60, (Link.GET_LINK,)))
    keyboard = FakeRazerDevice(0x0203)
    with pytest.raises(HidError, match='no interrupt OUT endpoint'):
        FakeHidBackend([keyboard]).open(keyboard.info).write_output_report(bytes(64))


def test_an_unplugged_receiver_is_an_error_and_can_be_tried_again(headset):
    device, fake = headset
    fake.input_reports.put(None)
    with pytest.raises(DeviceError, match='disconnected'):
        device.get_battery_level()
    assert device.channel._reader is None and device.channel._writer is None
    assert device.get_battery_level() == 85


def test_a_handle_that_will_not_open_is_an_error(tmp_path):
    meta = get_database().get(0x1532, PID)
    fake = FakeRazerDevice(PID, meta.name, headset=True)
    backend = FakeHidBackend()      # nothing behind the collection
    device = BarracudaDevice(meta, TunnelChannel(backend, fake.headset_info, timeout=0.05),
                             Persistence(str(tmp_path / 'p.json')))
    with pytest.raises(DeviceError):
        device.get_battery_level()
    assert device.is_headset_connected() is False
    assert device.serial == SERIAL


def test_the_channel_listens_before_it_writes_and_pads_to_what_windows_declares():
    calls = []

    class Writer:
        def write_output_report(self, data):
            calls.append(('write', len(data)))

        def close(self):
            calls.append(('close', 'writer'))

    class Reader:
        def read(self, timeout):
            calls.append(('read', timeout > 0))
            return None

        def close(self):
            calls.append(('close', 'reader'))

    class Backend:
        def open(self, info):
            calls.append(('open',))
            return Writer()

        def open_input(self, info):
            calls.append(('open_input',))
            return Reader()

    channel = TunnelChannel(Backend(), collection(3, 1, 0xFF00, size=65), timeout=0.01)
    assert channel.exchange(bytes(64), lambda frame: True) is None
    assert calls[:4] == [('open_input',), ('open',), ('read', False), ('write', 65)]
    assert ('read', True) in calls
    channel.close()
    assert calls[-2:] == [('close', 'writer'), ('close', 'reader')]


@pytest.mark.skipif(sys.platform != 'win32', reason='reads the Win32 last error')
def test_writing_goes_through_writefile_and_says_when_it_fails(monkeypatch):
    calls = []

    class Kernel:
        ok = True

        def WriteFile(self, handle, buffer, length, written, overlapped):
            calls.append((handle, bytes(buffer.raw), length, overlapped))
            written._obj.value = length if Kernel.ok else length - 1
            return 1

    monkeypatch.setattr(win32, '_kernel32', Kernel())
    handle = win32.Win32HidHandle(collection(3, 1, 0xFF00), 1234)
    sent = protocol.request(Family.LINK, 0x60, (Link.GET_LINK,))
    handle.write_output_report(sent)
    assert calls == [(1234, sent, 64, None)]
    Kernel.ok = False
    with pytest.raises(HidError, match='63 of 64'):
        handle.write_output_report(sent)
    monkeypatch.setattr(Kernel, 'WriteFile', lambda self, *arguments: 0)
    with pytest.raises(HidError, match='WriteFile failed'):
        handle.write_output_report(sent)
    handle._handle = None           # nothing of ours to close


# -- through the daemon -------------------------------------------------------

def test_what_it_has_can_be_reached_over_rpc():
    assert {'get_battery_level', 'is_charging', 'get_idle_time', 'set_idle_time', 'get_audio',
            'set_equalizer_preset', 'set_equalizer_bands', 'set_do_not_disturb'} <= DEVICE_METHODS


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """A daemon with the receiver plugged in, for the command line to talk to."""
    service = DaemonService(backend=build_demo_backend((PID,)), enable_effects=True,
                            persistence=Persistence(str(tmp_path / 'p.json')), chroma_port=0)
    service.manager.bluetooth = False
    service.start()
    server = DaemonServer(service, port=0)
    path = str(tmp_path / 'daemon.json')
    server.endpoint.write(path)
    monkeypatch.setattr('openrazer_win.daemon.protocol.endpoint_path', lambda: path)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    try:
        yield service
    finally:
        server.shutdown()
        server.server_close()
        service.stop()
        thread.join(timeout=2)


def test_the_daemon_serves_it_from_start_to_finish(daemon):
    assert daemon._pending == {}
    listed = daemon.rpc_list_devices()
    # A firmware version nobody can ask for is one it does not have.
    assert (listed[0]['serial'], listed[0]['firmware']) == (SERIAL, None)
    assert listed[0]['name'] == 'Razer Barracuda X (Wireless)'
    capabilities = daemon.rpc_capabilities(SERIAL)
    assert capabilities['protocol'] == 'barracuda'
    assert capabilities['idle_time_choices'] == list(IDLE_SECONDS)
    assert daemon.rpc_call(SERIAL, 'get_battery_level') == 85
    assert daemon.rpc_call(SERIAL, 'is_charging') is False
    daemon.rpc_call(SERIAL, 'set_equalizer_preset', ['movie'])
    daemon.rpc_call(SERIAL, 'set_do_not_disturb', [True])
    daemon.rpc_call(SERIAL, 'set_idle_time', [1800])
    audio = daemon.rpc_call(SERIAL, 'get_audio')
    assert (audio['equalizer_preset'], audio['do_not_disturb']) == ('movie', True)
    assert daemon.rpc_call(SERIAL, 'get_idle_time') == 1800
    daemon.rpc_call(SERIAL, 'set_equalizer_bands', [[1, 2, 3, 4, 5, 0, -1, -2, -3, -4]])
    audio = daemon.rpc_call(SERIAL, 'get_audio')
    assert audio['equalizer_preset'] == 'custom'
    assert audio['equalizer_bands'] == [1, 2, 3, 4, 5, 0, -1, -2, -3, -4]
    assert daemon.rpc_profile_list(SERIAL) == []


def test_the_daemon_says_what_it_lacks_and_what_it_will_not_take(daemon):
    with pytest.raises(RpcError, match='5, 15, 30, 45 or 60 minutes'):
        daemon.rpc_call(SERIAL, 'set_idle_time', [600])
    for method, arguments in (('set_sidetone', [True]), ('set_sidetone_level', [5]),
                              ('set_dongle_led', ['off']), ('get_microphone_muted', []),
                              ('set_power_saving', [True])):
        with pytest.raises(RpcError, match='has no'):
            daemon.rpc_call(SERIAL, method, arguments)


def test_the_daemon_shows_a_headset_that_is_off_as_off(daemon):
    daemon.manager.backend.devices[0].headset.switched_on = False
    assert daemon.rpc_call(SERIAL, 'get_audio') == {'connected': False}
    with pytest.raises(RpcError, match='switched off'):
        daemon.rpc_call(SERIAL, 'get_battery_level')


def test_the_command_line_sets_what_it_has(daemon, capsys):
    emulated = daemon.manager.backend.devices[0].headset
    assert main(['headset', 'eq', 'game']) == 0
    assert emulated.registers[Register.EQ_PRESET] == b'\x07'
    assert main(['headset', 'eq', '1,2,3,4,5,0,-1,-2,-3,-4']) == 0
    assert emulated.registers[Register.EQ_BANDS] == bytes((6, 7, 8, 9, 10, 5, 4, 3, 2, 1))
    assert emulated.registers[Register.EQ_PRESET] == b'\xff'
    assert main(['headset', 'dnd', 'on']) == 0
    assert emulated.registers[Register.DO_NOT_DISTURB] == b'\x01'
    assert main(['headset', 'sleep', '45']) == 0
    assert emulated.registers[Register.IDLE_MINUTES] == bytes((45,))
    assert main(['headset', 'sleep', '0']) == 0
    assert emulated.registers[Register.IDLE_MINUTES] == b'\x00'
    assert emulated.route == protocol.ROUTE_LOCAL


def test_the_command_line_says_what_it_lacks(daemon, capsys):
    for arguments in (['headset', 'sidetone', '9'], ['headset', 'led', 'battery'],
                      ['headset', 'eq', 'off'], ['headset', 'nc', 'on'],
                      ['headset', 'sleep', '10'], ['headset', 'eq', '6,0,0,0,0,0,0,0,0,0']):
        assert main(arguments) == 1
    errors = capsys.readouterr().err
    assert 'sidetone' in errors and 'receiver' in errors and 'switched off' in errors
    assert 'noise cancelling' in errors and '5, 15, 30, 45 or 60 minutes' in errors
    assert 'from -5 to 5' in errors
