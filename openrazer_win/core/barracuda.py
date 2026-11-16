"""The Barracuda X (2022), a headset with sound settings and no lights.

Its own class beside the BlackSharks for the same reason they are two: what it
offers is much the same, and nothing of how it is asked.  Its receiver does not
pass a question on to the headset until it is told to, and has to be told to
stop again afterwards, so every read and every write here is a small ceremony
of a dozen requests, done under one lock from start to finish.

It has less than a BlackShark: no sidetone that anyone has made sense of, no
switch on its equalizer, nothing to say about its microphone.  Everything set
here is stored in the headset and works without the PC, so nothing is written
to it when it is found.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Sequence

from ..devices.database import DeviceInfo
from ..devices.recipes import NotSupported
from ..hid.base import HidError
from ..protocol import barracuda as protocol
from ..protocol.barracuda import Family, Link, Register
from .device import DeviceError
from .persistence import Persistence
from .transport import TunnelChannel

logger = logging.getLogger(__name__)

#: How long a headset that was not there is taken to be off.  Every setting read
#: while it is would otherwise ask the receiver again, and Studio reads several
#: at a time.
QUIET_SECONDS = 2.0

#: The sleep timers it takes, in seconds: never, and five lengths of time.
IDLE_SECONDS = tuple(minutes * 60 for minutes in protocol.IDLE_MINUTES)

#: What the receiver goes by when Windows has no serial for it.
FALLBACK_SERIAL = 'BARX{0:04X}'


class HeadsetOff(DeviceError):
    """The receiver is there and no headset is linked to it."""


class BarracudaDevice:
    """A Barracuda X, on its receiver."""

    def __init__(self, info: DeviceInfo, channel: TunnelChannel,
                 persistence: Optional[Persistence] = None):
        self.info = info
        self.channel = channel
        self.persistence = persistence or Persistence()
        self._lock = threading.RLock()
        self._sequence = 0
        self._serial: Optional[str] = None
        self._capabilities: Optional[dict] = None
        #: Until when not to bother a headset that was not there.
        self._quiet_until = 0.0
        #: The receiver was left passing requests on, and would not stop.
        self._route_failed = False
        #: Whether the receiver answers the read length: None until asked.
        self._read_length_heard: Optional[bool] = None

    # -- identity ----------------------------------------------------------
    @property
    def name(self) -> str:
        return self.info.name

    @property
    def type(self) -> str:
        return self.info.type

    @property
    def pid(self) -> int:
        return self.info.pid

    @property
    def vid(self) -> int:
        return self.info.vid

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return '<BarracudaDevice {0} {1:04x}:{2:04x}>'.format(
            self.name, self.vid, self.pid)

    @property
    def serial(self) -> str:
        """The serial Windows read from the receiver, or a fixed name for it.

        Nobody has found a command that asks the receiver or the headset for a
        serial.  What is left is the string in the receiver's USB descriptor,
        which is there whether the headset is on or off; a receiver without
        one always goes by the same name, product id on the end, so that it is
        the same device from one start of the daemon to the next.
        """
        if self._serial is None:
            text = ''.join(c for c in (self.channel.info.serial or '') if c.isalnum())
            self._serial = text or FALLBACK_SERIAL.format(self.pid)
        return self._serial

    @property
    def firmware_version(self) -> str:
        raise NotSupported('{0} has no known way to ask for its firmware version'.format(
            self.name))

    # -- capabilities ------------------------------------------------------
    def capabilities(self) -> dict:
        if self._capabilities is None:
            self._capabilities = {
                'name': self.info.name,
                'type': self.info.type,
                'vid': self.info.vid,
                'pid': self.info.pid,
                'image': self.info.image,
                'protocol': 'barracuda',
                'zones': {},
                'matrix': False,
                'matrix_dimensions': None,
                'dedicated_macro_keys': False,
                'dpi': False, 'dpi_stages': False, 'max_dpi': None,
                'poll_rate': False, 'supported_poll_rates': [],
                'battery': True, 'idle_time': True,
                'idle_time_max': max(IDLE_SECONDS),
                #: Not any length of time up to that: only these.
                'idle_time_choices': list(IDLE_SECONDS),
                'game_mode': False, 'macro_mode': False, 'keyboard_layout': False,
                'keyswitch_optimization': False,
                'scroll_mode': False, 'custom_frame': False, 'software_effects': False,
                'readback': True,
                'audio': {
                    'sidetone_max': 0,
                    'equalizer_presets': list(protocol.EQ_PRESETS),
                    'equalizer_frequencies': list(protocol.EQ_FREQUENCIES),
                    'equalizer_range': [protocol.EQ_MIN, protocol.EQ_MAX],
                    'dongle_led_modes': [],
                    #: It cannot be switched off; its curve is the custom
                    #: preset's, as on the BlackShark V2.
                    'equalizer_switch': False,
                    #: Steps of the headset's own, not decibels.
                    'equalizer_unit': '',
                },
                'transport': {
                    'path': self.channel.info.path,
                    'interface': self.channel.info.interface,
                    'wait_us': 0,
                },
            }
        return self._capabilities

    def has_effect(self, effect: str, zone: str = 'backlight') -> bool:
        return False

    def available_effects(self, zone: str = 'backlight') -> list:
        return []

    def supports(self, attribute: str) -> bool:
        return False

    def run(self, attribute: str, buf: bytes = b'', require_response: bool = False,
            **variables):
        raise NotSupported('{0} has no sysfs recipes'.format(self.name))

    # -- sending -----------------------------------------------------------
    def _request(self, family: int, payload: Sequence[int], accept: Callable):
        """Send one request; what `accept` made of the answer, ``None`` for silence.

        `accept` is given the sequence number the request went out under, and
        each frame that comes back.
        """
        self._sequence = protocol.next_sequence(self._sequence)
        sequence = self._sequence
        report = protocol.request(family, sequence, payload)
        try:
            return self.channel.exchange(report, lambda frame: accept(sequence, frame))
        except (HidError, OSError) as error:
            raise DeviceError('{0}: {1}'.format(self.name, error)) from error
        except protocol.ProtocolError as error:
            raise DeviceError('{0}: {1}'.format(self.name, error)) from error

    def _ask(self, command: int) -> int:
        """Ask the receiver for one of its own values.

        The answer is numbered by the receiver and could as well be an old one;
        only the acknowledgment says which request it is for, so the answer is
        the first one after that.
        """
        acknowledged = []

        def accept(sequence: int, frame: bytes):
            if not acknowledged:
                if protocol.ack(frame, Family.LINK, sequence) is not None:
                    acknowledged.append(True)
                return None
            return protocol.link_reply(frame, command)

        value = self._request(Family.LINK, (command,), accept)
        if value is None:
            raise DeviceError('{0} did not answer'.format(self.name))
        return value

    def _set_route(self, route: int) -> None:
        """Tell the receiver where requests for the headset go, and see that it has."""
        done = self._request(
            Family.LINK, (Link.SET_ROUTE, route),
            lambda sequence, frame: protocol.ack(frame, Family.LINK, sequence))
        if done is None:
            raise DeviceError('{0} did not answer'.format(self.name))
        if self._ask(Link.GET_ROUTE) != route:
            raise DeviceError('{0}: the receiver did not change its route'.format(self.name))

    def _read_max_length(self) -> None:
        """What Razer's software says before it asks for settings.

        Asked for longer reads, a receiver tried on Windows answered with a
        length of 0 where the one the reference project saw answered 240, and
        at times not at all (issue #21); the battery, asked without it, answers
        either way.  So the answer is noted and nothing more, and a receiver
        that once said nothing is not asked again: each time would cost the
        whole wait.
        """
        if self._read_length_heard is False:
            return
        try:
            result = self._request(
                Family.DIAG, protocol.READ_MAX_LEN,
                lambda sequence, frame: protocol.ack(frame, Family.DIAG, sequence))
        except DeviceError as error:
            self.channel_note('read length refused: {0}'.format(error))
            return
        if result is None:
            self._read_length_heard = False
            self.channel_note('read length: no answer; not asked again')
            return
        self._read_length_heard = True
        if result != protocol.READ_MAX_LEN_RESULT:
            self.channel_note('read length answered {0}'.format(result.hex(' ') or 'nothing'))

    def channel_note(self, text: str) -> None:
        note = getattr(self.channel, 'note', None)
        if note is not None:
            note(text)
        logger.debug('%s: %s', self.name, text)

    def get_trace(self) -> list:
        """The last requests and answers, oldest first, for a report."""
        return list(getattr(self.channel, 'trace', ()))

    def _off(self) -> HeadsetOff:
        self._quiet_until = time.monotonic() + QUIET_SECONDS
        return HeadsetOff('{0} is switched off'.format(self.name))

    def _reach(self) -> None:
        """Fail at once for a headset just found to be off."""
        if time.monotonic() < self._quiet_until:
            raise HeadsetOff('{0} is switched off'.format(self.name))

    def _with_headset(self, work: Callable, settings: bool = True):
        """Do `work` with the receiver passing requests on to the headset.

        The whole of it under one lock: a second caller's requests in the
        middle would go wherever the first had left the route.  The route is
        always put back, whatever `work` did, and if it will not go back
        nothing more is passed on until the receiver says it has.  `settings`
        is for everything but the battery and the cable, which Razer's software
        asks for without the preliminary that the rest needs.
        """
        with self._lock:
            self._reach()
            if self._ask(Link.GET_LINK) != 1:
                raise self._off()
            if self._route_failed:
                # Left passing things on last time.  Nothing but the receiver
                # itself saying otherwise makes it safe to go on.
                if self._ask(Link.GET_ROUTE) != protocol.ROUTE_LOCAL:
                    raise DeviceError('{0}: the receiver is still set to pass requests '
                                      'on; unplug it and plug it in again'.format(self.name))
                self._route_failed = False
            if not self._ask(Link.GET_TRANSPORT) & protocol.TRANSPORT_HEADSET:
                raise DeviceError('{0}: the receiver cannot pass requests on to the '
                                  'headset yet'.format(self.name))
            if self._ask(Link.GET_ROUTE) != protocol.ROUTE_LOCAL:
                # Left that way by whoever asked last and did not finish: a
                # daemon that was stopped half way, say.  The receiver keeps it
                # until it is unplugged, so refusing would shut the headset
                # out for as long; it is put back instead, once.
                logger.info('%s: its receiver was left passing requests on; '
                            'putting it back', self.name)
                if not self._restore_route():
                    raise DeviceError('{0}: another program is talking to the headset '
                                      'through its receiver'.format(self.name))
            try:
                self._set_route(protocol.ROUTE_REMOTE)
                if settings:
                    self._read_max_length()
                result = work()
            except BaseException:
                self._restore_route()
                raise
            if not self._restore_route():
                raise DeviceError('{0}: the receiver would not go back to answering '
                                  'for itself'.format(self.name))
            return result

    def _restore_route(self) -> bool:
        """Have the receiver answer for itself again; twice if it takes that."""
        for _attempt in range(2):
            try:
                self._set_route(protocol.ROUTE_LOCAL)
            except DeviceError as error:
                logger.debug('%s: restoring the route failed: %s', self.name, error)
                continue
            self._route_failed = False
            return True
        self._route_failed = True
        logger.warning('%s: could not set the receiver back to answering for itself',
                       self.name)
        return False

    def _get(self, register: int) -> bytes:
        """Read a register.  Only inside :meth:`_with_headset`."""
        value = self._request(
            Family.CUSTOMER, protocol.get(register),
            lambda _sequence, frame: protocol.register_reply(frame, register))
        if value is None:
            raise DeviceError('{0} did not answer when asked for register 0x{1:02x}'.format(
                self.name, register))
        return value

    def _byte(self, register: int, what: str, allowed) -> int:
        value = self._get(register)
        if len(value) != 1 or value[0] not in allowed:
            raise DeviceError('{0}: unexpected {1} {2}'.format(self.name, what, value.hex()))
        return value[0]

    def _put(self, register: int, value: Sequence[int], what: str) -> None:
        """Write a register and see that it took.  Only inside :meth:`_with_headset`.

        The headset says a write went well when it did not always: one that is
        not there to be read back afterwards did not happen.
        """
        value = bytes(value)
        answer = self._request(
            Family.CUSTOMER, protocol.put(register, value),
            lambda _sequence, frame: protocol.register_reply(frame, register | protocol.WRITE))
        if answer is None:
            raise DeviceError('{0} did not answer when register 0x{1:02x} was set'.format(
                self.name, register))
        if answer != b'\x00':
            raise DeviceError('{0} refused the {1}'.format(self.name, what))
        if self._get(register) != value:
            raise DeviceError('{0} did not keep the {1}'.format(self.name, what))

    def is_headset_connected(self) -> bool:
        """Whether the headset is on and linked to its receiver.

        The receiver says, and does so without the headset being asked a thing.
        """
        with self._lock:
            if time.monotonic() < self._quiet_until:
                return False
            try:
                linked = self._ask(Link.GET_LINK) == 1
            except DeviceError:
                return False
            if not linked:
                self._off()
            return linked

    # -- power -------------------------------------------------------------
    def get_battery_level(self) -> int:
        return self._with_headset(lambda: self._byte(
            Register.BATTERY, 'battery level', range(protocol.BATTERY_MAX + 1)),
            settings=False)

    def is_charging(self) -> bool:
        """Whether its cable is in, which is all it says: not when it is full."""
        return bool(self._with_headset(lambda: self._byte(
            Register.CHARGING, 'charging state', (0, 1)), settings=False))

    def get_idle_time(self) -> int:
        """Seconds before it switches itself off; 0 means it never does."""
        return 60 * self._with_headset(lambda: self._byte(
            Register.IDLE_MINUTES, 'sleep timer', range(256)))

    def set_idle_time(self, seconds: int) -> None:
        seconds = int(seconds)
        if seconds not in IDLE_SECONDS:
            raise DeviceError('the sleep timer can be {0} or {1} minutes, or 0 for '
                              'never'.format(
                                  ', '.join(str(m) for m in protocol.IDLE_MINUTES[1:-1]),
                                  protocol.IDLE_MINUTES[-1]))
        self._with_headset(lambda: self._put(
            Register.IDLE_MINUTES, (seconds // 60,), 'sleep timer'))

    # -- sound -------------------------------------------------------------
    def get_audio(self) -> dict:
        """All the sound settings at once, and whether the headset is there."""
        def read() -> dict:
            return {
                'connected': True,
                # Always on; said so that what shows a preset does not show "off".
                'equalizer': True,
                'equalizer_preset': self._preset(),
                'equalizer_bands': self._bands(),
                'do_not_disturb': bool(self._byte(
                    Register.DO_NOT_DISTURB, 'do-not-disturb state', (0, 1))),
            }
        try:
            return self._with_headset(read)
        except HeadsetOff:
            return {'connected': False}

    def _preset(self) -> str:
        value = self._byte(Register.EQ_PRESET, 'equalizer preset',
                           protocol.EQ_PRESETS.values())
        return next(name for name, code in protocol.EQ_PRESETS.items() if code == value)

    def _bands(self) -> list:
        try:
            return protocol.decode_bands(self._get(Register.EQ_BANDS))
        except protocol.ProtocolError as error:
            raise DeviceError('{0}: {1}'.format(self.name, error)) from error

    def get_equalizer_preset(self) -> str:
        return self._with_headset(self._preset)

    def set_equalizer_preset(self, preset: str) -> None:
        if preset not in protocol.EQ_PRESETS:
            raise DeviceError('no such preset: {0}; there is {1}'.format(
                preset, ', '.join(protocol.EQ_PRESETS)))
        self._with_headset(lambda: self._put(
            Register.EQ_PRESET, (protocol.EQ_PRESETS[preset],), 'preset'))

    def set_equalizer(self, enabled: bool) -> None:
        if not enabled:
            raise NotSupported("{0}'s equalizer cannot be switched off; its flat "
                               'preset is the plain one'.format(self.name))

    def get_equalizer_bands(self) -> list:
        """The custom curve, ten bands of -5 to 5."""
        return self._with_headset(self._bands)

    def set_equalizer_bands(self, gains: Sequence[int]) -> None:
        """Set the custom curve, ten bands of -5 to 5, and switch to it."""
        try:
            bands = protocol.encode_bands(gains)
        except ValueError as error:
            raise DeviceError(str(error)) from error

        def write() -> None:
            self._put(Register.EQ_BANDS, bands, 'equalizer curve')
            self._put(Register.EQ_PRESET, (protocol.EQ_PRESETS['custom'],), 'preset')
        self._with_headset(write)

    def set_do_not_disturb(self, enabled: bool) -> None:
        self._with_headset(lambda: self._put(
            Register.DO_NOT_DISTURB, (int(bool(enabled)),), 'do-not-disturb mode'))

    # -- what the BlackSharks have and this one does not -------------------
    def _lacks(self, what: str) -> NotSupported:
        return NotSupported('{0} has no {1}'.format(self.name, what))

    def get_microphone_muted(self) -> bool:
        raise self._lacks('way to say whether its microphone is muted')

    def set_sidetone(self, enabled: bool) -> None:
        raise self._lacks('sidetone that can be set from here')

    def set_sidetone_level(self, level: int) -> None:
        raise self._lacks('sidetone that can be set from here')

    def set_dongle_led(self, mode: str) -> None:
        raise self._lacks('light on its receiver to set')

    def set_microphone_noise_cancelling(self, enabled: bool) -> None:
        raise self._lacks('noise cancelling to switch')

    def set_power_saving(self, enabled: bool) -> None:
        raise self._lacks('power saving to switch')

    # -- what the daemon asks of every device ------------------------------
    def enter_driver_mode(self, keys_handled: bool = False) -> None:
        """It has no driver mode."""

    def restore(self) -> None:
        """Its settings live in the headset, so there is nothing to put back."""

    def close(self) -> None:
        self.channel.close()
