"""OpenRGB's SDK protocol: its packets, and the controllers they describe.

Taken from OpenRGB's own source (``NetworkProtocol.h``, ``NetworkServer.cpp``
and ``RGBController.cpp``, releases 0.9 to 1.1).  Everything is little-endian,
as OpenRGB copies its structs straight onto the wire.  A packet is a 16-byte
header -- ``ORGB``, then the device index, the packet id and the size of what
follows, each a u32 -- and that many bytes.

What a controller description holds grew with the protocol: version 1 added
the vendor, 3 a brightness to each mode, 4 segments to each zone, 5 flags to
zones and controllers and display names for LEDs.  Version 6 (OpenRGB 1.0)
also changed how a conversation goes -- acknowledgements, controller ids in
place of indices -- and a client that speaks it uses 5 with this server, as
OpenRGB's own client does with any server older than itself.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

#: The newest protocol this server speaks.
VERSION = 5

#: Where OpenRGB's SDK server listens: "ORGB" on a phone keypad.
PORT = 6742

MAGIC = b'ORGB'
HEADER = struct.Struct('<4sIII')

#: Far more than any description or frame; OpenRGB's own limit is 8 MB.
MAX_PACKET = 1024 * 1024

# -- packet ids ----------------------------------------------------------------
REQUEST_CONTROLLER_COUNT = 0
REQUEST_CONTROLLER_DATA = 1
REQUEST_PROTOCOL_VERSION = 40
SET_CLIENT_NAME = 50
DEVICE_LIST_UPDATED = 100
REQUEST_RESCAN_DEVICES = 140
REQUEST_PROFILE_LIST = 150
REQUEST_SAVE_PROFILE = 151
REQUEST_LOAD_PROFILE = 152
REQUEST_DELETE_PROFILE = 153
REQUEST_PLUGIN_LIST = 200
RESIZE_ZONE = 1000
UPDATE_LEDS = 1050
UPDATE_ZONE_LEDS = 1051
UPDATE_SINGLE_LED = 1052
SET_CUSTOM_MODE = 1100
UPDATE_MODE = 1101
SAVE_MODE = 1102

# -- device types (``device_type``) ------------------------------------------
LEDSTRIP = 4
KEYBOARD = 5
MOUSE = 6
MOUSEMAT = 7
HEADSET = 8
HEADSET_STAND = 9
SPEAKER = 12
CASE = 15
ACCESSORY = 17
KEYPAD = 18

# -- mode flags ------------------------------------------------------------------
HAS_SPEED = 1 << 0
HAS_DIRECTION_LR = 1 << 1
HAS_BRIGHTNESS = 1 << 4
HAS_PER_LED_COLOR = 1 << 5
HAS_MODE_SPECIFIC_COLOR = 1 << 6
HAS_RANDOM_COLOR = 1 << 7
AUTOMATIC_SAVE = 1 << 9

# -- what a mode's colours are -----------------------------------------------------
COLORS_NONE = 0
COLORS_PER_LED = 1
COLORS_MODE_SPECIFIC = 2
COLORS_RANDOM = 3

DIRECTION_LEFT = 0
DIRECTION_RIGHT = 1

# -- zone types ----------------------------------------------------------------------
ZONE_SINGLE = 0
ZONE_LINEAR = 1
ZONE_MATRIX = 2

#: A cell of a matrix map with no LED in it.
NO_LED = 0xFFFFFFFF

Colour = Tuple[int, int, int]


class ProtocolError(ValueError):
    """A packet that does not hold what its id says it does."""


@dataclass
class Mode:
    name: str
    value: int = 0
    flags: int = 0
    speed_min: int = 0
    speed_max: int = 0
    brightness_min: int = 0
    brightness_max: int = 0
    colors_min: int = 0
    colors_max: int = 0
    speed: int = 0
    brightness: int = 0
    direction: int = 0
    color_mode: int = COLORS_NONE
    colors: List[Colour] = field(default_factory=list)


@dataclass
class Segment:
    name: str
    type: int
    start: int
    count: int


@dataclass
class Zone:
    name: str
    type: int
    count: int
    #: Rows of LED numbers within the zone, ``None`` where there is no LED.
    matrix: Optional[List[List[Optional[int]]]] = None
    segments: List[Segment] = field(default_factory=list)
    flags: int = 0


@dataclass
class Controller:
    type: int
    name: str
    vendor: str = ''
    description: str = ''
    version: str = ''
    serial: str = ''
    location: str = ''
    modes: List[Mode] = field(default_factory=list)
    active_mode: int = 0
    zones: List[Zone] = field(default_factory=list)
    #: LED names, in the order of their colours: zone after zone.
    leds: List[str] = field(default_factory=list)
    colors: List[Colour] = field(default_factory=list)
    flags: int = 0


# -- writing ---------------------------------------------------------------------------
def packet(device: int, packet_id: int, data: bytes = b'') -> bytes:
    return HEADER.pack(MAGIC, device, packet_id, len(data)) + data


def _string(text: str) -> bytes:
    raw = text.encode('utf-8')[:0xFFFE] + b'\0'
    return struct.pack('<H', len(raw)) + raw


def _colour(colour: Colour) -> bytes:
    """OpenRGB's ``RGBColor``: red in the low byte, then green and blue."""
    red, green, blue = (max(0, min(255, int(part))) for part in colour[:3])
    return bytes((red, green, blue, 0))


def _colours(colours) -> bytes:
    return struct.pack('<H', len(colours)) + b''.join(_colour(c) for c in colours)


def _mode_numbers(version: int) -> List[str]:
    """A mode's u32 fields after its name and value, in the order they are sent."""
    names = ['flags', 'speed_min', 'speed_max']
    if version >= 3:
        names += ['brightness_min', 'brightness_max']
    names += ['colors_min', 'colors_max', 'speed']
    if version >= 3:
        names.append('brightness')
    return names + ['direction', 'color_mode']


def _mode(mode: Mode, version: int) -> bytes:
    data = _string(mode.name) + struct.pack('<i', mode.value)
    data += b''.join(struct.pack('<I', getattr(mode, name)) for name in _mode_numbers(version))
    return data + _colours(mode.colors)


def _zone(zone: Zone, version: int) -> bytes:
    data = _string(zone.name) + struct.pack('<iIII', zone.type, zone.count, zone.count, zone.count)
    if zone.matrix:
        height, width = len(zone.matrix), len(zone.matrix[0])
        cells = [NO_LED if led is None else led for row in zone.matrix for led in row]
        data += struct.pack('<HII', 8 + 4 * len(cells), height, width)
        data += struct.pack('<{0}I'.format(len(cells)), *cells)
    else:
        data += struct.pack('<H', 0)
    if version >= 4:
        data += struct.pack('<H', len(zone.segments))
        for segment in zone.segments:
            data += _string(segment.name) + struct.pack('<iII', segment.type, segment.start,
                                                        segment.count)
    if version >= 5:
        data += struct.pack('<I', zone.flags)
    return data


def controller_data(controller: Controller, version: int) -> bytes:
    """What REQUEST_CONTROLLER_DATA is answered with: its size, then the description."""
    data = struct.pack('<i', controller.type) + _string(controller.name)
    if version >= 1:
        data += _string(controller.vendor)
    for text in (controller.description, controller.version, controller.serial,
                 controller.location):
        data += _string(text)
    data += struct.pack('<Hi', len(controller.modes), controller.active_mode)
    data += b''.join(_mode(mode, version) for mode in controller.modes)
    data += struct.pack('<H', len(controller.zones))
    data += b''.join(_zone(zone, version) for zone in controller.zones)
    data += struct.pack('<H', len(controller.leds))
    # An LED's value is the controller's own business; its number will do.
    data += b''.join(_string(name) + struct.pack('<I', index)
                     for index, name in enumerate(controller.leds))
    data += _colours(controller.colors)
    if version >= 5:
        data += struct.pack('<HI', 0, controller.flags)     # no LED display names
    return struct.pack('<I', len(data) + 4) + data


def mode_data(index: int, mode: Mode, version: int) -> bytes:
    """An UPDATEMODE or SAVEMODE packet's data, as a client sends it."""
    data = struct.pack('<i', index) + _mode(mode, version)
    return struct.pack('<I', len(data) + 4) + data


def leds_data(colours) -> bytes:
    """An UPDATELEDS packet's data."""
    data = _colours(colours)
    return struct.pack('<I', len(data) + 4) + data


def zone_leds_data(zone: int, colours) -> bytes:
    """An UPDATEZONELEDS packet's data."""
    data = struct.pack('<I', zone) + _colours(colours)
    return struct.pack('<I', len(data) + 4) + data


def single_led_data(led: int, colour: Colour) -> bytes:
    return struct.pack('<i', led) + _colour(colour)


def u32(value: int) -> bytes:
    return struct.pack('<I', value)


#: An empty list, in the form both profile and plugin lists take: its size, then a count.
EMPTY_LIST = struct.pack('<IH', 6, 0)


# -- reading -----------------------------------------------------------------------------
class Reader:
    """Fields read one after another from a packet's data."""

    def __init__(self, data: bytes):
        self.data = bytes(data)
        self.at = 0

    def fields(self, fmt: str) -> tuple:
        size = struct.calcsize(fmt)
        if self.at + size > len(self.data):
            raise ProtocolError('the packet ends {0} bytes early'.format(
                self.at + size - len(self.data)))
        values = struct.unpack_from(fmt, self.data, self.at)
        self.at += size
        return values

    def take(self, fmt: str):
        values = self.fields(fmt)
        return values[0] if len(values) == 1 else values

    def string(self) -> str:
        length = self.take('<H')
        raw = self.take('<{0}s'.format(length))
        return raw.split(b'\0', 1)[0].decode('utf-8', 'replace')

    def colour(self) -> Colour:
        red, green, blue, _ = self.take('<4B')
        return (red, green, blue)

    def colours(self) -> List[Colour]:
        return [self.colour() for _ in range(self.take('<H'))]

    def mode(self, version: int) -> Mode:
        mode = Mode(self.string(), value=self.take('<i'))
        for name in _mode_numbers(version):
            setattr(mode, name, self.take('<I'))
        mode.colors = self.colours()
        return mode

    def zone(self, version: int) -> Zone:
        name = self.string()
        kind, _minimum, _maximum, count = self.take('<iIII')
        zone = Zone(name, kind, count)
        if self.take('<H'):
            height, width = self.take('<II')
            cells = [None if cell == NO_LED else cell
                     for cell in self.fields('<{0}I'.format(height * width))]
            zone.matrix = [cells[row * width:(row + 1) * width] for row in range(height)]
        if version >= 4:
            for _ in range(self.take('<H')):
                segment_name = self.string()
                zone.segments.append(Segment(segment_name, *self.take('<iII')))
        if version >= 5:
            zone.flags = self.take('<I')
        return zone


def read_controller(data: bytes, version: int) -> Controller:
    """The other side of :func:`controller_data`: what a client makes of it."""
    reader = Reader(data)
    size = reader.take('<I')
    if size != len(data):
        raise ProtocolError('the description says it is {0} bytes, not {1}'.format(size, len(data)))
    controller = Controller(reader.take('<i'), reader.string())
    if version >= 1:
        controller.vendor = reader.string()
    controller.description = reader.string()
    controller.version = reader.string()
    controller.serial = reader.string()
    controller.location = reader.string()
    count, controller.active_mode = reader.take('<Hi')
    controller.modes = [reader.mode(version) for _ in range(count)]
    controller.zones = [reader.zone(version) for _ in range(reader.take('<H'))]
    for _ in range(reader.take('<H')):
        controller.leds.append(reader.string())
        reader.take('<I')
    controller.colors = reader.colours()
    if version >= 5:
        for _ in range(reader.take('<H')):
            reader.string()
        controller.flags = reader.take('<I')
    return controller


def read_mode(data: bytes, version: int) -> Tuple[int, Mode]:
    """UPDATEMODE and SAVEMODE: which mode, and what it is set to."""
    reader = Reader(data)
    reader.take('<I')       # a size some old clients get wrong, so not checked
    index = reader.take('<i')
    return index, reader.mode(version)


def read_leds(data: bytes) -> List[Colour]:
    reader = Reader(data)
    reader.take('<I')
    return reader.colours()


def read_zone_leds(data: bytes) -> Tuple[int, List[Colour]]:
    reader = Reader(data)
    reader.take('<I')
    zone = reader.take('<I')
    return zone, reader.colours()


def read_single_led(data: bytes) -> Tuple[int, Colour]:
    reader = Reader(data)
    return reader.take('<i'), reader.colour()
