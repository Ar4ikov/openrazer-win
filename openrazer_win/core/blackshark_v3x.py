"""The BlackShark V3 X, a headset with sound settings and no lights.

Its own class beside :class:`~openrazer_win.core.blackshark.BlackSharkDevice`
rather than a kind of it: the two share a name and what they offer, and nothing
of how they are asked.  The V3 X has fewer switches -- sidetone is a level with
no on and off, the equalizer is always on and has no custom preset -- and two
settings the V2 HyperSpeed lacks, for the microphone's noise cancelling and for
saving power on the radio.

Everything set here is stored in the headset and works without the PC, so
nothing is written to it when it is found.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Sequence

from ..devices.database import DeviceInfo
from ..devices.recipes import NotSupported
from ..hid.base import HidError
from ..protocol import blackshark_v3x as protocol
from ..protocol.blackshark_v3x import Class, Command
from .device import DeviceError
from .persistence import Persistence
from .transport import FeatureChannel

logger = logging.getLogger(__name__)

#: How long a headset that was not there is taken to be off.  Every setting read
#: while it is would otherwise wait out its attempts, and Studio reads several
#: at a time.
QUIET_SECONDS = 2.0

#: The level sidetone comes on at when the headset was never seen with one.
DEFAULT_SIDETONE = 8

#: As far as the sleep timer is offered.  It is kept in seconds and would take
#: eighteen hours, which is no use on a slider.
IDLE_SECONDS_MAX = 60 * 60


class BlackSharkV3XDevice:
    """A BlackShark V3 X, on its receiver or on its cable."""

    def __init__(self, info: DeviceInfo, channel: FeatureChannel,
                 persistence: Optional[Persistence] = None):
        self.info = info
        self.channel = channel
        self.persistence = persistence or Persistence()
        self.wired = info.pid == protocol.PID_WIRED
        self._lock = threading.RLock()
        self._transaction = 0
        self._serial: Optional[str] = None
        self._firmware: Optional[str] = None
        self._capabilities: Optional[dict] = None
        #: Until when not to bother a headset that was not there.
        self._quiet_until = 0.0
        #: The level to go back to when sidetone is switched on again.
        self._sidetone_level = DEFAULT_SIDETONE

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
        return '<BlackSharkV3XDevice {0} {1:04x}:{2:04x}>'.format(
            self.name, self.vid, self.pid)

    @property
    def serial(self) -> str:
        """The headset's serial on the cable, and a fixed name for the receiver.

        The receiver has no serial of its own: it passes the headset's on, and
        answers with zeroes while the headset is off, which is how the daemon
        usually finds it at logon.  A name that depended on that would change
        from one start to the next, so the receiver always goes by the same.
        The product id is on the end of both, as the cable and the receiver
        can be plugged in together and would otherwise be one device twice.
        """
        if self._serial is None:
            text = ''
            if self.wired:
                try:
                    raw = self._exchange(Class.INFO, Command.SERIAL | protocol.READ,
                                         protocol.QUERY, at_least=1)
                    text = raw[:protocol.SERIAL_LENGTH].split(b'\x00')[0].decode('ascii', 'ignore')
                    text = ''.join(c for c in text if c.isalnum())
                except (DeviceError, NotSupported):
                    text = ''
            self._serial = '{0}{1:04X}'.format(text or 'BSV3X', self.pid)
        return self._serial

    @property
    def firmware_version(self) -> str:
        if self._firmware is None:
            raw = self._exchange(Class.INFO, Command.FIRMWARE | protocol.READ,
                                 protocol.QUERY, at_least=3)
            if not any(raw[:3]):
                # What the receiver says before the headset has told it.
                raise DeviceError('{0}: firmware version not known yet'.format(self.name))
            # Three numbers where other devices give two: the third is the one
            # that changes from one release to the next.
            self._firmware = 'v{0}.{1}.{2}'.format(*raw[:3])
        return self._firmware

    # -- capabilities ------------------------------------------------------
    def capabilities(self) -> dict:
        if self._capabilities is None:
            self._capabilities = {
                'name': self.info.name,
                'type': self.info.type,
                'vid': self.info.vid,
                'pid': self.info.pid,
                'image': self.info.image,
                'protocol': 'blackshark_v3x',
                'zones': {},
                'matrix': False,
                'matrix_dimensions': None,
                'dedicated_macro_keys': False,
                'dpi': False, 'dpi_stages': False, 'max_dpi': None,
                'poll_rate': False, 'supported_poll_rates': [],
                'battery': True, 'idle_time': True,
                'idle_time_max': IDLE_SECONDS_MAX,
                'game_mode': False, 'macro_mode': False, 'keyboard_layout': False,
                'keyswitch_optimization': False,
                'scroll_mode': False, 'custom_frame': False, 'software_effects': False,
                'readback': True,
                'audio': {
                    'sidetone_max': protocol.SIDETONE_MAX,
                    'equalizer_presets': list(protocol.EQ_PRESETS),
                    'equalizer_frequencies': list(protocol.EQ_FREQUENCIES),
                    'equalizer_range': [protocol.EQ_MIN_DB, protocol.EQ_MAX_DB],
                    'dongle_led_modes': [],
                    #: It cannot be switched off, and the curve to edit is the
                    #: chosen preset's own rather than a custom preset's.
                    'equalizer_switch': False,
                    'equalizer_curve_per_preset': True,
                },
                'transport': {
                    'path': self.channel.info.path,
                    'interface': self.channel.info.interface,
                    'wait_us': int(self.channel.wait * 1000000),
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
    def _exchange(self, command_class: int, command: int,
                  arguments: Sequence[int] = (), at_least: int = 0) -> bytes:
        """Send a command and return its reply's arguments, `at_least` that many.

        A reply read before it is ready is the one before it, or short; the
        command is then sent again from the start, as the driver does.
        """
        with self._lock:
            for _attempt in range(protocol.ATTEMPTS):
                self._transaction = (self._transaction + 1) & 0xFF
                report = protocol.request(self._transaction, command_class, command, arguments)
                try:
                    reply = self.channel.exchange(report)
                except (HidError, OSError) as error:
                    raise DeviceError('{0}: {1}'.format(self.name, error)) from error
                if protocol.answers(reply, report, at_least):
                    break
                time.sleep(protocol.WAIT_SECONDS)
            else:
                if command_class != Class.INFO and not self.wired:
                    # The receiver has nothing to say for a headset out of reach.
                    raise self._off()
                raise DeviceError('{0} did not answer'.format(self.name))
        try:
            return protocol.result(reply)
        except protocol.Unsupported as error:
            raise NotSupported('{0}: {1}'.format(self.name, error)) from error
        except protocol.ProtocolError as error:
            raise DeviceError('{0}: {1}'.format(self.name, error)) from error

    def _off(self) -> DeviceError:
        self._quiet_until = time.monotonic() + QUIET_SECONDS
        return DeviceError('{0} is switched off'.format(self.name))

    def _strange(self, what: str, value: int) -> DeviceError:
        """An answer outside what the setting can hold.

        A receiver with no headset in reach fills its replies with 0xFF, so on
        the receiver that is the headset being off.
        """
        if not self.wired:
            return self._off()
        return DeviceError('{0}: unexpected {1} {2}'.format(self.name, what, value))

    def _reach(self) -> None:
        """Fail at once for a headset just found to be off."""
        if not self.wired and time.monotonic() < self._quiet_until:
            raise DeviceError('{0} is switched off'.format(self.name))

    def _read(self, command_class: int, command: int, index: int) -> int:
        self._reach()
        answer = self._exchange(command_class, command | protocol.READ, protocol.QUERY,
                                at_least=index + 1)
        return answer[index]

    def _write(self, command_class: int, command: int, arguments: Sequence[int]) -> None:
        with self._lock:
            if not self.wired:
                # The receiver takes a setting for a headset that is off, says
                # it went well and even reads it back; the headset never gets it.
                self.get_battery_level()
            self._exchange(command_class, command, arguments)
            # Under the lock, so that nothing reads it back before it is there.
            time.sleep(protocol.SETTLE_SECONDS)

    def is_headset_connected(self) -> bool:
        """Whether the headset is on and in touch with its receiver."""
        if self.wired:
            return True
        try:
            self.get_battery_level()
        except DeviceError:
            return False
        return True

    # -- power -------------------------------------------------------------
    def get_battery_level(self) -> int:
        level = self._read(Class.POWER, Command.BATTERY, 1)
        if level > protocol.BATTERY_MAX:
            raise self._strange('battery level', level)
        return level

    def is_charging(self) -> bool:
        state = self._read(Class.POWER, Command.CHARGING, 1)
        if state > 1:
            raise self._strange('charging state', state)
        return bool(state)

    def get_idle_time(self) -> int:
        """Seconds before it switches itself off."""
        self._reach()
        answer = self._exchange(Class.POWER, Command.SLEEP | protocol.READ, protocol.QUERY,
                                at_least=2)
        seconds = (answer[0] << 8) | answer[1]
        if seconds == protocol.NOT_LINKED:
            raise self._strange('sleep timer', seconds)
        return seconds

    def set_idle_time(self, seconds: int) -> None:
        seconds = int(seconds)
        if not 0 <= seconds < protocol.NOT_LINKED:
            raise DeviceError('the sleep timer goes up to {0} seconds'.format(
                protocol.NOT_LINKED - 1))
        self._write(Class.POWER, Command.SLEEP, (seconds >> 8, seconds & 0xFF))

    def get_power_saving(self) -> bool:
        return self._flag(Class.POWER, Command.POWER_SAVING, 'power saving')

    def set_power_saving(self, enabled: bool) -> None:
        """The headset's own power saving on the radio."""
        self._write(Class.POWER, Command.POWER_SAVING, (0x00, int(bool(enabled))))

    def _flag(self, command_class: int, command: int, what: str) -> bool:
        state = self._read(command_class, command, 1)
        if state > 1:
            raise self._strange(what, state)
        return bool(state)

    # -- sound -------------------------------------------------------------
    def get_audio(self) -> dict:
        """All the sound settings at once, and whether the headset is there."""
        audio: dict = {'connected': False}
        if not self.is_headset_connected():
            return audio
        level = self.get_sidetone_level()
        audio.update({
            'connected': True,
            'sidetone': level > 0,
            'sidetone_level': level,
            'equalizer': True,
            'equalizer_preset': self.get_equalizer_preset(),
            'equalizer_bands': self.get_equalizer_bands(),
            'microphone_noise_cancelling': self.get_microphone_noise_cancelling(),
            'power_saving': self.get_power_saving(),
        })
        return audio

    def get_sidetone_level(self) -> int:
        level = self._read(Class.AUDIO, Command.SIDETONE, 2)
        if level > protocol.SIDETONE_MAX:
            raise self._strange('sidetone level', level)
        if level:
            self._sidetone_level = level
        return level

    def set_sidetone_level(self, level: int) -> None:
        level = int(level)
        if not 0 <= level <= protocol.SIDETONE_MAX:
            raise DeviceError('sidetone goes from 0 to {0}'.format(protocol.SIDETONE_MAX))
        self._write(Class.AUDIO, Command.SIDETONE, (0x00, 0x03, level))
        if level:
            self._sidetone_level = level

    def set_sidetone(self, enabled: bool) -> None:
        """It has a level and no switch: off is level 0, on the level it last had."""
        with self._lock:
            if bool(self.get_sidetone_level()) != bool(enabled):
                self.set_sidetone_level(self._sidetone_level if enabled else 0)

    def get_equalizer_preset(self) -> str:
        value = self._read(Class.AUDIO, Command.EQ_PRESET, 3)
        name = next((name for name, code in protocol.EQ_PRESETS.items() if code == value), None)
        if name is None:
            raise self._strange('equalizer preset', value)
        return name

    def set_equalizer_preset(self, preset: str) -> None:
        if preset not in protocol.EQ_PRESETS:
            raise DeviceError('no such preset: {0}; there is {1}'.format(
                preset, ', '.join(protocol.EQ_PRESETS)))
        self._write(Class.AUDIO, Command.EQ_PRESET,
                    (0x00, 0x01, 0x00, protocol.EQ_PRESETS[preset]))

    def set_equalizer(self, enabled: bool) -> None:
        if not enabled:
            raise NotSupported("{0}'s equalizer cannot be switched off; its default "
                               'preset is the plain one'.format(self.name))

    def get_equalizer_bands(self) -> list:
        """The curve of the preset chosen, ten gains in dB."""
        self._reach()
        answer = self._exchange(Class.AUDIO, Command.EQ_BANDS | protocol.READ, protocol.QUERY,
                                at_least=2 + len(protocol.EQ_FREQUENCIES))
        try:
            return protocol.decode_bands(answer[2:2 + len(protocol.EQ_FREQUENCIES)])
        except protocol.ProtocolError as error:
            raise self._strange('equalizer curve', answer[2]) from error

    def set_equalizer_bands(self, gains: Sequence[int]) -> None:
        """Change the curve of the preset chosen: each preset keeps its own."""
        try:
            bands = protocol.encode_bands(gains)
        except ValueError as error:
            raise DeviceError(str(error)) from error
        self._write(Class.AUDIO, Command.EQ_BANDS, bytes((0x00, 0x01)) + bands)

    def get_microphone_noise_cancelling(self) -> bool:
        return self._flag(Class.AUDIO, Command.NOISE_CANCELLING, 'noise cancelling')

    def set_microphone_noise_cancelling(self, enabled: bool) -> None:
        self._write(Class.AUDIO, Command.NOISE_CANCELLING, (0x00, int(bool(enabled))))

    # -- what the other BlackShark has and this one does not ---------------
    def _lacks(self, what: str) -> NotSupported:
        return NotSupported('{0} has no {1}'.format(self.name, what))

    def get_microphone_muted(self) -> bool:
        raise self._lacks('way to say whether its microphone is muted')

    def set_do_not_disturb(self, enabled: bool) -> None:
        raise self._lacks('do-not-disturb mode')

    def set_dongle_led(self, mode: str) -> None:
        raise self._lacks('light on its receiver to set')

    # -- what the daemon asks of every device ------------------------------
    def enter_driver_mode(self, keys_handled: bool = False) -> None:
        """It has no driver mode."""

    def restore(self) -> None:
        """Its settings live in the headset, so there is nothing to put back."""

    def close(self) -> None:
        self.channel.close()
