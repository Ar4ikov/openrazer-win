"""Razer's Bluetooth LE lighting protocol.

Not from OpenRazer -- upstream has never covered a Bluetooth Razer device.
This was recovered by capturing what Razer Synapse sends over the air: an HCI
trace while the colour was changed in Synapse showed ATT Write Commands to one
vendor characteristic, carrying a three-byte header and one RGB triple per
zone.  Setting pure red, green and blue in Synapse produced exactly
``ff 00 00 ff 00 00``, ``00 ff 00 00 ff 00`` and ``00 00 ff 00 00 ff``, and a
control capture with Synapse closed produced no writes at all.

Only what was observed is implemented here.  Effects like breathing and
spectrum are driven by Synapse streaming frames rather than by a device-side
mode, so they are rendered on the host instead -- see
:mod:`openrazer_win.effects`.
"""
from __future__ import annotations

from typing import Optional, Sequence

#: Razer's vendor GATT service.  The UUID spells "Amel-RazerBLE" in ASCII.
SERVICE_UUID = '416d656c-2d52-617a-6572-424c4501f40a'

#: Write without response -- the one Synapse drives lighting through.
WRITE_CHARACTERISTIC = '416d0000-2d52-617a-6572-424c4501f40a'

#: Write with response, and a notify characteristic.  Their meaning is not
#: known; nothing here uses them.
COMMAND_CHARACTERISTIC = '416d0002-2d52-617a-6572-424c4501f40a'
NOTIFY_CHARACTERISTIC = '416d0001-2d52-617a-6572-424c4501f40a'

#: Razer USA Ltd, as it appears in Bluetooth advertisements.
COMPANY_ID = 0x068E

#: Razer's manufacturer-specific advertisement payload, as captured from the
#: hardware::
#:
#:     05 62 00 60 34 57 cd 5e 44 00
#:     ^^^^^    ^^^^^^^^^^^^^^^^^^
#:     pid      classic address, least-significant byte first
#:
#: which is product id 0x0562 and BD_ADDR 44:5E:CD:57:34:60 -- the same headset
#: whose LE address is 44:5E:CD:58:34:60, one byte away.  That the product id
#: is right there is what makes identification reliable: the local name arrives
#: in a separate packet that carries no manufacturer data at all.
MANUFACTURER_DATA_LENGTH = 10

#: The local name a device calls itself, kept as a fallback for a device whose
#: manufacturer data never arrives.  Observed on the hardware.
ADVERTISED_NAMES = {
    'Razer Stereo': (0x0562,),      # Kraken Kitty V2 BT
}

#: Every command starts with 0xC4 0x00, then the payload length.
COMMAND_PREFIX = 0xC4
COLOUR_COMMAND_LENGTH = 0x06

#: The two lighting zones, in the order their bytes appear on the wire.
#: Verified on the hardware: a command carrying red then blue lit the LEFT ear
#: red and the right ear blue, as worn -- so the first triple is the left ear.
ZONES = ('left', 'right')


def _channel(value: int) -> int:
    return 0 if value < 0 else (255 if value > 255 else int(value))


def colour_command(zones: Sequence) -> bytes:
    """Build the lighting command for a list of ``(r, g, b)`` triples.

    A single colour is applied to every zone, matching what Synapse sends when
    the user picks one colour: the same triple repeated.
    """
    colours = [tuple(colour)[:3] for colour in zones]
    if not colours:
        raise ValueError('at least one colour is required')
    if len(colours) == 1:
        colours = colours * len(ZONES)
    if len(colours) != len(ZONES):
        raise ValueError('this device has {0} zones, got {1} colours'.format(
            len(ZONES), len(colours)))

    payload = bytearray((COMMAND_PREFIX, 0x00, COLOUR_COMMAND_LENGTH))
    for colour in colours:
        payload.extend(_channel(component) for component in colour)
    return bytes(payload)


def off_command() -> bytes:
    """All zones black.  The capture shows Synapse using this for "off"."""
    return colour_command([(0, 0, 0)] * len(ZONES))


def product_id_from_advertisement(data) -> Optional[int]:
    """The product id in Razer's manufacturer data, or None if it is not there.

    Windows hands over the payload with the company id already stripped, so the
    product id is the first two bytes, most significant first.
    """
    raw = bytes(data or b'')
    if len(raw) < 2:
        return None
    return (raw[0] << 8) | raw[1]


def classic_address_from_advertisement(data) -> Optional[int]:
    """The device's classic Bluetooth address, which it also advertises.

    Useful for tying an LE advertisement to the paired device Windows shows,
    since a dual-mode device need not use the same address on both radios.
    """
    raw = bytes(data or b'')
    if len(raw) < 9:
        return None
    address = 0
    for byte in reversed(raw[3:9]):
        address = (address << 8) | byte
    return address
