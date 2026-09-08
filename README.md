<div align="center">

# OpenRazer for Windows

**Control your Razer keyboard, mouse and mousepad on Windows 10 / 11 — without Synapse, without a kernel driver, without a single third-party package.**

[![CI](https://github.com/Ar4ikov/openrazer-win/actions/workflows/ci.yml/badge.svg)](https://github.com/Ar4ikov/openrazer-win/actions/workflows/ci.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)
[![License: GPL-2.0-or-later](https://img.shields.io/badge/license-GPL--2.0--or--later-green)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/openrazer-win)](https://pypi.org/project/openrazer-win/)
[![Devices: 268](https://img.shields.io/badge/devices-268-orange)](https://ar4ikov.github.io/openrazer-win/#devices)

[Documentation](https://ar4ikov.github.io/openrazer-win/) ·
[Supported devices](https://ar4ikov.github.io/openrazer-win/#devices) ·
[Upstream OpenRazer](https://github.com/openrazer/openrazer)

</div>

---

[OpenRazer](https://github.com/openrazer/openrazer) is the free Razer driver for Linux: a kernel
module that speaks Razer's vendor USB protocol, plus a D-Bus daemon on top of it. Neither half runs
on Windows.

This is a full port. The protocol layer moves into user space on top of the Windows HID class driver,
D-Bus is replaced by a loopback JSON-RPC daemon, and **every device quirk is transpiled directly from
the upstream C sources** — so all 267 devices OpenRazer knows about behave the same way here as they
do on Linux, plus one Bluetooth headset upstream cannot reach at all.

```console
$ openrazer-win list
Razer BlackWidow Chroma  keyboard   PM1523E01801234     v1.10
Razer Viper              mouse      PM1948H09500666     v1.3

$ openrazer-win effect breath_dual "#ff0080" --colour2 cyan
Razer BlackWidow Chroma: breath_dual on backlight

$ openrazer-win dpi 3200
Razer Viper: dpi (3200, 3200)
```

## Why this exists

| | Synapse | openrazer-win |
|---|---|---|
| Account required | Yes | No |
| Runs in the background | ~200 MB, several services | ~25 MB, one process you start |
| Telemetry | Yes | None — no network access at all |
| Scriptable | No | CLI + Python API + JSON-RPC |
| Works offline / on a locked-down machine | Partially | Fully |
| Source available | No | GPL-2.0-or-later |

## Install

```bash
pip install openrazer-win
```

There is no dependency list to speak of — the port runs on the Python standard library alone. No
compiler, no `hidapi` wheel, no driver signing, no administrator rights, no reboot.

One optional extra exists, for Bluetooth-only devices such as the Kraken Kitty V2 BT:

```bash
pip install "openrazer-win[ble]"
```

That pulls in PyWinRT, because Windows exposes Bluetooth LE through WinRT. Everything on the USB
side keeps working without it.

Prefer a single file? Grab `openrazer-win.exe` from the
[latest release](https://github.com/Ar4ikov/openrazer-win/releases/latest) and run it directly; it
needs no Python at all.

> **Close Razer Synapse before using this.** Synapse holds the device open and will fight you for
> control of the LEDs. `openrazer-win doctor` tells you if that is happening.

## Quick start

### Command line

```bash
openrazer-win daemon start          # start the background service
openrazer-win list                  # what is plugged in
openrazer-win info                  # everything about it

openrazer-win effect static red
openrazer-win effect spectrum
openrazer-win effect wave --direction 2
openrazer-win effect breath_dual "#ff0080" --colour2 "#00ffff"
openrazer-win effect ripple green   # host-rendered, follows your typing

openrazer-win zones                  # list a device's zones
openrazer-win zones red blue        # one colour each: left ear red, right ear blue

openrazer-win profile save night    # remember what is showing now
openrazer-win profile               # list saved profiles
openrazer-win profile load night    # put it back

openrazer-win brightness 60
openrazer-win dpi 1800
openrazer-win poll-rate 1000
openrazer-win battery
```

Every command takes `--device` (serial, product id or a fragment of the name), `--zone`
(`backlight`, `logo`, `scroll`, `left`, `right`, …) and `--json`.

To have the daemon come up with Windows:

```bash
openrazer-win autostart enable      # per-user, no admin rights
openrazer-win autostart status
openrazer-win autostart disable
```

This writes one entry under `HKCU\...\CurrentVersion\Run`, so it also appears in Task Manager's
Startup tab and can be removed from there.

### Python

The API mirrors upstream's `openrazer.client`, so scripts written for Linux usually port by changing
one import:

```python
from openrazer_win.client import DeviceManager

for device in DeviceManager().devices:
    print(device.name, device.serial, device.firmware_version)

    device.fx.static(0, 255, 0)
    device.brightness = 75

    if device.has('dpi'):
        device.dpi = (1800, 1800)
```

Per-key lighting works the same way as upstream's `fx.advanced`:

```python
keyboard = DeviceManager().by_name('BlackWidow')[0]
matrix = keyboard.fx.advanced
for row in range(matrix.rows):
    for column in range(matrix.columns):
        matrix[row, column] = (255, 0, 128)
matrix.draw()
```

Zones can be painted individually, and a device that can take them all in one message does so, so
the two never disagree even for an instant:

```python
headset = DeviceManager().by_name('Kraken Kitty V2 BT')[0]
headset.colour_zones()                          # ['left', 'right']
headset.set_colour_zones([(255, 0, 0), (0, 0, 255)])
headset.fx_for('left').static(0, 255, 0)        # or one ear at a time
```

Profiles are named snapshots of a device's lighting, kept on the PC — up to ten per device, in
`%LOCALAPPDATA%\openrazer-win\profiles.json`:

```python
device.save_profile('night')
device.profiles                     # [{'name': 'night', 'saved': ..., 'zones': [...]}]
device.load_profile('night')
```

No daemon running? Pass `direct=True` and the library opens the HID handles itself:

```python
DeviceManager(direct=True)
```

### Graphical control panel

```bash
openrazer-win-gui
```

A small Tk window — device list, zone and effect pickers, colour swatches, brightness, DPI and
polling rate. Tk ships with Python on Windows, so this needs no extra install either.

## How it works

The Linux stack is a kernel module plus a D-Bus daemon. Neither is available on Windows, so each
layer has a direct replacement:

```
                Linux                                   Windows
    ┌────────────────────────────┐        ┌────────────────────────────────┐
    │ openrazer.client (D-Bus)   │        │ openrazer_win.client (JSON-RPC)│
    ├────────────────────────────┤        ├────────────────────────────────┤
    │ openrazer-daemon           │        │ openrazer_win.daemon           │
    │   D-Bus session bus        │        │   loopback TCP + bearer token  │
    ├────────────────────────────┤        ├────────────────────────────────┤
    │ razer*.ko  (kernel module) │        │ openrazer_win.devices          │
    │   sysfs attributes         │        │   recipes transpiled from the  │
    │   usb_control_msg()        │        │   same C sources               │
    ├────────────────────────────┤        ├────────────────────────────────┤
    │ usbhid / usbcore           │        │ hid.dll + setupapi.dll (ctypes)│
    └────────────────────────────┘        └────────────────────────────────┘
```

**The protocol.** Every Razer peripheral speaks the same vendor protocol: a fixed 90-byte structure
delivered as HID feature report `0x00`, with an XOR checksum over bytes 2–87.
`openrazer_win/protocol/` is a line-by-line port of `razercommon.c` and `razerchromacommon.c`.

**Reaching the device.** The kernel driver sends `usb_control_msg` to a specific USB interface.
Windows exposes each interface as its own HID collection, so the port opens the one whose path
carries the matching `&mi_XX` *and* whose report descriptor declares a 90-byte feature report. When
Windows denies read/write access to a keyboard or mouse collection, the port falls back to a
zero-access handle — feature-report IOCTLs are `FILE_ANY_ACCESS`, so they still work.

**The device quirks — the interesting part.** OpenRazer's drivers encode ~20 000 lines of
per-device behaviour as `switch (device->usb_pid)` statements: which report builder to call, which
LED id, which transaction id. Hand-porting that would be a guaranteed source of drift.

Instead, [`tools/transpile_recipes.py`](tools/transpile_recipes.py) parses the C, folds each switch
against every known product id, and emits a small program per (attribute, device):

```json
[{"op": "build", "fn": "razer_chroma_extended_matrix_effect_static",
  "args": [{"k": "const", "value": 1}, {"k": "var", "name": "led_id"},
           {"k": "rgb", "offset": 0}]},
 {"op": "txid", "value": {"k": "const", "value": 63}},
 {"op": "send"}]
```

565 distinct recipes cover all 267 devices. `openrazer_win/devices/recipes.py` interprets them at
runtime. Tracking a new upstream release means re-running the transpiler, not rewriting Python.

**The device database** comes from the same place: `tools/extract_device_db.py` imports upstream's
`openrazer_daemon.hardware` package with the Linux-only dependencies stubbed out, and dumps every
device class to JSON — names, matrix dimensions, DPI limits, and the exact method list each device
advertises. Capability reporting intersects that list with the recipe table, so a mouse never claims
to have a backlight it does not have.

## What is supported

| Category | Devices |
|---|---:|
| Mice | 113 |
| Keyboards & laptops | 112 |
| Accessories (docks, stands, ARGB controllers) | 17 |
| Headsets (Kraken protocol) | 8 |
| Headsets (Bluetooth LE) | 1 |
| Mousepads | 8 |
| Keypads | 7 |
| eGPU enclosures | 2 |
| **Total** | **268** |

166 have addressable matrices; 76 report battery level. The full searchable list is
[on the documentation site](https://ar4ikov.github.io/openrazer-win/#devices), or run
`openrazer-win supported`.

**Profiles:** up to ten named lighting snapshots per device, saved on the PC and re-applied on
demand — `openrazer-win profile save night`, `profile load night`.

**Features:** all hardware effects (static, spectrum, wave, wheel, reactive, blinking, breathing ×3,
starlight ×3), per-key custom frames, per-zone brightness, DPI and DPI stages, polling rate up to
8000 Hz, battery level and charging state, idle timeout, low-battery threshold, game mode, macro LED,
scroll mode and acceleration, keyboard layout, addressable-RGB channels, a host-rendered ripple
effect driven by a low-level keyboard hook, and autostart at logon.

### Bluetooth

One device in the range has no USB data mode at all: the **Kraken Kitty V2 BT**. Plugging it in
charges it and nothing more, so upstream has no driver for it — there is no USB device for a kernel
module to bind to.

Its lighting protocol was recovered for this port by capturing what Razer Synapse sends over the
air. It turns out to be a vendor GATT service whose UUID spells `Amel-RazerBLE`, driven by write
commands of the form `c4 00 06` followed by one RGB triple per ear, left first. Setting pure red,
green and blue in Synapse produced exactly those bytes, and a control capture with Synapse closed
produced no writes at all. See
[`openrazer_win/protocol/razer_ble.py`](openrazer_win/protocol/razer_ble.py).

The device itself only knows a static colour per zone. Synapse's breathing, spectrum and
audio-reactive modes are drawn by the host, one frame at a time — which is why closing Synapse stops
the traffic dead — so this port renders them the same way, through its own effect engine:
`effect spectrum` and `effect wave` fall back to the frame-by-frame renderer on any device whose
firmware has no such mode, and say so when they do.

Brightness, battery level and charging state work over Bluetooth too: the vendor service answers
requests on a notify characteristic, since nothing in it can be read directly. Brightness the device
*does* keep across a power cycle — unlike the colour.

**The headset does not store the colour you set.** It shows what it was last told for as long as
the Bluetooth link is up, and reverts to the colour saved in it — whatever Synapse last wrote — the
moment that link goes. So a colour set here is *held*: re-asserted every couple of seconds for as
long as the daemon runs, which is the same thing Synapse does. A command that writes the *stored* colour does exist — this
headset shipped showing white and has shown Razer green ever since Synapse first set it up — but it
appeared in none of the traffic captured here, which points at the one-off setup Synapse performs on
install. It is not implemented and not guessed at: the device answers an unknown opcode with silence
rather than an error, so guessing would be blind, on the channel where firmware pushes live. Profiles
are the practical answer instead.

Three facts about the link, all confirmed on the hardware, decide how it is managed. Resolving the
characteristic costs about 90 ms, most of that service discovery, which no amount of frame streaming
survives — held open, a write costs about 1 ms. A *connected* BLE device stops advertising
altogether, so discovery keeps a device it is connected to even when a sweep hears nothing, and a
link with nothing left to hold is given up after five seconds so the device becomes visible again.
One consequence worth knowing: while the daemon is holding a device, a `--direct` client will not
find it, because there is no advertisement to find.

Devices are found by listening for advertisements rather than by pairing: a dual-mode headset need
not use its classic Bluetooth address on the LE side (this one advertises one byte away from it),
and Windows creates no device node for an unpaired LE connection. Because a sweep takes a few
seconds, the radio is swept far less often than the USB bus, and a missed advertisement is not
treated as an unplugged device.

The Kraken headsets are covered too, through their own protocol: rather than the 90-byte control
report, they expose the lighting controller's RAM, so colours are written to fixed addresses and a
one-byte selector picks the effect. Reading state back from a Kraken is not supported -- the kernel
collects that from unsolicited HID input reports -- so the daemon serves those values from its
persistence file, as upstream's daemon does for devices that cannot report.

**Not ported:** macro recording and playback, which upstream implements on Linux input events.

## Troubleshooting

Start with:

```bash
openrazer-win doctor
```

It lists every Razer HID collection Windows can see, marks the control interface, and says whether
each product id is in the database.

| Symptom | Cause |
|---|---|
| `no Razer control interface` | Synapse is running, or another program holds the device. Close it. |
| Device listed as `NOT IN DATABASE` | Newer than the bundled data. [Open an issue](https://github.com/Ar4ikov/openrazer-win/issues) with the product id. |
| `device replied not supported` | The firmware refused that command; the device genuinely lacks the feature. |
| Effects reset after sleep | Windows cuts USB power. The daemon replays the stored state when the device reappears. |
| Device is connected but nothing is listed | It may be on a charge-only cable, or be a Bluetooth-only model. `doctor` names what Windows sees it as, and says which of the two it is. |
| A Bluetooth device is powered on but not listed | Install the extra: `pip install "openrazer-win[ble]"`. `doctor` ends with a Bluetooth section that says whether support is present and what the radio can hear. |
| Nothing at all listed | Only Razer devices (vendor `1532`) are handled. |

## Development

```bash
git clone https://github.com/Ar4ikov/openrazer-win
cd openrazer-win
pip install -e ".[dev]"

pytest                              # 315 tests, no hardware needed
ruff check .
python tools/simulate_devices.py    # every capability of all 267 devices
```

The test suite runs against a built-in device emulator that validates CRCs and answers the protocol,
so the whole stack — transport, recipes, daemon, client, CLI — is exercised without plugging anything
in. `--emulate` does the same for the daemon:

```bash
openrazer-win-daemon --emulate
```

### Re-syncing with upstream

```bash
git clone --depth 1 https://github.com/openrazer/openrazer /tmp/openrazer
python tools/extract_device_db.py  /tmp/openrazer openrazer_win/devices/data/devices.json
python tools/transpile_recipes.py  /tmp/openrazer openrazer_win/devices/data/recipes.json
pytest && python tools/simulate_devices.py
```

New devices and changed quirks are picked up automatically.

## Credits

All protocol knowledge here is [OpenRazer](https://github.com/openrazer/openrazer)'s work —
years of reverse engineering by Terri Cain, Tim Theede and a long list of contributors. This project
adds a Windows transport and a translation layer; it does not add a single byte of new protocol
research.

Licensed **GPL-2.0-or-later**, the same as upstream, because the device database and the transpiled
recipes are derived from OpenRazer's GPL sources.

Not affiliated with or endorsed by Razer Inc. Razer, Chroma and Synapse are trademarks of Razer Inc.
