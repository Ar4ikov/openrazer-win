"""Host-rendered effects, driven without touching the OS keyboard hook."""
from __future__ import annotations

import time

import pytest

from openrazer_win.devices.recipes import NotSupported
from openrazer_win.effects.engine import (
    RIPPLE_DURATION, EffectEngine, EffectThread, KeyPressSource,
)

from .conftest import KEYBOARD_PID, MOUSE_PID, make_device


class FakeKeySource:
    """Stands in for the Win32 hook so the tests never install one."""

    def __init__(self):
        self.subscribers = []
        self.running = False

    def subscribe(self, callback):
        self.subscribers.append(callback)
        self.running = True

    def unsubscribe(self, callback):
        if callback in self.subscribers:
            self.subscribers.remove(callback)
        self.running = bool(self.subscribers)

    def press(self, key_name):
        for callback in list(self.subscribers):
            callback(key_name)

    def stop(self):
        self.subscribers.clear()
        self.running = False


@pytest.fixture
def ripple(persistence):
    device, fake = make_device(KEYBOARD_PID, persistence)
    keys = FakeKeySource()
    thread = EffectThread(device, 'ripple', {'refresh_rate': 0.01,
                                             'colour': (0, 255, 0)}, keys)
    yield device, fake, keys, thread
    thread.stop()
    if thread.is_alive():
        thread.join(timeout=2)
    device.close()


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_a_key_press_lights_the_matrix_around_it(ripple):
    _device, _fake, _keys, thread = ripple
    thread.on_key('G')                      # row 3, column 6 in the matrix map
    assert thread._render_ripple() is True
    lit = [(row, column)
           for row in range(thread.frame.rows)
           for column in range(thread.frame.columns)
           if thread.frame.get(row, column) != (0, 0, 0)]
    assert lit, 'a ripple should light at least one key'


def test_keys_outside_the_matrix_are_ignored(ripple):
    _device, _fake, _keys, thread = ripple
    thread.on_key('NOT_A_KEY')
    assert thread._render_ripple() is False


def test_the_matrix_goes_dark_once_a_ripple_expires(ripple):
    _device, _fake, _keys, thread = ripple
    thread.on_key('G')
    thread._render_ripple()
    # Age the press past its lifetime rather than sleeping for it.
    started, position, colour = thread._presses[0]
    thread._presses[0] = (started - RIPPLE_DURATION - 0.1, position, colour)
    assert thread._render_ripple() is True          # one blanking frame
    assert thread._render_ripple() is False         # then idle
    assert all(thread.frame.get(row, column) == (0, 0, 0)
               for row in range(thread.frame.rows)
               for column in range(thread.frame.columns))


def test_the_ripple_expands_over_time(ripple):
    _device, _fake, _keys, thread = ripple
    thread.on_key('ESC')
    thread._render_ripple()
    early = sum(1 for row in range(thread.frame.rows)
                for column in range(thread.frame.columns)
                if thread.frame.get(row, column) != (0, 0, 0))

    started, position, colour = thread._presses[0]
    thread._presses[0] = (started - RIPPLE_DURATION / 2, position, colour)
    thread._render_ripple()
    later = sum(1 for row in range(thread.frame.rows)
                for column in range(thread.frame.columns)
                if thread.frame.get(row, column) != (0, 0, 0))
    assert later > early, 'the wavefront should cover more keys as it travels'


def test_running_the_thread_pushes_frames_to_the_device(ripple):
    device, fake, keys, thread = ripple
    thread.start()
    assert wait_for(lambda: keys.running), 'the thread should subscribe to key events'
    before = len(fake.sent_reports)
    keys.press('SPACE')
    assert wait_for(lambda: len(fake.sent_reports) > before), \
        'a key press should produce custom-frame traffic'
    thread.stop()
    thread.join(timeout=2)
    assert not keys.running, 'stopping should release the key subscription'


def test_random_ripple_varies_the_colour(persistence):
    device, _fake = make_device(KEYBOARD_PID, persistence)
    keys = FakeKeySource()
    thread = EffectThread(device, 'ripple_random', {}, keys)
    for key in ('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'):
        thread.on_key(key)
    colours = {press[2] for press in thread._presses}
    assert len(colours) > 1, 'random ripples should not all share one colour'
    device.close()


# -- engine -----------------------------------------------------------------

def test_engine_refuses_effects_a_device_cannot_render(persistence):
    from openrazer_win.core.manager import DeviceManager
    from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice

    backend = FakeHidBackend([FakeRazerDevice(MOUSE_PID, 'Razer Viper', interface=0)])
    manager = DeviceManager(backend=backend, persistence=persistence)
    manager.scan()
    engine = EffectEngine(manager)
    device = manager.devices[0]

    with pytest.raises(NotSupported, match='unknown software effect'):
        engine.set_effect(device, 'disco', {})

    engine.stop()
    manager.close()


def test_engine_tracks_and_clears_the_active_effect(persistence):
    from openrazer_win.core.manager import DeviceManager
    from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice

    backend = FakeHidBackend([FakeRazerDevice(KEYBOARD_PID,
                                              'Razer BlackWidow Chroma', interface=1)])
    manager = DeviceManager(backend=backend, persistence=persistence)
    manager.scan()
    engine = EffectEngine(manager)
    engine.keys = FakeKeySource()          # never install a real OS hook
    device = manager.devices[0]

    result = engine.set_effect(device, 'ripple', {'refresh_rate': 0.05})
    assert result['effect'] == 'ripple'
    assert engine.status()['active'] == {device.serial: 'ripple'}

    assert engine.set_effect(device, 'none', {})['effect'] is None
    assert engine.status()['active'] == {}

    engine.stop()
    manager.close()


def test_key_press_source_shares_one_hook(monkeypatch):
    source = KeyPressSource()
    started = []
    stopped = []
    monkeypatch.setattr(source._hook, 'start', lambda: started.append(True) or True)
    monkeypatch.setattr(source._hook, 'stop', lambda: stopped.append(True))

    def first(_name):
        pass

    def second(_name):
        pass

    source.subscribe(first)
    source.subscribe(second)
    assert len(started) == 1, 'the OS hook should be installed once, not per effect'

    source.unsubscribe(first)
    assert stopped == [], 'the hook stays while another effect still needs it'
    source.unsubscribe(second)
    assert len(stopped) == 1, 'the hook is released when the last effect stops'


def test_the_effect_thread_does_not_shadow_thread_internals(ripple):
    """Nothing on the subclass may hide a ``threading.Thread`` member.

    The engine originally stored its shutdown flag as ``self._stop``, which on
    Python 3.9-3.12 shadows the private ``Thread._stop()`` that ``join()``
    calls -- joining a live thread then raised ``TypeError: 'Event' object is
    not callable``. Python 3.13 dropped that member, so a local run passed and
    only CI caught it. Comparing against the real class keeps the check honest
    on every version.
    """
    import threading

    _device, _fake, _keys, thread = ripple

    # Names Thread sets on its own instances are fine; names it defines on the
    # class are not, because assigning to them replaces the inherited member.
    own = set(vars(threading.Thread(target=lambda: None)))
    thread_members = set(dir(threading.Thread)) - own
    shadowed = sorted(name for name in vars(thread) if name in thread_members)
    assert shadowed == [], (
        'these attributes hide threading.Thread members: {0}'.format(shadowed))

    thread.start()
    thread.stop()
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_the_os_hook_installs_and_shuts_down():
    """Install the real WH_KEYBOARD_LL hook, on Windows only.

    Regression guard for a ctypes prototype bug: without an explicit restype,
    ``GetModuleHandleW`` returned a c_int and truncated the 64-bit module
    handle, so ``SetWindowsHookEx`` failed with ERROR_MOD_NOT_FOUND and every
    key-reactive effect silently did nothing. Only a real install catches that.
    """
    from openrazer_win.effects.keyboard_hook import KeyboardHook, is_available

    if not is_available():
        pytest.skip('the OS hook is a Windows feature')

    hook = KeyboardHook(lambda name: None)
    try:
        assert hook.start(), 'SetWindowsHookEx failed; see the log for the WinError'
        assert hook.running
        assert hook._hook is not None
        assert hook._procedure is not None, 'the callback must stay referenced'
    finally:
        hook.stop()
    assert not hook.running
    assert hook._hook is None


def test_a_single_led_device_is_not_offered_host_effects(persistence):
    """A 1x1 matrix cannot show a ripple, so do not pretend it can."""
    from openrazer_win.effects.engine import can_render

    mouse, _fake = make_device(MOUSE_PID, persistence)          # Viper: 1x1
    keyboard, _fake2 = make_device(KEYBOARD_PID, persistence)   # Chroma: 6x22
    try:
        assert mouse.capabilities()['custom_frame'] is True
        assert mouse.capabilities()['software_effects'] is False
        assert not can_render(mouse)

        assert keyboard.capabilities()['software_effects'] is True
        assert can_render(keyboard)
    finally:
        mouse.close()
        keyboard.close()


def test_the_engine_refuses_a_device_it_cannot_draw_on(persistence):
    from openrazer_win.core.manager import DeviceManager
    from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice

    backend = FakeHidBackend([FakeRazerDevice(MOUSE_PID, 'Razer Viper', interface=0)])
    manager = DeviceManager(backend=backend, persistence=persistence)
    manager.scan()
    engine = EffectEngine(manager)
    engine.keys = FakeKeySource()
    try:
        with pytest.raises(NotSupported, match='matrix big enough'):
            engine.set_effect(manager.devices[0], 'ripple', {})
    finally:
        engine.stop()
        manager.close()
