# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [semantic versioning](https://semver.org/).

## [1.2.1] - 2026-09-08

### Fixed

- **A Bluetooth device disappeared from the device list shortly after being
  used, and its lighting was then left being driven by something unreachable.**
  Reported from 1.2.0 as colours resetting and then the lighting going dark,
  flickering occasionally.

  The cause: a BLE peripheral stops advertising while it has a link up --
  verified on the hardware, a scan during a live connection hears nothing at
  all. 1.2.0 held the link open after every write, for the sake of frame
  streaming, so the device went silent; the next sweep concluded it had gone
  and dropped it. That was self-perpetuating, because whatever held the link
  kept it up, so the device never advertised again and was never rediscovered.
  Four separate faults came out of it, all fixed:

  - An idle link is now given up after five seconds, so a device at rest goes
    back to advertising and stays visible both to this port and to everything
    else on the machine. A link in active use is kept, so effects still stream
    at about 1 ms a frame.
  - Discovery no longer takes silence as proof of absence: a device it is
    connected to is kept in the list even when a sweep hears nothing.
  - A host-rendered effect no longer outlives its device. It used to keep
    writing to hardware that was no longer listed -- and because it was not
    listed, `effect none` answered "no supported Razer devices found", so there
    was no way to stop it short of killing the daemon.
  - `ripple` and `ripple_random` are refused on a device with fewer than eight
    addressable cells. A ripple spreads outward from the key that was pressed,
    so on the headset's two ears every press covered the whole thing at once --
    a blink, not a ripple -- and that is what was driving the lighting dark and
    flickering. Clock-driven effects like `spectrum` and `wave`, which do work
    across two zones, are unaffected.

  If a daemon is stuck in this state after 1.2.0, `openrazer-win daemon stop`
  releases the device; 1.2.1 does not get into it.

## [1.2.0] - 2026-09-08

### Added

- **The Kraken Kitty V2 BT, over Bluetooth LE.** This headset has no USB data
  mode at all -- plugging it in only charges it -- so there is no USB device
  for a kernel module to bind to, and upstream has never covered it. Its
  lighting protocol was recovered for this port from a Bluetooth HCI capture of
  Razer Synapse: a vendor GATT service whose UUID spells `Amel-RazerBLE`, taking
  write commands of the form `c4 00 06` followed by one RGB triple per ear.
  Setting pure red, green and blue in Synapse produced exactly those bytes, and
  a control capture with Synapse closed produced no writes at all. Confirmed on
  the hardware, including which triple is which ear.

  Only what was observed is implemented. The device itself knows a static
  colour per zone; Synapse's breathing, spectrum and audio-reactive modes are
  drawn by the host one frame at a time -- which is why closing Synapse stops
  the traffic dead -- so this port renders them through its own effect engine
  instead of pretending the hardware has them.

- **Per-zone colours, in a single write.** `openrazer-win zones red blue` gives
  each zone its own colour -- on the headset, one per ear, left first. Two
  separate writes would light one ear before the other, visibly, so devices
  that can take every zone in one message do. The Python API gains
  `device.colour_zones()` and `device.set_colour_zones([...])`, and the GUI's
  existing zone picker covers the ears with no change. `--zone left` also works
  on `effect`, and `--zone` defaults to both ears.

- **Effects the hardware lacks are drawn by the host.** `effect spectrum` and
  `effect wave` now fall back to the frame-by-frame renderer on a device whose
  firmware has no such mode, and say so. This is how Synapse does it too: the
  Bluetooth headset only knows a static colour per ear, and everything moving is
  streamed to it. The renderers existed already but nothing reached them; the
  Python API exposes them as `fx.spectrum_soft()` and `fx.wave_soft()`.

- **An optional `[ble]` extra.** Windows exposes Bluetooth LE through WinRT, so
  the Bluetooth path needs PyWinRT: `pip install "openrazer-win[ble]"`. The USB
  side keeps its no-dependency promise, and `doctor` says which state you are
  in.

### Changed

- **Bluetooth devices are found by advertisement, not by pairing.** A dual-mode
  headset need not use its classic Bluetooth address on the LE side -- this one
  advertises one byte away from it -- and Windows creates no device node for an
  unpaired LE connection, so scanning is the only reliable way in. Because a
  sweep takes seconds, the radio is swept on a much lazier schedule than the
  hot-plug poll, and a missed advertisement is not treated as an unplugged
  device.

  Devices are identified by the product id Razer puts in its manufacturer data,
  which also carries the classic Bluetooth address. That matters because a
  device splits what it says across packets: this headset sends its local name
  in one and its manufacturer data in another, so filtering per packet on the
  company id threw the name away and left the device unidentifiable. The local
  name is now only a fallback.

- **`doctor` no longer says Bluetooth is out of reach.** For a device the
  database marks Bluetooth-only it now says so, says that plugging it in only
  charges it, and either confirms the support is installed or gives the command
  to install it -- rather than advising a USB data cable that does not exist for
  that model. The report ends with what the radio can currently hear.

- **A Bluetooth connection is held open rather than rebuilt per write.**
  Resolving the characteristic costs about 90 ms, most of it GATT service
  discovery, which no amount of frame streaming survives; held open, a write
  costs about 1 ms. The link is re-resolved once, transparently, if the device
  drops it, so a headset that went to sleep recovers by itself. All Bluetooth
  work now runs on one long-lived event loop thread, so a held connection is
  never owned by an event loop that has already been torn down.

- Devices this port adds on top of upstream now live in the extractor rather
  than being hand-written into the generated database, so re-running it against
  a newer OpenRazer cannot silently drop them.

## [1.1.2] - 2026-09-08

### Changed

- **`doctor` now explains devices it cannot reach.** A Razer device connected
  over Bluetooth, or plugged into a charge-only cable, is obviously attached
  from the user's point of view but is completely absent from the HID
  enumeration, so the report was an unhelpful "none -- is the device plugged
  in?". It now also lists what Windows *does* see the device as, and says why
  that form is not controllable: this port drives lighting over USB HID, and
  many bundled cables carry power only. Where a headset also exposes a
  Bluetooth serial port -- the proprietary channel vendor software uses to
  reach it wirelessly -- the report says so, rather than claiming Bluetooth is
  audio-only. Devices that are plainly not lighting hardware, like a webcam,
  are named as such rather than reported as gaps in the database.

## [1.1.0] - 2026-09-08

### Added

- **Kraken headsets.** The eight Kraken models speak a different protocol from
  the rest of the range: rather than the 90-byte control report, they expose
  the lighting controller's RAM over 37-byte HID output reports on interface 3.
  Colours are written to fixed addresses and a one-byte selector picks the
  effect. Ported from `razerkraken_driver.c` by hand -- unlike the other three
  drivers it has no product-id switch tables to transpile -- covering off,
  static, spectrum, custom and one-, two- and three-colour breathing, with the
  RAM layout and the available effects following the controller generation.

  Reading state back from a Kraken is not supported: the kernel collects it
  from unsolicited HID input reports, so the daemon serves those values from
  its persistence file instead.

### Changed

- Host-rendered effects are offered only where they can be seen. A mouse with a
  single addressable LED reports a 1x1 matrix, and the CLI and GUI were both
  listing ripple for it.

## [1.0.1] - 2026-09-08

Fixes for the shipped executables. Neither problem could happen when running
from source, which is why 1.0.0 went out with them.

### Fixed

- **The GUI asked to start the daemon in a loop.** `openrazer-win-gui.exe`
  ignored its command line, so when the control panel offered to start the
  daemon and relaunched itself, the new process opened a second control panel --
  which offered again, and again. The frozen GUI entry point now dispatches to
  the CLI whenever it is given arguments, and only opens a window when it is
  not. The panel also reports failure in a dialog with the path to the log,
  instead of leaving an empty device list.
- **`Failed to remove temporary directory` after `daemon start`.** A onefile
  child inherits `_MEIPASS2` from its parent and therefore reuses the parent's
  extraction directory, which the parent then cannot delete. Those variables
  are now stripped from the spawned daemon's environment.
- **A detached daemon had no way to report why it failed.** Its output went to
  `DEVNULL`, and a windowed build has no `sys.stderr` at all -- attaching a log
  handler to `None` would have broken logging itself. The daemon now keeps a
  bounded rotating log at `%LOCALAPPDATA%\openrazer-win\daemon.log`, which
  `doctor` and the failure messages point at.

## [1.0.0] - 2026-09-07

First release. A complete port of OpenRazer to Windows 10 and 11 (amd64).

### Added

- **User-space HID transport** built on `hid.dll` and `setupapi.dll` through ctypes, replacing the
  Linux kernel module. Selects the control interface by matching the USB interface number the
  driver addresses *and* the 90-byte feature report length, and falls back to a zero-access handle
  when Windows reserves read/write on a keyboard or mouse collection.
- **Protocol layer** ported line by line from `razercommon.c` and `razerchromacommon.c`: the
  90-byte report, its XOR checksum, and 92 report builders.
- **Transpiled device quirks.** `tools/transpile_recipes.py` parses OpenRazer's C drivers, folds
  every `switch (device->usb_pid)` against each known product id, and emits 565 recipes covering
  all 267 devices. A runtime interpreter executes them, so tracking upstream is a regeneration
  rather than a rewrite.
- **Device database** extracted from upstream's `openrazer_daemon.hardware` package: names, types,
  matrix dimensions, DPI limits and per-device method lists.
- **Daemon** serving newline-delimited JSON-RPC 2.0 over loopback TCP, authenticated with a random
  bearer token in an owner-only file. The listening socket takes `SO_EXCLUSIVEADDRUSE` so no other
  local process can hijack it. Watches for hot-plug, restores persisted lighting state, and exposes
  a fixed method allowlist.
- **Client library** mirroring upstream's `openrazer.client`, with an in-process `direct=True` mode
  for scripts that do not want a daemon.
- **Command line** (`openrazer-win`) covering devices, effects, brightness, DPI, DPI stages,
  polling rate, battery, the daemon lifecycle, and a `doctor` command that diagnoses detection
  problems.
- **Tk control panel** (`openrazer-win-gui`), stdlib-only.
- **Host-rendered ripple** driven by a `WH_KEYBOARD_LL` hook that observes key presses without
  swallowing or synthesising input.
- **Device emulator** that validates CRCs and answers the protocol, so the entire stack is testable
  without hardware. 208 tests plus a fleet simulation covering every capability of all 267 devices.
- **Autostart** via the per-user `Run` key (`openrazer-win autostart enable`), so the daemon comes
  up at logon without administrator rights or a Windows service.
- Standalone `openrazer-win.exe` and `openrazer-win-gui.exe` builds.

### Known limitations

- Macro recording and playback are not ported; upstream implements them on Linux input events.
- The Kraken headset family uses a separate protocol that is not covered.
- Razer Synapse must be closed: it holds the device open and competes for the LEDs.

[1.1.2]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.1.2
[1.1.0]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.1.0
[1.0.1]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.0.1
[1.0.0]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.0.0
