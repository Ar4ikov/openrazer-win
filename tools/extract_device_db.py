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

import upstream_source


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
#: extractor against a newer OpenRazer does not silently drop them.  A device
#: waiting in an upstream pull request goes in tools/upstream_patches/ instead,
#: which brings its driver quirks along with its record.
#: Where Razer keeps its product photos.
RAZER_IMAGES = 'https://medias-p1.phoenix.razer.com/sys-master-phoenix-images-container/'

#: Its gallery photos all have a backdrop to them; this one is cut out already.
BLACKSHARK_V2_HYPERSPEED_IMAGE = (
    'https://assets2.razerzone.com/images/pnx.assets/16eac26cd230ce343f56af70ec49626a/'
    'razer-blackshark-v2-hyperspeed-black-500x500.webp')
BLACKSHARK_METHODS = ['get_device_type_headset', 'get_battery', 'is_charging',
                      'get_idle_time', 'set_idle_time']
#: The picture openrazer/openrazer#2899 gives it.
BLACKSHARK_V3_X_IMAGE = 'https://dl.razerzone.com/src2/20181/20181-1-en-v3.png'
BLACKSHARK_V3_X_PS_IMAGE = (
    'https://apps.razer.com/synapse/products/4130/ui/4130_0/PluginImages/'
    '4130_0_0_dashboard3x.png')

#: Its settings are the BlackSharks'; its lights are none either.
BARRACUDA_METHODS = BLACKSHARK_METHODS

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
        'image': RAZER_IMAGES + 'h2b/hcb/9631979241502/230808-kraken-kitty-v2-bt-quartz-1500x1000-5.jpg',
        #: Sold in three colours, which nothing it reports tells apart, so the
        #: control panel lets its owner pick the one that is theirs.
        'images': {
            'Quartz': RAZER_IMAGES + 'h2b/hcb/9631979241502/230808-kraken-kitty-v2-bt-quartz-1500x1000-5.jpg',
            'White': RAZER_IMAGES + 'hfb/hc5/9879046815774/250306-kraken-kitty-v2-bt-whiet-1500x1000-4.jpg',
            'Black': RAZER_IMAGES + 'ha3/hc5/9879046848542/250306-kraken-kitty-v2-bt-black-1500x1000-4.jpg',
        },
        'methods': [
            'get_device_type_headset',
            'set_static_effect',
            'set_none_effect',
            'set_key_row',
            'set_custom_effect',
        ],
        'transport': 'ble',
    },
    {
        # A MediaTek headset with no lights, so openrazer/openrazer#2974 and not
        # the Kraken driver is where upstream may yet take it.  Its protocol is
        # in openrazer_win/protocol/blackshark.py.  The receiver calls itself
        # "Razer BlackShark V2 HS 2.4".
        'class_name': 'RazerBlackSharkV2HyperSpeedWireless',
        'name': 'Razer BlackShark V2 HyperSpeed (Wireless)',
        'vid': 0x1532,
        'pid': 0x0565,
        'type': 'headset',
        'has_matrix': False,
        'matrix_dims': None,
        'dedicated_macro_keys': False,
        'driver_mode': False,
        'wave_dirs': [1, 2],
        'poll_rates': None,
        'dpi_max': None,
        'image': BLACKSHARK_V2_HYPERSPEED_IMAGE,
        'methods': BLACKSHARK_METHODS,
        'transport': 'hid',
    },
    {
        'class_name': 'RazerBlackSharkV2HyperSpeedWired',
        'name': 'Razer BlackShark V2 HyperSpeed (Wired)',
        'vid': 0x1532,
        'pid': 0x056E,
        'type': 'headset',
        'has_matrix': False,
        'matrix_dims': None,
        'dedicated_macro_keys': False,
        'driver_mode': False,
        'wave_dirs': [1, 2],
        'poll_rates': None,
        'dpi_max': None,
        'image': BLACKSHARK_V2_HYPERSPEED_IMAGE,
        'methods': BLACKSHARK_METHODS,
        'transport': 'hid',
    },
    {
        # Another protocol again, on a feature report of its own, which is why
        # openrazer/openrazer#2899 gives it a driver to itself and why it is
        # here rather than in tools/upstream_patches/: there are no recipes to
        # transpile.  It is in openrazer_win/protocol/blackshark_v3x.py.  The
        # class names are that pull request's.
        'class_name': 'RazerBlackSharkV3XUSB',
        'name': 'Razer BlackShark V3 X HyperSpeed (Wired)',
        'vid': 0x1532,
        'pid': 0x057C,
        'type': 'headset',
        'has_matrix': False,
        'matrix_dims': None,
        'dedicated_macro_keys': False,
        'driver_mode': False,
        'wave_dirs': [1, 2],
        'poll_rates': None,
        'dpi_max': None,
        'image': BLACKSHARK_V3_X_IMAGE,
        'methods': BLACKSHARK_METHODS,
        'transport': 'hid',
    },
    {
        'class_name': 'RazerBlackSharkV3XWireless',
        'name': 'Razer BlackShark V3 X HyperSpeed (Wireless)',
        'vid': 0x1532,
        'pid': 0x057D,
        'type': 'headset',
        'has_matrix': False,
        'matrix_dims': None,
        'dedicated_macro_keys': False,
        'driver_mode': False,
        'wave_dirs': [1, 2],
        'poll_rates': None,
        'dpi_max': None,
        'image': BLACKSHARK_V3_X_IMAGE,
        'methods': BLACKSHARK_METHODS,
        'transport': 'hid',
    },
    {
        # PlayStation edition of the BlackShark V3 X HyperSpeed: same 64-byte
        # feature report protocol and features on both its HyperSpeed receiver
        # (1532:1021, "BlackShark V3 X PS") and its USB cable (1532:1022,
        # "BlackShark V3 X PS USB").
        'class_name': 'RazerBlackSharkV3XPSWireless',
        'name': 'Razer BlackShark V3 X HyperSpeed for PlayStation (Wireless)',
        'vid': 0x1532,
        'pid': 0x1021,
        'type': 'headset',
        'has_matrix': False,
        'matrix_dims': None,
        'dedicated_macro_keys': False,
        'driver_mode': False,
        'wave_dirs': [1, 2],
        'poll_rates': None,
        'dpi_max': None,
        'image': BLACKSHARK_V3_X_PS_IMAGE,
        'methods': BLACKSHARK_METHODS,
        'transport': 'hid',
    },
    {
        'class_name': 'RazerBlackSharkV3XPSUSB',
        'name': 'Razer BlackShark V3 X HyperSpeed for PlayStation (Wired)',
        'vid': 0x1532,
        'pid': 0x1022,
        'type': 'headset',
        'has_matrix': False,
        'matrix_dims': None,
        'dedicated_macro_keys': False,
        'driver_mode': False,
        'wave_dirs': [1, 2],
        'poll_rates': None,
        'dpi_max': None,
        'image': BLACKSHARK_V3_X_PS_IMAGE,
        'methods': BLACKSHARK_METHODS,
        'transport': 'hid',
    },
    {
        # The 2023 BlackShark V2 Pro's 2.4 GHz receiver, "Razer BlackShark V2
        # Pro 2.4": the V2 HyperSpeed's registers in the Barracuda X's "PA"
        # frames, after openrazer/openrazer#2862 (not merged).  It is in
        # openrazer_win/protocol/blackshark_v2pro.py.  No picture, as for the
        # Barracuda X: none could be checked from here.
        'class_name': 'RazerBlackSharkV2Pro2023Wireless',
        'name': 'Razer BlackShark V2 Pro (2023, Wireless)',
        'vid': 0x1532,
        'pid': 0x0555,
        'type': 'headset',
        'has_matrix': False,
        'matrix_dims': None,
        'dedicated_macro_keys': False,
        'driver_mode': False,
        'wave_dirs': [1, 2],
        'poll_rates': None,
        'dpi_max': None,
        'image': None,
        'methods': BLACKSHARK_METHODS,
        'transport': 'hid',
    },
    {
        # The 2022 model's USB-C receiver, which calls itself "Razer Barracuda
        # X 2.4".  A tunnel to the headset rather than a control report, worked
        # out by leag/razer-barracuda; upstream has nothing for it.  It is in
        # openrazer_win/protocol/barracuda.py.  No picture: none of Razer's own
        # could be checked from here, and a wrong one is worse than none.
        'class_name': 'RazerBarracudaXWireless',
        'name': 'Razer Barracuda X (Wireless)',
        'vid': 0x1532,
        'pid': 0x0552,
        'type': 'headset',
        'has_matrix': False,
        'matrix_dims': None,
        'dedicated_macro_keys': False,
        'driver_mode': False,
        'wave_dirs': [1, 2],
        'poll_rates': None,
        'dpi_max': None,
        'image': None,
        'methods': BARRACUDA_METHODS,
        'transport': 'hid',
    },
]


#: The product ids upstream's daemon marked with DEDICATED_MACRO_KEYS until it
#: dropped its macro support (openrazer/openrazer 7edc0b5, on master after
#: 477f4d6), which took the attribute with it but for the Tartarus Pro.  This
#: port binds macro keys itself (openrazer_win/keys) and Studio draws M1-M5
#: from the flag, so the list as upstream last had it is kept here.
DEDICATED_MACRO_KEY_PIDS = frozenset({
    0x0015,  # Razer Naga
    0x0016,  # Razer DeathAdder 3.5G
    0x001F,  # Razer Naga Epic
    0x0029,  # Razer DeathAdder 3.5G Black
    0x002E,  # Razer Naga 2012
    0x0034,  # Razer Taipan
    0x0036,  # Razer Naga Hex (Red)
    0x0040,  # Razer Naga 2014
    0x0041,  # Razer Naga Hex
    0x0050,  # Razer Naga Hex V2
    0x0053,  # Razer Naga Chroma
    0x0065,  # Razer Basilisk Essential
    0x0067,  # Razer Naga Trinity
    0x008D,  # Razer Naga Left Handed Edition 2020
    0x008F,  # Razer Naga Pro (Wired)
    0x0090,  # Razer Naga Pro (Wireless)
    0x0096,  # Razer Naga X
    0x00A7,  # Razer Naga V2 Pro (Wired)
    0x00A8,  # Razer Naga V2 Pro (Wireless)
    0x010D,  # Razer BlackWidow Ultimate 2012
    0x010E,  # Razer BlackWidow Stealth Edition
    0x010F,  # Razer Anansi
    0x0111,  # Razer Nostromo
    0x0113,  # Razer Orbweaver
    0x011A,  # Razer BlackWidow Ultimate 2013
    0x011B,  # Razer BlackWidow (Classic)
    0x0201,  # Razer Tartarus
    0x0203,  # Razer BlackWidow Chroma
    0x0204,  # Razer DeathStalker Chroma
    0x0207,  # Razer Orbweaver Chroma
    0x0208,  # Razer Tartarus Chroma
    0x0211,  # Razer BlackWidow Chroma (Overwatch)
    0x0221,  # BlackWidow Chroma V2
    0x022B,  # Razer Tartarus V2
    0x0244,  # Razer Tartarus Pro
})


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
    with upstream_source.checkout(argv[1]) as upstream:
        return extract(upstream, argv[2])


def extract(upstream: str, out_path: str) -> int:
    sys.path.insert(0, os.path.join(upstream, 'daemon'))
    _install_stubs()

    from openrazer_daemon.hardware import get_device_classes  # noqa: E402
    from openrazer_daemon.hardware.device_base import RazerDeviceBrightnessSuspend  # noqa: E402

    devices = []
    seen = set()
    for cls in get_device_classes():
        vid = getattr(cls, 'USB_VID', None)
        pid = getattr(cls, 'USB_PID', None)
        if not isinstance(vid, int) or not isinstance(pid, int):
            continue
        methods = sorted(set(getattr(cls, 'METHODS', []) or []))
        # The base class adds brightness in __init__, outside the static METHODS.
        if issubclass(cls, RazerDeviceBrightnessSuspend):
            methods = sorted(set(methods) | {'get_brightness', 'set_brightness'})
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
            'dedicated_macro_keys': bool(getattr(cls, 'DEDICATED_MACRO_KEYS', False))
            or (vid == 0x1532 and pid in DEDICATED_MACRO_KEY_PIDS),
            'driver_mode': bool(getattr(cls, 'DRIVER_MODE', False)),
            'wave_dirs': list(getattr(cls, 'WAVE_DIRS', (1, 2))),
            'poll_rates': list(getattr(cls, 'POLL_RATES', None) or []) or None,
            'dpi_max': getattr(cls, 'DPI_MAX', None),
            'image': getattr(cls, 'DEVICE_IMAGE', None),
            'methods': methods,
            'transport': 'hid',
        }
        # Only where a device has more than upstream's default tactile and free
        # spin, so the other entries stay as they were.
        scroll_modes = tuple(getattr(cls, 'SCROLL_MODES', None) or (0, 1))
        if 'set_scroll_mode' in methods and scroll_modes != (0, 1):
            entry['scroll_modes'] = list(scroll_modes)
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
