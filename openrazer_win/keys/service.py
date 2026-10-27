"""Macro keys and the Fn layer, for keyboards in driver mode.

In driver mode a Razer keyboard leaves its macro keys and its whole Fn layer
to the host.  On Linux the kernel driver picks them up; on Windows nothing
did, which is why the M-keys of a BlackWidow did nothing without Synapse.
This service is the stand-in, built from the same logic as the kernel's:

* a reader per keyboard follows the vendor report -- report id 0x04, one byte
  per held key -- for Fn and the macro keys;
* a low-level keyboard hook swaps keys pressed with Fn held, through the
  translation table upstream gives that keyboard (Fn+F1 -> Mute, ...);
* a macro key runs its binding, or sends the key upstream turns it into --
  F13-F17 for M1-M5 -- when it has none;
* Fn+F9 records a macro the way upstream's daemon does, Fn+F10 toggles game
  mode (which holds back the Windows key) and Fn+F11/F12 step the brightness.

A keyboard is only put in driver mode once all of that is in place for it;
without it the keyboard stays in normal mode and keeps its Fn layer to itself.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from functools import lru_cache
from typing import Callable, Optional

from ..devices.recipes import DATA_DIR
from ..hid.base import HidError
from ..protocol.report import VENDOR_ID
from .codes import (
    ACTIONS, IGNORED, MACRO_KEYS, MACRO_TARGETS, VK_LWIN, VK_RWIN, output_for, source_name,
)
from .hook import FilterHook, KeyEvent
from .inject import Injector
from .macros import MacroError, MacroStore

logger = logging.getLogger(__name__)

KEYS_PATH = os.path.join(DATA_DIR, 'keys.json')

#: How far one Fn brightness press moves the backlight, as upstream does.
BRIGHTNESS_STEP = 10

#: What a macro key sends when it has no binding and upstream gives it no key
#: of its own -- the Fn-layer macro keys of the BlackWidow V3 Mini.
FN_MACRO_DEFAULTS = {'M1': 'KEY_F13', 'M2': 'KEY_F14', 'M3': 'KEY_F15', 'M4': 'KEY_F16'}


@lru_cache(maxsize=1)
def load_key_data() -> dict:
    with open(KEYS_PATH, encoding='utf-8') as handle:
        return json.load(handle)


def is_event_collection(info) -> bool:
    """Whether a collection is where Razer's vendor key reports arrive.

    The generic-desktop collection with no usage, or a vendor page -- never
    the keyboard or mouse collections Windows keeps for itself.
    """
    if not info.input_length:
        return False
    return (info.usage_page == 0x0001 and info.usage == 0x0000) or info.usage_page >= 0xFF00


def parse_report(report: bytes, report_id: int = 0x04) -> Optional[set]:
    """The key codes held in one vendor report, or ``None`` if it is not one."""
    if not report or report[0] != report_id:
        return None
    return {code for code in report[1:] if code}


def _launch(target: str) -> None:
    """Start a program, open a document, or follow a URL."""
    if '://' in target or os.path.exists(target):
        if sys.platform == 'win32':
            os.startfile(target)  # noqa: S606 - the user's own binding
            return
    subprocess.Popen(target, close_fds=True)  # noqa: S603 - the user's own binding


class _Binding:
    """One keyboard the service is handling."""

    def __init__(self, device, serial: str, table: dict):
        self.device = device
        self.serial = serial
        self.table = table
        self.readers: list = []
        self.stop = threading.Event()
        self.fn = False
        #: Vendor codes currently held.
        self.held: set = set()
        #: Keys that went down with Fn held; their release is translated too,
        #: even if Fn is let go first -- the kernel's ``pressed_fn``.
        self.pressed_fn: set = set()
        #: Macro keys that went down unbound, and so sent their default key.
        self.defaulted: dict = {}
        self.game_mode = False
        #: Macro keys this keyboard has been seen to send.
        self.seen: set = set()


class _Recording:
    def __init__(self, binding: _Binding, key: Optional[str] = None):
        self.binding = binding
        self.key = key
        self.events: list = []
        self.last: Optional[int] = None

    def add(self, event: KeyEvent) -> None:
        delay = 0 if self.last is None else max(0, event.time - self.last)
        self.last = event.time
        self.events.append({'vk': event.vk, 'scan': event.scan, 'ext': event.extended,
                            'down': event.down, 'delay': min(delay, 30000)})


class KeyService:
    """Handles macro keys and the Fn layer for every keyboard attached to it."""

    def __init__(self, backend, macros: Optional[MacroStore] = None, injector=None,
                 hook=None, launcher: Optional[Callable[[str], None]] = None,
                 data: Optional[dict] = None):
        self.backend = backend
        self.data = data or load_key_data()
        self.macros = macros if macros is not None else MacroStore()
        self.injector = injector if injector is not None else Injector()
        self.hook = hook if hook is not None else FilterHook(self.handle_key)
        self.launcher = launcher or _launch
        self._lock = threading.RLock()
        self._bindings: dict = {}
        self._recording: Optional[_Recording] = None
        self._playing: set = set()
        self._work: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._no_translation = set(self.data.get('no_translation_pids', ()))

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        if self._worker is None:
            self._worker = threading.Thread(target=self._work_loop, name='key-actions',
                                            daemon=True)
            self._worker.start()
        running = self.hook.start()
        if not running:
            logger.info('the key filter is not available; keyboards keep their own Fn layer')
        return running

    @property
    def running(self) -> bool:
        return bool(getattr(self.hook, 'running', False))

    def stop(self) -> None:
        with self._lock:
            bindings = list(self._bindings.values())
        for binding in bindings:
            self.detach(binding.device)
        self.hook.stop()
        if self._worker is not None:
            self._work.put(None)
            self._worker.join(timeout=2.0)
            self._worker = None

    def _work_loop(self) -> None:
        while True:
            job = self._work.get()
            if job is None:
                return
            try:
                job()
            except Exception:  # noqa: BLE001 - one failed action must not stop the rest
                logger.warning('key action failed', exc_info=True)

    # -- devices -----------------------------------------------------------
    def wants(self, device) -> bool:
        """Whether `device` is a keyboard whose keys this service would take over."""
        info = getattr(device, 'info', None)
        return (info is not None and getattr(device, 'driver', None) == 'kbd'
                and info.driver_mode
                and '{0:04x}'.format(info.pid) not in self._no_translation)

    def table_for(self, pid: int) -> dict:
        name = self.data['tables_by_pid'].get('{0:04x}'.format(pid), self.data['default_table'])
        return dict(self.data['tables'].get(name, []))

    def attach(self, device) -> bool:
        """Start handling `device`'s keys.  True if they are now handled here."""
        if not self.running or not self.wants(device):
            return False
        serial = device.serial
        with self._lock:
            if serial in self._bindings:
                return True
        binding = _Binding(device, serial, self.table_for(device.pid))
        for info in self.backend.enumerate(vendor_id=VENDOR_ID, product_id=device.pid):
            if not is_event_collection(info):
                continue
            try:
                binding.readers.append(self.backend.open_input(info))
            except (HidError, OSError, AttributeError) as error:
                logger.debug('cannot read %s: %s', info.describe(), error)
        if not binding.readers:
            logger.info('%s exposes no key report this port can read; it keeps its '
                        'own Fn layer and its macro keys stay idle', device.name)
            return False
        with self._lock:
            self._bindings[serial] = binding
        for reader in binding.readers:
            threading.Thread(target=self._read_loop, args=(binding, reader),
                             name='keys-{0}'.format(serial), daemon=True).start()
        logger.info('handling the macro keys and Fn layer of %s', device.name)
        return True

    def detach(self, device) -> None:
        with self._lock:
            binding = None
            for serial, candidate in list(self._bindings.items()):
                if candidate.device is device:
                    binding = self._bindings.pop(serial)
            if binding is None:
                return
            if self._recording is not None and self._recording.binding is binding:
                self._recording = None
        binding.stop.set()

    def handles(self, device) -> bool:
        with self._lock:
            return any(b.device is device for b in self._bindings.values())

    def _read_loop(self, binding: _Binding, reader) -> None:
        report_id = self.data.get('report_id', 0x04)
        try:
            while not binding.stop.is_set():
                try:
                    report = reader.read(0.5)
                except (HidError, OSError):
                    logger.debug('key reader for %s ended', binding.device.name, exc_info=True)
                    return
                if report is None:
                    continue
                codes = parse_report(report, report_id)
                if codes is not None:
                    self.handle_report(binding, codes)
        finally:
            try:
                reader.close()
            except Exception:  # noqa: BLE001 - closing a dead handle
                pass

    # -- the vendor report -------------------------------------------------
    def handle_report(self, binding: _Binding, codes: set) -> None:
        fn_code = self.data.get('fn_code', 0x01)
        with self._lock:
            binding.fn = fn_code in codes
            keys = set(codes) - {fn_code}
            pressed = sorted(keys - binding.held)
            released = sorted(binding.held - keys)
            binding.held = keys
        for code in pressed:
            self._vendor_key(binding, code, True)
        for code in released:
            self._vendor_key(binding, code, False)

    def _vendor_key(self, binding: _Binding, code: int, down: bool) -> None:
        target = self.data['vendor_codes'].get('{0:02x}'.format(code))
        name = MACRO_KEYS.get(code)
        if name is not None:
            binding.seen.add(name)
            self._macro_key(binding, name, target, down)
            return
        if target is None:
            return
        output = output_for(target)
        if output is not None:
            self.injector.key(output, down)

    def _macro_key(self, binding: _Binding, name: str, default: Optional[str],
                   down: bool) -> None:
        if not down:
            sent = binding.defaulted.pop(name, None)
            if sent is not None:
                self.injector.key(sent, False)
            return
        with self._lock:
            recording = self._recording
            if recording is not None and recording.binding is binding:
                if recording.key is None:
                    recording.key = name
                    self._work.put(lambda: self._macro_led(binding, recording=True,
                                                           blinking=False))
                elif recording.key == name:
                    # Recording a key's own macro into itself: upstream cancels.
                    self._recording = None
                    self._work.put(lambda: self._macro_led(binding, recording=False))
                return
        action = self.macros.get(binding.serial, name)
        if action is None:
            output = output_for(default) if default else None
            if output is not None:
                binding.defaulted[name] = output
                self.injector.key(output, True)
            return
        if action['type'] != 'disabled':
            self._play_async(binding.serial, name, action)

    # -- the keyboard hook -------------------------------------------------
    def handle_key(self, event: KeyEvent) -> bool:
        """Decide one key event.  True holds it back from everything else."""
        if event.injected:
            # Typed by software -- a macro, another remapper, this daemon --
            # not by a hand on a Razer keyboard's Fn layer.
            return False
        actions = []
        swallow = False
        with self._lock:
            bindings = list(self._bindings.values())
            if event.vk in (VK_LWIN, VK_RWIN) and any(b.game_mode for b in bindings):
                return True
            source = source_name(event.scan, event.extended, event.vk)
            if source is not None:
                for binding in bindings:
                    target = binding.table.get(source)
                    if target is None:
                        continue
                    if event.down and (binding.fn or source in binding.pressed_fn):
                        repeat = source in binding.pressed_fn
                        binding.pressed_fn.add(source)
                    elif not event.down and source in binding.pressed_fn:
                        repeat = False
                        binding.pressed_fn.discard(source)
                    else:
                        continue
                    actions.append((binding, target, repeat))
                    swallow = True
                    break
            if not swallow and self._recording is not None and self._recording.key:
                self._recording.add(event)
        for binding, target, repeat in actions:
            self._fn_target(binding, target, event.down, repeat)
        return swallow

    def _fn_target(self, binding: _Binding, target: str, down: bool, repeat: bool) -> None:
        action = ACTIONS.get(target)
        if action is not None:
            if down and not repeat:
                self._work.put(lambda: self.perform(binding, action))
            return
        macro = MACRO_TARGETS.get(target)
        if macro is not None:
            if not repeat:
                self._macro_key(binding, macro, FN_MACRO_DEFAULTS.get(macro), down)
            return
        if target in IGNORED:
            return
        output = output_for(target)
        if output is not None:
            self.injector.key(output, down)

    # -- what the Fn layer's own keys do -----------------------------------
    def perform(self, binding: _Binding, action: str) -> None:
        device = binding.device
        if action == 'macro_record':
            self._toggle_recording(binding)
        elif action == 'game_mode':
            binding.game_mode = not binding.game_mode
            logger.info('%s: game mode %s', device.name, 'on' if binding.game_mode else 'off')
            try:
                device.set_game_mode(binding.game_mode)
            except Exception:  # noqa: BLE001 - the LED is a courtesy
                logger.debug('no game mode LED on %s', device.name, exc_info=True)
        elif action in ('brightness_down', 'brightness_up'):
            step = BRIGHTNESS_STEP if action == 'brightness_up' else -BRIGHTNESS_STEP
            try:
                current = float(device.get_brightness())
            except Exception:  # noqa: BLE001 - fall back to a middle value
                current = 50.0
            device.set_brightness(max(0.0, min(100.0, current + step)))

    def _macro_led(self, binding: _Binding, recording: bool, blinking: bool = True) -> None:
        device = binding.device
        try:
            device.set_macro_mode(recording)
            device.set_macro_effect(0x01 if (recording and blinking) else 0x00)
        except Exception:  # noqa: BLE001 - not every keyboard has the LED
            logger.debug('no macro LED on %s', device.name, exc_info=True)

    def _toggle_recording(self, binding: _Binding) -> None:
        with self._lock:
            recording = self._recording
            if recording is None or recording.binding is not binding:
                self._recording = _Recording(binding)
                starting = True
            else:
                self._recording = None
                starting = False
        if starting:
            logger.info('%s: recording a macro; press the macro key to record it to',
                        binding.device.name)
            self._macro_led(binding, recording=True, blinking=True)
            return
        self._finish(recording)

    def _finish(self, recording: _Recording) -> Optional[dict]:
        binding = recording.binding
        self._macro_led(binding, recording=False)
        if recording.key is None:
            return None
        if not recording.events:
            # Upstream: finishing with nothing recorded clears the key.
            self.macros.delete(binding.serial, recording.key)
            return None
        try:
            saved = self.macros.set(binding.serial, recording.key,
                                    {'type': 'keys', 'events': recording.events})
        except MacroError:
            logger.warning('could not keep the recorded macro', exc_info=True)
            return None
        logger.info('%s: recorded %d events to %s', binding.device.name,
                    len(recording.events), recording.key)
        return saved

    # -- playing a binding -------------------------------------------------
    def _play_async(self, serial: str, key: str, action: dict) -> None:
        with self._lock:
            if (serial, key) in self._playing:
                return          # still playing from the last press
            self._playing.add((serial, key))
        threading.Thread(target=self._play, args=(serial, key, action),
                         name='macro-{0}'.format(key), daemon=True).start()

    def _play(self, serial: str, key: str, action: dict) -> None:
        try:
            self.play(action)
        except Exception:  # noqa: BLE001 - a bad binding must not kill anything
            logger.warning('macro %s failed', key, exc_info=True)
        finally:
            with self._lock:
                self._playing.discard((serial, key))

    def play(self, action: dict) -> None:
        kind = action.get('type')
        if kind == 'keys':
            held = set()
            try:
                for event in action['events']:
                    if event.get('delay'):
                        time.sleep(event['delay'] / 1000.0)
                    identity = (event['vk'], event['scan'], event['ext'])
                    self.injector.raw(event['vk'], event['scan'], event['ext'], event['down'])
                    if event['down']:
                        held.add(identity)
                    else:
                        held.discard(identity)
            finally:
                # A macro cut short must not leave Shift held down.
                for vk, scan, extended in held:
                    self.injector.raw(vk, scan, extended, False)
        elif kind == 'text':
            self.injector.text(action['text'])
        elif kind == 'launch':
            self.launcher(action['target'])

    # -- for the RPC layer -------------------------------------------------
    def _binding_for(self, serial: str) -> _Binding:
        with self._lock:
            binding = self._bindings.get(serial)
        if binding is None:
            raise KeyError(serial)
        return binding

    def macro_keys(self, device) -> list:
        """The bindable keys `device` has, as far as is known."""
        keys = []
        if getattr(device.info, 'dedicated_macro_keys', False):
            keys.extend(['M1', 'M2', 'M3', 'M4', 'M5'])
        table = self.table_for(device.pid) if self.wants(device) else {}
        for target in table.values():
            name = MACRO_TARGETS.get(target)
            if name and name not in keys:
                keys.append(name)
        with self._lock:
            binding = self._bindings.get(device.serial)
            seen = sorted(binding.seen) if binding else []
        for name in seen:
            if name not in keys:
                keys.append(name)
        order = list(MACRO_KEYS.values())
        return sorted(keys, key=lambda k: order.index(k) if k in order else len(order))

    def start_recording(self, serial: str, key: str) -> None:
        binding = self._binding_for(serial)
        with self._lock:
            self._recording = _Recording(binding, key)
        self._macro_led(binding, recording=True, blinking=False)

    def stop_recording(self) -> Optional[dict]:
        with self._lock:
            recording, self._recording = self._recording, None
        if recording is None:
            return None
        return self._finish(recording)

    def status(self, devices: list) -> dict:
        with self._lock:
            recording = self._recording
            rows = []
            for device in devices:
                binding = self._bindings.get(getattr(device, 'serial', None))
                if not self.wants(device) and binding is None:
                    continue
                rows.append({
                    'serial': device.serial,
                    'name': device.name,
                    'handled': binding is not None,
                    'game_mode': bool(binding and binding.game_mode),
                    'macro_keys': self.macro_keys(device),
                })
        return {
            'running': self.running,
            'devices': rows,
            'recording': None if recording is None else {
                'serial': recording.binding.serial, 'key': recording.key,
                'events': len(recording.events)},
        }
