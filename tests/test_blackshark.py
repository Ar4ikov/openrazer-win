"""The BlackShark V2 HyperSpeed: its reports, and the device built on them."""
from __future__ import annotations

import pytest

from openrazer_win.core.blackshark import BlackSharkDevice
from openrazer_win.core.device import DeviceError
from openrazer_win.core.manager import DeviceManager
from openrazer_win.core.persistence import Persistence
from openrazer_win.core.transport import ReportChannel, select_blackshark_interface
from openrazer_win.daemon.server import DaemonService, DEVICE_METHODS
from openrazer_win.devices.database import get_database
from openrazer_win.devices.recipes import NotSupported
from openrazer_win.hid.base import HidDeviceInfo, HidError
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice
from openrazer_win.protocol import blackshark as protocol
from openrazer_win.protocol.blackshark import Command

WIRELESS = protocol.PID_WIRELESS
WIRED = protocol.PID_WIRED


def build(pid, tmp_path, timeout=0.05):
    meta = get_database().get(0x1532, pid)
    fake = FakeRazerDevice(pid, meta.name, headset=True, serial='BS0123456789')
    channel = ReportChannel(FakeHidBackend([fake]), fake.headset_info, timeout=timeout)
    return BlackSharkDevice(meta, channel, Persistence(str(tmp_path / 'p.json'))), fake


@pytest.fixture
def headset(tmp_path):
    return build(WIRELESS, tmp_path)


def writes(fake):
    """The commands that wrote something, in the order they arrived."""
    return [(command & ~protocol.WRITE, payload)
            for _domain, command, payload in fake.headset.requests
            if command & protocol.WRITE]


def reply(sequence=1, domain=0x80, command=Command.BATTERY, payload=b'\x50',
          status=protocol.Status.OK, with_report_id=False):
    report = bytearray(protocol.REPORT_SIZE)
    report[0] = protocol.REPORT_ID
    report[1] = status
    report[2] = 0x60 | sequence
    report[6] = 4 + len(payload)
    report[8] = 0x80
    report[9] = domain
    report[10] = command
    report[11] = 1
    report[12] = len(payload)
    report[13:13 + len(payload)] = payload
    start = 0 if with_report_id else 1
    for byte in report[start:62]:
        report[62] ^= byte
    return bytes(report)


# -- reports ------------------------------------------------------------------

def test_a_read_is_laid_out_as_documented():
    report = protocol.request(5, protocol.DOMAIN_HEADSET, Command.BATTERY)
    assert len(report) == protocol.REPORT_SIZE
    assert report[0] == 0x02
    assert report[2] == 0x65
    assert report[6] == 4
    assert (report[9], report[10], report[12]) == (0x80, 0x21, 0)
    # The sum covers everything up to it, the report id included.
    assert report[62] == 0x02 ^ 0x65 ^ 4 ^ 0x80 ^ 0x21


def test_a_write_carries_its_value():
    report = protocol.request(1, protocol.DOMAIN_HEADSET,
                              Command.SIDETONE_LEVEL | protocol.WRITE, (9,))
    assert (report[6], report[10], report[12], report[13]) == (5, 0x99, 1, 9)


def test_the_sequence_counter_wraps_at_five_bits():
    assert protocol.request(0x21, 0, Command.SERIAL)[2] == 0x61


def test_a_request_cannot_overrun_the_report():
    with pytest.raises(ValueError):
        protocol.request(1, 0, Command.SERIAL, bytes(protocol.MAX_PAYLOAD + 1))


def test_replies_are_taken_with_either_checksum():
    assert protocol.is_reply(reply())
    assert protocol.is_reply(reply(with_report_id=True))


@pytest.mark.parametrize('damage', [
    lambda r: r.__setitem__(0, 0x03),               # another report
    lambda r: r.__setitem__(8, 0x00),               # a request, not a reply
    lambda r: r.__setitem__(13, r[13] ^ 0xFF),      # corrupted
    lambda r: r.__setitem__(12, 200),               # claims more than fits
])
def test_damaged_replies_are_not_replies(damage):
    report = bytearray(reply())
    damage(report)
    assert not protocol.is_reply(bytes(report))


def test_a_short_report_is_not_a_reply():
    assert not protocol.is_reply(reply()[:32])


def test_a_reply_answers_only_its_own_request():
    report = reply(sequence=7)
    assert protocol.answers(report, 7, 0x80, Command.BATTERY)
    assert not protocol.answers(report, 8, 0x80, Command.BATTERY)
    assert not protocol.answers(report, 7, 0x00, Command.BATTERY)
    assert not protocol.answers(report, 7, 0x80, Command.CHARGING)


def test_on_the_cable_a_local_request_is_answered_as_the_headset():
    report = reply(sequence=7, domain=0x80)
    assert protocol.answers(report, 7, 0x00, Command.BATTERY, wired=True)
    assert not protocol.answers(report, 7, 0x00, Command.BATTERY, wired=False)


def test_a_failed_write_is_told_from_a_good_one():
    write = Command.SIDETONE | protocol.WRITE
    assert protocol.result(reply(command=write, payload=b'\x00'), write) == b'\x00'
    with pytest.raises(protocol.ProtocolError):
        protocol.result(reply(command=write, payload=b'\x01'), write)


@pytest.mark.parametrize('status, error', [
    (protocol.Status.UNSUPPORTED, protocol.Unsupported),
    (protocol.Status.BUSY, protocol.ProtocolError),
    (protocol.Status.TIMED_OUT, protocol.ProtocolError),
    (0x7F, protocol.ProtocolError),
])
def test_a_status_other_than_ok_is_an_error(status, error):
    with pytest.raises(error):
        protocol.result(reply(status=status), Command.BATTERY)


def test_bands_go_out_five_up_and_read_back_as_stored():
    gains = [-9, -5, 0, 1, 2, 3, 4, 5, 6, 0]
    encoded = protocol.encode_bands(gains)
    assert encoded[:3] == bytes((0xFC, 0x00, 0x05))
    stored = bytes((byte - 5) & 0xFF for byte in encoded)
    assert protocol.decode_bands(stored) == gains


@pytest.mark.parametrize('gains', [[0] * 9, [0] * 11, [7] + [0] * 9, [-10] + [0] * 9])
def test_a_curve_it_cannot_hold_is_refused(gains):
    with pytest.raises(ValueError):
        protocol.encode_bands(gains)


# -- finding it ---------------------------------------------------------------

def collection(usage_page, length=64, interface=3, col=1):
    return HidDeviceInfo(path='x{0}{1}'.format(usage_page, col), vendor_id=0x1532,
                         product_id=WIRELESS, interface=interface, collection=col,
                         usage_page=usage_page, input_length=length, output_length=length)


def test_the_vendor_collection_is_picked_over_the_telephony_one():
    telephony = collection(0x000B, length=4, interface=3, col=1)
    vendor = collection(0xFF14, col=2)
    assert select_blackshark_interface([telephony, vendor]) is vendor


def test_the_other_usage_page_will_do():
    other = collection(0xFF00)
    assert select_blackshark_interface([collection(0x000C, length=4), other]) is other
    assert select_blackshark_interface([other, collection(0xFF14, col=2)]).usage_page == 0xFF14


def test_nothing_usable_is_nothing():
    assert select_blackshark_interface([collection(0xFF14, length=4)]) is None


def test_it_is_in_the_database_under_both_ids():
    wireless = get_database().get(0x1532, WIRELESS)
    wired = get_database().get(0x1532, WIRED)
    assert wireless.type == wired.type == 'headset'
    assert 'BlackShark V2 HyperSpeed' in wireless.name
    assert wireless.image == wired.image
    assert wireless.image.startswith('https://')


@pytest.mark.parametrize('pid', [WIRELESS, WIRED])
def test_the_manager_builds_it(pid, tmp_path):
    fake = FakeRazerDevice(pid, 'Razer BlackShark V2 HS 2.4', headset=True)
    backend = FakeHidBackend([fake])
    with DeviceManager(backend=backend, persistence=Persistence(str(tmp_path / 'p.json')),
                       bluetooth=False) as manager:
        device = manager.devices[0]
        assert isinstance(device, BlackSharkDevice)
        assert manager.unsupported == []
        assert device.channel.info.usage_page == protocol.USAGE_PAGE


def test_a_receiver_with_no_vendor_collection_is_left_out(tmp_path, caplog):
    fake = FakeRazerDevice(WIRELESS, 'Razer BlackShark V2 HS 2.4')
    manager = DeviceManager(backend=FakeHidBackend([fake]), bluetooth=False,
                            persistence=Persistence(str(tmp_path / 'p.json')))
    assert manager.scan() == []
    assert 'no 64-byte vendor report' in caplog.text


# -- the device ---------------------------------------------------------------

def test_who_it_is_comes_from_the_receiver(headset):
    device, fake = headset
    assert device.serial == 'BS0123456789'
    assert device.firmware_version == 'v1.7.0.0'
    assert {domain for domain, _command, _payload in fake.headset.requests} == {0x00}


def test_nothing_is_written_when_it_is_found(headset):
    device, fake = headset
    device.enter_driver_mode()
    device.restore()
    assert device.serial and device.capabilities()
    assert writes(fake) == []


def test_it_has_nothing_to_light(headset):
    device, _fake = headset
    capabilities = device.capabilities()
    assert capabilities['zones'] == {}
    assert capabilities['battery'] and capabilities['idle_time']
    assert capabilities['audio']['equalizer_frequencies'][0] == 31
    assert device.available_effects() == []


def test_battery_and_charging(headset):
    device, fake = headset
    assert device.get_battery_level() == 85
    assert device.is_charging() is False
    fake.headset.registers[Command.CHARGING] = b'\x01'
    assert device.is_charging() is True


def test_the_sleep_timer_is_in_seconds_and_stored_in_minutes(headset):
    device, fake = headset
    assert device.get_idle_time() == 1800
    device.set_idle_time(300)
    assert fake.headset.registers[Command.IDLE_MINUTES] == bytes((5,))
    device.set_idle_time(0)
    assert device.get_idle_time() == 0
    with pytest.raises(DeviceError):
        device.set_idle_time(256 * 60)


def test_all_the_sound_settings_at_once(headset):
    device, fake = headset
    fake.headset.registers[Command.MIC_MUTED] = b'\x01'
    fake.headset.registers[Command.EQ_PRESET] = bytes((8,))
    assert device.get_audio() == {
        'connected': True, 'dongle_led': 'link', 'microphone_muted': True,
        'sidetone': True, 'sidetone_level': 5, 'equalizer': True,
        'equalizer_preset': 'music', 'equalizer_bands': [0] * 10,
        'do_not_disturb': False,
    }


def test_sidetone(headset):
    device, fake = headset
    device.set_sidetone(False)
    device.set_sidetone_level(15)
    assert fake.headset.registers[Command.SIDETONE] == b'\x00'
    assert fake.headset.registers[Command.SIDETONE_LEVEL] == bytes((15,))
    with pytest.raises(DeviceError):
        device.set_sidetone_level(16)


def test_a_preset_turns_the_equalizer_on(headset):
    device, fake = headset
    device.set_equalizer(False)
    fake.headset.requests.clear()
    device.set_equalizer_preset('game')
    assert writes(fake) == [(Command.EQ_PRESET, bytes((7,))), (Command.EQ_ENABLED, b'\x01')]
    with pytest.raises(DeviceError):
        device.set_equalizer_preset('loud')


def test_a_custom_curve_is_written_between_two_selections_of_custom(headset):
    device, fake = headset
    gains = [-9, 0, 0, 0, 0, 0, 0, 0, 0, 6]
    device.set_equalizer_bands(gains)
    custom = (Command.EQ_PRESET, bytes((255,)))
    assert writes(fake) == [(Command.EQ_ENABLED, b'\x01'), custom,
                            (Command.EQ_BANDS, protocol.encode_bands(gains)), custom]
    assert device.get_audio()['equalizer_bands'] == gains


def test_a_curve_it_cannot_hold_sends_nothing(headset):
    device, fake = headset
    with pytest.raises(DeviceError):
        device.set_equalizer_bands([9] * 10)
    assert writes(fake) == []


def test_do_not_disturb_and_the_receiver_light(headset):
    device, fake = headset
    device.set_do_not_disturb(True)
    device.set_dongle_led('battery')
    assert fake.headset.registers[Command.DO_NOT_DISTURB] == b'\x01'
    assert fake.headset.registers[Command.DONGLE_LED] == bytes((2,))
    # The light is the receiver's, so it goes to the receiver.
    assert writes(fake)[-1] == (Command.DONGLE_LED, bytes((2,)))
    assert fake.headset.requests[-1][0] == protocol.DOMAIN_LOCAL
    with pytest.raises(DeviceError):
        device.set_dongle_led('disco')


def test_the_mute_button_is_only_read(headset):
    device, fake = headset
    assert device.get_microphone_muted() is False
    fake.headset.registers[Command.MIC_MUTED] = b'\x01'
    assert device.get_microphone_muted() is True
    assert not hasattr(device, 'set_microphone_muted')


# -- switched off -------------------------------------------------------------

def test_a_headset_that_is_off_says_so_quickly(headset, monkeypatch):
    device, fake = headset
    fake.headset.switched_on = False
    with pytest.raises(DeviceError, match='switched off'):
        device.get_battery_level()
    # One question to the receiver, none to a headset that cannot answer.
    assert [command for _domain, command, _payload in fake.headset.requests] == [Command.LINK]


def test_the_answer_about_the_link_is_kept_a_moment(headset):
    device, fake = headset
    device.get_battery_level()
    device.is_charging()
    device.get_idle_time()
    asked = [command for _domain, command, _payload in fake.headset.requests]
    assert asked.count(Command.LINK) == 1


def test_it_is_noticed_coming_back(headset, monkeypatch):
    device, fake = headset
    fake.headset.switched_on = False
    with pytest.raises(DeviceError):
        device.get_battery_level()
    fake.headset.switched_on = True
    monkeypatch.setattr('openrazer_win.core.blackshark.LINK_CHECK_SECONDS', 0.0)
    assert device.get_battery_level() == 85


def test_who_it_is_and_the_light_need_no_headset(headset):
    device, fake = headset
    fake.headset.switched_on = False
    assert device.serial == 'BS0123456789'
    assert device.firmware_version == 'v1.7.0.0'
    assert device.get_audio() == {'connected': False, 'dongle_led': 'link'}
    device.set_dongle_led('off')


def test_a_headset_that_goes_quiet_mid_request_is_an_error(headset):
    device, fake = headset
    device.is_headset_connected()
    fake.headset.switched_on = False    # the link answer above is still believed
    with pytest.raises(DeviceError, match='did not answer'):
        device.get_battery_level()


# -- on the cable -------------------------------------------------------------

def test_on_the_cable_everything_is_local(tmp_path):
    device, fake = build(WIRED, tmp_path)
    assert device.get_battery_level() == 85
    assert device.is_charging() is True
    assert device.get_audio()['connected'] is True
    assert {domain for domain, _command, _payload in fake.headset.requests} == {0x00}
    assert Command.LINK not in [c for _d, c, _p in fake.headset.requests]


def test_the_cable_has_no_receiver_light(tmp_path):
    device, _fake = build(WIRED, tmp_path)
    assert device.capabilities()['audio']['dongle_led_modes'] == []
    assert 'dongle_led' not in device.get_audio()
    with pytest.raises(NotSupported):
        device.set_dongle_led('off')


# -- failing ------------------------------------------------------------------

def test_a_command_the_firmware_lacks_is_not_supported(headset):
    device, fake = headset
    fake.headset.force_status = protocol.Status.UNSUPPORTED
    with pytest.raises(NotSupported):
        device.set_do_not_disturb(True)


def test_a_busy_headset_is_a_device_error(headset):
    device, fake = headset
    device.is_headset_connected()
    fake.headset.force_status = protocol.Status.BUSY
    with pytest.raises(DeviceError, match='busy'):
        device.set_sidetone(True)


def test_a_stale_report_is_passed_over(headset):
    device, fake = headset
    assert device.get_battery_level() == 85
    # Synapse's traffic, or a reply that came too late for an earlier request.
    fake.input_reports.put(reply(sequence=30, payload=b'\x07'))
    fake.input_reports.put(b'\x01' + bytes(63))
    assert device.get_battery_level() == 85


def test_an_unplugged_receiver_is_an_error_and_can_be_tried_again(headset):
    device, fake = headset
    device.is_headset_connected()
    fake.input_reports.put(None)
    with pytest.raises(DeviceError):
        device.get_battery_level()
    assert device.channel._reader is None
    assert device.get_battery_level() == 85


def test_a_handle_that_will_not_open_is_an_error(tmp_path):
    meta = get_database().get(0x1532, WIRELESS)
    fake = FakeRazerDevice(WIRELESS, meta.name, headset=True)
    backend = FakeHidBackend()      # nothing behind the collection
    device = BlackSharkDevice(meta, ReportChannel(backend, fake.headset_info),
                              Persistence(str(tmp_path / 'p.json')))
    with pytest.raises(DeviceError):
        device.get_battery_level()
    assert device.serial == 'RZ15320565'


def test_the_channel_pads_to_the_report_windows_declares(tmp_path):
    sent = []

    class Writer:
        def send_output_report(self, data):
            sent.append(data)

        def close(self):
            pass

    class Reader:
        def read(self, timeout):
            return None

        def close(self):
            pass

    class Backend:
        def open(self, info):
            return Writer()

        def open_input(self, info):
            return Reader()

    channel = ReportChannel(Backend(), collection(0xFF14, length=65), timeout=0.01)
    assert channel.exchange(bytes(64), lambda answer: True) is None
    assert len(sent[0]) == 65


def test_an_input_error_closes_the_channel():
    class Reader:
        closed = False

        def read(self, timeout):
            raise HidError('gone')

        def close(self):
            Reader.closed = True

    class Backend:
        def open(self, info):
            return type('W', (), {'send_output_report': lambda s, d: None,
                                  'close': lambda s: None})()

        def open_input(self, info):
            return Reader()

    channel = ReportChannel(Backend(), collection(0xFF14), timeout=0.01)
    with pytest.raises(HidError):
        channel.exchange(bytes(64), lambda answer: True)
    assert Reader.closed


# -- through the daemon -------------------------------------------------------

def test_the_sound_settings_can_be_reached_over_rpc():
    assert {'get_audio', 'get_microphone_muted', 'set_sidetone', 'set_sidetone_level',
            'set_equalizer', 'set_equalizer_preset', 'set_equalizer_bands',
            'set_do_not_disturb', 'set_dongle_led'} <= DEVICE_METHODS


def test_the_daemon_serves_it_from_start_to_finish(tmp_path):
    fake = FakeRazerDevice(WIRELESS, 'Razer BlackShark V2 HS 2.4', headset=True,
                           serial='BS0123456789')
    service = DaemonService(backend=FakeHidBackend([fake]), enable_effects=True,
                            persistence=Persistence(str(tmp_path / 'p.json')))
    service.manager.bluetooth = False
    service.start()
    try:
        assert service._pending == {}
        listed = service.rpc_list_devices()
        assert listed[0]['serial'] == 'BS0123456789'
        assert listed[0]['firmware'] == 'v1.7.0.0'
        assert service.rpc_capabilities('BS0123456789')['protocol'] == 'blackshark'
        service.rpc_call('BS0123456789', 'set_equalizer_preset', ['movie'])
        audio = service.rpc_call('BS0123456789', 'get_audio')
        assert audio['equalizer_preset'] == 'movie'
        assert service.rpc_profile_list('BS0123456789') == []
    finally:
        service.stop()
