"""The BlackShark V2 Pro (2023) on its receiver, 1532:0555 (issue #32)."""
import pytest

from openrazer_win.core.blackshark_v2pro import BlackSharkV2ProDevice
from openrazer_win.core.device import DeviceError
from openrazer_win.core.manager import DeviceManager
from openrazer_win.core.persistence import Persistence
from openrazer_win.devices.recipes import NotSupported
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice
from openrazer_win.protocol import blackshark_v2pro as protocol

PID = 0x0555


def test_frames_are_the_ones_the_sources_send():
    assert protocol.remote()[:12].hex(' ') == '02 80 07 00 00 50 41 0e 00 02 e1 01'
    assert protocol.read(0x21)[:13].hex(' ') == '02 80 08 00 00 50 41 08 00 03 21 00 00'
    assert protocol.write(0x19, [7])[:14].hex(' ') == '02 80 09 00 00 50 41 08 00 04 99 00 01 07'
    assert len(protocol.read(0x21)) == protocol.REPORT_SIZE


def test_a_reply_captured_from_the_headset_reads():
    # matej-basic/blackshark-battery: battery at 75 %.
    captured = bytes.fromhex('020e504908ce2d8303000400210101' '4b').ljust(64, b'\x00')
    assert protocol.reply(captured, 0x21) == bytes((75,))
    assert protocol.reply(captured, 0x2A) is None
    # Something the receiver says by itself is no reply.
    assert protocol.reply(bytes.fromhex('020d504901c0').ljust(64, b'\x00'), 0x21) is None


@pytest.fixture
def rig(tmp_path):
    fake = FakeRazerDevice(PID, 'Razer BlackShark V2 Pro 2.4', headset=True)
    backend = FakeHidBackend([fake])
    manager = DeviceManager(backend=backend, persistence=Persistence(str(tmp_path / 'p.json')),
                            bluetooth=False)
    device = manager.scan()[0]
    yield device, fake.headset
    manager.close()


def test_the_receiver_becomes_a_v2_pro(rig):
    device, _ = rig
    assert isinstance(device, BlackSharkV2ProDevice)
    caps = device.capabilities()
    assert caps['battery'] and caps['audio']['sidetone_max'] == 10
    assert caps['audio']['equalizer_presets'] == [] and caps['audio']['dongle_led_modes'] == []
    assert caps['idle_time_choices'] == [900, 1800, 2700, 3600]
    assert device.serial == 'RZ15320555'


def test_every_request_wakes_the_link_first(rig):
    device, headset = rig
    assert device.get_battery_level() == 64
    assert headset.frames[0][7] == protocol.FAMILY_RECEIVER      # remote mode
    assert headset.frames[1][10] == 0x21                           # then the read


def test_settings_reach_the_headset(rig):
    device, headset = rig
    device.set_sidetone_level(4)
    assert headset.registers[0x19] == 4
    device.set_sidetone(True)
    assert headset.registers[0x18] == 1
    device.set_idle_time(40 * 60)          # Synapse offers 15, 30, 45 and 60 minutes
    assert headset.registers[0x2C] == 45
    device.set_do_not_disturb(True)
    assert headset.registers[0x27] == 1
    audio = device.get_audio()
    assert audio == {'connected': True, 'microphone_muted': False, 'sidetone': True,
                     'sidetone_level': 4, 'do_not_disturb': True}
    with pytest.raises(DeviceError):
        device.set_sidetone_level(11)


def test_sidetone_turned_on_at_zero_gets_synapses_level(rig):
    device, headset = rig
    headset.registers[0x19] = 0
    device.set_sidetone(True)
    assert headset.registers[0x19] == protocol.SIDETONE_DEFAULT


def test_a_switched_off_headset_says_so(rig):
    device, headset = rig
    headset.on = False
    with pytest.raises(DeviceError, match='switched off'):
        device.get_battery_level()
    assert device.get_audio() == {'connected': False}


def test_what_is_not_worked_out_is_refused(rig):
    device, _ = rig
    with pytest.raises(NotSupported):
        device.set_equalizer_preset('game')
    with pytest.raises(NotSupported):
        device.set_dongle_led('off')


def test_the_firmware_is_two_numbers(rig):
    device, _ = rig
    assert device.firmware_version == 'v2.6'
