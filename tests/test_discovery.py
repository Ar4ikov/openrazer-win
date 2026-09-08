"""Reporting Razer hardware that exists but is not a usable HID collection.

A headset paired over Bluetooth, or on a charge-only cable, is obviously
connected from where the user is standing while being entirely absent from the
HID enumeration.  ``doctor`` has to say what Windows *does* see it as.
"""
from __future__ import annotations

import sys

import pytest

from openrazer_win.hid.devices import AttachedDevice, list_attached, summarise


def node(instance_id, description='', device_class='', vendor=0x1532, product=None):
    return AttachedDevice(instance_id=instance_id, description=description,
                          device_class=device_class, vendor_id=vendor,
                          product_id=product)


# -- identifier parsing -----------------------------------------------------

def test_usb_and_bluetooth_spell_the_vendor_id_differently():
    """Bluetooth ids carry a four-digit source prefix before the vendor."""
    from openrazer_win.hid.devices import _PRODUCT_RE, _VENDOR_RE, _identifier

    usb = r'USB\VID_1532&PID_0078&MI_00\7&1F4F5F27&0&0000'
    assert _identifier(_VENDOR_RE.search(usb)) == 0x1532
    assert _identifier(_PRODUCT_RE.search(usb)) == 0x0078

    bluetooth = (r'BTHENUM\{0000110B-0000-1000-8000-00805F9B34FB}'
                 r'_VID&00011532_PID&0562\8&33428996&0&445ECD573460_C00000000')
    assert _identifier(_VENDOR_RE.search(bluetooth)) == 0x1532
    assert _identifier(_PRODUCT_RE.search(bluetooth)) == 0x0562


def test_a_node_without_identifiers_is_tolerated():
    from openrazer_win.hid.devices import _VENDOR_RE, _identifier

    assert _identifier(_VENDOR_RE.search(r'ROOT\SYSTEM\0000')) is None


@pytest.mark.parametrize('instance_id, expected', [
    (r'USB\VID_1532&PID_0078', 'USB'),
    (r'HID\VID_1532&PID_0078&MI_01', 'HID'),
    (r'BTHENUM\{0000110B}_VID&00011532_PID&0562', 'Bluetooth'),
    (r'BTHHFENUM\BTHHFPAUDIO\9&3239CD9F', 'Bluetooth'),
    (r'SWD\MMDEVAPI\{0.0.1.00000000}', 'software'),
])
def test_the_transport_is_read_from_the_enumerator(instance_id, expected):
    assert node(instance_id).transport == expected


# -- grouping ---------------------------------------------------------------

def test_a_headset_collapses_to_one_line():
    """Windows exposes a Bluetooth headset as half a dozen nodes."""
    nodes = [
        node(r'BTHENUM\{1101}_VID&00011532_PID&0562',
             'Standard Serial over Bluetooth link (COM4)', 'Ports', product=0x0562),
        node(r'BTHENUM\{111E}_VID&00011532_PID&0562',
             'Razer Kraken Kitty V2 BT Hands-Free AG', 'System', product=0x0562),
        node(r'BTHENUM\{110B}_VID&00011532_PID&0562',
             'Razer Kraken Kitty V2 BT', 'MEDIA', product=0x0562),
        node(r'BTHENUM\{110C}_VID&00011532_PID&0562',
             'AVRCP transport Razer Kraken Kitty V2 BT', 'Bluetooth', product=0x0562),
    ]
    rows = summarise(nodes)
    assert len(rows) == 1
    row = rows[0]
    assert row['product_id'] == 0x0562
    assert row['transport'] == 'Bluetooth'
    assert row['nodes'] == 4
    # The shortest description is the product name; the rest wrap it.
    assert row['name'] == 'Razer Kraken Kitty V2 BT'
    assert 'MEDIA' in row['classes']


def test_the_same_product_over_two_transports_stays_separate():
    rows = summarise([
        node(r'USB\VID_1532&PID_0560', 'Kraken', 'HIDClass', product=0x0560),
        node(r'BTHENUM\x_VID&00011532_PID&0560', 'Kraken BT', 'MEDIA', product=0x0560),
    ])
    assert {row['transport'] for row in rows} == {'USB', 'Bluetooth'}


def test_summarise_handles_nothing_attached():
    assert summarise([]) == []


# -- doctor's report --------------------------------------------------------

def test_doctor_explains_a_bluetooth_only_headset(capsys, monkeypatch):
    from openrazer_win import cli
    from openrazer_win.devices import get_database

    monkeypatch.setattr(cli, 'get_database', get_database, raising=False)
    monkeypatch.setattr('openrazer_win.hid.devices.list_attached', lambda vendor_id=None: [
        node(r'BTHENUM\{110B}_VID&00011532_PID&0562',
             'Razer Kraken Kitty V2 BT', 'MEDIA', product=0x0562),
    ])

    cli._report_devices_beyond_hid([], get_database())
    output = capsys.readouterr().out
    assert 'Razer Kraken Kitty V2 BT' in output
    # It has no USB data mode at all, so telling the user to find a better
    # cable would send them chasing a fault that is not there.
    assert 'only charges it' in output
    assert 'Bluetooth LE' in output
    assert 'USB *data* cable' not in output


def test_doctor_says_how_to_install_the_bluetooth_support(capsys, monkeypatch):
    from openrazer_win import cli
    from openrazer_win.devices import get_database

    monkeypatch.setattr('openrazer_win.ble.is_available', lambda: False)
    monkeypatch.setattr('openrazer_win.hid.devices.list_attached', lambda vendor_id=None: [
        node(r'BTHENUM\{110B}_VID&00011532_PID&0562',
             'Razer Kraken Kitty V2 BT', 'MEDIA', product=0x0562),
    ])
    cli._report_devices_beyond_hid([], get_database())
    assert 'openrazer-win[ble]' in capsys.readouterr().out


def test_doctor_still_asks_for_a_data_cable_on_a_usb_device(capsys, monkeypatch):
    """A wireless mouse paired over Bluetooth is still driven over USB.

    Only devices the database marks as Bluetooth-only get the other advice.
    """
    from openrazer_win import cli
    from openrazer_win.devices import get_database

    monkeypatch.setattr('openrazer_win.hid.devices.list_attached', lambda vendor_id=None: [
        node(r'BTHENUM\{110B}_VID&00011532_PID&0078',
             'Razer Viper', 'MEDIA', product=0x0078),
    ])
    cli._report_devices_beyond_hid([], get_database())
    output = capsys.readouterr().out
    assert 'Paired over Bluetooth' in output
    assert 'USB *data* cable' in output


def test_doctor_does_not_ask_users_to_report_a_webcam(capsys, monkeypatch):
    from openrazer_win import cli
    from openrazer_win.devices import get_database

    monkeypatch.setattr('openrazer_win.hid.devices.list_attached', lambda vendor_id=None: [
        node(r'USB\VID_1532&PID_0E05&MI_00', 'Razer Kiyo Pro', 'Camera', product=0x0E05),
    ])
    cli._report_devices_beyond_hid([], get_database())
    output = capsys.readouterr().out
    assert 'not a device openrazer-win controls' in output
    assert 'reporting' not in output


def test_doctor_does_ask_about_an_unknown_lighting_device(capsys, monkeypatch):
    from openrazer_win import cli
    from openrazer_win.devices import get_database

    monkeypatch.setattr('openrazer_win.hid.devices.list_attached', lambda vendor_id=None: [
        node(r'USB\VID_1532&PID_ABCD', 'Razer Something New', 'HIDClass',
             product=0xABCD),
    ])
    cli._report_devices_beyond_hid([], get_database())
    assert 'Worth reporting' in capsys.readouterr().out


def test_doctor_stays_quiet_about_devices_already_listed(capsys, monkeypatch):
    from openrazer_win import cli
    from openrazer_win.devices import get_database
    from openrazer_win.hid.base import HidDeviceInfo

    monkeypatch.setattr('openrazer_win.hid.devices.list_attached', lambda vendor_id=None: [
        node(r'USB\VID_1532&PID_0078', 'Razer Viper', 'HIDClass', product=0x0078),
    ])
    collections = [HidDeviceInfo(path='a', vendor_id=0x1532, product_id=0x0078,
                                 feature_length=91)]
    cli._report_devices_beyond_hid(collections, get_database())
    assert capsys.readouterr().out == ''


# -- the real thing ---------------------------------------------------------

def test_enumeration_runs_on_this_machine():
    if sys.platform != 'win32':
        assert list_attached(vendor_id=0x1532) == []
        return
    # Every node must at least parse into the dataclass without raising.
    for device in list_attached():
        assert isinstance(device.instance_id, str) and device.instance_id
        assert isinstance(device.transport, str)
        device.describe()
