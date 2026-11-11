"""The BlackShark V3 X: its reports, and the device built on them."""
from __future__ import annotations

import threading

import pytest

from openrazer_win.cli import main
from openrazer_win.core.blackshark_v3x import DEFAULT_SIDETONE, BlackSharkV3XDevice
from openrazer_win.core.device import DeviceError
from openrazer_win.core.manager import DeviceManager
from openrazer_win.core.persistence import Persistence
from openrazer_win.core.transport import (
    FeatureChannel, is_blackshark_v3x_interface, select_blackshark_v3x_interface,
)
from openrazer_win.daemon.demo import build_demo_backend
from openrazer_win.daemon.server import DEVICE_METHODS, DaemonServer, DaemonService
from openrazer_win.devices.database import get_database
from openrazer_win.devices.recipes import NotSupported
from openrazer_win.hid.base import HidDeviceInfo, HidError
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice
from openrazer_win.protocol import blackshark_v3x as protocol
from openrazer_win.protocol.blackshark_v3x import Class, Command, Status

WIRED = protocol.PID_WIRED
WIRELESS = protocol.PID_WIRELESS
SERIAL = 'PM2530H12345678'


def build(pid, tmp_path):
    meta = get_database().get(0x1532, pid)
    fake = FakeRazerDevice(pid, meta.name, headset=True, serial=SERIAL)
    channel = FeatureChannel(FakeHidBackend([fake]), fake.headset_info, wait=0)
    return BlackSharkV3XDevice(meta, channel, Persistence(str(tmp_path / 'p.json'))), fake


@pytest.fixture
def headset(tmp_path):
    """On its receiver, which is how most of them are used."""
    return build(WIRELESS, tmp_path)


@pytest.fixture
def cabled(tmp_path):
    return build(WIRED, tmp_path)


def writes(fake):
    """The commands that wrote something, in the order they arrived."""
    return [(command_class, command, arguments)
            for command_class, command, arguments in fake.headset.requests
            if not command & protocol.READ]


def reply(sent, arguments=b'\x00\x50', status=Status.OK):
    """The reply a headset would give to the request `sent`."""
    report = bytearray(protocol.REPORT_SIZE)
    report[0] = protocol.REPORT_ID
    report[1] = status
    report[2] = sent[2]
    report[6] = len(arguments)
    report[7], report[8] = sent[7], sent[8]
    report[9:9 + len(arguments)] = arguments
    report[62] = protocol.checksum(report)
    return bytes(report)


# -- reports ------------------------------------------------------------------

def test_a_request_is_laid_out_as_the_driver_sends_it():
    report = protocol.request(5, Class.AUDIO, Command.SIDETONE, (0x00, 0x03, 9))
    assert len(report) == protocol.REPORT_SIZE
    assert (report[0], report[1], report[2]) == (0x07, 0x00, 5)
    assert (report[6], report[7], report[8]) == (3, 0x18, 0x03)
    assert report[9:12] == bytes((0x00, 0x03, 9))
    # The sum leaves out the report id, the status and the transaction id.
    assert report[62] == 3 ^ 0x18 ^ 0x03 ^ 0x03 ^ 9
    assert report[63] == 0


def test_reading_is_the_command_that_writes_with_its_top_bit_set():
    report = protocol.request(1, Class.POWER, Command.BATTERY | protocol.READ, protocol.QUERY)
    assert (report[6], report[7], report[8]) == (2, 0x07, 0x80)


def test_a_request_cannot_overrun_the_report():
    with pytest.raises(ValueError):
        protocol.request(1, Class.AUDIO, Command.EQ_BANDS, bytes(protocol.MAX_ARGUMENTS + 1))


def test_a_reply_answers_only_its_own_request():
    sent = protocol.request(7, Class.POWER, Command.BATTERY | protocol.READ, protocol.QUERY)
    assert protocol.answers(reply(sent), sent, at_least=2)
    other = protocol.request(8, Class.POWER, Command.BATTERY | protocol.READ, protocol.QUERY)
    assert not protocol.answers(reply(other), sent)
    charging = protocol.request(7, Class.POWER, Command.CHARGING | protocol.READ, protocol.QUERY)
    assert not protocol.answers(reply(charging), sent)


@pytest.mark.parametrize('damage', [
    lambda r: r.__setitem__(0, 0x02),       # another report id
    lambda r: r.__setitem__(62, r[62] ^ 1),  # the sum does not add up
    lambda r: r.__setitem__(10, r[10] ^ 1),  # ... nor after a change to what it covers
])
def test_a_damaged_reply_is_not_an_answer(damage):
    sent = protocol.request(1, Class.POWER, Command.BATTERY | protocol.READ, protocol.QUERY)
    report = bytearray(reply(sent))
    damage(report)
    assert not protocol.answers(bytes(report), sent)


def test_a_reply_read_too_soon_is_not_an_answer_yet():
    sent = protocol.request(1, Class.AUDIO, Command.EQ_BANDS | protocol.READ, protocol.QUERY)
    assert not protocol.answers(reply(sent, b'\x00'), sent, at_least=12)
    assert not protocol.answers(reply(sent)[:32], sent)
    assert not protocol.answers(bytes(protocol.REPORT_SIZE), sent)
    # A refusal has nothing more to wait for, however little it carries.
    assert protocol.answers(reply(sent, b'', Status.UNSUPPORTED), sent, at_least=12)


@pytest.mark.parametrize('status, error', [
    (Status.UNSUPPORTED, protocol.Unsupported),
    (Status.TIMED_OUT, protocol.ProtocolError),
    (Status.FAILED, protocol.ProtocolError),
    (Status.NOT_RUN, protocol.ProtocolError),
])
def test_a_status_that_is_not_an_answer_is_an_error(status, error):
    sent = protocol.request(1, Class.POWER, Command.BATTERY | protocol.READ, protocol.QUERY)
    with pytest.raises(error):
        protocol.result(reply(sent, status=status))


def test_busy_is_an_answer_as_much_as_ok():
    sent = protocol.request(1, Class.POWER, Command.BATTERY | protocol.READ, protocol.QUERY)
    assert protocol.result(reply(sent, status=Status.BUSY)) == b'\x00\x50'
    assert protocol.result(reply(sent)) == b'\x00\x50'


def test_bands_are_a_sign_bit_and_a_size():
    gains = [-6, -1, 0, 1, 6, 0, 0, 0, 0, 0]
    encoded = protocol.encode_bands(gains)
    assert encoded[:5] == bytes((0x86, 0x81, 0x00, 0x01, 0x06))
    assert protocol.decode_bands(encoded) == gains


@pytest.mark.parametrize('gains', [[0] * 9, [0] * 11, [7] + [0] * 9, [-7] + [0] * 9])
def test_a_curve_it_cannot_hold_is_refused(gains):
    with pytest.raises(ValueError):
        protocol.encode_bands(gains)


def test_a_curve_it_could_not_have_sent_is_not_taken():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_bands(b'\xff' * 10)
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_bands(bytes(9))


# -- finding it ---------------------------------------------------------------

def collection(number, usage_page, feature=0, size=64):
    return HidDeviceInfo(
        path='\\\\?\\hid#vid_1532&pid_057d&mi_00&col{0:02d}#x'.format(number),
        vendor_id=0x1532, product_id=WIRELESS, interface=0, collection=number,
        usage_page=usage_page, usage=1, feature_length=feature,
        input_length=size, output_length=size)


def test_the_collection_with_the_feature_report_is_the_one():
    """As a receiver showed them in the report that asked for this headset."""
    seen = [collection(1, 0x000C, size=3), collection(2, 0x000B, size=3),
            collection(3, 0xFF90), collection(4, 0xFF82, feature=64)]
    assert select_blackshark_v3x_interface(seen).collection == 4
    assert [is_blackshark_v3x_interface(info) for info in seen] == [False, False, False, True]


def test_nothing_usable_is_nothing():
    assert select_blackshark_v3x_interface([collection(3, 0xFF90)]) is None
    # The 90-byte control report of other devices is not it either.
    assert select_blackshark_v3x_interface([collection(1, 0xFF00, feature=91)]) is None


def test_it_is_in_the_database_under_both_ids():
    database = get_database()
    wired, wireless = database.get(0x1532, WIRED), database.get(0x1532, WIRELESS)
    assert wired.name == 'Razer BlackShark V3 X HyperSpeed (Wired)'
    assert wireless.name == 'Razer BlackShark V3 X HyperSpeed (Wireless)'
    assert wired.type == wireless.type == 'headset'
    assert wireless.image and not wireless.has_matrix


@pytest.mark.parametrize('pid', [WIRELESS, WIRED])
def test_the_manager_builds_it(pid, tmp_path):
    fake = FakeRazerDevice(pid, 'Razer BlackShark V3 X', headset=True)
    backend = FakeHidBackend([fake])
    with DeviceManager(backend=backend, persistence=Persistence(str(tmp_path / 'p.json')),
                       bluetooth=False) as manager:
        device = manager.devices[0]
        assert isinstance(device, BlackSharkV3XDevice)
        assert manager.unsupported == []
        assert device.channel.info.feature_length == protocol.REPORT_SIZE
        assert device.get_battery_level() == 85


def test_a_receiver_with_no_feature_report_is_left_out(tmp_path, caplog):
    fake = FakeRazerDevice(WIRELESS, 'Razer BlackShark V3 X')
    manager = DeviceManager(backend=FakeHidBackend([fake]), bluetooth=False,
                            persistence=Persistence(str(tmp_path / 'p.json')))
    assert manager.scan() == []
    assert 'no 64-byte feature report' in caplog.text


# -- the device ---------------------------------------------------------------

def test_on_the_cable_it_goes_by_its_serial_and_its_product_id(cabled):
    device, _fake = cabled
    assert device.serial == SERIAL + '057C'
    assert device.firmware_version == 'v1.0.14'


def test_the_receiver_goes_by_the_same_name_whether_the_headset_is_on_or_off(tmp_path):
    names = set()
    for switched_on in (True, False):
        device, fake = build(WIRELESS, tmp_path)
        fake.headset.switched_on = switched_on
        names.add(device.serial)
        # It never asked: the answer would depend on the headset being on.
        assert fake.headset.requests == []
    assert names == {'BSV3X057D'}


def test_a_firmware_version_the_receiver_does_not_know_yet_is_asked_for_again(headset):
    device, fake = headset
    fake.headset.switched_on = False
    with pytest.raises(DeviceError, match='not known yet'):
        _ = device.firmware_version
    fake.headset.switched_on = True
    assert device.firmware_version == 'v1.0.14'


def test_nothing_is_written_when_it_is_found(headset):
    device, fake = headset
    device.enter_driver_mode()
    device.restore()
    assert device.serial and device.capabilities()
    assert writes(fake) == []


def test_it_has_nothing_to_light_and_says_what_its_equalizer_is_like(headset):
    device, _fake = headset
    capabilities = device.capabilities()
    assert capabilities['zones'] == {}
    assert capabilities['battery'] and capabilities['idle_time']
    assert capabilities['protocol'] == 'blackshark_v3x'
    audio = capabilities['audio']
    assert audio['equalizer_presets'] == ['default', 'game', 'music', 'movie']
    assert audio['equalizer_range'] == [-6, 6]
    assert audio['equalizer_switch'] is False and audio['equalizer_curve_per_preset'] is True
    assert audio['dongle_led_modes'] == []
    assert device.available_effects() == []
    with pytest.raises(NotSupported):
        device.run('read:device_serial')


def test_battery_and_charging(headset, cabled):
    device, fake = headset
    assert device.get_battery_level() == 85
    assert device.is_charging() is False
    fake.headset.battery = 100
    assert device.get_battery_level() == 100
    assert cabled[0].is_charging() is True


def test_the_sleep_timer_is_kept_in_seconds(headset):
    device, fake = headset
    assert device.get_idle_time() == 600
    device.set_idle_time(1800)
    assert fake.headset.idle_seconds == 1800
    assert writes(fake)[-1] == (Class.POWER, Command.SLEEP, bytes((0x07, 0x08)))
    for seconds in (-1, 0xFFFF):
        with pytest.raises(DeviceError):
            device.set_idle_time(seconds)


def test_all_the_sound_settings_at_once(headset):
    device, fake = headset
    fake.headset.sidetone = 4
    fake.headset.preset = 2
    fake.headset.curves[2] = [1, 0, -1, 0, 0, 0, 0, 0, 0, 2]
    fake.headset.noise_cancelling = True
    assert device.get_audio() == {
        'connected': True, 'sidetone': True, 'sidetone_level': 4, 'equalizer': True,
        'equalizer_preset': 'music', 'equalizer_bands': [1, 0, -1, 0, 0, 0, 0, 0, 0, 2],
        'microphone_noise_cancelling': True, 'power_saving': False,
    }


def test_sidetone_is_a_level_and_off_is_none_of_it(headset):
    device, fake = headset
    device.set_sidetone_level(11)
    assert fake.headset.sidetone == 11
    assert writes(fake)[-1] == (Class.AUDIO, Command.SIDETONE, bytes((0x00, 0x03, 11)))
    device.set_sidetone(False)
    assert fake.headset.sidetone == 0
    # Back on is back to where it was, not to some level of ours.
    device.set_sidetone(True)
    assert fake.headset.sidetone == 11
    for level in (-1, 16):
        with pytest.raises(DeviceError):
            device.set_sidetone_level(level)


def test_switching_sidetone_to_what_it_is_writes_nothing(headset):
    device, fake = headset
    device.set_sidetone(False)
    assert writes(fake) == []
    device.set_sidetone(True)
    assert fake.headset.sidetone == DEFAULT_SIDETONE
    before = len(writes(fake))
    device.set_sidetone(True)
    assert len(writes(fake)) == before


def test_a_preset_is_chosen_by_name(headset):
    device, fake = headset
    device.set_equalizer_preset('movie')
    assert fake.headset.preset == 3
    assert writes(fake)[-1] == (Class.AUDIO, Command.EQ_PRESET, bytes((0x00, 0x01, 0x00, 3)))
    assert device.get_equalizer_preset() == 'movie'
    with pytest.raises(DeviceError, match='no such preset'):
        device.set_equalizer_preset('custom')


def test_a_curve_goes_into_the_preset_chosen_and_each_keeps_its_own(headset):
    device, fake = headset
    curve = [-6, -3, 0, 1, 2, 3, 4, 5, 6, 0]
    device.set_equalizer_preset('game')
    device.set_equalizer_bands(curve)
    assert fake.headset.curves[1] == curve
    assert device.get_equalizer_bands() == curve
    device.set_equalizer_preset('default')
    assert device.get_equalizer_bands() == [0] * 10


def test_a_curve_it_cannot_hold_sends_nothing(headset):
    device, fake = headset
    with pytest.raises(DeviceError):
        device.set_equalizer_bands([9] + [0] * 9)
    assert writes(fake) == []


def test_noise_cancelling_and_power_saving(headset):
    device, fake = headset
    device.set_microphone_noise_cancelling(True)
    device.set_power_saving(True)
    assert fake.headset.noise_cancelling and fake.headset.power_saving
    assert writes(fake)[-2:] == [(Class.AUDIO, Command.NOISE_CANCELLING, b'\x00\x01'),
                                 (Class.POWER, Command.POWER_SAVING, b'\x00\x01')]
    assert device.get_microphone_noise_cancelling() and device.get_power_saving()
    device.set_microphone_noise_cancelling(False)
    assert not device.get_audio()['microphone_noise_cancelling']


def test_what_only_the_other_blackshark_has_is_not_supported(headset):
    device, fake = headset
    for call in (lambda: device.set_equalizer(False), device.get_microphone_muted,
                 lambda: device.set_do_not_disturb(True), lambda: device.set_dongle_led('off')):
        with pytest.raises(NotSupported):
            call()
    device.set_equalizer(True)      # it is on, always
    assert writes(fake) == []


# -- a reply that is not there yet --------------------------------------------

def test_a_reply_read_too_soon_is_asked_for_again(headset):
    device, fake = headset
    fake.headset.slow = 2
    assert device.get_battery_level() == 85
    # Sent afresh each time, under a new transaction id.
    assert len(fake.headset.requests) == 3
    transactions = [report[2] for _info, report in fake.feature_reports]
    assert len(set(transactions)) == 3


def test_a_headset_that_never_has_its_reply_ready_did_not_answer(cabled):
    device, fake = cabled
    fake.headset.slow = 100
    with pytest.raises(DeviceError, match='did not answer'):
        device.get_battery_level()
    assert len(fake.headset.requests) == protocol.ATTEMPTS


# -- a headset that is off ----------------------------------------------------

def test_a_headset_that_is_off_is_not_there(headset):
    device, fake = headset
    fake.headset.switched_on = False
    assert device.get_audio() == {'connected': False}
    assert device.is_headset_connected() is False
    with pytest.raises(DeviceError, match='switched off'):
        device.get_battery_level()


def test_nothing_is_set_on_a_headset_that_is_off(headset):
    """The receiver would take it, say it went well, and lose it."""
    device, fake = headset
    fake.headset.switched_on = False
    for change in (lambda: device.set_sidetone_level(5),
                   lambda: device.set_equalizer_preset('game'),
                   lambda: device.set_idle_time(900),
                   lambda: device.set_power_saving(True)):
        with pytest.raises(DeviceError, match='switched off'):
            change()
    assert writes(fake) == [] and fake.headset.lost == []


def test_a_headset_that_is_off_is_asked_once_and_then_left_alone(headset):
    device, fake = headset
    fake.headset.switched_on = False
    assert device.is_headset_connected() is False
    asked = len(fake.headset.requests)
    for read in (device.get_idle_time, device.get_sidetone_level, device.is_charging,
                 device.get_equalizer_bands, device.get_equalizer_preset):
        with pytest.raises(DeviceError, match='switched off'):
            read()
    assert len(fake.headset.requests) == asked


def test_it_is_noticed_coming_back(headset):
    device, fake = headset
    fake.headset.switched_on = False
    assert device.get_audio() == {'connected': False}
    fake.headset.switched_on = True
    device._quiet_until = 0.0       # the quiet spell is over
    assert device.get_audio()['connected'] is True


def test_the_sleep_timer_of_a_headset_out_of_reach_is_not_a_time(headset):
    device, fake = headset
    fake.headset.switched_on = False
    with pytest.raises(DeviceError, match='switched off'):
        device.get_idle_time()


def test_on_the_cable_a_strange_answer_is_an_error_and_not_the_headset_being_off(cabled):
    device, fake = cabled
    fake.headset.battery = 0xFF
    with pytest.raises(DeviceError, match='unexpected battery level'):
        device.get_battery_level()
    fake.headset.battery = 60
    assert device.get_battery_level() == 60     # and nothing keeps it quiet


def test_who_it_is_needs_no_headset(headset):
    device, fake = headset
    fake.headset.switched_on = False
    assert device.get_audio() == {'connected': False}
    assert device.serial == 'BSV3X057D'
    assert device.capabilities()['audio']


# -- when things go wrong -----------------------------------------------------

def test_a_command_the_firmware_lacks_is_not_supported(headset):
    device, fake = headset
    fake.headset.force_status = Status.UNSUPPORTED
    with pytest.raises(NotSupported):
        device.get_battery_level()


def test_a_command_that_failed_is_a_device_error(headset):
    device, fake = headset
    fake.headset.force_status = Status.FAILED
    with pytest.raises(DeviceError, match='failed'):
        device.get_power_saving()


def test_an_unplugged_receiver_is_an_error_and_can_be_tried_again(headset, monkeypatch):
    device, fake = headset

    def gone(length, report_id):
        raise HidError('HidD_GetFeature failed: WinError 1167')

    with monkeypatch.context() as patched:
        patched.setattr(fake.headset, 'get_feature', gone)
        with pytest.raises(DeviceError, match='1167'):
            device.get_battery_level()
    assert device.channel._handle is None
    assert device.get_battery_level() == 85


def test_a_handle_that_will_not_open_is_an_error(tmp_path):
    meta = get_database().get(0x1532, WIRED)
    fake = FakeRazerDevice(WIRED, meta.name, headset=True)
    backend = FakeHidBackend()      # nothing behind the collection
    device = BlackSharkV3XDevice(meta, FeatureChannel(backend, fake.headset_info, wait=0),
                                 Persistence(str(tmp_path / 'p.json')))
    with pytest.raises(DeviceError):
        device.get_battery_level()
    assert device.serial == 'BSV3X057C'


def test_the_channel_asks_for_the_report_id_it_sent_and_the_size_windows_declares():
    calls = []

    class Handle:
        def send_feature_report(self, data):
            calls.append(('set', len(data), data[0]))

        def get_feature_report(self, length, report_id=0):
            calls.append(('get', length, report_id))
            return bytes(length)

        def close(self):
            calls.append(('close',))

    class Backend:
        def open(self, info):
            return Handle()

    channel = FeatureChannel(Backend(), collection(4, 0xFF82, feature=65), wait=0)
    channel.exchange(protocol.request(1, Class.POWER, Command.BATTERY | protocol.READ))
    assert calls == [('set', 65, 0x07), ('get', 65, 0x07)]
    channel.close()
    assert calls[-1] == ('close',)


# -- through the daemon -------------------------------------------------------

def test_its_own_settings_can_be_reached_over_rpc():
    assert {'set_microphone_noise_cancelling', 'set_power_saving'} <= DEVICE_METHODS


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """A daemon with the receiver plugged in, for the command line to talk to."""
    service = DaemonService(backend=build_demo_backend((WIRELESS,)), enable_effects=True,
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
    serial = 'BSV3X057D'
    assert daemon._pending == {}
    listed = daemon.rpc_list_devices()
    assert (listed[0]['serial'], listed[0]['firmware']) == (serial, 'v1.0.14')
    assert daemon.rpc_capabilities(serial)['protocol'] == 'blackshark_v3x'
    daemon.rpc_call(serial, 'set_equalizer_preset', ['movie'])
    daemon.rpc_call(serial, 'set_microphone_noise_cancelling', [True])
    audio = daemon.rpc_call(serial, 'get_audio')
    assert (audio['equalizer_preset'], audio['microphone_noise_cancelling']) == ('movie', True)
    assert daemon.rpc_profile_list(serial) == []


def test_the_command_line_shows_and_sets_what_it_has(daemon, capsys):
    assert main(['headset', 'eq', 'game']) == 0
    assert main(['headset', 'eq', '1,2,3,4,5,6,0,-1,-2,-3']) == 0
    assert main(['headset', 'sidetone', '9']) == 0
    assert main(['headset', 'nc', 'on']) == 0
    assert main(['headset', 'saving', 'on']) == 0
    assert main(['headset', 'sleep', '45']) == 0
    capsys.readouterr()
    assert main(['headset']) == 0
    output = capsys.readouterr().out
    assert 'battery     85%' in output
    assert 'sidetone    level 9' in output
    assert 'equalizer   game' in output
    assert 'its curve   1 2 3 4 5 6 0 -1 -2 -3' in output
    assert 'noise cancelling  on' in output and 'power saving  on' in output
    assert 'after 45 min' in output
    # Settings it does not have are not shown as if they were off.
    assert 'microphone' not in output and 'do not disturb' not in output


def test_the_command_line_says_what_it_lacks(daemon, capsys):
    for arguments in (['headset', 'dnd', 'on'], ['headset', 'led', 'battery'],
                      ['headset', 'eq', 'off']):
        assert main(arguments) == 1
    errors = capsys.readouterr().err
    assert 'do-not-disturb' in errors and 'receiver' in errors and 'switched off' in errors
