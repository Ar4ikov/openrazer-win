"""The public Python API.

Mirrors ``openrazer.client`` from upstream closely enough that scripts written
for Linux mostly port by changing the import::

    from openrazer_win.client import DeviceManager

    for device in DeviceManager().devices:
        print(device.name, device.serial)
        device.fx.static(0, 255, 0)

Two transports are available.  By default the manager talks to the daemon over
loopback RPC, so several programs can share the hardware.  ``direct=True`` skips
the daemon and opens the HID handles in-process, which is handy for one-off
scripts -- but only one process can hold a device that way.
"""
from __future__ import annotations

from typing import Optional

from .._version import __version__
from ..devices.recipes import NotSupported
from .rpc import DaemonUnavailable, RpcClient, is_daemon_running

__all__ = ['DeviceManager', 'RazerDevice', 'DaemonUnavailable', 'NotSupported',
           'is_daemon_running', '__version__']

#: Wave directions, matching upstream's constants.
WAVE_LEFT = 1
WAVE_RIGHT = 2

#: Reactive speeds.
REACTIVE_500MS = 1
REACTIVE_1000MS = 2
REACTIVE_1500MS = 3
REACTIVE_2000MS = 4


class _Backend:
    """Either an RPC connection or an in-process device manager."""

    def list_devices(self) -> list:
        raise NotImplementedError

    def capabilities(self, serial: str) -> dict:
        raise NotImplementedError

    def call(self, serial: str, method: str, *args, **kwargs):
        raise NotImplementedError

    def software_effect(self, serial: str, effect: str, options: dict) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass


class _RpcBackend(_Backend):
    def __init__(self, client: RpcClient):
        self.client = client

    def list_devices(self) -> list:
        return self.client.call('devices.list')

    def capabilities(self, serial: str) -> dict:
        return self.client.call('device.capabilities', serial=serial)

    def call(self, serial: str, method: str, *args, **kwargs):
        return self.client.call('device.call', serial=serial, method=method,
                                args=list(args), kwargs=kwargs)

    def software_effect(self, serial: str, effect: str, options: dict) -> dict:
        return self.client.call('effect.set', serial=serial, effect=effect,
                                options=options)

    def close(self) -> None:
        self.client.close()


class _DirectBackend(_Backend):
    def __init__(self, hid_backend=None):
        from ..core.manager import DeviceManager as _CoreManager
        self.manager = _CoreManager(backend=hid_backend)
        self.manager.scan()

    def _device(self, serial: str):
        device = self.manager.by_serial(serial)
        if device is None:
            raise KeyError('no such device: {0}'.format(serial))
        return device

    def list_devices(self) -> list:
        summaries = []
        for device in self.manager.devices:
            try:
                firmware = device.firmware_version
            except Exception:  # noqa: BLE001 - an optional field, never fatal
                firmware = None
            summaries.append({
                'serial': device.serial, 'name': device.name, 'type': device.type,
                'vid': device.vid, 'pid': device.pid, 'image': device.info.image,
                'firmware': firmware,
            })
        return summaries

    def capabilities(self, serial: str) -> dict:
        return self._device(serial).capabilities()

    def call(self, serial: str, method: str, *args, **kwargs):
        result = getattr(self._device(serial), method)(*args, **kwargs)
        self.manager.persistence.save()
        return result

    def software_effect(self, serial: str, effect: str, options: dict) -> dict:
        raise NotSupported('software effects need the daemon; start it first')

    def close(self) -> None:
        self.manager.close()


class Matrix:
    """A writable RGB grid, flushed to the device by :meth:`draw`."""

    def __init__(self, device: 'RazerDevice'):
        from ..effects.frame import Frame
        rows, columns = device.matrix_dimensions or (1, 1)
        self._device = device
        self._frame = Frame(rows, columns)

    @property
    def rows(self) -> int:
        return self._frame.rows

    @property
    def columns(self) -> int:
        return self._frame.columns

    def set(self, row: int, column: int, colour) -> 'Matrix':
        red, green, blue = tuple(colour)[:3]
        self._frame.set(row, column, red, green, blue)
        return self

    def __setitem__(self, position, colour) -> None:
        row, column = position
        self.set(row, column, colour)

    def __getitem__(self, position):
        row, column = position
        return self._frame.get(row, column)

    def clear(self) -> 'Matrix':
        self._frame.clear()
        return self

    def fill(self, colour) -> 'Matrix':
        red, green, blue = tuple(colour)[:3]
        self._frame.fill(red, green, blue)
        return self

    def draw(self) -> None:
        """Send the grid and switch the device to the custom effect."""
        self._device._call('set_key_row', list(self._frame.to_payload()))
        self._device._call('set_custom')


class Fx:
    """Lighting effects for one zone."""

    def __init__(self, device: 'RazerDevice', zone: str = 'backlight'):
        self._device = device
        self._zone = zone
        self._advanced: Optional[Matrix] = None

    @property
    def zone(self) -> str:
        return self._zone

    @property
    def effects(self) -> list:
        return self._device.capabilities.get('zones', {}).get(
            self._zone, {}).get('effects', [])

    def has(self, effect: str) -> bool:
        return effect in self.effects

    @property
    def advanced(self) -> Matrix:
        """The per-key frame buffer (upstream calls this ``fx.advanced``)."""
        if self._advanced is None:
            self._advanced = Matrix(self._device)
        return self._advanced

    # -- effects -----------------------------------------------------------
    def none(self):
        return self._device._call('set_none', zone=self._zone)

    def on(self):
        return self._device._call('set_on', zone=self._zone)

    def static(self, red: int, green: int, blue: int):
        return self._device._call('set_static', red, green, blue, zone=self._zone)

    def spectrum(self):
        return self._device._call('set_spectrum', zone=self._zone)

    def wave(self, direction: int = WAVE_RIGHT):
        return self._device._call('set_wave', direction, zone=self._zone)

    def wheel(self, direction: int = 1):
        return self._device._call('set_wheel', direction, zone=self._zone)

    def reactive(self, red: int, green: int, blue: int, speed: int = REACTIVE_500MS):
        return self._device._call('set_reactive', red, green, blue, speed,
                                  zone=self._zone)

    def blinking(self, red: int, green: int, blue: int):
        return self._device._call('set_blinking', red, green, blue, zone=self._zone)

    def breath_random(self):
        return self._device._call('set_breath_random', zone=self._zone)

    def breath_single(self, red: int, green: int, blue: int):
        return self._device._call('set_breath_single', red, green, blue,
                                  zone=self._zone)

    def breath_dual(self, red1, green1, blue1, red2, green2, blue2):
        return self._device._call('set_breath_dual', red1, green1, blue1,
                                  red2, green2, blue2, zone=self._zone)

    def breath_triple(self, red1, green1, blue1, red2, green2, blue2,
                      red3, green3, blue3):
        return self._device._call('set_breath_triple', red1, green1, blue1,
                                  red2, green2, blue2, red3, green3, blue3,
                                  zone=self._zone)

    def starlight_random(self, speed: int = 1):
        return self._device._call('set_starlight_random', speed, zone=self._zone)

    def starlight_single(self, red, green, blue, speed: int = 1):
        return self._device._call('set_starlight_single', red, green, blue, speed,
                                  zone=self._zone)

    def starlight_dual(self, red1, green1, blue1, red2, green2, blue2, speed: int = 1):
        return self._device._call('set_starlight_dual', red1, green1, blue1,
                                  red2, green2, blue2, speed, zone=self._zone)

    # -- host-rendered -----------------------------------------------------
    def ripple(self, red: int = 0, green: int = 255, blue: int = 0,
               refresh_rate: float = 0.040):
        return self._device._software_effect(
            'ripple', {'colour': [red, green, blue], 'refresh_rate': refresh_rate})

    def ripple_random(self, refresh_rate: float = 0.040):
        return self._device._software_effect(
            'ripple_random', {'refresh_rate': refresh_rate})

    def spectrum_soft(self, refresh_rate: float = 0.040):
        """Spectrum drawn frame by frame, for hardware that has no spectrum."""
        return self._device._software_effect(
            'spectrum_soft', {'refresh_rate': refresh_rate})

    def wave_soft(self, red: int = 0, green: int = 255, blue: int = 0,
                  refresh_rate: float = 0.040):
        """Wave drawn frame by frame, for hardware that has no wave.

        The renderer sweeps in one direction only, so there is no `direction`
        here -- unlike the hardware effect.
        """
        return self._device._software_effect(
            'wave_soft', {'colour': [red, green, blue],
                          'refresh_rate': refresh_rate})

    def stop_software_effect(self):
        return self._device._software_effect('none', {})


class RazerDevice:
    """One device, as seen by client code."""

    def __init__(self, backend: _Backend, summary: dict):
        self._backend = backend
        self._summary = summary
        self._capabilities: Optional[dict] = None
        self._zones: dict = {}

    # -- identity ----------------------------------------------------------
    @property
    def serial(self) -> str:
        return self._summary['serial']

    @property
    def name(self) -> str:
        return self._summary['name']

    @property
    def type(self) -> str:
        return self._summary['type']

    @property
    def product_id(self) -> int:
        return self._summary['pid']

    @property
    def vendor_id(self) -> int:
        return self._summary['vid']

    @property
    def device_image(self) -> Optional[str]:
        return self._summary.get('image')

    @property
    def firmware_version(self) -> Optional[str]:
        return self._summary.get('firmware')

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return '<RazerDevice {0} ({1})>'.format(self.name, self.serial)

    # -- plumbing ----------------------------------------------------------
    def _call(self, method: str, *args, **kwargs):
        return self._backend.call(self.serial, method, *args, **kwargs)

    def _software_effect(self, effect: str, options: dict):
        return self._backend.software_effect(self.serial, effect, options)

    @property
    def capabilities(self) -> dict:
        if self._capabilities is None:
            self._capabilities = self._backend.capabilities(self.serial)
        return self._capabilities

    def has(self, capability: str) -> bool:
        return bool(self.capabilities.get(capability))

    @property
    def zones(self) -> list:
        return list(self.capabilities.get('zones', {}))

    def colour_zones(self) -> list:
        """The zones a colour can be written to one at a time, in device order.

        A device that says so publishes the order its zones sit in -- the
        Bluetooth headset's two ears, left first.  Otherwise it is every
        lighting zone that can hold a static colour, with the catch-all
        ``backlight`` dropped when there are named zones beside it.
        """
        order = self.capabilities.get('zone_order')
        if order:
            return list(order)
        zones = self.capabilities.get('zones', {})
        named = [zone for zone, caps in zones.items()
                 if 'static' in caps.get('effects', [])]
        if len(named) > 1 and 'backlight' in named:
            named.remove('backlight')
        return named

    def set_colour_zones(self, colours) -> None:
        """Paint one colour per zone, from :meth:`colour_zones`.

        Devices that can take every zone in a single message do so, which
        matters for the headset: two writes would light one ear before the
        other, and the mismatch is visible.
        """
        colours = [tuple(colour)[:3] for colour in colours]
        zones = self.colour_zones()
        if len(colours) > len(zones):
            raise ValueError('{0} has {1} zones, got {2} colours'.format(
                self.name, len(zones), len(colours)))
        if self.capabilities.get('zone_colours') and len(colours) == len(zones):
            self._call('set_zone_colours', [list(colour) for colour in colours])
            return
        for zone, colour in zip(zones, colours):
            self.fx_for(zone).static(*colour)

    def fx_for(self, zone: str) -> Fx:
        if zone not in self._zones:
            self._zones[zone] = Fx(self, zone)
        return self._zones[zone]

    @property
    def fx(self) -> Fx:
        """The main lighting zone."""
        return self.fx_for(self.primary_zone())

    def primary_zone(self) -> str:
        zones = self.zones
        if 'backlight' in zones:
            return 'backlight'
        return zones[0] if zones else 'backlight'

    def zone_for_effect(self, effect: str) -> str:
        """The zone that can actually do `effect`.

        A mouse has no backlight, and a keyboard has no scroll wheel, so the
        sensible default depends on what is being asked for.
        """
        zones = self.capabilities.get('zones', {})
        primary = self.primary_zone()
        if effect in zones.get(primary, {}).get('effects', []):
            return primary
        for zone, caps in zones.items():
            if effect in caps.get('effects', []):
                return zone
        return primary

    def zone_with_brightness(self) -> str:
        zones = self.capabilities.get('zones', {})
        primary = self.primary_zone()
        if zones.get(primary, {}).get('brightness'):
            return primary
        for zone, caps in zones.items():
            if caps.get('brightness'):
                return zone
        return primary

    @property
    def matrix_dimensions(self) -> Optional[tuple]:
        dims = self.capabilities.get('matrix_dimensions')
        return tuple(dims) if dims else None

    # -- brightness --------------------------------------------------------
    @property
    def brightness(self) -> float:
        return self._call('get_brightness')

    @brightness.setter
    def brightness(self, value: float) -> None:
        self._call('set_brightness', value)

    # -- mouse -------------------------------------------------------------
    @property
    def dpi(self) -> tuple:
        return tuple(self._call('get_dpi'))

    @dpi.setter
    def dpi(self, value) -> None:
        if isinstance(value, (list, tuple)):
            self._call('set_dpi', int(value[0]), int(value[1]))
        else:
            self._call('set_dpi', int(value))

    @property
    def max_dpi(self) -> Optional[int]:
        return self.capabilities.get('max_dpi')

    @property
    def dpi_stages(self) -> tuple:
        active, stages = self._call('get_dpi_stages')
        return active, [tuple(stage) for stage in stages]

    def set_dpi_stages(self, active_stage: int, stages) -> None:
        self._call('set_dpi_stages', active_stage, [list(s) for s in stages])

    @property
    def poll_rate(self) -> int:
        return self._call('get_poll_rate')

    @poll_rate.setter
    def poll_rate(self, value: int) -> None:
        self._call('set_poll_rate', int(value))

    @property
    def supported_poll_rates(self) -> list:
        return self.capabilities.get('supported_poll_rates', [])

    # -- power -------------------------------------------------------------
    @property
    def battery_level(self) -> float:
        return self._call('get_battery_level')

    @property
    def is_charging(self) -> bool:
        return self._call('is_charging')

    @property
    def idle_time(self) -> int:
        return self._call('get_idle_time')

    @idle_time.setter
    def idle_time(self, seconds: int) -> None:
        self._call('set_idle_time', int(seconds))

    @property
    def low_battery_threshold(self) -> float:
        return self._call('get_low_battery_threshold')

    @low_battery_threshold.setter
    def low_battery_threshold(self, percent: float) -> None:
        self._call('set_low_battery_threshold', percent)

    # -- keyboard ----------------------------------------------------------
    @property
    def game_mode(self) -> bool:
        return self._call('get_game_mode')

    @game_mode.setter
    def game_mode(self, enabled: bool) -> None:
        self._call('set_game_mode', bool(enabled))

    @property
    def macro_mode(self) -> bool:
        return self._call('get_macro_mode')

    @macro_mode.setter
    def macro_mode(self, enabled: bool) -> None:
        self._call('set_macro_mode', bool(enabled))

    @property
    def keyboard_layout(self) -> str:
        return self._call('get_keyboard_layout')


class DeviceManager:
    """Entry point: discovers devices through the daemon, or directly."""

    def __init__(self, direct: bool = False, endpoint=None, hid_backend=None,
                 timeout: float = 15.0):
        if direct:
            self._backend: _Backend = _DirectBackend(hid_backend)
        else:
            self._backend = _RpcBackend(RpcClient(endpoint, timeout=timeout))
        self._devices: Optional[list] = None

    @property
    def devices(self) -> list:
        if self._devices is None:
            self._devices = [RazerDevice(self._backend, summary)
                             for summary in self._backend.list_devices()]
        return self._devices

    def refresh(self) -> list:
        self._devices = None
        return self.devices

    def by_serial(self, serial: str) -> Optional[RazerDevice]:
        for device in self.devices:
            if device.serial == serial:
                return device
        return None

    def by_name(self, needle: str) -> list:
        lowered = needle.lower()
        return [d for d in self.devices if lowered in d.name.lower()]

    @property
    def version(self) -> str:
        return __version__

    def close(self) -> None:
        self._backend.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
