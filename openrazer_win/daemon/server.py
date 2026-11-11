"""The openrazer-win daemon.

Owns the HID handles, serialises access to them, watches for hot-plug, and
serves an explicitly whitelisted RPC surface to local clients.
"""
from __future__ import annotations

import logging
import os
import socket
import socketserver
import sys
import threading
import time
from typing import Any, Callable, Optional

from .._version import __version__
from ..core.device import DeviceError, RazerDevice
from ..core.manager import DeviceManager
from ..core.persistence import Persistence
from ..chroma import PORT as CHROMA_PORT
from ..chroma import ChromaBridge, ChromaServer
from ..core.profiles import (
    ProfileError, ProfileStore, apply_profile, capture_current, default_state, snapshot,
)
from ..devices.recipes import NotSupported, RecipeError
from ..effects.engine import EffectEngine, zone_order
from ..integrations import AccentWatch, IntegrationSettings, read_accent
from . import protocol
from .protocol import (
    DEVICE_ERROR, DEVICE_NOT_FOUND, INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST,
    METHOD_NOT_FOUND, NOT_SUPPORTED, UNAUTHORIZED, Endpoint, RpcError,
)

logger = logging.getLogger(__name__)

#: How often to rescan the HID bus for plug/unplug events.
HOTPLUG_INTERVAL = 3.0

#: How long to wait before trying again to set up a device that was found but
#: would not take its settings.  At logon that is the normal case for a
#: Bluetooth headset -- heard advertising before Windows will hold a link to
#: it -- and giving up after one try left it showing its own colour until the
#: daemon was restarted by hand.
RETRY_DELAYS = (5.0, 15.0, 30.0, 60.0)

#: Device methods clients may invoke.  Anything not listed here is refused --
#: the RPC surface is a deliberate allowlist, not ``getattr`` on the device.
DEVICE_METHODS = {
    # lighting
    'set_none', 'set_on', 'set_static', 'set_spectrum', 'set_wave', 'set_wheel',
    'set_reactive', 'trigger_reactive', 'set_blinking', 'set_custom',
    'set_breath_random', 'set_breath_single', 'set_breath_dual', 'set_breath_triple',
    'set_starlight_random', 'set_starlight_single', 'set_starlight_dual',
    'set_brightness', 'get_brightness', 'set_key_row', 'set_custom_frame',
    'set_zone_colours',
    # mouse
    'get_dpi', 'set_dpi', 'get_dpi_stages', 'set_dpi_stages',
    'get_poll_rate', 'set_poll_rate', 'supported_poll_rates',
    'get_scroll_mode', 'set_scroll_mode', 'get_scroll_acceleration',
    'set_scroll_acceleration', 'get_scroll_smart_reel', 'set_scroll_smart_reel',
    # power
    'get_battery_level', 'is_charging', 'get_idle_time', 'set_idle_time',
    'get_low_battery_threshold', 'set_low_battery_threshold',
    # sound
    'get_audio', 'get_microphone_muted', 'set_sidetone', 'set_sidetone_level',
    'set_equalizer', 'set_equalizer_preset', 'set_equalizer_bands',
    'set_do_not_disturb', 'set_dongle_led',
    'set_microphone_noise_cancelling', 'set_power_saving',
    # keyboard
    'get_game_mode', 'set_game_mode', 'get_macro_mode', 'set_macro_mode',
    'set_macro_effect', 'set_fn_toggle', 'get_keyboard_layout',
    'get_keyswitch_optimization', 'set_keyswitch_optimization',
    # misc
    'get_device_mode', 'set_device_mode', 'restore', 'available_effects',
}

#: Calls that give a zone an effect of the device's own.  On the backlight, a
#: host-drawn effect would go on painting over it -- and come back with the
#: daemon -- so it is stopped and forgotten first.
HARDWARE_EFFECTS = {
    'set_none', 'set_on', 'set_static', 'set_spectrum', 'set_wave', 'set_wheel',
    'set_reactive', 'set_blinking', 'set_breath_random', 'set_breath_single',
    'set_breath_dual', 'set_breath_triple', 'set_starlight_random',
    'set_starlight_single', 'set_starlight_dual', 'set_zone_colours',
}

#: What a zone with an effect of its own can be given by the device's own calls.
ZONE_CALLS = {'set_static': 'static', 'set_none': 'none'}


def _device_summary(device: RazerDevice) -> dict:
    return {
        'serial': device.serial,
        'name': device.name,
        'type': device.type,
        'vid': device.vid,
        'pid': device.pid,
        'image': device.info.image,
        'images': dict(device.info.images),
        'firmware': _safe(device, 'firmware_version'),
    }


def _snaptap_applies(device) -> bool:
    """Whether `device`'s profiles carry Snap Tap.  It acts on the keyboard as a
    whole, so keyboards' only: the test Studio uses to show its Snap Tap panel."""
    return device.capabilities().get('type') in ('keyboard', 'keypad')


def _safe(device: RazerDevice, attribute: str) -> Any:
    try:
        return getattr(device, attribute)
    except Exception:  # noqa: BLE001 - a missing optional field must not 500
        return None


class DaemonService:
    """Everything the RPC layer is allowed to touch."""

    def __init__(self, backend=None, persistence: Optional[Persistence] = None,
                 enable_effects: bool = True,
                 profiles: Optional[ProfileStore] = None,
                 enable_keys: bool = False, keys=None, macros=None,
                 integrations: Optional[IntegrationSettings] = None,
                 chroma_port: int = CHROMA_PORT, snaptap=None):
        self.persistence = persistence or Persistence()
        self.profiles = profiles if profiles is not None else ProfileStore()
        self.manager = DeviceManager(backend=backend, persistence=self.persistence)
        self.effects = (EffectEngine(self.manager, persistence=self.persistence)
                        if enable_effects else None)
        from ..keys import KeyService, MacroStore
        from ..keys.snaptap import SnapTapSettings
        from ..keys.snaptap_host import SnapTapHost
        #: Bindings can be edited whether or not keys are being handled.
        self.macros = macros if macros is not None else MacroStore()
        #: Off unless asked for: it installs a system-wide keyboard hook.
        self.keys = keys
        if self.keys is None and enable_keys:
            self.keys = KeyService(self.manager.backend, macros=self.macros)
        #: Off until asked for, and it rides on the hook above when there is one.
        self.snaptap = snaptap if snaptap is not None else SnapTapHost(
            SnapTapSettings(os.path.join(os.path.dirname(self.persistence.path), 'snaptap.json')),
            keys=self.keys)
        self.started_at = time.time()
        self._lock = threading.RLock()
        #: ``starting`` until the first scan and set-up have finished; the
        #: daemon answers requests long before that.
        self.state = 'starting'
        #: True until the first poll has swept the radio, which comes after ``ready``.
        self.first_sweep_pending = True
        #: Devices still waiting to be set up: device -> (attempts, retry at).
        self._pending: dict = {}
        #: Switched on and off by the user, and remembered beside the lighting.
        self.integrations = integrations if integrations is not None else IntegrationSettings(
            os.path.join(os.path.dirname(self.persistence.path), 'integrations.json'))
        self.accent = AccentWatch(self._follow_accent)
        self.chroma = ChromaBridge(lambda: self.manager.devices, effects=self.effects,
                                   persistence=self.persistence, on_release=self._after_game)
        self.chroma_port = chroma_port
        self.chroma_server: Optional[ChromaServer] = None
        #: Why games cannot reach the daemon, when they cannot.
        self.chroma_error: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.keys is not None:
            self.keys.start()
        self.snaptap.start()
        # USB first: a sweep of the radio listens for seconds.  The first poll does it.
        self.manager.scan(sweep_bluetooth=False)
        for device in self.manager.devices:
            self._initialise(device)
        if self.effects is not None:
            self.effects.start()
        if self.integrations.enabled('accent'):
            self.accent.start()
        if self.integrations.enabled('chroma'):
            self._start_chroma()
        self.state = 'ready'

    def _initialise(self, device) -> bool:
        """Take a device over and give it back its saved lighting.

        Anything may go wrong here, not just the errors a device call is
        documented to raise: a Bluetooth call that times out raises the
        standard TimeoutError, and letting that through ended the daemon's
        start-up before it was even listening.  A failure is not final -- the
        device is tried again on a back-off until it takes.
        """
        try:
            handled = self.keys is not None and self.keys.attach(device)
            device.enter_driver_mode(keys_handled=handled)
            device.restore()
            if self.effects is not None:
                # A per-key or live effect is part of what the device was showing.
                self.effects.restore(device)
        except Exception as error:  # noqa: BLE001 - reported, then retried
            attempts = self._pending.get(device, (0, 0.0))[0] + 1
            delay = RETRY_DELAYS[min(attempts, len(RETRY_DELAYS)) - 1]
            self._pending[device] = (attempts, time.monotonic() + delay)
            logger.warning('could not set up %s yet (%s); trying again in %.0fs',
                           device.name, error or type(error).__name__, delay)
            return False
        if self._pending.pop(device, None) is not None:
            logger.info('%s is set up', device.name)
        return True

    def stop(self) -> None:
        self.accent.stop()
        self._stop_chroma()
        if self.effects is not None:
            self.effects.stop()
        for device in self.manager.devices:
            leave = getattr(device, 'leave_driver_mode', None)
            if leave is None:
                continue
            try:
                leave()
            except Exception:  # noqa: BLE001 - shutting down regardless
                logger.debug('could not hand %s back to its firmware', device.name,
                             exc_info=True)
        # First: what it undoes lives on the thread of the hook stopped next.
        self.snaptap.stop()
        if self.keys is not None:
            self.keys.stop()
        self.manager.close()

    def poll_hotplug(self, force_bluetooth: bool = False) -> None:
        before = self.manager.devices
        known = {device.serial for device in before}
        self.manager.scan(force_bluetooth=force_bluetooth)
        current = self.manager.devices
        present = {device.serial for device in current}
        for device in current:
            if device not in before and self._initialise(device) and self.accent.colour:
                # Away when the colour last changed, perhaps: it is told now.
                self._paint_accent(device, self.accent.colour)
        now = time.monotonic()
        for device, (_attempts, retry_at) in list(self._pending.items()):
            if device not in current:
                del self._pending[device]
            elif now >= retry_at:
                self._initialise(device)
        if self.keys is not None:
            for device in before:
                if device not in current:
                    self.keys.detach(device)
        if self.effects is not None:
            # A host-rendered effect outlives its device otherwise: the thread
            # keeps writing to hardware that is no longer listed, which cannot
            # then be reached to stop it.
            for serial in known - present:
                self.effects.clear_serial(serial)

    # -- integrations --------------------------------------------------------
    def _paint_accent(self, device, colour) -> None:
        """Light every part of `device` that takes a colour in `colour`."""
        if self.chroma.drawing(device):
            return      # a game has it; it is told when the game is done
        zones = [zone for zone, caps in (device.capabilities().get('zones') or {}).items()
                 if 'static' in (caps.get('effects') or [])]
        if not zones:
            return
        try:
            if self.effects is not None:
                self.effects.stop_for(device)
            if hasattr(device, 'set_zone_colours'):
                device.set_zone_colours([list(colour)])
            else:
                for zone in zones:
                    device.set_static(*colour, zone=zone)
        except Exception as error:  # noqa: BLE001 - the others still follow
            logger.debug('could not colour %s: %s', device.name, error)

    def _follow_accent(self, colour) -> None:
        logger.info('following the accent colour #%02X%02X%02X', *colour)
        for device in self.manager.devices:
            self._paint_accent(device, colour)
        self.persistence.save()

    def _after_game(self, device) -> None:
        if self.accent.running and self.accent.colour:
            self._paint_accent(device, self.accent.colour)

    def _start_chroma(self) -> None:
        if self.chroma_server is not None:
            return
        try:
            server = ChromaServer(self.chroma, port=self.chroma_port)
        except OSError as error:
            self.chroma_error = 'port {0} is taken, most likely by Razer Synapse: {1}'.format(
                self.chroma_port, error)
            logger.warning('games cannot reach the daemon: %s', self.chroma_error)
            return
        self.chroma_error = None
        self.chroma.start()
        server.start()
        self.chroma_server = server
        logger.info('answering Razer Chroma on port %d', server.server_address[1])

    def _stop_chroma(self) -> None:
        server, self.chroma_server = self.chroma_server, None
        if server is not None:
            server.stop()
        self.chroma.stop()

    def rpc_integration_status(self) -> dict:
        switches = self.integrations.as_dict()
        colour = read_accent()
        server = self.chroma_server
        return {
            'accent': {'enabled': switches['accent'], 'available': colour is not None,
                       'colour': list(colour) if colour else None},
            'chroma': dict(self.chroma.status(), enabled=switches['chroma'],
                           listening=server is not None,
                           port=server.server_address[1] if server is not None else self.chroma_port,
                           error=self.chroma_error),
        }

    def rpc_integration_set(self, name: str, enabled: bool) -> dict:
        try:
            self.integrations.set(name, bool(enabled))
        except ValueError as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error
        if name == 'accent':
            if enabled:
                self.accent.start()
            else:
                self.accent.stop()
        elif name == 'chroma':
            if enabled:
                self._start_chroma()
            else:
                self._stop_chroma()
        return self.rpc_integration_status()

    # -- Snap Tap ------------------------------------------------------------
    def rpc_snaptap_status(self) -> dict:
        return self.snaptap.status()

    def rpc_snaptap_set(self, enabled: Optional[bool] = None, pairs=None, deny=None, allow=None,
                        acknowledged: Optional[bool] = None) -> dict:
        try:
            self.snaptap.configure(enabled=enabled, pairs=pairs, deny=deny, allow=allow,
                                   acknowledged=acknowledged)
        except ValueError as error:     # SnapTapError
            raise RpcError(INVALID_PARAMS, str(error)) from error
        # Snap Tap belongs to no one device, so every keyboard following a
        # profile takes the change into it.
        for device in self.manager.devices:
            if _snaptap_applies(device):
                self._keep_in_profile(device)
        return self.snaptap.status()

    # -- lookups -----------------------------------------------------------
    def _device(self, serial: str) -> RazerDevice:
        device = self.manager.by_serial(serial)
        if device is None:
            matches = self.manager.find(serial)
            if len(matches) == 1:
                return matches[0]
            raise RpcError(DEVICE_NOT_FOUND, 'no such device: {0}'.format(serial))
        return device

    # -- RPC methods -------------------------------------------------------
    def rpc_version(self) -> dict:
        from .._version import UPSTREAM_VERSION
        return {
            'version': __version__,
            'upstream': UPSTREAM_VERSION,
            'pid': os.getpid(),
            'uptime': round(time.time() - self.started_at, 1),
            'backend': self.manager.backend.name,
            'devices': len(self.manager.devices),
            'state': self.state,
            #: Absent from older daemons, which Studio takes as False.
            'first_sweep_pending': self.first_sweep_pending,
            #: Found, but not yet willing to take their settings.
            'pending': sorted(device.name for device in self._pending),
        }

    def rpc_list_devices(self) -> list:
        return [_device_summary(device) for device in self.manager.devices]

    def rpc_unsupported(self) -> list:
        return [{'pid': pid, 'product': name} for pid, name in self.manager.unsupported]

    def rpc_rescan(self) -> list:
        # Someone asked: sweep the radio now rather than when it is next due.
        self.poll_hotplug(force_bluetooth=True)
        return self.rpc_list_devices()

    def rpc_capabilities(self, serial: str) -> dict:
        return self._device(serial).capabilities()

    def rpc_call(self, serial: str, method: str, args=None, kwargs=None) -> Any:
        if method not in DEVICE_METHODS:
            raise RpcError(METHOD_NOT_FOUND,
                           'method not exposed over RPC: {0}'.format(method))
        device = self._device(serial)
        target = getattr(device, method, None)
        if target is None:
            raise RpcError(METHOD_NOT_FOUND, 'unknown method: {0}'.format(method))
        args = list(args or [])
        kwargs = dict(kwargs or {})
        zone = kwargs.get('zone', 'backlight')
        if (self.effects is not None and method in ZONE_CALLS and self.effects.drawing(device)
                and zone in zone_order(device)):
            # The host is drawing the other ear: this one joins in, rather than
            # being painted over by the next frame.
            colour = (args + [kwargs.get(k) for k in ('red', 'green', 'blue')])[:3]
            try:
                self.effects.set_zone_effect(device, zone, ZONE_CALLS[method],
                                             {'colour': colour if method == 'set_static' else None})
            except (NotSupported, ValueError) as error:
                raise RpcError(INVALID_PARAMS, str(error)) from error
            self.persistence.save()
            self._keep_in_profile(device)
            return None
        if (self.effects is not None and method in HARDWARE_EFFECTS
                and kwargs.get('zone', 'backlight') == 'backlight'):
            self.effects.stop_for(device)
        try:
            result = target(*args, **kwargs) if callable(target) else target
        except NotSupported as error:
            raise RpcError(NOT_SUPPORTED, str(error)) from error
        except (DeviceError, RecipeError) as error:
            raise RpcError(DEVICE_ERROR, str(error)) from error
        except (TypeError, ValueError) as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error
        self.persistence.save()
        self._keep_in_profile(device)
        return result

    def rpc_effect(self, serial: str, effect: str, options=None) -> dict:
        if self.effects is None:
            raise RpcError(NOT_SUPPORTED, 'software effects are disabled')
        device = self._device(serial)
        try:
            result = self.effects.set_effect(device, effect, dict(options or {}))
        except NotSupported as error:
            raise RpcError(NOT_SUPPORTED, str(error)) from error
        except ValueError as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error
        self.persistence.save()
        self._keep_in_profile(device)
        return result

    def rpc_effect_status(self) -> dict:
        if self.effects is None:
            return {'enabled': False, 'active': {}}
        return self.effects.status()

    def rpc_persistence(self) -> dict:
        return self.persistence.as_dict()

    # -- macro keys ----------------------------------------------------------
    def rpc_keys_status(self) -> dict:
        if self.keys is None:
            return {'running': False, 'devices': [], 'recording': None}
        return self.keys.status(self.manager.devices)

    def rpc_macro_list(self, serial: str) -> dict:
        device = self._device(serial)
        keys = self.keys.macro_keys(device) if self.keys is not None else []
        if not keys and getattr(getattr(device, 'info', None), 'dedicated_macro_keys', False):
            keys = ['M1', 'M2', 'M3', 'M4', 'M5']
        return {'keys': keys, 'bindings': self.macros.bindings(device.serial),
                'handled': bool(self.keys is not None and self.keys.handles(device))}

    def rpc_macro_set(self, serial: str, key: str, action: dict) -> dict:
        from ..keys import MacroError
        from ..keys.codes import parse_sequence
        device = self._device(serial)
        try:
            # A written sequence ("ctrl+c, ctrl+v") is turned into events here,
            # so every client shares the one table of key names.
            if isinstance(action, dict) and action.get('type') == 'keys' \
                    and 'events' not in action and 'sequence' in action:
                action = {'type': 'keys', 'events': parse_sequence(str(action['sequence']))}
            return self.macros.set(device.serial, str(key), action)
        except (MacroError, ValueError) as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error

    def rpc_macro_delete(self, serial: str, key: str) -> dict:
        device = self._device(serial)
        return {'deleted': self.macros.delete(device.serial, str(key))}

    def rpc_macro_record_start(self, serial: str, key: str) -> dict:
        device = self._device(serial)
        if self.keys is None or not self.keys.handles(device):
            raise RpcError(NOT_SUPPORTED,
                           'keys of {0} are not being handled'.format(device.name))
        self.keys.start_recording(device.serial, str(key))
        return {'recording': str(key)}

    def rpc_macro_record_stop(self) -> Optional[dict]:
        if self.keys is None:
            return None
        return self.keys.stop_recording()

    def rpc_macro_play(self, serial: str, key: str) -> dict:
        device = self._device(serial)
        action = self.macros.get(device.serial, str(key))
        if action is None:
            raise RpcError(INVALID_PARAMS, 'nothing is bound to {0}'.format(key))
        if self.keys is None:
            raise RpcError(NOT_SUPPORTED, 'macro keys are not being handled')
        self.keys._play_async(device.serial, str(key), action)
        return {'playing': str(key)}

    # -- profiles ----------------------------------------------------------
    def _snapshot(self, device, new: bool = False) -> dict:
        """What `device` has now, for a profile -- or, `new`, what a new one starts from."""
        state = default_state(device, self.persistence) if new else snapshot(device, self.persistence)
        if _snaptap_applies(device):
            from ..keys.snaptap import DEFAULT_PAIRS
            # A new profile never turns Snap Tap on, and has the pairs a first start has.
            settings = {'enabled': False, 'pairs': DEFAULT_PAIRS} if new else self.snaptap.status()
            state['snaptap'] = {'enabled': settings['enabled'], 'pairs': settings['pairs']}
        return state

    def _keep_in_profile(self, device) -> None:
        """Write what `device` has now into the profile it follows, if any: what
        spares the user a save button.  Under the lock _switch holds, so that a
        change made meanwhile cannot land, half of each, in the profile left."""
        with self._lock:
            if self.profiles.active(device.serial) is None:
                return
            try:
                self.profiles.update_active(device.serial, self._snapshot(device))
            except Exception:  # noqa: BLE001 - the change itself has been made
                logger.warning('could not keep the profile of %s up to date', device.name,
                               exc_info=True)

    def _apply_snaptap(self, device, profile: dict) -> None:
        stored = profile.get('snaptap')
        if not stored or not _snaptap_applies(device):
            return
        # Never on through a profile until the warning has been accepted here:
        # a file that says so is not someone reading it.  Without that, Snap
        # Tap is off already, so turning it off changes nothing.
        enabled = bool(stored.get('enabled')) and self.snaptap.status()['acknowledged']
        try:
            self.snaptap.configure(pairs=stored.get('pairs'), enabled=enabled)
        except ValueError as error:     # SnapTapError
            logger.warning('the Snap Tap in profile %r was not applied: %s',
                           profile.get('name'), error)

    def rpc_profile_list(self, serial: str) -> list:
        return self.profiles.summaries(self._device(serial).serial)

    def rpc_profile_save(self, serial: str, name: str) -> dict:
        device = self._device(serial)
        capture_current(device, self.persistence)
        try:
            entry = self.profiles.put(
                device.serial, name, self._snapshot(device), activate=True)
        except ProfileError as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error
        return {'name': entry['name'], 'saved': entry['saved']}

    def _switch(self, device, profile: dict) -> None:
        """Put `profile` on `device` and make it the one the device follows."""
        with self._lock:
            try:
                apply_profile(device, self.persistence, profile)
                if self.effects is not None:
                    # A profile saved with a host-drawn effect brings it back; one
                    # saved without stops whatever is running, or it would paint
                    # over the profile's own lighting.
                    if (profile.get('device') or {}).get('software'):
                        self.effects.restore(device)
                    else:
                        self.effects.stop_for(device)
                self._apply_snaptap(device, profile)
            except NotSupported as error:
                raise RpcError(NOT_SUPPORTED, str(error)) from error
            except (DeviceError, RecipeError) as error:
                raise RpcError(DEVICE_ERROR, str(error)) from error
            finally:
                # Even when a step above failed: apply_profile writes persistence
                # before it touches the device, so what the daemon keeps is this
                # profile's by now.  Left following the previous one, the next
                # change would save that mixture into it.
                self.persistence.save()
                self.profiles.activate(device.serial, profile['name'])

    def rpc_profile_load(self, serial: str, name: str) -> dict:
        device = self._device(serial)
        try:
            profile = self.profiles.get(device.serial, name)
        except ProfileError as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error
        self._switch(device, profile)
        return {'name': profile['name']}

    def rpc_profile_create(self, serial: str, name: str, source: Optional[str] = None) -> dict:
        """A new profile, switched to at once: a copy of `source`, or one that
        starts from the defaults -- unless the device follows no profile yet.

        What such a device has is kept nowhere else, and starting it over
        would lose it, so its first profile is of what it has now.  From then
        on its settings are in the profile being left, and a new one is new.
        """
        device = self._device(serial)
        capture_current(device, self.persistence)
        try:
            if source is not None:
                state = self.profiles.get(device.serial, source)
            else:
                state = self._snapshot(
                    device, new=self.profiles.active(device.serial) is not None)
            entry = self.profiles.put(device.serial, name, state, replace=False)
        except ProfileError as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error
        self._switch(device, entry)
        return {'name': entry['name'], 'saved': entry['saved']}

    def rpc_profile_delete(self, serial: str, name: str) -> dict:
        device = self._device(serial)
        try:
            self.profiles.delete(device.serial, name)
        except ProfileError as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error
        return {'name': name}


class RpcHandler(socketserver.StreamRequestHandler):
    """One client connection: authenticate once, then serve requests."""

    server: 'DaemonServer'

    def handle(self) -> None:
        authenticated = False
        while True:
            try:
                message = protocol.read_message(self.rfile)
            except RpcError as error:
                self._send_error(None, error)
                return
            except (OSError, ValueError):
                return
            if message is None:
                return

            request_id = message.get('id')
            try:
                if message.get('token') != self.server.endpoint.token:
                    raise RpcError(UNAUTHORIZED, 'invalid or missing token')
                authenticated = True
                result = self._dispatch(message)
            except RpcError as error:
                self._send_error(request_id, error)
                if not authenticated:
                    return
                continue
            except Exception as error:  # noqa: BLE001 - never kill the daemon
                logger.exception('unhandled error serving %s', message.get('method'))
                self._send_error(request_id, RpcError(INTERNAL_ERROR, str(error)))
                continue

            if request_id is not None:
                self.wfile.write(protocol.encode(
                    {'jsonrpc': '2.0', 'id': request_id, 'result': result}))

    def _dispatch(self, message: dict) -> Any:
        method = message.get('method')
        if not isinstance(method, str):
            raise RpcError(INVALID_REQUEST, 'missing method')
        params = message.get('params') or {}
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, 'params must be an object')

        if method == 'daemon.shutdown':
            self.server.request_shutdown()
            return {'stopping': True}

        handler: Optional[Callable] = self.server.methods.get(method)
        if handler is None:
            raise RpcError(METHOD_NOT_FOUND, 'unknown method: {0}'.format(method))
        try:
            return handler(**params)
        except TypeError as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error

    def _send_error(self, request_id, error: RpcError) -> None:
        try:
            self.wfile.write(protocol.encode(
                {'jsonrpc': '2.0', 'id': request_id, 'error': error.to_dict()}))
        except OSError:
            pass


class DaemonServer(socketserver.ThreadingTCPServer):
    """Loopback-only threading server with a bearer token."""

    daemon_threads = True

    # Not SO_REUSEADDR: on Windows it lets another process bind a port that is
    # already being listened on, which would let anything on the machine
    # hijack the daemon's socket.  SO_EXCLUSIVEADDRUSE is the Windows way to
    # say "this port is mine".
    allow_reuse_address = False

    def server_bind(self) -> None:
        if sys.platform == 'win32':
            try:
                self.socket.setsockopt(socket.SOL_SOCKET,
                                       getattr(socket, 'SO_EXCLUSIVEADDRUSE', -5), 1)
            except OSError:
                logger.debug('SO_EXCLUSIVEADDRUSE unavailable', exc_info=True)
        super().server_bind()

    def __init__(self, service: DaemonService, host: str = '127.0.0.1', port: int = 0):
        super().__init__((host, port), RpcHandler)
        self.service = service
        actual_host, actual_port = self.server_address[:2]
        self.endpoint = Endpoint(host=actual_host, port=actual_port,
                                 token=protocol.new_token(), pid=os.getpid(),
                                 version=__version__)
        self.methods = {
            'daemon.version': service.rpc_version,
            'daemon.rescan': service.rpc_rescan,
            'daemon.persistence': service.rpc_persistence,
            'devices.list': service.rpc_list_devices,
            'devices.unsupported': service.rpc_unsupported,
            'device.capabilities': service.rpc_capabilities,
            'device.call': service.rpc_call,
            'effect.set': service.rpc_effect,
            'effect.status': service.rpc_effect_status,
            'integration.status': service.rpc_integration_status,
            'integration.set': service.rpc_integration_set,
            'snaptap.status': service.rpc_snaptap_status,
            'snaptap.set': service.rpc_snaptap_set,
            'profile.list': service.rpc_profile_list,
            'profile.save': service.rpc_profile_save,
            'profile.load': service.rpc_profile_load,
            'profile.create': service.rpc_profile_create,
            'profile.delete': service.rpc_profile_delete,
            'keys.status': service.rpc_keys_status,
            'macro.list': service.rpc_macro_list,
            'macro.set': service.rpc_macro_set,
            'macro.delete': service.rpc_macro_delete,
            'macro.record_start': service.rpc_macro_record_start,
            'macro.record_stop': service.rpc_macro_record_stop,
            'macro.play': service.rpc_macro_play,
        }
        self._shutdown_requested = threading.Event()

    def request_shutdown(self) -> None:
        self._shutdown_requested.set()
        threading.Thread(target=self.shutdown, daemon=True).start()

    @property
    def stopping(self) -> bool:
        return self._shutdown_requested.is_set()


def device_loop(service: 'DaemonService', stop: threading.Event) -> None:
    """Set devices up, then poll until `stop`: the first time at once, radio included."""
    try:
        service.start()
    except Exception:  # noqa: BLE001 - the daemon keeps serving regardless
        logger.exception('setting up devices failed')
        service.state = 'ready'
    logger.info('%d device(s) ready', len(service.manager.devices))
    interval = 0
    while not stop.wait(interval):
        try:
            service.poll_hotplug()
        except Exception:  # noqa: BLE001 - a bad scan must not kill the daemon
            logger.debug('hot-plug scan failed', exc_info=True)
        service.first_sweep_pending = False
        interval = HOTPLUG_INTERVAL


def serve(backend=None, host: str = '127.0.0.1', port: int = 0,
          enable_effects: bool = True, enable_keys: bool = True) -> bool:
    """Run the daemon until it is asked to stop.

    Returns ``False`` without doing anything if this session already has a
    daemon.  Otherwise the socket is up and the endpoint file written before a
    single device is touched: finding and setting up devices takes seconds,
    and a daemon that does not answer for that long looks like one that is not
    running -- which is how a second one used to get started beside it.
    """
    from .instance import SingleInstance

    instance = SingleInstance()
    if not instance.acquire():
        logger.info('another openrazer-win daemon is already running in this session')
        return False

    service = DaemonService(backend=backend, enable_effects=enable_effects,
                            enable_keys=enable_keys)
    server = DaemonServer(service, host=host, port=port)
    endpoint_file = server.endpoint.write()
    logger.info('openrazer-win %s listening on %s:%d (endpoint file: %s)', __version__,
                server.endpoint.host, server.endpoint.port, endpoint_file)

    stop = threading.Event()

    watcher = threading.Thread(target=device_loop, args=(service, stop), name='devices',
                               daemon=True)
    watcher.start()

    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        logger.info('interrupted, shutting down')
    finally:
        stop.set()
        server.server_close()
        watcher.join(timeout=15)
        service.stop()
        Endpoint.remove(endpoint_file)
        instance.release()
        logger.info('stopped')
    return True
