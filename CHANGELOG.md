# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [semantic versioning](https://semver.org/).

## [1.4.2] - 2026-10-27

### Fixed

- **The daemon opened a console window, and closing it killed the
  daemon.** The panel started the daemon detached from any console; the
  frozen daemon runs its real program as a child, which then found no
  console to inherit and was given a new, visible one. It now starts with a
  console that has no window, so nothing shows. `openrazer-win daemon
  start` had the same problem and the same fix.
- **A headset ear that had been turned off could not be lit again.** The
  panel blanked the ear's effect a moment after one was chosen, taking the
  colour picker with it, and "Static" lit the ear in its stored colour --
  black. An ear now reads as lit or dark from its colour, "Both ears" shows
  the colour of whichever ear is lit, and choosing an effect replaces black
  colours with defaults.
- Lighting zones were listed back to front.

### Added

- The Kraken Kitty V2 BT has a picture, in the colour it is: *About* lets
  you pick Quartz, White or Black, since the headset does not say which it
  is. Photos on a dark backdrop are cut out to sit cleanly in the panel.

## [1.4.1] - 2026-10-27

### Fixed

- **The control panel could not start the daemon on a machine that had
  only the control panel**, and pointed at a log that did not exist.
  Reported on a laptop with Python 3.14: the panel found that Python on
  `PATH` and ran the daemon in it, but openrazer-win was not installed
  there, so it died on its first import -- before it could create even the
  folder its log goes in.

  - The control panel now **carries the daemon inside it**, so the one
    download is all there is. It is unpacked once per version to
    `%LOCALAPPDATA%\openrazer-win\bin`.
  - A Python is only used if it really runs openrazer-win; each one found
    is asked, rather than trusted for being called `python`.
  - Each way of running the daemon is tried in turn, and what it printed is
    kept in `daemon-start.log` and shown in the panel when none comes up.
  - With no daemon anywhere, the panel offers to set one up: it downloads
    the Python 3.13 installer from python.org and opens it, then installs
    openrazer-win into it with pip -- or into a Python already there.

- The daemon no longer copies its whole log into a `stderr` that is a file.

### Changed

- Tested on Python 3.14 as well.

## [1.4.0] - 2026-10-27

### Added

- **A new control panel, written in Rust.** `openrazer-win-gui.exe` in the
  release is now a native Windows app rather than the Tk window:

  - a device list with product pictures, and per device **Lighting** --
    every effect as a tile with a preview, a colour picker with presets and
    hex, speed, direction and brightness, live effects beside the device's
    own -- **Performance** (DPI with presets, DPI stages, polling rate),
    **Power** (battery, sleep timer, low-battery warning), **Macro keys**
    (bind, record, try out), **Profiles** and the device's details;
  - **Settings** shows the daemon's state -- version, devices still being
    set up, whether macro keys are handled -- with start, stop and restart,
    and warns when the daemon is from another version;
  - it starts the daemon itself, without a console window, from beside it,
    from `PATH` or as a Python module, so the two release files in one
    folder are all there is to it;
  - **Start with Windows** brings it up in the tray at sign-in, keeping the
    daemon running, and closing the window leaves it there. A second launch
    brings the running one forward instead of starting another;
  - every daemon call runs off the UI thread, so a slow Bluetooth call never
    freezes the window.

  The Tk panel is still what `openrazer-win-gui` opens after `pip install`.

- `macro.set` accepts a written key sequence (`{"type": "keys", "sequence":
  "ctrl+c"}`), so every client shares the daemon's table of key names.

- The emulated keyboard of `--emulate` has a key report, so macro keys can
  be tried without hardware.

### Fixed

- **Breathing, reactive, blinking and starlight forgot their colours.** Only
  a static colour was saved, so after a replug or a sleep these effects came
  back in whatever colours an earlier static had left, and profiles recorded
  the same stale colours. Each effect now saves its colours, speed and
  direction, and every one of them is restored, including wheel, triple
  breathing and starlight, which restore skipped.

- A frozen `autostart enable` registers the control panel in the tray rather
  than the console build, whose window closed the daemon with it.

- The test suite no longer leaves a scratch registry key behind.

## [1.3.1] - 2026-10-27

### Added

- **Macro keys work** ([#1](https://github.com/Ar4ikov/openrazer-win/issues/1)). M1-M5
  on a BlackWidow and the like did nothing without Synapse. In driver mode
  a Razer keyboard hands its macro keys -- and its whole Fn layer -- to the
  host; on Linux the kernel driver takes them over, and now the daemon does
  the same here, from the same logic:

  - an unbound macro key sends F13-F17, like upstream, so any program that
    takes hotkeys can use it;
  - a bound one plays its macro: a key sequence, a piece of text, or a
    program, document or URL to open -- `openrazer-win macro set M1 --keys
    "ctrl+shift+esc"`, `--text`, `--launch`, `--disable`;
  - `openrazer-win macro record M2` records one, and so does **Fn+F9** on
    the keyboard, as on Linux;
  - **Fn+F10** toggles game mode, holding back the Windows key, and
    **Fn+F11/F12** step the backlight;
  - the rest of the Fn layer keeps working -- Fn+F1 is still Mute -- through
    the per-keyboard translation tables of upstream's driver, generated
    into `keys.json` by the new `tools/transpile_keys.py`.

  A keyboard is only switched to driver mode once its key report can be read
  and the keyboard hook is in place; otherwise, or with the daemon's new
  `--no-keys`, it stays in normal mode and keeps its Fn layer to itself.
  `openrazer-win macro watch` prints the raw key reports, for working out a
  keyboard that does not respond. Bindings are kept in
  `%LOCALAPPDATA%\openrazer-win\macros.json`.

- **Seven more device entries**, from upstream: the Cobra HyperSpeed and its
  receiver, the Viper V3 Pro SE wired and wireless, the Joro, and the Blade
  16 and 18 (2026). Device data now comes from
  OpenRazer master at 477f4d6, which carries everything in the 3.12.4
  release.

- `openrazer-win daemon restart`, and "Start with Windows" in the control
  panel.

### Fixed

- **A daemon started with Windows did nothing to the devices**, while one
  started by hand worked. Several things combined at logon:
  - a device that would not take its settings yet -- normal at logon for a
    Bluetooth headset, heard before Windows will hold a link to it -- was
    tried once and never again. Set-up is now retried until it takes, and
    `daemon status` names what is still waiting;
  - a Bluetooth call that timed out raised an error nothing caught, ending
    start-up silently;
  - the daemon only started answering once every device was set up, so the
    control panel took it for not running and started a second one, and
    the two fought over the hardware. It answers at once now, sets devices
    up in the background, and only one daemon runs per session;
  - the radio was swept once a minute while every client request waited for
    the sweep; it is now swept outside the lock, every 10 seconds for the
    first three minutes, and at once when a client asks for a rescan;
  - the console build registered itself for logon, opening a console window
    whose closing killed the daemon; the windowless build is registered
    instead, and an entry pointing at an executable that has since moved is
    repointed.

- **Setting the device mode did nothing on most keyboards.** The catch-all
  recipe started with a stop, because the transpiler treated `if
  (is_blade_laptop(device))` as undecidable. Regenerating against upstream
  turned up more driver logic that had been dropped: device-mode switches
  inside handlers (the Charging Pad and Mouse Dock Pro around their
  effects), `fallthrough` between cases, and the device mode the kernel puts
  accessories in when it binds -- most of them into driver mode, "just to
  function". All of it is now transpiled and run.

- The Naga V2 Pro's lighting, and the Huntsman V3 Pro 8KHz writing lighting
  to a profile it is not showing -- both upstream fixes, which needed the
  Naga Trinity effect builders and the active-profile query ported.

- Brightness on devices that inherit it from upstream's base class, such as
  the Mamba and the Firefly ([#2](https://github.com/Ar4ikov/openrazer-win/pull/2),
  thanks @VoyagerVPN).

- The Chroma Addressable RGB Controller: the right report id on Windows, and
  custom mode before the frame rather than after it, which wiped the
  colours ([#3](https://github.com/Ar4ikov/openrazer-win/pull/3), thanks
  @VoyagerVPN).

- A device seen for the first time was sent default lighting nobody had
  chosen, and changing only the brightness saved "spectrum" beside it to be
  replayed later ([#4](https://github.com/Ar4ikov/openrazer-win/pull/4),
  thanks @VoyagerVPN, and a follow-up covering Krakens and the Bluetooth
  headset).

- The test suite wrote to the real daemon log and Run key of whoever ran it.

## [1.3.0] - 2026-09-08

### Added

- **Named lighting profiles, for every device.** `openrazer-win profile save
  night` records what a device is showing -- per-zone effect, colours and
  brightness -- and `profile load night` puts it back. Up to ten per device,
  kept on the PC in `%LOCALAPPDATA%\openrazer-win\profiles.json`, which is
  plain readable JSON. Also `device.profiles`, `save_profile()`,
  `load_profile()` and `delete_profile()` in the Python API.

  This is the practical answer to a Bluetooth device not storing its colour:
  the daemon holds the colour, and a profile is how you keep more than one and
  switch between them. Nothing about it is Bluetooth-specific, so it works on
  keyboards and mice too.

- **Brightness on the Bluetooth headset.** Opcode `0xC1`, captured from
  Synapse's slider. It can also be read back, and unlike the colour the device
  keeps it across a power cycle. Loading a profile applies brightness too;
  reconnecting does not, because the device already knows it.

- **Battery level over Bluetooth.** `openrazer-win battery` now works on the
  headset: opcode `0x21` answered 0x57 while Synapse displayed 87%. The device
  also pushes the value unprompted when it changes. Charging state is read the
  same way, with the caveat that only the discharging value has been observed.

- **A request/response layer for Bluetooth devices.** The vendor service turns
  out to answer requests on its notify characteristic, which is how anything is
  read at all -- nothing in it is readable by a plain GATT read. Requests are
  retried, because they travel as unacknowledged Write Commands and one can be
  lost on the air.

### Changed

- **The Bluetooth protocol is now documented as a command set, not one
  command.** Everything is `<opcode> <kind> <length> <payload>`, and reads and
  writes of a setting differ by bit 0x80 -- a rule pinned down by seeing the
  auto-shutoff timer both ways as `0x27`/`0xA7`, then confirmed by predicting
  `0x41` for reading brightness and finding it works.

- Three more captures established that the device has **no effects of its
  own**: breathing and spectrum in Synapse produced 737 colour writes with 495
  distinct values in 105 seconds, and the fades between colours are drawn the
  same way. The lighting off-timers write nothing to the device at all.

- **A Bluetooth device held by another program is now named as such.** Windows
  answers GATT enumeration with AccessDenied while Razer Synapse holds the
  service, which used to surface as "device has no characteristic" -- reporting
  a working headset as the wrong hardware.

### Fixed

- A correction to 1.2.3's changelog: it said no command exists to store a
  colour in the device. That was more than the evidence supported. This headset
  shipped showing white and has shown green since Synapse first configured it,
  so something does write a stored colour; it simply was not in any traffic
  captured here, which points at the one-off setup Synapse performs on install.
  It remains unimplemented and, on a channel where the device answers unknown
  opcodes with silence, not worth guessing at.

## [1.2.3] - 2026-09-08

### Fixed

- **A colour set on the Bluetooth headset reverted after about five seconds.**
  Reported against 1.2.2, and traced to 1.2.2's own idle disconnect.

  The headset does not store the colour it is told. It shows it for as long as
  the link is up and reverts to the colour saved in it -- whatever Synapse last
  wrote, which is why it went back to a green nobody here had set -- the moment
  the link drops. 1.2.2 released an idle link after five seconds, so the colour
  went with it.

  A colour is now *held*: re-asserted every couple of seconds for as long as
  the daemon runs, the same thing Synapse does. An animation frame is not held,
  since the next frame is along in a moment, and switching the lighting off
  stops holding it -- so a device with nothing to show still releases its link
  and goes back to advertising.

### Known limitation

- Writing a colour into the headset's own memory, so that it survives with
  nothing connected, is not implemented. The vendor service has two more
  characteristics than the one this port drives -- one of them write-with-
  response, which is where a save would plausibly live -- but no capture of
  that exchange exists, and unknown opcodes are not worth guessing at against
  a device's flash.

## [1.2.2] - 2026-09-08

### Fixed

- **"device ... has no characteristic" on a device that plainly has it.** A
  write failed three times running against a headset that was advertising and
  whose GATT carries that characteristic -- enumerating it by hand a moment
  later listed all three services.

  Windows' service cache can answer both successfully and *empty* for a device
  that is not currently connected, and an empty service list is
  indistinguishable from a device that lacks the characteristic, so a working
  headset was reported as the wrong hardware. The cache is still consulted
  first, because warm it answers in milliseconds against about three seconds
  uncached, but a fruitless answer is retried uncached rather than believed.
  The two failures are also reported differently now: no services came back,
  versus the services are there and the characteristic is not among them.

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

[1.4.2]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.4.2
[1.4.1]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.4.1
[1.4.0]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.4.0
[1.3.1]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.3.1
[1.1.2]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.1.2
[1.1.0]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.1.0
[1.0.1]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.0.1
[1.0.0]: https://github.com/Ar4ikov/openrazer-win/releases/tag/v1.0.0
