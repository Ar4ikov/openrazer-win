"""Extract the OpenRazer device database from an upstream checkout.

Imports ``openrazer_daemon.hardware`` with every Linux-only dependency stubbed
out, then dumps each device class into a JSON file the Windows port consumes at
runtime.  Run with::

    python tools/extract_device_db.py <path-to-openrazer-checkout> <out.json>
"""
from __future__ import annotations

import json
import os
import sys
import types


def _install_stubs() -> None:
    """Fake out dbus/gi/evdev so the hardware modules import on Windows."""

    class _Any:
        """Permissive stand-in: any attribute access returns another _Any."""

        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, item):
            return _Any()

        def __call__(self, *args, **kwargs):
            return _Any()

    def _module(name: str, **attrs) -> types.ModuleType:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        mod.__getattr__ = lambda item: _Any()  # type: ignore[attr-defined]
        sys.modules[name] = mod
        return mod

    for name in ('dbus', 'dbus.service', 'dbus.exceptions', 'gi', 'gi.repository',
                 'evdev', 'pyudev', 'notify2', 'setproctitle', 'daemonize'):
        _module(name)

    # openrazer_daemon.dbus_services.service.DBusService must be a real class so
    # the hardware classes can subclass it.
    class DBusService:
        def __init__(self, *args, **kwargs):
            pass

        def add_dbus_method(self, *args, **kwargs):
            pass

    _module('openrazer_daemon.dbus_services.service', DBusService=DBusService)
    _module('openrazer_daemon.dbus_services.dbus_methods')
    _module('openrazer_daemon.misc.effect_sync')
    _module('openrazer_daemon.misc.battery_notifier', BatteryManager=_Any)
    _module('openrazer_daemon.misc.key_event_management',
            KeyboardKeyManager=_Any, GamepadKeyManager=_Any, OrbweaverKeyManager=_Any,
            NagaHexV2KeyManager=_Any, TartarusKeyManager=_Any)
    _module('openrazer_daemon.misc.ripple_effect', RippleManager=_Any)


# Device-type marker methods -> the canonical type string used by the port.
TYPE_METHODS = {
    'get_device_type_keyboard': 'keyboard',
    'get_device_type_mouse': 'mouse',
    'get_device_type_mousemat': 'mousemat',
    'get_device_type_keypad': 'keypad',
    'get_device_type_headset': 'headset',
    'get_device_type_core': 'core',
    'get_device_type_accessory': 'accessory',
    'get_device_type_mug': 'mug',
    'get_device_type_egpu': 'core',
}

MODULE_TYPE_FALLBACK = {
    'keyboards': 'keyboard',
    'mouse': 'mouse',
    'mouse_mat': 'mousemat',
    'headsets': 'headset',
    'accessory': 'accessory',
    'core': 'core',
}


#: Devices upstream does not cover, added by this port.  They are kept here
#: rather than hand-written into the generated JSON so that re-running the
#: extractor against a newer OpenRazer does not silently drop them.
LOCAL_DEVICES = [
    {
        # Bluetooth-only: plugging it in charges it and exposes no data
        # interface, so there is no USB device for upstream to bind to.  Its
        # lighting protocol was recovered from an HCI capture -- see
        # openrazer_win/protocol/razer_ble.py.
        'class_name': 'RazerKrakenKittyV2BT',
        'name': 'Razer Kraken Kitty V2 BT',
        'vid': 0x1532,
        'pid': 0x0562,
        'type': 'headset',
        'has_matrix': True,
        'matrix_dims': [1, 2],
        'dedicated_macro_keys': False,
        'driver_mode': False,
        'wave_dirs': [1, 2],
        'poll_rates': None,
        'dpi_max': None,
        'image': None,
        'methods': [
            'get_device_type_headset',
            'set_static_effect',
            'set_none_effect',
            'set_key_row',
            'set_custom_effect',
        ],
        'transport': 'ble',
    },
]


def _pretty_name(cls) -> str:
    """Turn RazerBlackWidowChroma into 'Razer BlackWidow Chroma'."""
    doc = (cls.__doc__ or '').strip()
    for line in doc.splitlines():
        line = line.strip()
        if line.lower().startswith('class for the '):
            return line[len('class for the '):].strip()
        if line.lower().startswith('class for '):
            return line[len('class for '):].strip()
    return cls.__name__


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    upstream, out_path = argv[1], argv[2]
    sys.path.insert(0, os.path.join(upstream, 'daemon'))
    _install_stubs()

    from openrazer_daemon.hardware import get_device_classes  # noqa: E402

    devices = []
    seen = set()
    for cls in get_device_classes():
        vid = getattr(cls, 'USB_VID', None)
        pid = getattr(cls, 'USB_PID', None)
        if not isinstance(vid, int) or not isinstance(pid, int):
            continue
        methods = sorted(set(getattr(cls, 'METHODS', []) or []))
        dev_type = None
        for method, name in TYPE_METHODS.items():
            if method in methods:
                dev_type = name
                break
        if dev_type is None:
            module = cls.__module__.rsplit('.', 1)[-1]
            dev_type = MODULE_TYPE_FALLBACK.get(module, 'accessory')

        matrix = getattr(cls, 'MATRIX_DIMS', None)
        entry = {
            'class_name': cls.__name__,
            'name': _pretty_name(cls),
            'vid': vid,
            'pid': pid,
            'type': dev_type,
            'has_matrix': bool(getattr(cls, 'HAS_MATRIX', False)),
            'matrix_dims': list(matrix) if matrix else None,
            'dedicated_macro_keys': bool(getattr(cls, 'DEDICATED_MACRO_KEYS', False)),
            'driver_mode': bool(getattr(cls, 'DRIVER_MODE', False)),
            'wave_dirs': list(getattr(cls, 'WAVE_DIRS', (1, 2))),
            'poll_rates': list(getattr(cls, 'POLL_RATES', None) or []) or None,
            'dpi_max': getattr(cls, 'DPI_MAX', None),
            'image': getattr(cls, 'DEVICE_IMAGE', None),
            'methods': methods,
            'transport': 'hid',
        }
        key = (vid, pid)
        if key in seen:
            print(f'warning: duplicate VID:PID {vid:04x}:{pid:04x} ({cls.__name__})',
                  file=sys.stderr)
        seen.add(key)
        devices.append(entry)

    for entry in LOCAL_DEVICES:
        key = (entry['vid'], entry['pid'])
        if key in seen:
            continue        # upstream has caught up; prefer its record
        seen.add(key)
        devices.append(dict(entry))

    devices.sort(key=lambda d: (d['vid'], d['pid']))
    payload = {
        'schema': 2,
        'source': 'openrazer/openrazer',
        'devices': devices,
    }
    with open(out_path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False, sort_keys=False)
        handle.write('\n')
    print(f'wrote {len(devices)} devices to {out_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
