"""The Kraken headset protocol and device class.

Krakens do not use the 90-byte control report at all: they expose their
lighting controller's RAM and take 37-byte output reports. The expected
addresses and effect bits below come from ``driver/razerkraken_driver.c`` and
its header, so a change that breaks compatibility with the Linux driver fails
here.
"""
from __future__ import annotations

import pytest

from openrazer_win.core.kraken import KrakenDevice
from openrazer_win.core.manager import DeviceManager
from openrazer_win.core.transport import (
    KRAKEN_REPORT_SIZE, Transport, is_kraken_interface, select_kraken_interface,
)
from openrazer_win.devices import get_database, get_recipes
from openrazer_win.devices.recipes import NotSupported
from openrazer_win.hid.base import HidDeviceInfo
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice
from openrazer_win.protocol import kraken as protocol

KITTY_V2 = 0x0560          # Kylie layout, full colour
KRAKEN_CHROMA = 0x0504     # Rainie layout, single breathing colour
KRAKEN_CLASSIC = 0x0501    # Rainie layout, no addressable colour


def build(pid, persistence):
    meta = get_database().get(0x1532, pid)
    assert meta is not None, hex(pid)
    fake = FakeRazerDevice(pid, meta.name, interface=3, kraken=True)
    backend = FakeHidBackend([fake])
    transport = Transport(backend, fake.kraken_info, kraken_info=fake.kraken_info)
    return KrakenDevice(meta, transport, persistence, get_recipes()), fake


# -- protocol ---------------------------------------------------------------

def test_the_request_report_layout():
    report = protocol.request(protocol.WRITE_RAM, 3, 0x172D, (1, 2, 3))
    assert len(report) == protocol.REPORT_SIZE == KRAKEN_REPORT_SIZE
    assert report[0] == 0x04                     # report id
    assert report[1] == 0x40                     # destination: write RAM
    assert report[2] == 3                        # length
    assert report[3:5] == bytes((0x17, 0x2D))    # address, big endian
    assert report[5:8] == bytes((1, 2, 3))


def test_the_layout_tables_match_the_driver_header():
    kylie = protocol.layout_for(KITTY_V2)
    assert (kylie.led_mode, kylie.custom) == (0x172D, 0x1189)
    assert kylie.breathing == (0x1741, 0x1745, 0x174D)

    rainie = protocol.layout_for(KRAKEN_CHROMA)
    assert (rainie.led_mode, rainie.custom) == (0x1008, 0x1189)
    assert rainie.breathing == (0x15DE,)


def test_every_kraken_product_id_has_a_layout():
    for pid in get_recipes().kraken_pids:
        assert protocol.layout_for(pid) is not None, hex(pid)


def test_effect_bytes_match_the_driver_bitfield():
    kylie = protocol.layout_for(KITTY_V2)
    assert protocol.effect_none(kylie)[-1][5] == 0x00
    assert protocol.effect_spectrum(kylie)[-1][5] == 0x01 | 0x04
    assert protocol.effect_static(kylie, 1, 2, 3)[-1][5] == 0x01
    assert protocol.effect_breathing(kylie, [(1, 2, 3)])[-1][5] == 0x01 | 0x02 | 0x08
    assert protocol.effect_breathing(
        kylie, [(1, 2, 3), (4, 5, 6)])[-1][5] == 0x01 | 0x10 | 0x08
    assert protocol.effect_breathing(
        kylie, [(1, 2, 3)] * 3)[-1][5] == 0x01 | 0x20 | 0x08


def test_breathing_colour_slots_are_four_bytes_apart():
    kylie = protocol.layout_for(KITTY_V2)
    reports = protocol.effect_breathing(kylie, [(1, 2, 3), (4, 5, 6), (7, 8, 9)])
    addresses = [(report[3] << 8) | report[4] for report in reports[:-1]]
    assert addresses == [0x174D, 0x174D + 4, 0x174D + 8]


def test_breathing_refuses_more_colours_than_the_device_has():
    rainie = protocol.layout_for(KRAKEN_CHROMA)
    with pytest.raises(ValueError, match='breathing colour'):
        protocol.effect_breathing(rainie, [(1, 2, 3), (4, 5, 6)])


def test_static_skips_the_colour_write_on_colourless_devices():
    rainie = protocol.layout_for(KRAKEN_CLASSIC)
    assert len(protocol.effect_static(rainie, 1, 2, 3, with_colour=False)) == 1


# -- transport --------------------------------------------------------------

def test_the_kraken_interface_is_chosen_by_output_report_length():
    plain = HidDeviceInfo(path='a', vendor_id=0x1532, product_id=KITTY_V2,
                          interface=0, output_length=0)
    control = HidDeviceInfo(path='b', vendor_id=0x1532, product_id=KITTY_V2,
                            interface=3, output_length=37)
    assert is_kraken_interface(control) and not is_kraken_interface(plain)
    assert select_kraken_interface([plain, control]) is control
    assert select_kraken_interface([plain]) is None


def test_interface_three_wins_when_several_offer_the_report():
    first = HidDeviceInfo(path='a', vendor_id=0x1532, product_id=KITTY_V2,
                          interface=0, output_length=37)
    third = HidDeviceInfo(path='b', vendor_id=0x1532, product_id=KITTY_V2,
                          interface=3, output_length=37)
    assert select_kraken_interface([first, third]) is third
    assert select_kraken_interface([first]) is first


# -- device -----------------------------------------------------------------

def test_effects_reach_the_controller_ram(persistence):
    device, fake = build(KITTY_V2, persistence)
    device.set_static(255, 0, 128)
    assert fake.kraken_ram[0x1741] == bytes((255, 0, 128))
    assert fake.kraken_ram[0x172D] == bytes((0x01,))

    device.set_spectrum()
    assert fake.kraken_ram[0x172D] == bytes((0x05,))

    device.set_none()
    assert fake.kraken_ram[0x172D] == bytes((0x00,))
    device.close()


def test_triple_breathing_writes_three_colours(persistence):
    device, fake = build(KITTY_V2, persistence)
    device.set_breath_triple(1, 2, 3, 4, 5, 6, 7, 8, 9)
    assert fake.kraken_ram[0x174D] == bytes((1, 2, 3))
    assert fake.kraken_ram[0x174D + 4] == bytes((4, 5, 6))
    assert fake.kraken_ram[0x174D + 8] == bytes((7, 8, 9))
    assert fake.kraken_ram[0x172D] == bytes((0x29,))
    device.close()


def test_capabilities_follow_the_controller_generation(persistence):
    kitty, _ = build(KITTY_V2, persistence)
    chroma, _ = build(KRAKEN_CHROMA, persistence)
    classic, _ = build(KRAKEN_CLASSIC, persistence)
    try:
        kitty_effects = kitty.capabilities()['zones']['backlight']['effects']
        assert 'breath_triple' in kitty_effects and 'static' in kitty_effects

        chroma_effects = chroma.capabilities()['zones']['backlight']['effects']
        assert 'breath_single' in chroma_effects
        assert 'breath_dual' not in chroma_effects   # Rainie has one slot

        classic_effects = classic.capabilities()['zones']['backlight']['effects']
        assert set(classic_effects) == {'none', 'spectrum'}
    finally:
        for device in (kitty, chroma, classic):
            device.close()


def test_unsupported_effects_are_refused(persistence):
    device, _fake = build(KRAKEN_CHROMA, persistence)
    with pytest.raises(NotSupported, match='breath_dual'):
        device.set_breath_dual(1, 2, 3, 4, 5, 6)
    device.close()


def test_a_kraken_reports_no_brightness_or_recipes(persistence):
    device, _fake = build(KITTY_V2, persistence)
    capabilities = device.capabilities()
    assert capabilities['protocol'] == 'kraken'
    assert capabilities['zones']['backlight']['brightness'] is False
    assert device.supports('write:matrix_effect_static') is False
    with pytest.raises(NotSupported, match='no sysfs recipes'):
        device.run('write:matrix_effect_static')
    with pytest.raises(NotSupported, match='brightness'):
        device.set_brightness(50)
    with pytest.raises(NotSupported, match='input reports'):
        _ = device.firmware_version
    assert device.serial.startswith('RZ')
    device.close()


def test_restore_reapplies_the_stored_effect(persistence):
    device, fake = build(KITTY_V2, persistence)
    device.set_breath_single(9, 8, 7)
    fake.kraken_ram.clear()
    device.restore()
    assert fake.kraken_ram[0x1741] == bytes((9, 8, 7))
    device.close()


def test_the_manager_builds_a_kraken_device(persistence):
    fake = FakeRazerDevice(KITTY_V2, 'Razer Kraken Kitty V2', interface=3, kraken=True)
    manager = DeviceManager(backend=FakeHidBackend([fake]), persistence=persistence)
    devices = manager.scan()
    assert len(devices) == 1
    assert isinstance(devices[0], KrakenDevice)
    devices[0].set_spectrum()
    assert fake.kraken_ram[0x172D] == bytes((0x05,))
    manager.close()
