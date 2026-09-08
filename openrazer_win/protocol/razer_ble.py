"""Razer's Bluetooth LE vendor protocol.

Not from OpenRazer -- upstream has never covered a Bluetooth Razer device.
This was recovered from HCI traces of Razer Synapse driving the hardware.

Everything travels as ``<opcode> <kind> <length> <payload>`` on one
write-without-response characteristic, and the device answers on a notify
characteristic with the same opcode and ``kind`` 0x01.  Nothing in the service
is readable by a plain GATT read, so that request/response pair is the only way
to ask the device anything.  The framing matches what others have found on
different Razer models, which is some comfort that it is the house style rather
than a coincidence.

What the captures established:

* ``c4`` carries one RGB triple per zone.  Pure red, green and blue in Synapse
  produced exactly ``ff 00 00 ff 00 00``, ``00 ff 00 00 ff 00`` and
  ``00 00 ff 00 00 ff``; a control capture with Synapse closed produced nothing.
* ``c1`` is brightness: Synapse's slider sent 0x7F at 50 and 0xFF at 100.
* ``21`` reads the charge percentage -- it answered 0x57 while Synapse displayed
  87% -- and the device also pushes it unprompted when it changes.
* The device has **no effects of its own**.  Breathing and spectrum in Synapse
  produced 737 colour writes with 495 distinct values over 105 seconds: they
  are drawn by the host, one frame at a time, and so are the fades between
  colours.  This port does the same, through :mod:`openrazer_win.effects`.
* Brightness and the auto-shutoff timer persist -- read back over a fresh
  connection, both still hold what was written to them.  The colour set through
  ``c4`` does not: the device falls back to a stored colour as soon as the link
  drops, at the stored brightness, so a low brightness makes that fallback dim.

* A command that writes the *stored* colour exists but has not been found.  The
  evidence is plain: this headset shipped showing white and has shown Razer
  green ever since Synapse first set it up, so something wrote green into it
  once.  It was not any of the traffic captured here -- three captures of
  colour, brightness, effect and power changes contain no such write -- which
  points at the one-off device setup Synapse performs on first install.  That
  is also the least safe place to go guessing, since it is where firmware
  pushes live, and the device answers an unknown opcode with silence rather
  than an error, so guessing would be blind.  Hence: not implemented, not
  guessed at, and not claimed to be absent.

Only what was observed is implemented.  The opcode space is plainly larger, and
the rest is deliberately not guessed at: the device answers an unknown opcode
with silence rather than an error, so guessing would be blind, on a channel
where a wrong guess could reach the firmware.
"""
from __future__ import annotations

from typing import Optional, Sequence

#: Razer's vendor GATT service.  The UUID spells "Amel-RazerBLE" in ASCII.
SERVICE_UUID = '416d656c-2d52-617a-6572-424c4501f40a'

#: Write without response -- the one Synapse drives lighting through.
WRITE_CHARACTERISTIC = '416d0000-2d52-617a-6572-424c4501f40a'

#: Where the device answers.  Subscribing to it is what makes reads possible.
NOTIFY_CHARACTERISTIC = '416d0001-2d52-617a-6572-424c4501f40a'

#: A second write characteristic, this one with response.  Synapse never used
#: it in any capture, and it accepts the colour command just as volatilely as
#: the other, so nothing here uses it either.
COMMAND_CHARACTERISTIC = '416d0002-2d52-617a-6572-424c4501f40a'

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

#: Every message is ``<opcode> <kind> <length> <payload>``.  Requests are sent
#: to the write characteristic and answers arrive on the notify one, which is
#: how the read-only values are obtained -- nothing in the service is readable
#: through a plain GATT read.
REQUEST = 0x00          # host -> device
RESPONSE = 0x01         # device -> host, answering a request
PUSH = 0x02             # device -> host, unprompted (a battery change)

#: Opcodes, recovered by capturing Synapse.  Only these were observed; the
#: space is clearly larger and the rest is deliberately not guessed at.
OP_STATUS = 0x20        # read; answered 1 on a working headset
OP_BATTERY = 0x21       # read; answers the charge percentage, 0..100
OP_IDLE_TIMER = 0x27    # read; answers the auto-shutoff setting as an index
OP_CHARGING = 0x29      # read; answered 0 on a headset running on battery
OP_TAKEOVER = 0xC0      # write; Synapse sends 0x05 before driving the lighting
OP_BRIGHTNESS = 0xC1    # write; 0x00..0xFF
OP_COLOUR = 0xC4        # write; one RGB triple per zone
OP_SET_IDLE_TIMER = 0xA7  # write; the same index, 0x00..0x05

#: Reads and writes of the same setting differ by this bit, which is set on
#: the write.  Every opcode observed obeys it -- reads 0x20, 0x21, 0x27, 0x29
#: against writes 0xA7, 0xC0, 0xC1, 0xC4 -- and the pair 0x27/0xA7 was seen
#: back to back on one setting, which is what pins it down.
WRITE_BIT = 0x80


def read_opcode_for(write_opcode: int) -> int:
    """The read that corresponds to a write, by the 0x80 convention."""
    return write_opcode & ~WRITE_BIT


def write_opcode_for(read_opcode: int) -> int:
    return read_opcode | WRITE_BIT

#: What Synapse passes to OP_TAKEOVER every time it applies an effect.  Its
#: meaning beyond "the host is about to drive the lighting" is not known.
TAKEOVER_VALUE = 0x05

#: Kept under its old name: this module used to know only the one command.
COMMAND_PREFIX = OP_COLOUR
COLOUR_COMMAND_LENGTH = 0x06

#: The two lighting zones, in the order their bytes appear on the wire.
#: Verified on the hardware: a command carrying red then blue lit the LEFT ear
#: red and the right ear blue, as worn -- so the first triple is the left ear.
ZONES = ('left', 'right')


def _channel(value: int) -> int:
    return 0 if value < 0 else (255 if value > 255 else int(value))


def message(opcode: int, payload: bytes = b'', kind: int = REQUEST) -> bytes:
    """Frame one message for the vendor service."""
    payload = bytes(payload)
    if len(payload) > 0xFF:
        raise ValueError('payload too long: {0} bytes'.format(len(payload)))
    return bytes((opcode, kind, len(payload))) + payload


def read_request(opcode: int) -> bytes:
    """A read carries no payload, so the length byte is zero."""
    return message(opcode)


def parse_response(data) -> tuple:
    """Split an answer into ``(opcode, kind, payload)``.

    Raises :class:`ValueError` on anything too short to be one, so a stray
    notification cannot be mistaken for a reply.
    """
    raw = bytes(data or b'')
    if len(raw) < 3:
        raise ValueError('truncated response: {0!r}'.format(raw))
    opcode, kind, length = raw[0], raw[1], raw[2]
    payload = raw[3:3 + length]
    if len(payload) != length:
        raise ValueError('response claims {0} bytes, carries {1}'.format(
            length, len(payload)))
    return opcode, kind, payload


def brightness_command(level: int) -> bytes:
    """Brightness, as the byte the device wants.

    Synapse's slider is a percentage: it sent 0x7F at 50 and 0xFF at 100.
    """
    return message(OP_BRIGHTNESS, bytes((_channel(level),)))


def takeover_command() -> bytes:
    """Announce that the host is about to drive the lighting."""
    return message(OP_TAKEOVER, bytes((TAKEOVER_VALUE,)))


def percentage_to_level(percent: float) -> int:
    """Turn a 0..100 percentage into the 0..255 byte the device takes."""
    percent = 0.0 if percent < 0 else (100.0 if percent > 100 else float(percent))
    return int(round(percent * 255 / 100))


def level_to_percentage(level: int) -> float:
    return round(_channel(level) * 100 / 255, 1)


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

    body = bytearray()
    for colour in colours:
        body.extend(_channel(component) for component in colour)
    return message(OP_COLOUR, bytes(body))


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
