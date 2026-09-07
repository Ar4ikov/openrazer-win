"""The ``openrazer-win`` command line interface."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from typing import Optional

from .._version import UPSTREAM_VERSION, __version__
from ..client import DaemonUnavailable, DeviceManager
from ..client.rpc import RpcClient, is_daemon_running
from ..daemon.protocol import RpcError, endpoint_path
from ..devices.database import get_database
from ..devices.recipes import NotSupported

COLOURS = {
    'red': (255, 0, 0), 'green': (0, 255, 0), 'blue': (0, 0, 255),
    'white': (255, 255, 255), 'black': (0, 0, 0), 'yellow': (255, 255, 0),
    'cyan': (0, 255, 255), 'magenta': (255, 0, 255), 'orange': (255, 96, 0),
    'purple': (128, 0, 255), 'pink': (255, 64, 128), 'razer': (0, 255, 0),
}

EFFECT_HELP = """\
none, on, static, spectrum, wave, wheel, reactive, blinking, custom,
breath_random, breath_single, breath_dual, breath_triple,
starlight_random, starlight_single, starlight_dual,
ripple, ripple_random (host-rendered, needs the daemon)"""


class CliError(Exception):
    """A user-facing error; printed without a traceback."""


def parse_colour(text: str) -> tuple:
    """Accept ``red``, ``#ff0080``, ``ff0080`` or ``255,0,128``."""
    text = text.strip().lower()
    if text in COLOURS:
        return COLOURS[text]
    if ',' in text:
        parts = [int(part) for part in text.split(',')]
        if len(parts) != 3:
            raise CliError('colour needs three components: {0}'.format(text))
        return tuple(max(0, min(255, part)) for part in parts)
    hexed = text.lstrip('#')
    if len(hexed) == 3:
        hexed = ''.join(char * 2 for char in hexed)
    if len(hexed) != 6:
        raise CliError('cannot parse colour {0!r}; try red, #ff0080 or 255,0,128'
                       .format(text))
    try:
        value = int(hexed, 16)
    except ValueError as error:
        raise CliError('cannot parse colour {0!r}'.format(text)) from error
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def open_manager(args) -> DeviceManager:
    try:
        return DeviceManager(direct=args.direct)
    except DaemonUnavailable as error:
        raise CliError(
            '{0}\n\nStart it with:  openrazer-win daemon start\n'
            'Or work without it:  openrazer-win --direct ...'.format(error)) from error


def pick_devices(manager: DeviceManager, selector: Optional[str]) -> list:
    devices = manager.devices
    if not devices:
        raise CliError('no supported Razer devices found')
    if not selector or selector == 'all':
        return devices
    lowered = selector.lower()
    exact = [d for d in devices
             if lowered in (d.serial.lower(), '{0:04x}'.format(d.product_id))]
    if exact:
        return exact
    matches = manager.by_name(selector)
    if not matches:
        raise CliError('no device matches {0!r}; try: openrazer-win list'.format(selector))
    return matches


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(args) -> int:
    with open_manager(args) as manager:
        devices = manager.devices
        if args.json:
            print(json.dumps([{
                'serial': d.serial, 'name': d.name, 'type': d.type,
                'pid': '{0:04x}'.format(d.product_id),
                'firmware': d.firmware_version,
            } for d in devices], indent=2))
            return 0
        if not devices:
            print('No supported Razer devices found.')
            print('Run "openrazer-win doctor" to see what is attached.')
            return 1
        width = max(len(d.name) for d in devices)
        for device in devices:
            print('{0:<{1}}  {2:<9}  {3:<18}  {4}'.format(
                device.name, width, device.type, device.serial,
                device.firmware_version or ''))
    return 0


def cmd_info(args) -> int:
    with open_manager(args) as manager:
        payload = []
        for device in pick_devices(manager, args.device):
            capabilities = device.capabilities
            entry = {'name': device.name, 'serial': device.serial,
                     'type': device.type,
                     'pid': '{0:04x}'.format(device.product_id),
                     'firmware': device.firmware_version,
                     'capabilities': capabilities}
            for key, getter in (('battery', 'battery_level'),
                                ('charging', 'is_charging'),
                                ('dpi', 'dpi'), ('poll_rate', 'poll_rate')):
                flag = {'battery': 'battery', 'charging': 'battery',
                        'dpi': 'dpi', 'poll_rate': 'poll_rate'}[key]
                if capabilities.get(flag):
                    try:
                        entry[key] = getattr(device, getter)
                    except (RpcError, NotSupported):
                        entry[key] = None
            payload.append(entry)

        if args.json:
            print(json.dumps(payload, indent=2, default=str))
            return 0

        for entry in payload:
            capabilities = entry['capabilities']
            print('{0}  ({1})'.format(entry['name'], entry['type']))
            print('  serial      {0}'.format(entry['serial']))
            print('  product id  0x{0}'.format(entry['pid']))
            print('  firmware    {0}'.format(entry['firmware'] or 'unknown'))
            transport = capabilities.get('transport') or {}
            if transport:
                print('  interface   mi_{0:02d}  ({1} us wait)'.format(
                    transport.get('interface') or 0, transport.get('wait_us')))
            dimensions = capabilities.get('matrix_dimensions')
            if dimensions:
                print('  matrix      {0} rows x {1} columns'.format(*dimensions))
            for zone, zone_caps in capabilities.get('zones', {}).items():
                effects = ', '.join(zone_caps['effects']) or 'none'
                print('  zone {0:<14} {1}'.format(zone, effects))
                if zone_caps['brightness']:
                    print('  {0:<19} brightness supported'.format(''))
            if 'dpi' in entry:
                print('  dpi         {0} (max {1})'.format(
                    entry['dpi'], capabilities.get('max_dpi')))
            if 'poll_rate' in entry:
                print('  poll rate   {0} Hz  (supported: {1})'.format(
                    entry['poll_rate'],
                    ', '.join(str(r) for r in capabilities.get(
                        'supported_poll_rates', []))))
            if 'battery' in entry and entry['battery'] is not None:
                print('  battery     {0}%{1}'.format(
                    entry['battery'], ' (charging)' if entry.get('charging') else ''))
            print()
    return 0


def cmd_effect(args) -> int:
    colour = parse_colour(args.colour) if args.colour else (0, 255, 0)
    colour2 = parse_colour(args.colour2) if args.colour2 else (0, 0, 255)
    colour3 = parse_colour(args.colour3) if args.colour3 else (255, 0, 0)

    with open_manager(args) as manager:
        failures = 0
        for device in pick_devices(manager, args.device):
            zone = args.zone or device.zone_for_effect(
                'custom' if args.effect in ('ripple', 'ripple_random') else args.effect)
            fx = device.fx_for(zone)
            try:
                _apply_effect(fx, args.effect, colour, colour2, colour3, args)
            except (RpcError, NotSupported, DaemonUnavailable) as error:
                print('{0}: {1}'.format(device.name, error), file=sys.stderr)
                failures += 1
                continue
            if not args.quiet:
                print('{0}: {1} on {2}'.format(device.name, args.effect, zone))
    return 1 if failures else 0


def _apply_effect(fx, effect: str, colour, colour2, colour3, args) -> None:
    if effect == 'none':
        fx.none()
    elif effect == 'on':
        fx.on()
    elif effect == 'static':
        fx.static(*colour)
    elif effect == 'spectrum':
        fx.spectrum()
    elif effect == 'wave':
        fx.wave(args.direction)
    elif effect == 'wheel':
        fx.wheel(args.direction)
    elif effect == 'reactive':
        fx.reactive(*colour, speed=args.speed)
    elif effect == 'blinking':
        fx.blinking(*colour)
    elif effect == 'breath_random':
        fx.breath_random()
    elif effect == 'breath_single':
        fx.breath_single(*colour)
    elif effect == 'breath_dual':
        fx.breath_dual(*colour, *colour2)
    elif effect == 'breath_triple':
        fx.breath_triple(*colour, *colour2, *colour3)
    elif effect == 'starlight_random':
        fx.starlight_random(args.speed)
    elif effect == 'starlight_single':
        fx.starlight_single(*colour, speed=args.speed)
    elif effect == 'starlight_dual':
        fx.starlight_dual(*colour, *colour2, speed=args.speed)
    elif effect == 'ripple':
        fx.ripple(*colour)
    elif effect == 'ripple_random':
        fx.ripple_random()
    elif effect == 'custom':
        fx.advanced.fill(colour).draw()
    else:
        raise CliError('unknown effect {0!r}\n\nAvailable:\n{1}'.format(
            effect, EFFECT_HELP))


def cmd_brightness(args) -> int:
    with open_manager(args) as manager:
        for device in pick_devices(manager, args.device):
            fx_zone = args.zone or device.zone_with_brightness()
            if args.value is None:
                try:
                    print('{0}: {1}%'.format(
                        device.name,
                        device._call('get_brightness', zone=fx_zone)))
                except (RpcError, NotSupported) as error:
                    print('{0}: {1}'.format(device.name, error), file=sys.stderr)
                continue
            device._call('set_brightness', args.value, zone=fx_zone)
            if not args.quiet:
                print('{0}: brightness {1}% on {2}'.format(
                    device.name, args.value, fx_zone))
    return 0


def cmd_dpi(args) -> int:
    with open_manager(args) as manager:
        for device in pick_devices(manager, args.device):
            if not device.has('dpi'):
                continue
            if args.x is None:
                print('{0}: {1}'.format(device.name, device.dpi))
                continue
            device.dpi = (args.x, args.y if args.y is not None else args.x)
            if not args.quiet:
                print('{0}: dpi {1}'.format(device.name, device.dpi))
    return 0


def cmd_poll_rate(args) -> int:
    with open_manager(args) as manager:
        for device in pick_devices(manager, args.device):
            if not device.has('poll_rate'):
                continue
            if args.rate is None:
                print('{0}: {1} Hz'.format(device.name, device.poll_rate))
                continue
            device.poll_rate = args.rate
            if not args.quiet:
                print('{0}: poll rate {1} Hz'.format(device.name, device.poll_rate))
    return 0


def cmd_battery(args) -> int:
    with open_manager(args) as manager:
        found = False
        for device in pick_devices(manager, args.device):
            if not device.has('battery'):
                continue
            found = True
            print('{0}: {1}%{2}'.format(
                device.name, device.battery_level,
                ' (charging)' if device.is_charging else ''))
        if not found:
            print('No battery-powered devices connected.')
    return 0


def cmd_supported(args) -> int:
    database = get_database()
    entries = database.find(args.search) if args.search else list(database)
    if args.json:
        print(json.dumps([entry.to_dict() for entry in entries], indent=2))
        return 0
    by_type: dict = {}
    for entry in entries:
        by_type.setdefault(entry.type, []).append(entry)
    for device_type in sorted(by_type):
        print('{0} ({1})'.format(device_type, len(by_type[device_type])))
        for entry in sorted(by_type[device_type], key=lambda e: e.name):
            print('  {0:04x}  {1}'.format(entry.pid, entry.name))
    print('\n{0} devices in total.'.format(len(entries)))
    return 0


def cmd_daemon(args) -> int:
    if args.action == 'status':
        return _daemon_status(args)
    if args.action == 'stop':
        try:
            client = RpcClient(timeout=5.0)
            client.call('daemon.shutdown')
            client.close()
        except (DaemonUnavailable, RpcError) as error:
            print('daemon is not running ({0})'.format(error))
            return 1
        print('daemon stopped')
        return 0
    if args.action == 'run':
        from ..daemon.server import serve
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.INFO,
            format='%(asctime)s  %(levelname)-7s %(name)s: %(message)s')
        serve(enable_effects=not args.no_effects)
        return 0
    return _daemon_start(args)


def _daemon_status(args) -> int:
    try:
        client = RpcClient(timeout=3.0)
        info = client.call('daemon.version')
        devices = client.call('devices.list')
        effects = client.call('effect.status')
        client.close()
    except (DaemonUnavailable, RpcError) as error:
        print('daemon: not running ({0})'.format(error))
        return 1
    if args.json:
        print(json.dumps({'daemon': info, 'devices': devices, 'effects': effects},
                         indent=2))
        return 0
    print('daemon      running (pid {0})'.format(info['pid']))
    print('version     {0}  (openrazer {1})'.format(info['version'], info['upstream']))
    print('uptime      {0}s'.format(info['uptime']))
    print('backend     {0}'.format(info['backend']))
    print('devices     {0}'.format(len(devices)))
    for device in devices:
        print('              {0}  {1}'.format(device['name'], device['serial']))
    active = effects.get('active') or {}
    print('effects     {0}'.format(
        ', '.join('{0}={1}'.format(k, v) for k, v in active.items()) or 'none active'))
    return 0


def daemon_command() -> list:
    """How to launch the daemon from wherever this code is running.

    A PyInstaller build has no importable ``-m`` target, so the frozen binary
    re-invokes itself with ``daemon run`` instead.
    """
    if getattr(sys, 'frozen', False):
        return [sys.executable, 'daemon', 'run']
    return [sys.executable, '-m', 'openrazer_win.daemon.main']


def _daemon_start(args) -> int:
    if is_daemon_running():
        print('daemon is already running')
        return 0
    command = daemon_command()
    if getattr(args, 'no_effects', False):
        command.append('--no-effects')
    creationflags = 0
    if sys.platform == 'win32':
        # Detach so the daemon outlives this console.
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | \
            getattr(subprocess, 'DETACHED_PROCESS', 0)
    subprocess.Popen(command, creationflags=creationflags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, close_fds=True)

    import time
    for _ in range(60):
        if is_daemon_running():
            print('daemon started')
            return 0
        time.sleep(0.25)
    print('daemon did not come up; run "openrazer-win daemon run" to see why',
          file=sys.stderr)
    return 1


def cmd_autostart(args) -> int:
    from ..daemon.autostart import AutostartError, disable, enable, status

    try:
        if args.action == 'status':
            command = status()
            if args.json:
                print(json.dumps({'enabled': command is not None,
                                  'command': command}, indent=2))
                return 0
            if command is None:
                print('autostart: off')
                print('Turn it on with:  openrazer-win autostart enable')
                return 1
            print('autostart: on')
            print('command:   {0}'.format(command))
            return 0

        if args.action == 'disable':
            if disable():
                print('autostart disabled')
            else:
                print('autostart was already off')
            return 0

        command = enable(args.no_effects)
        print('autostart enabled; the daemon will start at logon')
        print('command:   {0}'.format(command))
        print('\nRemove it later with:  openrazer-win autostart disable')
        print('It also shows up in Task Manager under Startup apps.')
        return 0
    except AutostartError as error:
        raise CliError(str(error)) from error


def cmd_doctor(args) -> int:
    from ..hid import get_backend, is_supported_platform
    from ..protocol.report import VENDOR_ID
    from ..core.transport import FEATURE_BUFFER_SIZE

    print('openrazer-win {0}  (device data from openrazer {1})'.format(
        __version__, UPSTREAM_VERSION))
    print('python       {0}'.format(sys.version.split()[0]))
    print('platform     {0}'.format(sys.platform))
    if not is_supported_platform():
        print('\nThis is not Windows, so only the emulator backend is available.')

    database = get_database()
    print('database     {0} supported product ids'.format(len(database)))
    print('endpoint     {0}{1}'.format(
        endpoint_path(), '' if os.path.exists(endpoint_path()) else '  (absent)'))
    print('daemon       {0}'.format(
        'running' if is_daemon_running() else 'not running'))

    try:
        backend = get_backend()
    except Exception as error:  # noqa: BLE001 - report, do not crash
        print('\nHID backend unavailable: {0}'.format(error))
        return 1

    collections = backend.enumerate(vendor_id=VENDOR_ID)
    print('\nRazer HID collections ({0}):'.format(len(collections)))
    if not collections:
        print('  none -- is the device plugged in?')
    for info in collections:
        control = ' <- control interface' if info.feature_length == FEATURE_BUFFER_SIZE else ''
        known = database.get(info.vendor_id, info.product_id)
        print('  {0}{1}'.format(info.describe(), control))
        print('      {0}'.format(known.name if known else 'NOT IN DATABASE'))

    unusable = [i for i in collections if i.feature_length == FEATURE_BUFFER_SIZE]
    if collections and not unusable:
        print('\nNo collection exposes the 90-byte feature report.')
        print('Razer Synapse may be holding the device -- close it and retry.')
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='openrazer-win',
        description='Control Razer devices on Windows.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Effects:\n' + EFFECT_HELP)
    parser.add_argument('--version', action='version',
                        version='openrazer-win {0} (openrazer {1})'.format(
                            __version__, UPSTREAM_VERSION))
    parser.add_argument('--direct', action='store_true',
                        help='talk to the hardware directly instead of via the daemon')
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    parser.add_argument('--quiet', '-q', action='store_true', help='suppress progress output')
    subparsers = parser.add_subparsers(dest='command')

    def add(name, handler, help_text, **kwargs):
        sub = subparsers.add_parser(name, help=help_text, **kwargs)
        sub.set_defaults(handler=handler)
        return sub

    add('list', cmd_list, 'list connected devices')

    info = add('info', cmd_info, 'show everything known about a device')
    info.add_argument('device', nargs='?', help='serial, product id or name fragment')

    effect = add('effect', cmd_effect, 'apply a lighting effect',
                 formatter_class=argparse.RawDescriptionHelpFormatter,
                 epilog='Effects:\n' + EFFECT_HELP)
    effect.add_argument('effect')
    effect.add_argument('colour', nargs='?', help='name, #rrggbb or r,g,b')
    effect.add_argument('--colour2', '--color2', dest='colour2')
    effect.add_argument('--colour3', '--color3', dest='colour3')
    effect.add_argument('--device', '-d')
    effect.add_argument('--zone', '-z')
    effect.add_argument('--speed', type=int, default=1, choices=(1, 2, 3, 4))
    effect.add_argument('--direction', type=int, default=1, choices=(0, 1, 2))

    brightness = add('brightness', cmd_brightness, 'get or set brightness')
    brightness.add_argument('value', nargs='?', type=float)
    brightness.add_argument('--device', '-d')
    brightness.add_argument('--zone', '-z')

    dpi = add('dpi', cmd_dpi, 'get or set mouse DPI')
    dpi.add_argument('x', nargs='?', type=int)
    dpi.add_argument('y', nargs='?', type=int)
    dpi.add_argument('--device', '-d')

    poll = add('poll-rate', cmd_poll_rate, 'get or set the polling rate in Hz')
    poll.add_argument('rate', nargs='?', type=int)
    poll.add_argument('--device', '-d')

    battery = add('battery', cmd_battery, 'show battery level')
    battery.add_argument('--device', '-d')

    supported = add('supported', cmd_supported, 'list every device the port knows')
    supported.add_argument('search', nargs='?', default='')

    daemon = add('daemon', cmd_daemon, 'start, stop or inspect the daemon')
    daemon.add_argument('action', nargs='?', default='start',
                        choices=('start', 'stop', 'status', 'run'))
    daemon.add_argument('--no-effects', action='store_true',
                        help='disable host-rendered effects and the keyboard hook')
    daemon.add_argument('--verbose', '-v', action='store_true')

    autostart = add('autostart', cmd_autostart,
                    'run the daemon automatically at logon')
    autostart.add_argument('action', nargs='?', default='status',
                           choices=('enable', 'disable', 'status'))
    autostart.add_argument('--no-effects', action='store_true',
                           help='start it without host-rendered effects')

    add('doctor', cmd_doctor, 'diagnose device detection problems')
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, 'command', None):
        parser.print_help()
        return 0
    try:
        return args.handler(args)
    except CliError as error:
        print('error: {0}'.format(error), file=sys.stderr)
        return 2
    except (RpcError, NotSupported) as error:
        print('error: {0}'.format(error), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
