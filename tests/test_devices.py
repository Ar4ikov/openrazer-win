"""The device layer, driven against the emulator."""
from __future__ import annotations

import pytest

from openrazer_win.core.device import DeviceError
from openrazer_win.core.transport import (
    is_argb_interface, is_control_interface, select_control_interface,
)
from openrazer_win.devices.recipes import NotSupported
from openrazer_win.hid.base import HidDeviceInfo
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice
from openrazer_win.protocol.report import Status

from .conftest import KEYBOARD_PID, MOUSE_PID, last_report, make_device


# -- identity ---------------------------------------------------------------

def test_serial_and_firmware_come_from_the_device(persistence):
    device, fake = make_device(KEYBOARD_PID, persistence)
    fake.state.serial = 'PM1234567890'
    fake.state.firmware = (2, 7)
    assert device.serial == 'PM1234567890'
    assert device.firmware_version == 'v2.7'
    device.close()


def test_serial_is_cached(keyboard):
    device, fake = keyboard
    before = len(fake.sent_reports)
    _ = device.serial
    assert len(fake.sent_reports) == before


def test_serial_falls_back_when_the_device_will_not_answer(persistence):
    device, fake = make_device(KEYBOARD_PID, persistence)
    fake.force_status = Status.NOT_SUPPORTED
    assert device.serial.startswith('RZ')
    device.close()


# -- effects ----------------------------------------------------------------

def test_static_effect_reaches_the_wire(keyboard):
    device, fake = keyboard
    device.set_static(255, 0, 0)
    report = last_report(fake, 0x03, 0x0A)
    assert report.arguments[0] == 0x06                       # matrix static
    assert bytes(report.arguments[1:4]) == b'\xff\x00\x00'


def test_breathing_arity_selects_the_right_command(keyboard):
    device, fake = keyboard
    device.set_breath_random()
    assert last_report(fake, 0x03, 0x0A).arguments[1] == 0x03
    device.set_breath_single(1, 2, 3)
    assert last_report(fake, 0x03, 0x0A).arguments[1] == 0x01
    device.set_breath_dual(1, 2, 3, 4, 5, 6)
    assert last_report(fake, 0x03, 0x0A).arguments[1] == 0x02


def test_wave_direction_is_clamped(keyboard):
    device, fake = keyboard
    device.set_wave(9)
    assert last_report(fake, 0x03, 0x0A).arguments[1] == 0x02


def test_reactive_carries_speed_then_colour(keyboard):
    device, fake = keyboard
    device.set_reactive(10, 20, 30, speed=3)
    report = last_report(fake, 0x03, 0x0A)
    assert report.arguments[1] == 3
    assert bytes(report.arguments[2:5]) == bytes((10, 20, 30))


def test_mouse_uses_the_extended_protocol_on_the_logo_zone(mouse):
    device, fake = mouse
    device.set_static(0, 255, 0, zone='logo')
    report = last_report(fake, 0x0F, 0x02)
    assert report.arguments[1] == 0x04           # logo LED
    assert bytes(report.arguments[6:9]) == bytes((0, 255, 0))


def test_unsupported_effect_raises_not_supported(mouse):
    device, _ = mouse
    with pytest.raises(NotSupported):
        device.set_wheel(1, zone='logo')


def test_available_effects_matches_the_upstream_device_class(mouse):
    device, _ = mouse
    effects = device.available_effects('logo')
    assert 'static' in effects and 'spectrum' in effects
    assert 'starlight_random' not in effects   # the Viper has no starlight


def test_capabilities_do_not_invent_zones(mouse):
    device, _ = mouse
    assert list(device.capabilities()['zones']) == ['logo']


# -- brightness -------------------------------------------------------------

def test_brightness_round_trips_as_a_percentage(keyboard):
    device, _ = keyboard
    device.set_brightness(50)
    assert device.get_brightness() == pytest.approx(50, abs=0.5)


def test_brightness_is_clamped(keyboard):
    device, fake = keyboard
    device.set_brightness(500)
    assert last_report(fake, 0x03, 0x03).arguments[2] == 255
    device.set_brightness(-20)
    assert last_report(fake, 0x03, 0x03).arguments[2] == 0


def test_brightness_is_remembered(keyboard):
    device, _ = keyboard
    device.set_brightness(42)
    assert device.persistence.get(device.serial, 'backlight', 'brightness') == 42


# -- mouse settings ---------------------------------------------------------

def test_dpi_round_trips(mouse):
    device, _ = mouse
    device.set_dpi(3200, 1600)
    assert device.get_dpi() == (3200, 1600)


def test_dpi_is_capped_at_the_device_maximum(mouse):
    device, _ = mouse
    device.set_dpi(999999)
    assert device.get_dpi()[0] == device.info.dpi_max


def test_dpi_stages_round_trip(mouse):
    device, _ = mouse
    device.set_dpi_stages(2, [(400, 400), (800, 800), (3200, 3200)])
    active, stages = device.get_dpi_stages()
    assert active == 2
    assert stages == [(400, 400), (800, 800), (3200, 3200)]


def test_poll_rate_round_trips(mouse):
    device, _ = mouse
    device.set_poll_rate(125)
    assert device.get_poll_rate() == 125


# -- custom frames ----------------------------------------------------------

def test_custom_frame_sends_one_report_per_row(keyboard):
    device, fake = keyboard
    rows, columns = device.matrix_dimensions
    before = len(fake.sent_reports)
    payload = b''
    for row in range(rows):
        payload += bytes((row, 0, columns - 1)) + bytes(columns * 3)
    device.set_key_row(payload)
    assert len(fake.sent_reports) - before == rows


# -- transport --------------------------------------------------------------

def test_transport_retries_then_reports_the_device_status(keyboard):
    device, fake = keyboard
    fake.force_status = Status.FAILURE
    with pytest.raises(DeviceError, match='failure'):
        device.set_static(1, 2, 3)


def test_unsupported_status_is_not_retried(keyboard):
    device, fake = keyboard
    fake.force_status = Status.NOT_SUPPORTED
    before = len(fake.sent_reports)
    with pytest.raises(DeviceError):
        device.set_static(1, 2, 3)
    assert len(fake.sent_reports) - before == 1


def test_busy_counts_as_success(keyboard):
    device, fake = keyboard
    fake.force_status = Status.BUSY
    device.set_static(1, 2, 3)      # must not raise


def test_control_interface_is_chosen_by_feature_report_length():
    plain = HidDeviceInfo(path='a', vendor_id=0x1532, product_id=1,
                          interface=0, feature_length=0)
    control = HidDeviceInfo(path='b', vendor_id=0x1532, product_id=1,
                            interface=1, feature_length=91)
    argb = HidDeviceInfo(path='c', vendor_id=0x1532, product_id=1,
                         interface=1, feature_length=321)
    assert is_control_interface(control) and not is_control_interface(plain)
    assert is_argb_interface(argb)
    assert select_control_interface([plain, control, argb]) is control
    assert select_control_interface([plain]) is None


def test_preferred_interface_wins_when_several_match():
    first = HidDeviceInfo(path='a', vendor_id=0x1532, product_id=1,
                          interface=0, feature_length=91)
    second = HidDeviceInfo(path='b', vendor_id=0x1532, product_id=1,
                           interface=3, feature_length=91)
    assert select_control_interface([first, second], preferred_index=3) is second
    assert select_control_interface([first, second], preferred_index=9) is first


def test_interface_number_is_parsed_from_the_windows_path():
    path = r'\\?\hid#vid_1532&pid_0203&mi_02&col01#7&abc#{4d1e55b2}'
    assert HidDeviceInfo.parse_interface(path) == 2
    assert HidDeviceInfo.parse_collection(path) == 1
    assert HidDeviceInfo.parse_interface(r'\\?\hid#vid_1532&pid_0203#7&abc') is None


def test_argb_frames_go_to_the_wide_collection(persistence):
    device, fake = make_device(0x0F1F, persistence, argb=True)   # ARGB controller
    device.set_key_row(bytes((0, 0, 2)) + bytes((1, 2, 3, 4, 5, 6, 7, 8, 9)))
    assert fake.argb_frames, 'expected an ARGB frame to be sent'
    frame = fake.argb_frames[-1]
    assert frame[0] == 0x04            # report id for channels below 5
    assert frame[5:14] == bytes((1, 2, 3, 4, 5, 6, 7, 8, 9))
    device.close()


# -- persistence ------------------------------------------------------------

def test_restore_reapplies_the_stored_effect(keyboard):
    device, fake = keyboard
    device.set_static(7, 8, 9)
    device.set_brightness(60)
    before = len(fake.sent_reports)
    device.restore()
    assert len(fake.sent_reports) > before
    assert bytes(last_report(fake, 0x03, 0x0A).arguments[1:4]) == bytes((7, 8, 9))


def test_persistence_survives_a_reload(persistence, tmp_path):
    device, _ = make_device(KEYBOARD_PID, persistence)
    device.set_brightness(33)
    persistence.save()
    device.close()

    from openrazer_win.core.persistence import Persistence
    reloaded = Persistence(persistence.path)
    assert reloaded.get(device.serial, 'backlight', 'brightness') == 33


def test_manager_finds_and_drops_devices(persistence):
    from openrazer_win.core.manager import DeviceManager
    fake = FakeRazerDevice(MOUSE_PID, 'Razer Viper', interface=0)
    backend = FakeHidBackend([fake])
    manager = DeviceManager(backend=backend, persistence=persistence)
    assert len(manager.scan()) == 1
    backend.remove(fake)
    assert manager.scan() == []
    manager.close()


def test_manager_reports_unknown_razer_hardware(persistence):
    from openrazer_win.core.manager import DeviceManager
    backend = FakeHidBackend([FakeRazerDevice(0xABCD, 'Razer Something New')])
    manager = DeviceManager(backend=backend, persistence=persistence)
    assert manager.scan() == []
    assert manager.unsupported == [(0xABCD, 'Razer Something New')]
    manager.close()
