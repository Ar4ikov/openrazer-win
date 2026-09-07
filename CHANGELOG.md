# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [semantic versioning](https://semver.org/).

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
  without hardware. 160 tests plus a fleet simulation covering every capability of all 267 devices.
- **Autostart** via the per-user `Run` key (`openrazer-win autostart enable`), so the daemon comes
  up at logon without administrator rights or a Windows service.
- Standalone `openrazer-win.exe` and `openrazer-win-gui.exe` builds.

### Known limitations

- Macro recording and playback are not ported; upstream implements them on Linux input events.
- The Kraken headset family uses a separate protocol that is not covered.
- Razer Synapse must be closed: it holds the device open and competes for the LEDs.

[1.0.1]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.0.1
[1.0.0]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.0.0
