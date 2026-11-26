"""The BlackShark V2 Pro (2023), through its 2.4 GHz receiver (issue #32).

The BlackShark V2 HyperSpeed's settings in another envelope (see
:mod:`..protocol.blackshark_v2pro`), so most of :class:`BlackSharkDevice` holds;
what differs is how a request travels, and what is left out:

* the equalizer -- its presets, the order they must be written in and how the
  firmware switches between preset families are not settled in any source;
* the receiver's light -- every write is acknowledged and none changes it.

Nobody here has the headset; this follows sources that were tested on one.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Sequence

from ..devices.database import DeviceInfo
from ..devices.recipes import NotSupported
from ..hid.base import HidError
from ..protocol import blackshark_v2pro as protocol
from ..protocol.blackshark import Command
from .blackshark import QUIET_SECONDS, BlackSharkDevice
from .device import DeviceError
from .persistence import Persistence
from .transport import ReportChannel

logger = logging.getLogger(__name__)

#: Between the frame that wakes the link and the request, as the sources wait.
WAKE_SECONDS = 0.035
#: How long a reply is waited for, and how many times a request is made.
REPLY_SECONDS = 0.15
ATTEMPTS = 3


class BlackSharkV2ProDevice(BlackSharkDevice):
    """A BlackShark V2 Pro (2023) on its receiver."""

    def __init__(self, info: DeviceInfo, channel: ReportChannel,
                 persistence: Optional[Persistence] = None):
        super().__init__(info, channel, persistence)
        self.wired = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return '<BlackSharkV2ProDevice {0:04x}:{1:04x}>'.format(self.vid, self.pid)

    @property
    def serial(self) -> str:
        # Only the headset knows its serial, and it may be off when the daemon
        # starts; one that changed with that would lose every saved setting.
        return 'RZ{0:04X}{1:04X}'.format(self.vid, self.pid)

    @property
    def firmware_version(self) -> str:
        if self._firmware is None:
            raw = self._exchange(0, Command.FIRMWARE)
            if len(raw) < 2:
                raise DeviceError('{0}: short firmware reply'.format(self.name))
            self._firmware = 'v{0}.{1}'.format(raw[0], raw[1])
        return self._firmware

    def capabilities(self) -> dict:
        if self._capabilities is None:
            caps = dict(super().capabilities())
            caps.update({
                'protocol': 'blackshark-v2pro',
                'idle_time_max': max(protocol.IDLE_MINUTES) * 60,
                'idle_time_min': min(protocol.IDLE_MINUTES) * 60,
                'idle_time_choices': [m * 60 for m in protocol.IDLE_MINUTES],
                'audio': {
                    'sidetone_max': protocol.SIDETONE_MAX,
                    'equalizer_presets': [],
                    'equalizer_frequencies': [],
                    'dongle_led_modes': [],
                },
            })
            self._capabilities = caps
        return self._capabilities

    # -- sending -----------------------------------------------------------
    def _exchange(self, domain: int, command: int, payload: Sequence[int] = ()) -> bytes:
        """Ask the headset, or tell it.  `domain` means nothing here: the
        receiver passes everything on in remote mode."""
        writing = bool(command & protocol.WRITE)
        request = (protocol.write(command, payload) if writing
                   else protocol.read(command))
        with self._lock:
            try:
                for _ in range(ATTEMPTS):
                    self.channel.send(protocol.remote())
                    time.sleep(WAKE_SECONDS)
                    if writing:
                        # No source waits for, or says what is in, a write's reply.
                        self.channel.send(request)
                        return b''
                    answer = self.channel.exchange(
                        request, lambda report: protocol.reply(report, command) is not None,
                        timeout=REPLY_SECONDS)
                    if answer is not None:
                        return protocol.reply(answer, command) or b''
            except (HidError, OSError) as error:
                raise DeviceError('{0}: {1}'.format(self.name, error)) from error
        self._quiet_until = time.monotonic() + QUIET_SECONDS
        raise DeviceError('{0} is switched off'.format(self.name))

    def _headset(self) -> int:
        if time.monotonic() < self._quiet_until:
            raise DeviceError('{0} is switched off'.format(self.name))
        return 0

    # -- power -------------------------------------------------------------
    def set_idle_time(self, seconds: int) -> None:
        """One of Synapse's 15, 30, 45 or 60 minutes, the nearest."""
        minutes = int(round(int(seconds) / 60.0))
        nearest = min(protocol.IDLE_MINUTES, key=lambda choice: abs(choice - minutes))
        self._write(Command.IDLE_MINUTES, nearest)

    # -- sound -------------------------------------------------------------
    def get_audio(self) -> dict:
        if not self.is_headset_connected():
            return {'connected': False}
        return {
            'connected': True,
            'microphone_muted': bool(self._read(Command.MIC_MUTED)),
            'sidetone': bool(self._read(Command.SIDETONE)),
            'sidetone_level': self._read(Command.SIDETONE_LEVEL),
            'do_not_disturb': bool(self._read(Command.DO_NOT_DISTURB)),
        }

    def set_sidetone(self, enabled: bool) -> None:
        self._write(Command.SIDETONE, bool(enabled))
        if enabled:
            level = self._read(Command.SIDETONE_LEVEL)
            self._write(Command.SIDETONE_LEVEL, level or protocol.SIDETONE_DEFAULT)

    def set_sidetone_level(self, level: int) -> None:
        level = int(level)
        if not 0 <= level <= protocol.SIDETONE_MAX:
            raise DeviceError('sidetone goes from 0 to {0}'.format(protocol.SIDETONE_MAX))
        self._write(Command.SIDETONE_LEVEL, level)

    def _no_equalizer(self, *args, **kwargs) -> None:
        raise NotSupported('{0}: its equalizer is not worked out yet'.format(self.name))

    set_equalizer = _no_equalizer
    set_equalizer_preset = _no_equalizer
    set_equalizer_bands = _no_equalizer

    def set_dongle_led(self, mode: str) -> None:
        raise NotSupported('{0}: its receiver light takes no setting'.format(self.name))
