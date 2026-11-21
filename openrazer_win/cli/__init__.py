"""The ``openrazer-win`` command line interface."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Optional

from .._version import UPSTREAM_VERSION, __version__
from ..client import DaemonUnavailable, DeviceManager
from ..client.rpc import RpcClient, is_daemon_running
from ..core.device import DeviceError
from ..core.transport import (
    is_barracuda_interface, is_blackshark_interface, is_blackshark_v3x_interface,
)
from ..daemon.protocol import RpcError, endpoint_path
from ..devices.database import get_database
from ..devices.recipes import NotSupported
from ..hid.base import HidError
from ..protocol import barracuda, blackshark_v3x

COLOURS = {
    'red': (255, 0, 0), 'green': (0, 255, 0), 'blue': (0, 0, 255),
    'white': (255, 255, 255), 'black': (0, 0, 0), 'yellow': (255, 255, 0),
    'cyan': (0, 255, 255), 'magenta': (255, 0, 255), 'orange': (255, 96, 0),
    'purple': (128, 0, 255), 'pink': (255, 64, 128), 'razer': (0, 255, 0),
}

#: Device classes that are plainly not lighting hardware, so an unknown product
#: id in one of them is not a gap worth reporting.
NON_LIGHTING_CLASSES = frozenset((
    'Camera', 'MEDIA', 'AudioEndpoint', 'Ports', 'Bluetooth', 'System',
    'Image', 'USB', 'SoftwareDevice'))

EFFECT_HELP = """\
none, on, static, spectrum, wave, wheel, reactive, blinking, custom,
breath_random, breath_single, breath_dual, breath_triple,
starlight_random, starlight_single, starlight_dual,
ripple, ripple_random, music, accent（主机渲染，需要后台服务）

accent 用 Windows 设定的颜色点亮设备，并跟随颜色变化。

在耳机上，--zone left 或 --zone right 可让一只耳朵拥有独立的灯效：
  openrazer-win effect music red --zone left
  openrazer-win effect spectrum --zone right"""

#: Effects the host can draw frame by frame for hardware that lacks them.
#: Reached automatically, because "spectrum" is what a user asks for whether or
#: not the firmware happens to implement it.
HOST_RENDERED_FALLBACK = {
    'spectrum': 'spectrum_soft',
    'wave': 'wave_soft',
    'breath_single': 'breath_soft',
    'starlight_single': 'starlight_soft',
}

INTEGRATION_HELP = """\
示例：
  openrazer-win integration                  开启了什么，以及它在做什么
  openrazer-win integration accent on        所有设备使用 Windows 主题色，
                                             并在颜色变化时跟随
  openrazer-win integration chroma on        支持 Razer Chroma 的游戏点亮
                                             你的设备，如同使用 Synapse 时一样
  openrazer-win integration chroma off"""

SNAPTAP_HELP = """\
在两个相对按键中，最后按下的生效：另一个在被覆盖期间保持松开。此功能在
本机完成，不在键盘内，因此适用于所有键盘，且仅在后台服务运行时可用。

示例：
  openrazer-win snaptap                  查看当前设置及其运行状态
  openrazer-win snaptap on               首次会要求你阅读一条警告
  openrazer-win snaptap on --accept      已阅读警告时使用，适合脚本
  openrazer-win snaptap off
  openrazer-win snaptap pair add a d     按位置指定按键，与布局无关
  openrazer-win snaptap pair remove a d  "remove" 对拒绝和允许列表同样适用
  openrazer-win snaptap deny add game.exe       不在此程序中生效
  openrazer-win snaptap allow add game.exe      仅在列出的程序中生效"""

PROFILE_HELP = """配置文件是设备设置的有名快照：各区域的灯效、颜色和亮度、
按键开关模式、DPI 与轮询率，以及键盘上的 Snap Tap。设备本身不保存颜色——
由后台服务保存——因此配置文件存在这里，并按需重新应用。

最后保存或加载的配置文件就是设备的当前配置文件，在列表中用 * 标记。
后台服务运行期间，之后每次更改都会保存到其中。

示例：
  openrazer-win profile                列出已保存的配置文件
  openrazer-win profile save night     记录当前设置；它将成为当前配置文件
  openrazer-win profile load night     恢复它并设为当前配置文件
  openrazer-win profile delete night"""

HEADSET_HELP = """\
适用于自行保存声音设置的耳机：BlackShark V2 HyperSpeed、BlackShark V3 X
HyperSpeed 和 Barracuda X。它们并非拥有全部相同设置，缺少某项的耳机会说明。

示例：
  openrazer-win headset                  显示当前设置
  openrazer-win headset sidetone 8       听到自己的声音，级别 0-15
  openrazer-win headset sidetone off
  openrazer-win headset eq game          flat、game、music、movie 或 custom；
                                         V3 X 上为 default、game、music、movie
  openrazer-win headset eq off
  openrazer-win headset eq -3,0,0,0,0,0,0,2,3,3
                                         自己的曲线：十个频段，-9 到 6 dB；
                                         V3 X 上是所选预设的曲线，
                                         -6 到 6 dB；Barracuda X 有自己的
                                         步进，-5 到 5
  openrazer-win headset dnd on           勿扰
  openrazer-win headset led battery      off、link、battery 或 warning
  openrazer-win headset nc on            麦克风降噪
  openrazer-win headset saving on        无线节能
  openrazer-win headset trace            Barracuda X：最近的请求与应答，
                                         用于错误报告
  openrazer-win headset sleep 30         关闭前的分钟数，0 表示永不；
                                         V3 X 没有永不，从 5 开始；
                                         Barracuda X 接受 0、5、15、30、45 或 60"""

ZONES_HELP = """\
示例：
  openrazer-win zones                  列出区域
  openrazer-win zones red blue         左耳红色，右耳蓝色
  openrazer-win zones "#ff8800"        所有区域使用同一种颜色"""


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
            raise CliError('颜色需要三个分量：{0}'.format(text))
        return tuple(max(0, min(255, part)) for part in parts)
    hexed = text.lstrip('#')
    if len(hexed) == 3:
        hexed = ''.join(char * 2 for char in hexed)
    if len(hexed) != 6:
        raise CliError('无法解析颜色 {0!r}；请试试 red、#ff0080 或 255,0,128'
                       .format(text))
    try:
        value = int(hexed, 16)
    except ValueError as error:
        raise CliError('无法解析颜色 {0!r}'.format(text)) from error
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def open_manager(args) -> DeviceManager:
    try:
        return DeviceManager(direct=args.direct)
    except DaemonUnavailable as error:
        raise CliError(
            '{0}\n\n用以下命令启动：  openrazer-win daemon start\n'
            '或者不使用后台服务：  openrazer-win --direct ...'.format(error)) from error


def pick_devices(manager: DeviceManager, selector: Optional[str]) -> list:
    devices = manager.devices
    if not devices:
        raise CliError('未找到支持的 Razer 设备')
    if not selector or selector == 'all':
        return devices
    lowered = selector.lower()
    exact = [d for d in devices
             if lowered in (d.serial.lower(), '{0:04x}'.format(d.product_id))]
    if exact:
        return exact
    matches = manager.by_name(selector)
    if not matches:
        raise CliError('没有匹配 {0!r} 的设备；试试：openrazer-win list'.format(selector))
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
            print('未找到支持的 Razer 设备。')
            print('运行 "openrazer-win doctor" 查看已连接的设备。')
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
                                ('dpi', 'dpi'), ('poll_rate', 'poll_rate'),
                                ('keyswitch_optimization', 'keyswitch_optimization')):
                flag = {'charging': 'battery'}.get(key, key)
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
            print('  序列号      {0}'.format(entry['serial']))
            print('  产品 ID     0x{0}'.format(entry['pid']))
            print('  固件        {0}'.format(entry['firmware'] or '未知'))
            transport = capabilities.get('transport') or {}
            if capabilities.get('protocol') == 'razer-ble':
                # No HID interface to speak of; the address is what identifies it.
                print('  蓝牙        {0}  （LE，厂商 GATT 服务）'.format(
                    transport.get('path') or '?'))
            elif transport:
                print('  接口        mi_{0:02d}  （{1} us 等待）'.format(
                    transport.get('interface') or 0, transport.get('wait_us')))
            dimensions = capabilities.get('matrix_dimensions')
            if dimensions:
                print('  矩阵        {0} 行 x {1} 列'.format(*dimensions))
            for zone, zone_caps in capabilities.get('zones', {}).items():
                effects = ', '.join(zone_caps['effects']) or '无'
                print('  区域 {0:<14} {1}'.format(zone, effects))
                if zone_caps['brightness']:
                    print('  {0:<19} 支持亮度'.format(''))
            if 'dpi' in entry:
                print('  DPI         {0} （最大 {1}）'.format(
                    entry['dpi'], capabilities.get('max_dpi')))
            if 'poll_rate' in entry:
                print('  轮询率      {0} Hz  （支持：{1}）'.format(
                    entry['poll_rate'],
                    ', '.join(str(r) for r in capabilities.get(
                        'supported_poll_rates', []))))
            if entry.get('keyswitch_optimization'):
                print('  按键开关    {0}'.format(entry['keyswitch_optimization']))
            if 'battery' in entry and entry['battery'] is not None:
                print('  电量        {0}%{1}'.format(
                    entry['battery'], '（充电中）' if entry.get('charging') else ''))
            print()
    return 0


def cmd_effect(args) -> int:
    colour = parse_colour(args.colour) if args.colour else (0, 255, 0)
    colour2 = parse_colour(args.colour2) if args.colour2 else (0, 0, 255)
    colour3 = parse_colour(args.colour3) if args.colour3 else (255, 0, 0)

    with open_manager(args) as manager:
        failures = 0
        for device in pick_devices(manager, args.device):
            if not args.device and not device.zones:
                continue        # nothing on it lights up
            zone = args.zone or device.zone_for_effect(
                'custom' if args.effect in ('ripple', 'ripple_random', 'music', 'accent')
                else args.effect)
            fx = device.fx_for(zone)
            effect = args.effect
            rendered = False
            if effect in HOST_RENDERED_FALLBACK and not fx.has(effect):
                # The Bluetooth headset, for one, only knows a static colour.
                # Synapse fakes the rest by streaming frames, and so can we.
                effect = HOST_RENDERED_FALLBACK[effect]
                rendered = True
            try:
                _apply_effect(fx, effect, colour, colour2, colour3, args)
            except (RpcError, NotSupported, DaemonUnavailable) as error:
                print('{0}: {1}'.format(device.name, error), file=sys.stderr)
                failures += 1
                continue
            if not args.quiet:
                print('{0}：{1} 已应用到 {2}{3}'.format(
                    device.name, args.effect, zone,
                    '  （主机渲染）' if rendered else ''))
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
    elif effect == 'spectrum_soft':
        fx.spectrum_soft()
    elif effect == 'wave_soft':
        fx.wave_soft(*colour)
    elif effect == 'wave':
        fx.wave(args.direction, speed=getattr(args, 'wave_speed', None))
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
    elif effect == 'music':
        fx.music(*colour)
    elif effect == 'breath_soft':
        fx.breath_soft(*colour)
    elif effect == 'starlight_soft':
        fx.starlight_soft(*colour)
    elif effect == 'accent':
        fx.accent()
    elif effect == 'custom':
        fx.advanced.fill(colour).draw()
    else:
        raise CliError('未知灯效 {0!r}\n\n可用灯效：\n{1}'.format(
            effect, EFFECT_HELP))


def cmd_zones(args) -> int:
    """Give each of a device's zones its own colour, in one go."""
    colours = [parse_colour(text) for text in args.colours]
    with open_manager(args) as manager:
        failures = 0
        for device in pick_devices(manager, args.device):
            names = device.colour_zones()
            if not names:
                print('{0}：没有可独立寻址的灯效区域'.format(device.name),
                      file=sys.stderr)
                failures += 1
                continue
            if not colours:
                print('{0}: {1}'.format(device.name, ', '.join(names)))
                continue
            wanted = colours * len(names) if len(colours) == 1 else colours
            if len(wanted) != len(names):
                print('{0} 有 {1} 个区域（{2}）；请给出同样数量的颜色，或只给一个'
                      .format(device.name, len(names), ', '.join(names)),
                      file=sys.stderr)
                failures += 1
                continue
            try:
                device.set_colour_zones(wanted)
            except (RpcError, NotSupported, DaemonUnavailable, ValueError) as error:
                print('{0}: {1}'.format(device.name, error), file=sys.stderr)
                failures += 1
                continue
            if not args.quiet:
                print('{0}: {1}'.format(device.name, ', '.join(
                    '{0}=#{1:02x}{2:02x}{3:02x}'.format(zone, *colour)
                    for zone, colour in zip(names, wanted))))
    return 1 if failures else 0


def cmd_profile(args) -> int:
    """Save, re-apply and remove named lighting profiles."""
    action = args.action
    if action in ('save', 'load', 'delete') and not args.name:
        raise CliError('"{0}" 需要配置文件名称'.format(action))

    with open_manager(args) as manager:
        failures = 0
        for device in pick_devices(manager, args.device):
            try:
                if action == 'list':
                    _print_profiles(device, args)
                    continue
                result = getattr(device, action + '_profile')(args.name)
            except (RpcError, NotSupported, DaemonUnavailable) as error:
                print('{0}: {1}'.format(device.name, error), file=sys.stderr)
                failures += 1
                continue
            if not args.quiet:
                verb = {'save': '已保存', 'load': '已应用',
                        'delete': '已删除'}[action]
                print('{0}：配置文件 {1!r} {2}'.format(
                    device.name, result.get('name', args.name), verb))
    return 1 if failures else 0


MACRO_HELP = """\
宏按键——BlackWidow 等键盘上的 M1-M5——在后台服务运行期间可用。
未绑定时，宏按键发送 F13-F17（M1-M5），任何接受热键的程序都能使用。
绑定后，它改为播放自己的宏：

  openrazer-win macro                              每把键盘绑定了什么
  openrazer-win macro set M1 --keys "ctrl+shift+esc"
  openrazer-win macro set M2 --text "Hello"
  openrazer-win macro set M3 --launch "C:\\Windows\\notepad.exe"
  openrazer-win macro set M4 --disable             让按键不执行任何操作
  openrazer-win macro record M5                    输入宏，然后按 Enter
  openrazer-win macro clear M1                     恢复为发送 F13

在键盘本身上，Fn+F9 可录制宏：按下它，按一个宏按键，输入内容，
再按一次 Fn+F9。macro watch 会打印原始按键报告，用于排查无响应的键盘。

鼠标侧键是 MOUSE4（后退）和 MOUSE5（前进），在后台服务运行期间于
本机重新分配。单键或快捷键会在按住按钮期间一直保持。Windows 不会说明
按钮来自哪只鼠标，因此在此鼠标连接期间，绑定会应用到每只鼠标的该按钮：

  openrazer-win macro set MOUSE4 --keys "ctrl+c" -d deathadder
  openrazer-win macro set MOUSE5 --button middle -d deathadder
  openrazer-win macro clear MOUSE4 -d deathadder   恢复后退和前进
"""


def _macro_client():
    try:
        return RpcClient(timeout=10.0)
    except DaemonUnavailable as error:
        raise CliError('{0}\n\n宏按键需要后台服务：  openrazer-win daemon start'.format(
            error)) from error


def _macro_devices(client, selector) -> list:
    devices = client.call('devices.list')
    if selector:
        needle = selector.lower()
        chosen = [d for d in devices if needle in (d['serial'].lower(), '{0:04x}'.format(d['pid']))
                  or needle in d['name'].lower()]
        if not chosen:
            raise CliError('没有匹配 {0!r} 的设备'.format(selector))
        return chosen
    listed = []
    for device in devices:
        info = client.call('macro.list', serial=device['serial'])
        if info['keys'] or info['bindings']:
            listed.append(device)
    if not listed:
        raise CliError('未连接带宏按键的键盘，也没有鼠标')
    return listed


def cmd_macro(args) -> int:
    from ..keys.codes import describe_event, parse_sequence

    if args.action == 'watch':
        return _macro_watch(args)
    client = _macro_client()
    try:
        devices = _macro_devices(client, args.device)
        if args.action == 'list':
            status = client.call('keys.status')
            for device in devices:
                info = client.call('macro.list', serial=device['serial'])
                state = '已接管' if info['handled'] else (
                    '未接管——按键处于空闲' if status['running'] else
                    '未接管（后台服务以 --no-keys 运行）')
                print('{0}  （{1}）'.format(device['name'], state))
                for key in info['keys'] or sorted(info['bindings']):
                    action = info['bindings'].get(key)
                    if action is None:
                        what = '默认'
                    elif action['type'] == 'keys':
                        downs = [describe_event(e) for e in action['events'] if e['down']]
                        what = '按键：{0}'.format(' '.join(downs[:12]) + (
                            ' ...' if len(downs) > 12 else ''))
                    elif action['type'] == 'text':
                        what = '文本：{0!r}'.format(action['text'])
                    elif action['type'] == 'launch':
                        what = '启动：{0}'.format(action['target'])
                    elif action['type'] == 'button':
                        what = '鼠标按键：{0}'.format(action['button'])
                    else:
                        what = '已禁用'
                    print('  {0:<6} {1}'.format(key, what))
            return 0

        if not args.key:
            raise CliError('"macro {0}" 需要一个按键，例如 M1，或鼠标的 MOUSE4 或 '
                           'MOUSE5'.format(args.action))
        key = args.key.upper()
        if len(devices) > 1:
            raise CliError('匹配到多个设备；请用 --device 选择一个')
        serial = devices[0]['serial']

        if args.action == 'clear':
            client.call('macro.delete', serial=serial, key=key)
            print('{0}：已恢复默认'.format(key))
            return 0
        if args.action == 'set':
            if args.keys:
                try:
                    action = {'type': 'keys', 'events': parse_sequence(args.keys)}
                except ValueError as error:
                    raise CliError(str(error)) from error
            elif args.text:
                action = {'type': 'text', 'text': args.text}
            elif args.launch:
                action = {'type': 'launch', 'target': args.launch}
            elif args.button:
                action = {'type': 'button', 'button': args.button}
            elif args.disable:
                action = {'type': 'disabled'}
            else:
                raise CliError('请说明按键的作用：--keys、--text、--launch、--button '
                               '或 --disable')
            client.call('macro.set', serial=serial, key=key, action=action)
            print('{0}：已绑定'.format(key))
            return 0
        if args.action == 'record':
            client.call('macro.record_start', serial=serial, key=key)
            print('正在录制 {0}：输入宏内容，然后在此处按 Enter。'.format(key))
            try:
                input()
            finally:
                saved = client.call('macro.record_stop')
            if saved:
                print('{0}：已录制 {1} 个按键事件'.format(key, len(saved['events'])))
            else:
                print('{0}：未录制任何内容；已恢复默认'.format(key))
            return 0
        if args.action == 'play':
            client.call('macro.play', serial=serial, key=key)
            return 0
    except RpcError as error:
        raise CliError(str(error)) from error
    finally:
        client.close()
    return 0


def _macro_watch(args) -> int:
    """Print every vendor key report, straight from the hardware."""
    import threading

    from ..hid import get_backend
    from ..keys.service import is_event_collection, parse_report
    from ..protocol.report import VENDOR_ID

    backend = get_backend()
    infos = [i for i in backend.enumerate(vendor_id=VENDOR_ID) if is_event_collection(i)]
    if args.device:
        infos = [i for i in infos if args.device.lower() in '{0:04x}'.format(i.product_id)]
    readers = []
    for info in infos:
        try:
            readers.append(backend.open_input(info))
            print('正在监听 {0}'.format(info.describe()))
        except (HidError, OSError) as error:
            print('无法读取 {0}：{1}'.format(info.describe(), error))
    if not readers:
        print('没有可读取的 Razer 按键集合。键盘插好了吗？')
        return 1
    print('请按宏按键和 Fn 组合键；按 Ctrl+C 停止。键盘只在驱动模式下报告这些'
          '按键，因此请保持后台服务运行。')
    stop = threading.Event()

    def follow(reader):
        while not stop.is_set():
            try:
                report = reader.read(0.5)
            except (HidError, OSError):
                return
            if report:
                codes = parse_report(report)
                print('{0:04x}  {1}  {2}'.format(
                    reader.info.product_id, report.hex(' '),
                    '' if codes is None else '按住：' + (
                        ' '.join('{0:02x}'.format(c) for c in sorted(codes)) or '无')))

    threads = [threading.Thread(target=follow, args=(r,), daemon=True) for r in readers]
    for thread in threads:
        thread.start()
    try:
        while True:
            stop.wait(1.0)
    except KeyboardInterrupt:
        stop.set()
    for reader in readers:
        reader.close()
    return 0


def _print_profiles(device, args) -> None:
    profiles = device.profiles
    if args.json:
        print(json.dumps({'device': device.name, 'profiles': profiles}, indent=2))
        return
    if not profiles:
        print('{0}：没有已保存的配置文件'.format(device.name))
        print('  保存一个：  openrazer-win profile save <name>')
        return
    print('{0}：'.format(device.name))
    width = max(len(entry['name']) for entry in profiles)
    for entry in profiles:
        print('  {0} {1:<{2}}  {3}  ({4})'.format(
            '*' if entry.get('active') else ' ', entry['name'], width, entry.get('saved') or '',
            ', '.join(entry.get('zones') or []) or '无区域'))


def cmd_brightness(args) -> int:
    with open_manager(args) as manager:
        for device in pick_devices(manager, args.device):
            if not args.device and not device.zones:
                continue        # nothing on it lights up
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
                print('{0}：亮度 {1}% 已应用到 {2}'.format(
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
                print('{0}：DPI {1}'.format(device.name, device.dpi))
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
                print('{0}：轮询率 {1} Hz'.format(device.name, device.poll_rate))
    return 0


def cmd_keyswitch(args) -> int:
    with open_manager(args) as manager:
        found = False
        for device in pick_devices(manager, args.device):
            if not device.has('keyswitch_optimization'):
                continue
            found = True
            if args.mode is None:
                print('{0}: {1}'.format(device.name, device.keyswitch_optimization))
                continue
            device.keyswitch_optimization = args.mode
            if not args.quiet:
                print('{0}：按键开关优化 {1}'.format(
                    device.name, device.keyswitch_optimization))
        if not found:
            # A mode that went nowhere must not look as if it was set.
            raise CliError('没有已连接的键盘支持按键开关优化')
    return 0


def cmd_battery(args) -> int:
    with open_manager(args) as manager:
        found = False
        for device in pick_devices(manager, args.device):
            if not device.has('battery'):
                continue
            found = True
            print('{0}：{1}%{2}'.format(
                device.name, device.battery_level,
                '（充电中）' if device.is_charging else ''))
        if not found:
            print('未连接电池供电的设备。')
    return 0


def _shown(enabled: bool) -> str:
    return '开 ' if enabled else '关'


def cmd_integration(args) -> int:
    try:
        client = RpcClient(timeout=10.0)
    except DaemonUnavailable as error:
        raise CliError('{0}\n\n集成需要后台服务：  openrazer-win daemon start'.format(
            error)) from error
    try:
        if args.name:
            if args.state is None:
                raise CliError('要开还是关？  openrazer-win integration {0} on'
                               .format(args.name))
            status = client.call('integration.set', name=args.name, enabled=args.state == 'on')
        else:
            status = client.call('integration.status')
    except RpcError as error:
        raise CliError(str(error)) from error
    finally:
        client.close()
    if args.json:
        print(json.dumps(status, indent=2))
        return 0
    accent = status['accent']
    colour = ('#{0:02X}{1:02X}{2:02X}'.format(*accent['colour'])
              if accent.get('colour') else '此处不可用')
    print('主题色      {0}   Windows 主题色 {1}'.format(_shown(accent['enabled']), colour))
    chroma = status['chroma']
    line = 'Chroma      {0}'.format(_shown(chroma['enabled']))
    if chroma.get('listening'):
        line += '   正在端口 {0} 上响应游戏'.format(chroma['port'])
    elif chroma.get('error'):
        line += '   {0}'.format(chroma['error'])
    print(line)
    for app in chroma.get('apps') or []:
        print('            {0}{1}'.format(
            app, '  （正在显示）' if app == chroma.get('showing') else ''))
    return 1 if chroma['enabled'] and not chroma.get('listening') else 0


def _snaptap_changes(args, status: dict) -> dict:
    """What ``snaptap.set`` should be told for this command line."""
    from ..keys.codes import canonical_name

    if args.action == 'pair':
        if len(args.items) != 2:
            raise CliError('openrazer-win snaptap pair add|remove KEY KEY')
        pairs = [list(pair) for pair in status['pairs']]
        if args.verb == 'add':
            return {'pairs': pairs + [args.items]}
        wanted = {canonical_name(item) for item in args.items}
        left = [pair for pair in pairs if set(pair) != wanted]
        if len(left) == len(pairs):
            raise CliError('没有这样的按键对：{0}'.format(' '.join(args.items)))
        return {'pairs': left}
    if len(args.items) != 1:
        raise CliError('openrazer-win snaptap {0} add|remove PROGRAM.exe'.format(args.action))
    programs = list(status[args.action])
    name = args.items[0].strip().lower()
    if args.verb == 'add':
        return {args.action: programs + [name]}
    if name not in programs:
        raise CliError('{0} 不在 {1} 列表中'.format(name, args.action))
    programs.remove(name)
    return {args.action: programs}


def cmd_snaptap(args) -> int:
    lists = args.action in ('pair', 'deny', 'allow')
    if lists != (args.verb is not None) or (args.items and not lists):
        raise CliError('openrazer-win snaptap [on|off | pair|deny|allow add|remove ...]')
    try:
        client = RpcClient(timeout=10.0)
    except DaemonUnavailable as error:
        raise CliError('{0}\n\nSnap Tap 需要后台服务：  openrazer-win daemon start'
                       .format(error)) from error
    try:
        status = client.call('snaptap.status')
        if args.action == 'on':
            changes = {'enabled': True}
            if not status['acknowledged']:
                from ..keys.snaptap import WARNING
                print(WARNING, file=sys.stderr)
                if not args.accept:
                    if not sys.stdin.isatty():
                        raise CliError('此处无法作答；读完后请加上 --accept')
                    if input('开启 Snap Tap？[y/N] ').strip().lower() not in ('y', 'yes'):
                        print('保持关闭。')
                        return 0
                changes['acknowledged'] = True
            status = client.call('snaptap.set', **changes)
        elif args.action == 'off':
            status = client.call('snaptap.set', enabled=False)
        elif lists:
            status = client.call('snaptap.set', **_snaptap_changes(args, status))
    except RpcError as error:
        raise CliError(str(error)) from error
    finally:
        client.close()
    if args.json:
        print(json.dumps(status, indent=2))
        return 0
    print('Snap Tap    {0}   对所有键盘生效'.format(_shown(status['enabled'])))
    if status['enabled']:
        print('            部分游戏禁止此功能。请在那些游戏中关闭。')
        if status['running']:
            state = '已暂停：{0}'.format(status['paused']) if status['paused'] else '工作中'
            print('状态        {0}  （前台：{1}）'.format(state, status['foreground'] or '未知'))
        else:
            print('状态        未生效：请查看后台服务日志')
    print('按键对      {0}'.format(', '.join(
        ' + '.join(key[4:] for key in pair) for pair in status['pairs']) or '无'))
    print('拒绝列表    {0}'.format(', '.join(status['deny']) or '无'))
    print('允许列表    {0}'.format(', '.join(status['allow']) or '所有未被拒绝的程序'))
    latency = status['latency_us']
    if latency['samples']:
        print('钩子        p50 {p50} us，p99 {p99} us，最大 {max} us，'
              '{samples} 个按键'.format(**latency))
    return 1 if status['enabled'] and not status['running'] else 0


def cmd_headset(args) -> int:
    with open_manager(args) as manager:
        headsets = [device for device in pick_devices(manager, args.device)
                    if device.has('audio')]
        if not headsets:
            print('未连接带声音设置的耳机。')
            return 1
        failures = 0
        for device in headsets:
            try:
                if args.setting == 'trace':
                    lines = device._call('get_trace')
                    print('{0}：最近 {1} 条请求与应答'.format(device.name, len(lines)))
                    for line in lines:
                        print('  ' + line)
                elif args.setting:
                    _set_headset(device, args.setting, args.value)
                else:
                    _show_headset(device)
            except (RpcError, NotSupported, DeviceError) as error:
                print('{0}: {1}'.format(device.name, error), file=sys.stderr)
                failures += 1
    return 1 if failures else 0


def _show_headset(device) -> None:
    audio = device.audio
    print(device.name)
    if not audio['connected']:
        print('  已关闭，或超出接收器范围')
        return
    print('  电量        {0}%{1}'.format(
        device.battery_level, '（充电中）' if device.is_charging else ''))
    # Not every headset has every setting: one it lacks is not in what it reports.
    if 'microphone_muted' in audio:
        print('  麦克风      {0}'.format('已静音' if audio['microphone_muted'] else '开'))
    if 'sidetone' in audio:
        print('  侧音        {0}'.format(
            '级别 {0}'.format(audio['sidetone_level']) if audio['sidetone'] else '关'))
    print('  均衡器      {0}'.format(
        audio['equalizer_preset'] if audio.get('equalizer', True) else '关'))
    per_preset = device.capabilities['audio'].get('equalizer_curve_per_preset')
    print('  {0}   {1}'.format('其曲线' if per_preset else '自定义均衡',
                             ' '.join(str(g) for g in audio['equalizer_bands'])))
    for key, label in (('do_not_disturb', '勿扰'),
                       ('microphone_noise_cancelling', '降噪'),
                       ('power_saving', '节能')):
        if key in audio:
            print('  {0}  {1}'.format(label, '开' if audio[key] else '关'))
    if 'dongle_led' in audio:
        print('  接收器      {0}'.format(audio['dongle_led']))
    print('  关闭时间    {0}'.format(
        '{0} 分钟后'.format(device.idle_time // 60) if device.idle_time else '永不'))


def _on_off(value: Optional[str]) -> bool:
    if value in ('on', 'off'):
        return value == 'on'
    raise CliError('请输入 on 或 off')


def _set_headset(device, setting: str, value: Optional[str]) -> None:
    if value is None:
        raise CliError('{0} 需要一个值；请见 openrazer-win headset --help'.format(setting))
    try:
        if setting == 'sidetone':
            if value not in ('on', 'off'):
                device.set_sidetone_level(int(value))
                value = 'on'
            device.set_sidetone(_on_off(value))
        elif setting == 'eq':
            if value in ('on', 'off'):
                device.set_equalizer(_on_off(value))
            elif ',' in value:
                device.set_equalizer_bands([int(gain) for gain in value.split(',')])
            else:
                device.set_equalizer_preset(value)
        elif setting == 'dnd':
            device.set_do_not_disturb(_on_off(value))
        elif setting == 'led':
            device.set_dongle_led(value)
        elif setting == 'nc':
            device.set_microphone_noise_cancelling(_on_off(value))
        elif setting == 'saving':
            device.set_power_saving(_on_off(value))
        elif setting == 'sleep':
            device.idle_time = int(value) * 60
    except ValueError as error:
        raise CliError('无法将 {0!r} 读取为数字'.format(value)) from error


def _byte_from_one(text: str) -> int:
    """A number 1-255, for argparse: the device would take 0 and 256 as something else."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError('{0!r} 不是数字'.format(text)) from None
    if not 1 <= value <= 255:
        raise argparse.ArgumentTypeError('{0} 超出 1-255 范围'.format(value))
    return value


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
    print('\n共 {0} 个设备。'.format(len(entries)))
    return 0


def cmd_daemon(args) -> int:
    if args.action == 'status':
        return _daemon_status(args)
    if args.action == 'stop':
        return 0 if stop_daemon(report=True) else 1
    if args.action == 'restart':
        stop_daemon(report=False)
        return _daemon_start(args)
    if args.action == 'run':
        from ..daemon import logs
        from ..daemon.server import serve
        logs.configure(getattr(args, 'verbose', False))
        serve(enable_effects=not args.no_effects,
              enable_keys=not getattr(args, 'no_keys', False))
        return 0
    return _daemon_start(args)


def stop_daemon(report: bool = True, timeout: float = 10.0) -> bool:
    """Ask the daemon to exit and wait until it has.  True if one was stopped."""
    try:
        client = RpcClient(timeout=5.0)
        client.call('daemon.shutdown')
        client.close()
    except DaemonUnavailable as error:
        # Clear the leftover endpoint file, or every later command reports
        # a connection failure against a daemon that is long gone.
        from ..daemon.protocol import Endpoint
        Endpoint.remove()
        if report:
            print('后台服务未运行（{0}）'.format(error))
        return False
    except RpcError as error:
        if report:
            print('后台服务拒绝了关闭请求（{0}）'.format(error))
        return False
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and is_daemon_running():
        time.sleep(0.2)
    if report:
        print('后台服务已停止')
    return True


def daemon_version_mismatch() -> Optional[str]:
    """The running daemon's version, if it is not this one.

    The daemon outlives upgrades: a new client talking to the old daemon gets
    the old daemon's bugs, which reads as the new release not fixing them.
    """
    try:
        client = RpcClient(timeout=3.0)
        info = client.call('daemon.version')
        client.close()
    except (DaemonUnavailable, RpcError, OSError):
        return None
    version = info.get('version')
    return version if version and version != __version__ else None


def _daemon_status(args) -> int:
    try:
        client = RpcClient(timeout=3.0)
        info = client.call('daemon.version')
        devices = client.call('devices.list')
        effects = client.call('effect.status')
        client.close()
    except (DaemonUnavailable, RpcError) as error:
        print('后台服务：未运行（{0}）'.format(error))
        return 1
    if args.json:
        print(json.dumps({'daemon': info, 'devices': devices, 'effects': effects},
                         indent=2))
        return 0
    print('后台服务  正在运行（pid {0}）'.format(info['pid']))
    print('版本      {0}  （openrazer {1}）'.format(info['version'], info['upstream']))
    if info['version'] != __version__:
        print('            不是当前版本（{0}）：openrazer-win daemon restart'.format(
            __version__))
    if info.get('state') == 'starting':
        print('状态      仍在查找设备')
    for name in info.get('pending') or []:
        print('等待      {0}（已找到，尚未响应）'.format(name))
    print('运行时间  {0}s'.format(info['uptime']))
    print('后端      {0}'.format(info['backend']))
    print('设备      {0}'.format(len(devices)))
    for device in devices:
        print('              {0}  {1}'.format(device['name'], device['serial']))
    active = effects.get('active') or {}
    print('灯效      {0}'.format(
        ', '.join('{0}={1}'.format(k, v) for k, v in active.items()) or '无活动灯效'))
    return 0


#: PyInstaller puts these in the environment of a running onefile app.  A child
#: that inherits them believes it is the same launch and reuses the parent's
#: extraction directory, so the parent cannot delete it on exit -- that is the
#: "Failed to remove temporary directory" warning.
_PYINSTALLER_ENV = ('_MEIPASS2', '_PYI_ARCHIVE_FILE', '_PYI_APPLICATION_HOME_DIR',
                    '_PYI_PARENT_PROCESS_LEVEL')


def daemon_command() -> list:
    """How to launch the daemon from wherever this code is running.

    A PyInstaller build has no importable ``-m`` target, so the frozen binary
    re-invokes itself.  Both frozen entry points accept CLI arguments, so this
    works from ``openrazer-win.exe`` and ``openrazer-win-gui.exe`` alike.
    """
    if getattr(sys, 'frozen', False):
        return [sys.executable, 'daemon', 'run']
    return [sys.executable, '-m', 'openrazer_win.daemon.main']


def child_environment() -> dict:
    """A copy of the environment safe to hand to a spawned daemon."""
    environment = dict(os.environ)
    for name in _PYINSTALLER_ENV:
        environment.pop(name, None)
    return environment


def daemon_creation_flags() -> int:
    """How to start the daemon so it outlives this console and shows nothing.

    A console of its own without a window, in a process group of its own --
    not ``DETACHED_PROCESS``, which leaves it no console at all.  The frozen
    daemon is a one-file build whose bootloader runs the real program as a
    child, and so is pip's ``openrazer-win.exe`` launcher; a detached parent
    left that child nothing to inherit, so Windows opened a new, visible
    console for it, and closing that window killed the daemon.
    """
    return (getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
            | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200))


def start_daemon_detached(no_effects: bool = False, timeout: float = 15.0,
                          no_keys: bool = False) -> bool:
    """Spawn the daemon so it outlives this process.  True once it answers.

    Shared with the GUI, which needs the same behaviour without an argparse
    namespace to hand over.
    """
    if is_daemon_running():
        return True
    command = daemon_command()
    if no_effects:
        command.append('--no-effects')
    if no_keys:
        command.append('--no-keys')
    creationflags = 0
    if sys.platform == 'win32':
        creationflags = daemon_creation_flags()
    subprocess.Popen(command, creationflags=creationflags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, close_fds=True,
                     env=child_environment())

    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_daemon_running():
            return True
        time.sleep(0.25)
    return False


def _daemon_start(args) -> int:
    from ..daemon import autostart
    try:
        refreshed = autostart.refresh()
    except autostart.AutostartError:
        refreshed = None
    if refreshed:
        print('开机自启指向的程序已不存在；现在为：\n  {0}'.format(
            refreshed))
    if is_daemon_running():
        print('后台服务已在运行')
        running = daemon_version_mismatch()
        if running:
            print('它是版本 {0}，当前版本是 {1}；请重启它以使用当前版本：\n'
                  '  openrazer-win daemon restart'.format(running, __version__))
        return 0
    if start_daemon_detached(getattr(args, 'no_effects', False),
                             no_keys=getattr(args, 'no_keys', False)):
        print('后台服务已启动')
        return 0
    from ..daemon.logs import log_path
    print('后台服务未能启动。其日志位于：\n  {0}\n'
          '或在前台运行：  openrazer-win daemon run'.format(log_path()),
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
                print('开机自启：关')
                print('启用：  openrazer-win autostart enable')
                return 1
            print('开机自启：开')
            print('命令：    {0}'.format(command))
            return 0

        if args.action == 'disable':
            if disable():
                print('开机自启已禁用')
            else:
                print('开机自启本来就是关的')
            return 0

        command = enable(args.no_effects)
        print('开机自启已启用；登录时将启动后台服务')
        print('命令：    {0}'.format(command))
        print('\n以后可用以下命令移除：  openrazer-win autostart disable')
        print('它也会出现在任务管理器的“启动应用”中。')
        return 0
    except AutostartError as error:
        raise CliError(str(error)) from error


def cmd_doctor(args) -> int:
    from ..hid import get_backend, is_supported_platform
    from ..protocol.report import VENDOR_ID
    from ..core.transport import FEATURE_BUFFER_SIZE

    print('openrazer-win {0}  （设备数据来自 openrazer {1}）'.format(
        __version__, UPSTREAM_VERSION))
    print('Python       {0}'.format(sys.version.split()[0]))
    print('平台        {0}'.format(sys.platform))
    if not is_supported_platform():
        print('\n这不是 Windows，因此只有模拟器后端可用。')

    database = get_database()
    print('数据库      {0} 个受支持的产品 ID'.format(len(database)))
    print('端点        {0}{1}'.format(
        endpoint_path(), '' if os.path.exists(endpoint_path()) else '  （缺失）'))
    from ..daemon.logs import log_path
    print('服务日志    {0}{1}'.format(
        log_path(), '' if os.path.exists(log_path()) else '  （缺失）'))
    print('后台服务    {0}'.format(
        '正在运行' if is_daemon_running() else '未运行'))

    try:
        backend = get_backend()
    except Exception as error:  # noqa: BLE001 - report, do not crash
        print('\nHID 后端不可用：{0}'.format(error))
        return 1

    collections = backend.enumerate(vendor_id=VENDOR_ID)
    print('\nRazer HID 集合（{0}）：'.format(len(collections)))
    if not collections:
        print('  无——设备插好了吗？')

    def reachable(info) -> bool:
        if info.product_id in blackshark_v3x.PRODUCT_IDS:
            # Two of its collections take 64-byte reports; one has the feature report.
            return is_blackshark_v3x_interface(info)
        if info.product_id in barracuda.PRODUCT_IDS:
            return is_barracuda_interface(info)
        return info.feature_length == FEATURE_BUFFER_SIZE or is_blackshark_interface(info)

    for info in collections:
        control = ' <- 控制接口' if reachable(info) else ''
        known = database.get(info.vendor_id, info.product_id)
        print('  {0}{1}'.format(info.describe(), control))
        print('      {0}'.format(known.name if known else '不在数据库中'))

    if collections and not any(reachable(info) for info in collections):
        print('\n没有集合提供 90 字节的特性报告。')
        print('Razer Synapse 可能占用了设备——请关闭它后重试。')

    _report_devices_beyond_hid(collections, database)
    _report_bluetooth_support()
    return 0


def _report_bluetooth_device(known) -> None:
    """What to say about a Razer device Windows has paired over Bluetooth."""
    from ..ble import is_available

    if known is not None and known.is_bluetooth:
        print('      仅支持蓝牙的设备——插线只会充电。'
              'openrazer-win 通过 Bluetooth LE 驱动其灯效。')
        if is_available():
            print('      已安装蓝牙支持。如果设备已开机但不在设备列表中，'
                  '请运行：openrazer-win list')
        else:
            print('      未安装蓝牙支持。请这样安装：')
            print('          pip install "openrazer-win[ble]"')
        return

    print('      已通过蓝牙配对。此型号的灯效通过 USB HID 驱动。')
    print('      请使用 USB 数据线连接；许多随附线缆只能供电。')


def _report_bluetooth_support() -> None:
    """Whether the Bluetooth path is usable, and what it can currently see."""
    from ..ble import is_available, scan
    from ..protocol import razer_ble

    if not is_available():
        print('\nBluetooth LE：未安装支持 '
              '（pip install "openrazer-win[ble]"）')
        return
    print('\nBluetooth LE：正在监听 Razer 广播…')
    try:
        advertisers = scan(seconds=4.0)
    except Exception as error:  # noqa: BLE001 - report, do not crash
        print('  扫描失败：{0}'.format(error))
        return
    if not advertisers:
        print('  没有设备广播——请打开设备并靠近本机')
        return
    database = get_database()
    from ..protocol.report import VENDOR_ID
    for advertiser in advertisers:
        known = (database.get(VENDOR_ID, advertiser.product_id)
                 if advertiser.product_id else None)
        if known is None and advertiser.name in razer_ble.ADVERTISED_NAMES:
            known = database.get(
                VENDOR_ID, razer_ble.ADVERTISED_NAMES[advertiser.name][0])
        print('  {0:012X}  {1:>4} dBm  {2}'.format(
            advertiser.address, advertiser.rssi,
            advertiser.name or '<无名称>'))
        if advertiser.product_id is not None:
            print('      广播产品 ID {0:04x}{1}'.format(
                advertiser.product_id,
                '  ({0})'.format(known.name) if known else
                '  —— 不在数据库中'))
        elif known is not None:
            print('      {0}'.format(known.name))
        else:
            print('      不是此移植版已知的 Razer 型号')


def _report_devices_beyond_hid(collections, database) -> None:
    """Explain Razer hardware Windows has, but not as a usable HID collection.

    A headset paired over Bluetooth, or plugged into a charge-only cable, is
    obviously connected from where the user is standing while being completely
    absent from the HID enumeration.  Saying what Windows *does* see it as
    turns a blank list into an answer.
    """
    from ..hid.devices import list_attached, summarise
    from ..protocol.report import VENDOR_ID

    seen_over_hid = {info.product_id for info in collections}
    others = [entry for entry in summarise(list_attached(vendor_id=VENDOR_ID))
              if entry['product_id'] not in seen_over_hid]
    if not others:
        return

    print('\nWindows 能看到的其他 Razer 设备（{0}）：'.format(len(others)))
    for entry in others:
        product_id = entry['product_id']
        known = database.get(VENDOR_ID, product_id) if product_id else None
        print('  {0:<11} {1}  {2}'.format(
            entry['transport'],
            '{0:04x}'.format(product_id) if product_id else '????',
            entry['name']))
        if entry['transport'].startswith('Bluetooth'):
            _report_bluetooth_device(known)
        elif known is not None:
            print('      已连接，但未提供灯效控制接口。')
        elif NON_LIGHTING_CLASSES.intersection(entry['classes']):
            print('      {0}——不是 openrazer-win 控制的设备。'.format(
                ', '.join(entry['classes'])))
        else:
            print('      不在数据库中。这个产品 ID 值得报告。')


# ---------------------------------------------------------------------------

#: argparse's own words, which it looks up through `argparse._`.
_ARGPARSE_TEXT = {
    'usage: ': '用法：',
    'positional arguments': '位置参数',
    'options': '选项',
    'optional arguments': '可选参数',
    'show this help message and exit': '显示此帮助信息并退出',
    "show program's version number and exit": '显示程序版本号并退出',
    '%(prog)s: error: %(message)s\n': '%(prog)s：错误：%(message)s\n',
}


def _localise_argparse() -> None:
    """Translate the words argparse adds to the help and to error messages."""
    original = argparse._
    if getattr(original, '_openrazer_localised', False):
        return

    def gettext(text):
        return _ARGPARSE_TEXT.get(text, original(text))

    gettext._openrazer_localised = True
    argparse._ = gettext


def build_parser() -> argparse.ArgumentParser:
    _localise_argparse()
    parser = argparse.ArgumentParser(
        prog='openrazer-win',
        description='在 Windows 上控制 Razer 设备。',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='灯效：\n' + EFFECT_HELP)
    parser.add_argument('--version', action='version',
                        version='openrazer-win {0} (openrazer {1})'.format(
                            __version__, UPSTREAM_VERSION))
    parser.add_argument('--direct', action='store_true',
                        help='直接与硬件通信，不经过后台服务')
    parser.add_argument('--json', action='store_true', help='机器可读输出')
    parser.add_argument('--quiet', '-q', action='store_true', help='不显示进度输出')
    subparsers = parser.add_subparsers(dest='command')

    def add(name, handler, help_text, **kwargs):
        sub = subparsers.add_parser(name, help=help_text, **kwargs)
        sub.set_defaults(handler=handler)
        return sub

    add('list', cmd_list, '列出已连接的设备')

    info = add('info', cmd_info, '显示设备的全部已知信息')
    info.add_argument('device', nargs='?', help='序列号、产品 ID 或名称片段')

    effect = add('effect', cmd_effect, '应用灯效',
                 formatter_class=argparse.RawDescriptionHelpFormatter,
                 epilog='灯效：\n' + EFFECT_HELP)
    effect.add_argument('effect')
    effect.add_argument('colour', nargs='?', help='名称、#rrggbb 或 r,g,b')
    effect.add_argument('--colour2', '--color2', dest='colour2')
    effect.add_argument('--colour3', '--color3', dest='colour3')
    effect.add_argument('--device', '-d')
    effect.add_argument('--zone', '-z')
    effect.add_argument('--speed', type=int, default=1, choices=(1, 2, 3, 4))
    effect.add_argument('--direction', type=int, default=1, choices=(0, 1, 2))
    effect.add_argument('--wave-speed', type=_byte_from_one, metavar='1-255',
                        help='实验性：设备自身波浪灯效的速度字节'
                             '（通常为 40），仅用于波浪灯效接受该参数的设备')

    zones = add('zones', cmd_zones,
                '为每个区域单独设置颜色（耳机的左右耳）',
                formatter_class=argparse.RawDescriptionHelpFormatter,
                epilog=ZONES_HELP)
    zones.add_argument('colours', nargs='*',
                       help='每个区域一个颜色，顺序与 zones 列出的一致')
    zones.add_argument('--device', '-d')

    profile = add('profile', cmd_profile,
                  '保存并重新应用命名的灯效配置文件',
                  formatter_class=argparse.RawDescriptionHelpFormatter,
                  epilog=PROFILE_HELP)
    profile.add_argument('action', nargs='?', default='list',
                         choices=('list', 'save', 'load', 'delete'))
    profile.add_argument('name', nargs='?')
    profile.add_argument('--device', '-d')

    macro = add('macro', cmd_macro, '绑定、录制和测试宏按键',
                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=MACRO_HELP)
    macro.add_argument('action', nargs='?', default='list',
                       choices=('list', 'set', 'clear', 'record', 'play', 'watch'))
    macro.add_argument('key', nargs='?', help='M1-M5、键盘上的其他宏按键，'
                       '或鼠标的 MOUSE4 / MOUSE5')
    macro.add_argument('--device', '-d')
    what = macro.add_mutually_exclusive_group()
    what.add_argument('--keys', help='要按下的键，例如 "ctrl+c" 或 "win+e, enter"')
    what.add_argument('--text', help='要输入的文本')
    what.add_argument('--launch', help='要打开的程序、文档或 URL')
    what.add_argument('--button', choices=('left', 'right', 'middle', 'back', 'forward'),
                      help='改为点击鼠标按键')
    what.add_argument('--disable', action='store_true', help='让按键不执行任何操作')

    brightness = add('brightness', cmd_brightness, '获取或设置亮度')
    brightness.add_argument('value', nargs='?', type=float)
    brightness.add_argument('--device', '-d')
    brightness.add_argument('--zone', '-z')

    dpi = add('dpi', cmd_dpi, '获取或设置鼠标 DPI')
    dpi.add_argument('x', nargs='?', type=int)
    dpi.add_argument('y', nargs='?', type=int)
    dpi.add_argument('--device', '-d')

    poll = add('poll-rate', cmd_poll_rate, '获取或设置轮询率（Hz）')
    poll.add_argument('rate', nargs='?', type=int)
    poll.add_argument('--device', '-d')

    keyswitch = add('keyswitch', cmd_keyswitch,
                    '获取或设置按键开关优化：typing 保留去抖延迟，'
                    'gaming 没有延迟')
    keyswitch.add_argument('mode', nargs='?', choices=('typing', 'gaming'))
    keyswitch.add_argument('--device', '-d')

    battery = add('battery', cmd_battery, '显示电量')
    battery.add_argument('--device', '-d')

    headset = add('headset', cmd_headset, '查看或更改耳机的声音设置',
                  formatter_class=argparse.RawDescriptionHelpFormatter,
                  epilog=HEADSET_HELP)
    headset.add_argument('setting', nargs='?',
                         choices=('sidetone', 'eq', 'dnd', 'led', 'nc', 'saving', 'sleep',
                                  'trace'))
    headset.add_argument('value', nargs='?')
    headset.add_argument('--device', '-d')

    integration = add('integration', cmd_integration,
                      '开/关集成：Windows 主题色、Chroma 游戏',
                      formatter_class=argparse.RawDescriptionHelpFormatter,
                      epilog=INTEGRATION_HELP)
    integration.add_argument('name', nargs='?', choices=('accent', 'chroma'))
    integration.add_argument('state', nargs='?', choices=('on', 'off'))
    integration.add_argument('--json', action='store_true')

    snaptap = add('snaptap', cmd_snaptap,
                  '在两个相对按键中，最后按下的生效；适用于所有键盘',
                  formatter_class=argparse.RawDescriptionHelpFormatter,
                  epilog=SNAPTAP_HELP)
    snaptap.add_argument('action', nargs='?', choices=('on', 'off', 'pair', 'deny', 'allow'))
    snaptap.add_argument('verb', nargs='?', choices=('add', 'remove'))
    snaptap.add_argument('items', nargs='*', metavar='item')
    snaptap.add_argument('--accept', action='store_true',
                         help='你已阅读首次执行 "on" 时显示的警告')
    snaptap.add_argument('--json', action='store_true')

    supported = add('supported', cmd_supported, '列出此移植版已知的所有设备')
    supported.add_argument('search', nargs='?', default='')

    daemon = add('daemon', cmd_daemon, '启动、停止或检查后台服务')
    daemon.add_argument('action', nargs='?', default='start',
                        choices=('start', 'stop', 'restart', 'status', 'run'))
    daemon.add_argument('--no-effects', action='store_true',
                        help='禁用主机渲染灯效和键盘钩子')
    daemon.add_argument('--no-keys', action='store_true',
                        help='让键盘保持普通模式：不使用宏按键，Fn 由键盘自身处理')
    daemon.add_argument('--verbose', '-v', action='store_true')

    autostart = add('autostart', cmd_autostart,
                    '登录时自动运行后台服务')
    autostart.add_argument('action', nargs='?', default='status',
                           choices=('enable', 'disable', 'status'))
    autostart.add_argument('--no-effects', action='store_true',
                           help='启动时不启用主机渲染灯效')

    add('doctor', cmd_doctor, '诊断设备检测问题')
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
        print('错误：{0}'.format(error), file=sys.stderr)
        return 2
    except (RpcError, NotSupported) as error:
        print('错误：{0}'.format(error), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
