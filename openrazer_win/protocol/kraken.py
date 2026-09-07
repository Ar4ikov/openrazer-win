"""The Kraken headset protocol -- a port of ``driver/razerkraken_driver.c``.

The Kraken family predates the 90-byte control report the rest of the range
uses.  Instead it exposes its lighting controller's RAM: you write colour bytes
to fixed addresses, then write one "effect byte" to a mode address to say what
to do with them.

On the wire each message is a 37-byte HID **output** report with id 0x04, sent
to interface 3 -- the kernel's ``usb_control_msg`` with wValue 0x0204 and
wIndex 0x0003.  Each write needs about 15 ms per byte to settle, because the
controller is committing to EEPROM-backed RAM.
"""
from __future__ import annotations

from typing import Optional, Sequence

#: Report id and total length of a Kraken request, including the id byte.
REPORT_ID = 0x04
REPORT_SIZE = 37
ARGUMENT_SIZE = 32

#: Destination byte: where the address lives.
READ_RAM = 0x00
READ_EEPROM = 0x20
WRITE_RAM = 0x40

#: The controller needs this long per byte written before the next message.
WRITE_SETTLE_SECONDS = 0.015


class EffectByte:
    """The one-byte effect selector written to the mode address.

    Bit positions come from ``union razer_kraken_effect_byte``.
    """

    ON_OFF_STATIC = 1 << 0
    SINGLE_COLOUR_BREATHING = 1 << 1
    SPECTRUM_CYCLING = 1 << 2
    SYNC = 1 << 3
    TWO_COLOUR_BREATHING = 1 << 4
    THREE_COLOUR_BREATHING = 1 << 5


class Addresses:
    """Controller RAM layout, which differs between the two generations."""

    __slots__ = ('led_mode', 'custom', 'breathing')

    def __init__(self, led_mode: int, custom: int, breathing: Sequence[int]):
        self.led_mode = led_mode
        self.custom = custom
        #: One entry per breathing mode: single, dual, triple.  Older devices
        #: only have the first.
        self.breathing = tuple(breathing)

    @property
    def breathing_modes(self) -> int:
        return len(self.breathing)


#: ``KYLIE_*`` in the driver header -- Kraken V2 and everything after it.
KYLIE = Addresses(led_mode=0x172D, custom=0x1189,
                  breathing=(0x1741, 0x1745, 0x174D))

#: ``RAINIE_*`` -- the original Kraken 7.1 Chroma and the two classics.
RAINIE = Addresses(led_mode=0x1008, custom=0x1189, breathing=(0x15DE,))

#: Product id -> RAM layout, from ``razer_kraken_probe``.
LAYOUTS = {
    0x0501: RAINIE,   # Kraken 7.1 (classic)
    0x0506: RAINIE,   # Kraken 7.1 (classic, alternate)
    0x0504: RAINIE,   # Kraken 7.1 Chroma
    0x0510: KYLIE,    # Kraken 7.1 V2
    0x0520: KYLIE,    # Kraken Tournament Edition
    0x0527: KYLIE,    # Kraken Ultimate
    0x0560: KYLIE,    # Kraken Kitty V2
}

#: The classics have no addressable colour at all -- only on/off and spectrum.
#: ``razer_attr_write_matrix_effect_static`` skips the colour write for them.
COLOURLESS_PIDS = frozenset((0x0501, 0x0506))


def layout_for(product_id: int) -> Optional[Addresses]:
    return LAYOUTS.get(product_id)


def request(destination: int, length: int, address: int,
            arguments: Sequence[int] = ()) -> bytes:
    """Build one request report, ready to send as an output report."""
    if length > ARGUMENT_SIZE:
        raise ValueError('Kraken request too long: {0} bytes'.format(length))
    report = bytearray(REPORT_SIZE)
    report[0] = REPORT_ID
    report[1] = destination & 0xFF
    report[2] = length & 0xFF
    report[3] = (address >> 8) & 0xFF
    report[4] = address & 0xFF
    payload = bytes(arguments)[:ARGUMENT_SIZE]
    report[5:5 + len(payload)] = payload
    return bytes(report)


def write_ram(address: int, values: Sequence[int]) -> bytes:
    data = bytes(values)
    return request(WRITE_RAM, len(data), address, data)


def read_ram(address: int, length: int) -> bytes:
    return request(READ_RAM, length, address)


def set_effect(addresses: Addresses, effect_byte: int) -> bytes:
    return write_ram(addresses.led_mode, (effect_byte,))


# ---------------------------------------------------------------------------
# Effects.  Each returns the ordered list of reports to send.
# ---------------------------------------------------------------------------

def effect_none(addresses: Addresses) -> list:
    return [set_effect(addresses, 0x00)]


def effect_spectrum(addresses: Addresses) -> list:
    return [set_effect(addresses,
                       EffectByte.ON_OFF_STATIC | EffectByte.SPECTRUM_CYCLING)]


def effect_static(addresses: Addresses, red: int, green: int, blue: int,
                  intensity: Optional[int] = None,
                  with_colour: bool = True) -> list:
    reports = []
    if with_colour:
        colour = [red, green, blue]
        if intensity is not None:
            colour.append(intensity)
        reports.append(write_ram(addresses.breathing[0], colour))
    reports.append(set_effect(addresses, EffectByte.ON_OFF_STATIC))
    return reports


def effect_custom(addresses: Addresses, red: int, green: int, blue: int,
                  intensity: Optional[int] = None) -> list:
    colour = [red, green, blue]
    if intensity is not None:
        colour.append(intensity)
    return [write_ram(addresses.custom, colour),
            set_effect(addresses, EffectByte.ON_OFF_STATIC)]


def effect_breathing(addresses: Addresses, colours: Sequence) -> list:
    """One, two or three colours, each a ``(r, g, b)`` triple.

    Colour slots are four bytes apart -- the fourth byte is intensity, which
    the driver leaves untouched.
    """
    count = len(colours)
    if count not in (1, 2, 3):
        raise ValueError('Kraken breathing takes one, two or three colours')
    if count > addresses.breathing_modes:
        raise ValueError(
            'this Kraken supports {0} breathing colour(s), not {1}'.format(
                addresses.breathing_modes, count))

    base = addresses.breathing[count - 1]
    reports = [write_ram(base + index * 4, tuple(colour)[:3])
               for index, colour in enumerate(colours)]
    flags = {
        1: EffectByte.SINGLE_COLOUR_BREATHING,
        2: EffectByte.TWO_COLOUR_BREATHING,
        3: EffectByte.THREE_COLOUR_BREATHING,
    }[count]
    reports.append(set_effect(
        addresses, EffectByte.ON_OFF_STATIC | flags | EffectByte.SYNC))
    return reports
