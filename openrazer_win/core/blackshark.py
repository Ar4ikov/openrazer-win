"""The BlackShark V2 HyperSpeed, a headset with sound settings and no lights.

Deliberately not a :class:`~openrazer_win.core.device.RazerDevice`, for the same
reason as the Bluetooth headset: there is no control report, no recipe table
and nothing to light, so inheriting would mean overriding nearly everything
with "not supported".

Everything set here is stored in the headset and works without the PC, so
nothing is written to it when it is found.  What Razer Synapse adds on top --
THX Spatial Audio, the microphone's noise gate and equalizer -- runs on the PC
and is not part of the device at all.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Sequence

from ..devices.database import DeviceInfo
from ..devices.recipes import NotSupported
from ..hid.base import HidError
from ..protocol import blackshark as protocol
from ..protocol.blackshark import Command
from .device import DeviceError
from .persistence import Persistence
from .transport import ReportChannel

logger = logging.getLogger(__name__)

#: How long a headset that did not answer is taken to be off.  Every setting read
#: while it is would otherwise wait out the timeout, and Studio reads ten at a time.
QUIET_SECONDS = 2.0

#: The firmware applies a new curve only if told again, a moment after it.
EQ_LATCH_SECONDS = 0.040


class BlackSharkDevice:
    """A BlackShark V2 HyperSpeed, on its receiver or on its cable."""

    def __init__(self, info: DeviceInfo, channel: ReportChannel,
                 persistence: Optional[Persistence] = None):
        self.info = info
        self.channel = channel
        self.persistence = persistence or Persistence()
        self.wired = info.pid == protocol.PID_WIRED
        self._lock = threading.RLock()
        self._sequence = 0
        self._serial: Optional[str] = None
        self._firmware: Optional[str] = None
        self._capabilities: Optional[dict] = None
        #: Until when not to bother a headset that did not answer.
        self._quiet_until = 0.0

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
        return '<BlackSharkDevice {0} {1:04x}:{2:04x}>'.format(
            self.name, self.vid, self.pid)

    @property
    def serial(self) -> str:
        """The receiver's serial, which is there with the headset off."""
        if self._serial is None:
            try:
                raw = self._exchange(protocol.DOMAIN_LOCAL, Command.SERIAL)
                text = raw.split(b'\x00')[0].decode('ascii', 'ignore').strip()
            except (DeviceError, NotSupported):
                text = ''
            self._serial = text or 'RZ{0:04X}{1:04X}'.format(self.vid, self.pid)
        return self._serial

    @property
    def firmware_version(self) -> str:
        if self._firmware is None:
            raw = self._exchange(protocol.DOMAIN_LOCAL, Command.FIRMWARE)
            if len(raw) < 4:
                raise DeviceError('{0}: short firmware reply'.format(self.name))
            self._firmware = 'v{0}.{1}.{2}.{3}'.format(*raw[:4])
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
                'protocol': 'blackshark',
                'zones': {},
                'matrix': False,
                'matrix_dimensions': None,
                'dedicated_macro_keys': False,
                'dpi': False, 'dpi_stages': False, 'max_dpi': None,
                'poll_rate': False, 'supported_poll_rates': [],
                'battery': True, 'idle_time': True,
                'idle_time_max': protocol.IDLE_MINUTES_MAX * 60,
                'game_mode': False, 'macro_mode': False, 'keyboard_layout': False,
                'keyswitch_optimization': False,
                'scroll_mode': False, 'custom_frame': False, 'software_effects': False,
                'readback': True,
                'audio': {
                    'sidetone_max': protocol.SIDETONE_MAX,
                    'equalizer_presets': list(protocol.EQ_PRESETS),
                    'equalizer_frequencies': list(protocol.EQ_FREQUENCIES),
                    'equalizer_range': [protocol.EQ_MIN_DB, protocol.EQ_MAX_DB],
                    'dongle_led_modes': ([] if self.wired
                                         else list(protocol.DONGLE_LED_MODES)),
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
    def _exchange(self, domain: int, command: int, payload: Sequence[int] = ()) -> bytes:
        with self._lock:
            self._sequence = (self._sequence + 1) & protocol.SEQUENCE_MASK
            sequence = self._sequence
            report = protocol.request(sequence, domain, command, payload)
            try:
                reply = self.channel.exchange(report, lambda answer: protocol.answers(
                    answer, sequence, domain, command, self.wired))
            except (HidError, OSError) as error:
                raise DeviceError('{0}: {1}'.format(self.name, error)) from error
        if reply is None:
            if domain == protocol.DOMAIN_HEADSET:
                self._quiet_until = time.monotonic() + QUIET_SECONDS
                raise DeviceError('{0} is switched off'.format(self.name))
            raise DeviceError('{0} did not answer'.format(self.name))
        try:
            return protocol.result(reply, command)
        except protocol.Unsupported as error:
            raise NotSupported('{0}: {1}'.format(self.name, error)) from error
        except protocol.ProtocolError as error:
            raise DeviceError('{0}: {1}'.format(self.name, error)) from error

    def is_headset_connected(self) -> bool:
        """Whether the headset is on and in touch with its receiver.

        By asking it something.  The receiver has a register that says so too,
        but only silence from the headset itself is sure: a register read
        wrongly would lock every setting away from a headset that is on.
        """
        if self.wired:
            return True
        try:
            self._read(Command.BATTERY)
        except DeviceError:
            # Anything but silence, such as being busy, is still an answer.
            return time.monotonic() >= self._quiet_until
        return True

    def _headset(self) -> int:
        """The domain the headset answers on, if it is there to answer."""
        if self.wired:
            return protocol.DOMAIN_LOCAL
        if time.monotonic() < self._quiet_until:
            raise DeviceError('{0} is switched off'.format(self.name))
        return protocol.DOMAIN_HEADSET

    def _read(self, command: int, domain: Optional[int] = None) -> int:
        answer = self._exchange(self._headset() if domain is None else domain, command)
        if not answer:
            raise DeviceError('{0}: empty reply to 0x{1:02x}'.format(self.name, command))
        return answer[0]

    def _write(self, command: int, value: int) -> None:
        self._exchange(self._headset(), command | protocol.WRITE, (int(value),))

    # -- power -------------------------------------------------------------
    def get_battery_level(self) -> int:
        return min(self._read(Command.BATTERY), 100)

    def is_charging(self) -> bool:
        return bool(self._read(Command.CHARGING))

    def get_idle_time(self) -> int:
        """Seconds before it switches itself off; 0 means it never does."""
        return self._read(Command.IDLE_MINUTES) * 60

    def set_idle_time(self, seconds: int) -> None:
        minutes = int(round(int(seconds) / 60.0))
        if not 0 <= minutes <= protocol.IDLE_MINUTES_MAX:
            raise DeviceError('the sleep timer goes up to {0} minutes'.format(
                protocol.IDLE_MINUTES_MAX))
        self._write(Command.IDLE_MINUTES, minutes)

    # -- sound -------------------------------------------------------------
    def get_audio(self) -> dict:
        """All the sound settings at once, and whether the headset is there."""
        audio: dict = {'connected': False}
        if not self.wired:
            audio['dongle_led'] = self._name_of(
                protocol.DONGLE_LED_MODES,
                self._read(Command.DONGLE_LED, protocol.DOMAIN_LOCAL))
        if not self.is_headset_connected():
            return audio
        audio.update({
            'connected': True,
            'microphone_muted': bool(self._read(Command.MIC_MUTED)),
            'sidetone': bool(self._read(Command.SIDETONE)),
            'sidetone_level': self._read(Command.SIDETONE_LEVEL),
            'equalizer': bool(self._read(Command.EQ_ENABLED)),
            'equalizer_preset': self._name_of(protocol.EQ_PRESETS,
                                              self._read(Command.EQ_PRESET)),
            'equalizer_bands': self._bands(),
            'do_not_disturb': bool(self._read(Command.DO_NOT_DISTURB)),
        })
        return audio

    def _bands(self) -> list:
        try:
            return protocol.decode_bands(self._exchange(self._headset(), Command.EQ_BANDS))
        except protocol.ProtocolError as error:
            raise DeviceError('{0}: {1}'.format(self.name, error)) from error

    @staticmethod
    def _name_of(names: dict, value: int) -> Optional[str]:
        return next((name for name, code in names.items() if code == value), None)

    def get_microphone_muted(self) -> bool:
        """Where the headset's own mute button stands."""
        return bool(self._read(Command.MIC_MUTED))

    def set_sidetone(self, enabled: bool) -> None:
        self._write(Command.SIDETONE, bool(enabled))

    def set_sidetone_level(self, level: int) -> None:
        level = int(level)
        if not 0 <= level <= protocol.SIDETONE_MAX:
            raise DeviceError('sidetone goes from 0 to {0}'.format(protocol.SIDETONE_MAX))
        self._write(Command.SIDETONE_LEVEL, level)

    def set_equalizer(self, enabled: bool) -> None:
        self._write(Command.EQ_ENABLED, bool(enabled))

    def set_equalizer_preset(self, preset: str) -> None:
        """Choose a preset, which also turns the equalizer on."""
        if preset not in protocol.EQ_PRESETS:
            raise DeviceError('no such preset: {0}; there is {1}'.format(
                preset, ', '.join(protocol.EQ_PRESETS)))
        with self._lock:
            self._write(Command.EQ_PRESET, protocol.EQ_PRESETS[preset])
            self._write(Command.EQ_ENABLED, True)

    def set_equalizer_bands(self, gains: Sequence[int]) -> None:
        """Set the custom curve, ten gains in dB, and switch to it."""
        try:
            bands = protocol.encode_bands(gains)
        except ValueError as error:
            raise DeviceError(str(error)) from error
        custom = protocol.EQ_PRESETS['custom']
        with self._lock:
            self._write(Command.EQ_ENABLED, True)
            self._write(Command.EQ_PRESET, custom)
            self._exchange(self._headset(), Command.EQ_BANDS | protocol.WRITE, bands)
            time.sleep(EQ_LATCH_SECONDS)
            self._write(Command.EQ_PRESET, custom)

    def set_do_not_disturb(self, enabled: bool) -> None:
        self._write(Command.DO_NOT_DISTURB, bool(enabled))

    def set_dongle_led(self, mode: str) -> None:
        """What the receiver's light shows.  There is no receiver on the cable."""
        if self.wired:
            raise NotSupported('{0} has no receiver'.format(self.name))
        if mode not in protocol.DONGLE_LED_MODES:
            raise DeviceError('no such mode: {0}; there is {1}'.format(
                mode, ', '.join(protocol.DONGLE_LED_MODES)))
        self._exchange(protocol.DOMAIN_LOCAL, Command.DONGLE_LED | protocol.WRITE,
                       (protocol.DONGLE_LED_MODES[mode],))

    # -- what the daemon asks of every device ------------------------------
    def enter_driver_mode(self, keys_handled: bool = False) -> None:
        """It has no driver mode."""

    def restore(self) -> None:
        """Its settings live in the headset, so there is nothing to put back."""

    def close(self) -> None:
        self.channel.close()
