"""Snap Tap, done on the PC because the keyboard does not do it.

These drive the engine, and the host around it, with made-up key events and a
recording injector.  Nothing is sent to Windows, and no hook is installed except
in the one test that says so.
"""
from __future__ import annotations

import json
import pathlib
import random
import sys
import threading
import time

import pytest

from openrazer_win.cli import main
from openrazer_win.core.persistence import Persistence
from openrazer_win.daemon.demo import build_demo_backend
from openrazer_win.daemon.protocol import INVALID_PARAMS, RpcError
from openrazer_win.daemon.server import DaemonServer, DaemonService
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice
from openrazer_win.keys import KeyService, MacroStore
from openrazer_win.keys import snaptap_host
from openrazer_win.keys.codes import KEYS
from openrazer_win.keys.hook import FilterHook, KeyEvent, Timings, is_available
from openrazer_win.keys.inject import RecordingInjector
from openrazer_win.keys.snaptap import (
    PASS, SWALLOW, WARNING, SnapTapEngine, SnapTapError, SnapTapSettings, clean_pairs,
    clean_programs, key_ids, pause_reason,
)
from openrazer_win.keys.snaptap_host import SILENCE_NS, SWITCH_INTERVAL, SnapTapHost

A, D, W, S = KEYS['KEY_A'], KEYS['KEY_D'], KEYS['KEY_W'], KEYS['KEY_S']
F1, Q, CTRL = KEYS['KEY_F1'], KEYS['KEY_Q'], KEYS['KEY_LEFTCTRL']
LALT, RALT = KEYS['KEY_LEFTALT'], KEYS['KEY_RIGHTALT']
LWIN = KEYS['KEY_LEFTMETA']
FN = 0x01
BLACKWIDOW_CHROMA = 0x0203


def ev(k, down, injected=False, t=0):
    return KeyEvent(vk=k.vk, scan=k.scan, extended=k.extended, down=down, injected=injected,
                    ours=False, time=t)


def ident(k):
    return (k.scan, k.extended)


def release(k):
    return (k.scan, k.extended, False)


def press(k):
    return (k.scan, k.extended, True)


def engine(*pairs):
    names = pairs or (('KEY_A', 'KEY_D'),)
    return SnapTapEngine(key_ids(names))


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeHook:
    """A hook whose deferred jobs wait until the test says the callback returned."""

    def __init__(self, on_event=None):
        self.on_event = on_event
        self.running = False
        self.jobs = []
        self.last_callback = time.perf_counter_ns()
        self.rearmed = 0
        self.timings = Timings()
        self.errors = 0
        self.rearm_works = True

    def start(self):
        self.running = True
        return True

    def stop(self):
        self.running = False

    def defer(self, job):
        if not self.running:
            return False
        self.jobs.append(job)
        return True

    def pump(self):
        jobs, self.jobs = self.jobs, []
        for job in jobs:
            job()

    def rearm(self):
        self.rearmed += 1
        return self.rearm_works


class SyncHook(FakeHook):
    """Runs anything but a resume at once, as if the hook's thread had got to it.

    A resume waits for pump(), because when it runs is what the tests are about.
    """

    def defer(self, job):
        if getattr(job, '__name__', '') == '_resume':
            return super().defer(job)
        if not self.running:
            return False
        job()
        return True


class FakeWatch:
    made: list = []

    def __init__(self, on_change):
        self.on_change = on_change
        self.running = False
        FakeWatch.made.append(self)

    def start(self):
        self.running = True
        return True

    def stop(self):
        self.running = False


class Rig:
    """A host on a fake hook: what it would send, and which program is in front."""

    def __init__(self, tmp_path, pairs=None, deny=None, allow=None):
        self.path = str(tmp_path / 'snaptap.json')
        self.settings = SnapTapSettings(self.path)
        self.injector = RecordingInjector()
        self.exe = 'notepad.exe'
        self.keys_down: set = set()
        self.hook = None
        self.host = SnapTapHost(self.settings, injector=self.injector,
                                hook_factory=self._make_hook, watch_factory=FakeWatch,
                                foreground=lambda: self.exe,
                                is_down=lambda vk: vk in self.keys_down)
        changes = {'enabled': True, 'acknowledged': True}
        if pairs is not None:
            changes['pairs'] = pairs
        if deny is not None:
            changes['deny'] = deny
        if allow is not None:
            changes['allow'] = allow
        self.host.configure(**changes)

    def _make_hook(self, on_event):
        self.hook = SyncHook(on_event)
        self.hook.start()
        return self.hook

    def batches(self):
        return [entry[1] for entry in self.injector.sent if entry[0] == 'batch']

    def key(self, k, down, **kwargs):
        return self.host.handle(ev(k, down, **kwargs))


@pytest.fixture
def rig(tmp_path):
    made = Rig(tmp_path)
    yield made
    made.host.stop()


# -- the table: what each event does ----------------------------------------

def test_a_press_with_the_partner_active_releases_the_partner_first():
    e = engine()
    assert e.handle(ev(A, True)) is PASS
    decision = e.handle(ev(D, True))
    assert decision.now == (release(A),) and not decision.swallow and not decision.later
    assert e.active_keys() == [ident(D)]


def test_a_press_with_the_partner_not_active_just_passes():
    e = engine()
    assert e.handle(ev(A, True)) is PASS
    assert e.active_keys() == [ident(A)]


def test_a_repeat_of_the_active_key_passes():
    e = engine()
    e.handle(ev(A, True))
    assert e.handle(ev(A, True)) is PASS


def test_a_repeat_of_the_suppressed_key_is_the_one_thing_swallowed():
    e = engine()
    e.handle(ev(A, True))
    e.handle(ev(D, True))
    assert e.handle(ev(A, True)) is SWALLOW


def test_letting_go_of_the_active_key_with_the_partner_held_resumes_the_partner_later():
    e = engine()
    e.handle(ev(A, True))
    e.handle(ev(D, True))
    decision = e.handle(ev(D, False))
    assert decision.later and not decision.now and not decision.swallow
    assert e.take_resumes() == [press(A)]
    assert e.active_keys() == [ident(A)]
    assert e.take_resumes() == []


def test_letting_go_of_the_suppressed_key_passes_and_resumes_nothing():
    e = engine()
    e.handle(ev(A, True))
    e.handle(ev(D, True))
    assert e.handle(ev(A, False)) is PASS
    assert e.handle(ev(D, False)) is PASS
    assert e.take_resumes() == []
    assert e.active_keys() == []


def test_letting_go_of_the_only_key_passes():
    e = engine()
    e.handle(ev(A, True))
    assert e.handle(ev(A, False)) is PASS
    assert e.active_keys() == [] and e.held_keys() == set()


def test_a_release_of_a_key_never_seen_passes():
    assert engine().handle(ev(A, False)) is PASS


@pytest.mark.parametrize('sequence', [
    [(A, True)], [(A, True), (D, True)], [(A, True), (D, True), (D, False)],
])
def test_injected_keys_from_anyone_are_ignored_and_change_nothing(sequence):
    e = engine()
    for k, down in sequence:
        e.handle(ev(k, down))
    active, held = e.active_keys(), e.held_keys()
    for k in (A, D):
        for down in (True, False):
            assert e.handle(ev(k, down, injected=True)) is PASS
    assert (e.active_keys(), e.held_keys()) == (active, held)


def test_keys_outside_a_pair_pass():
    e = engine()
    assert e.handle(ev(Q, True)) is PASS
    assert e.handle(ev(Q, False)) is PASS


def test_keys_are_told_apart_by_the_extended_flag():
    e = engine(('KEY_ENTER', 'KEY_HOME'))
    keypad_enter = KEYS['KEY_KPENTER']
    assert e.handle(ev(keypad_enter, True)) is PASS
    assert e.active_keys() == []


def test_keys_are_matched_by_position_not_by_what_the_layout_calls_them():
    # On AZERTY the key at the position of Q on a US board reports VK_A.
    e = engine(('KEY_Q', 'KEY_D'))
    azerty = KeyEvent(vk=A.vk, scan=Q.scan, extended=False, down=True, injected=False,
                      ours=False, time=0)
    assert e.handle(azerty) is PASS
    assert e.active_keys() == [ident(Q)]


def test_pairs_do_not_touch_each_other():
    e = engine(('KEY_A', 'KEY_D'), ('KEY_W', 'KEY_S'))
    e.handle(ev(A, True))
    assert e.handle(ev(W, True)) is PASS
    decision = e.handle(ev(S, True))
    assert decision.now == (release(W),)
    assert sorted(e.active_keys()) == sorted([ident(A), ident(S)])
    e.handle(ev(S, False))
    assert e.take_resumes() == [press(W)]


def test_going_back_and_forth_keeps_one_key_active():
    e = engine()
    e.handle(ev(A, True))
    e.handle(ev(D, True))
    e.handle(ev(D, False))
    e.take_resumes()
    assert e.handle(ev(D, True)).now == (release(A),)
    e.handle(ev(A, False))
    assert e.handle(ev(A, True)).now == (release(D),)
    assert e.active_keys() == [ident(A)]


# -- what may happen before the deferred press is sent ----------------------

def test_a_key_let_go_before_its_resume_is_sent_is_not_pressed():
    e = engine()
    e.handle(ev(A, True))
    e.handle(ev(D, True))
    e.handle(ev(D, False))
    assert e.handle(ev(A, False)) is PASS
    assert e.take_resumes() == []
    assert e.active_keys() == []


def test_the_partner_coming_back_before_the_resume_cancels_it():
    e = engine()
    e.handle(ev(A, True))
    e.handle(ev(D, True))
    e.handle(ev(D, False))
    e.handle(ev(D, True))
    assert e.take_resumes() == []
    assert e.active_keys() == [ident(D)]


def test_a_reset_before_the_resume_cancels_it():
    e = engine()
    e.handle(ev(A, True))
    e.handle(ev(D, True))
    e.handle(ev(D, False))
    e.reset()
    assert e.take_resumes() == []


# -- resets -----------------------------------------------------------------

def test_a_reset_sends_nothing_and_forgets_everything(rig):
    rig.key(A, True)
    rig.key(D, True)
    sent = list(rig.injector.sent)
    rig.host.on_system_change()
    assert rig.injector.sent == sent
    assert rig.host.engine.active_keys() == []
    assert rig.host.engine.held_keys() == set()


def test_a_key_up_lost_on_another_desktop_cannot_become_a_stuck_key(rig):
    """A held, D over it, the screen locks, and A's release goes to the lock screen."""
    rig.key(A, True)
    rig.key(D, True)
    rig.host.on_system_change()                 # to the secure desktop
    rig.host.on_system_change()                 # and back; A has been let go there
    rig.key(D, False)
    rig.hook.pump()
    assert press(A) not in [event for batch in rig.batches() for event in batch]


def test_a_key_still_held_after_a_reset_is_picked_up_by_its_repeat(rig):
    rig.key(A, True)
    rig.host.on_system_change()
    assert rig.key(A, True) is False
    assert rig.host.engine.active_keys() == [ident(A)]
    rig.key(D, True)
    assert rig.batches()[-1] == (release(A),)


def test_every_window_change_counts_as_a_reset(rig):
    rig.host.on_system_change()
    rig.host.on_system_change()
    assert rig.host.status()['resets'] == 2


# -- the program in front ---------------------------------------------------

@pytest.mark.parametrize('exe, deny, allow, reason', [
    ('notepad.exe', ['cs2.exe'], [], None),
    ('cs2.exe', ['cs2.exe'], [], 'denied'),
    ('game.exe', [], ['game.exe'], None),
    ('other.exe', [], ['game.exe'], 'not allowed'),
    ('game.exe', ['game.exe'], ['game.exe'], 'denied'),
    (None, [], [], 'unknown program'),
])
def test_where_it_runs(exe, deny, allow, reason):
    assert pause_reason(exe, deny, allow) == reason


def test_it_pauses_while_a_denied_program_is_in_front(rig):
    rig.exe = 'cs2.exe'
    rig.host.on_system_change()
    assert rig.host.status()['paused'] == 'denied'
    rig.key(A, True)
    assert rig.key(D, True) is False
    assert rig.key(A, True) is False            # not swallowed: nothing is tracked
    rig.key(D, False)
    assert rig.batches() == []
    rig.exe = 'notepad.exe'
    rig.host.on_system_change()
    assert rig.host.status()['paused'] is None


def test_a_held_key_is_not_resumed_into_a_denied_program(rig):
    rig.key(A, True)
    rig.key(D, True)
    rig.exe = 'cs2.exe'
    rig.host.on_system_change()
    rig.key(D, False)
    rig.hook.pump()
    assert rig.batches() == [(release(A),)]


def test_an_allow_list_limits_it_to_the_programs_listed(tmp_path):
    rig = Rig(tmp_path, allow=['game.exe'])
    try:
        assert rig.host.status()['paused'] == 'not allowed'
        rig.exe = 'game.exe'
        rig.host.on_system_change()
        rig.key(A, True)
        rig.key(D, True)
        assert rig.batches() == [(release(A),)]
    finally:
        rig.host.stop()


def test_an_unnamed_program_is_paused_for(rig):
    rig.exe = None
    rig.host.on_system_change()
    assert rig.host.status()['paused'] == 'unknown program'


@pytest.mark.parametrize('modifier', [LALT, RALT, LWIN], ids=['alt', 'altgr', 'win'])
def test_alt_and_windows_step_aside_while_held(modifier):
    e = engine()
    assert e.handle(ev(modifier, True)) is PASS
    assert e.handle(ev(A, True)) is PASS
    assert e.handle(ev(D, True)) is PASS        # no release sent: this is a shortcut
    assert e.handle(ev(D, False)) is PASS
    assert e.handle(ev(A, False)) is PASS
    assert e.take_resumes() == []
    assert e.handle(ev(modifier, False)) is PASS
    e.handle(ev(A, True))
    assert e.handle(ev(D, True)).now == (release(A),)


def test_a_held_key_is_not_pressed_again_after_a_shortcut():
    e = engine()
    e.handle(ev(A, True))
    e.handle(ev(D, True))
    e.handle(ev(LALT, True))
    e.handle(ev(LALT, False))
    assert e.handle(ev(D, False)) is PASS
    assert e.take_resumes() == []


def test_ctrl_does_not_stop_it():
    e = engine()
    e.handle(ev(CTRL, True))
    e.handle(ev(A, True))
    assert e.handle(ev(D, True)).now == (release(A),)


def test_alt_held_across_a_window_change_keeps_it_aside(rig):
    rig.key(LALT, True)
    rig.keys_down.add(LALT.vk)
    rig.host.on_system_change()                 # Alt+Tab
    rig.key(A, True)
    rig.key(D, True)
    assert rig.batches() == []


# -- host: sending in the right order ---------------------------------------

def test_the_release_goes_inside_the_callback_and_the_press_after_it(rig):
    rig.key(A, True)
    assert rig.key(D, True) is False
    assert rig.batches() == [(release(A),)]     # already sent, before D goes on
    assert rig.key(D, False) is False
    assert rig.batches() == [(release(A),)]     # not yet: D's release goes first
    rig.hook.pump()
    assert rig.batches() == [(release(A),), (press(A),)]


def test_a_resumed_key_gets_one_press_and_no_repeats(rig):
    rig.key(A, True)
    rig.key(D, True)
    rig.key(D, False)
    rig.hook.pump()
    rig.hook.pump()
    assert [event for batch in rig.batches() for event in batch].count(press(A)) == 1


def test_a_failed_send_is_counted_rather_than_logged(rig, caplog):
    rig.injector.batch = lambda events: 0
    rig.key(A, True)
    rig.key(D, True)
    assert rig.host.status()['failed_sends'] == 1
    assert caplog.records == []


def test_an_injector_never_sends_text_for_snap_tap(rig):
    rig.key(A, True)
    rig.key(D, True)
    rig.key(D, False)
    rig.hook.pump()
    assert {entry[0] for entry in rig.injector.sent} == {'batch'}


# -- seeded random sequences ------------------------------------------------

class Screen:
    """What the rest of the system believes, from what got past the hook."""

    def __init__(self, pairs):
        self.engine = SnapTapEngine(key_ids(pairs))
        self.names = {ident(KEYS[name]): KEYS[name] for pair in pairs for name in pair}
        self.pairs = [tuple(ident(KEYS[name]) for name in pair) for pair in pairs]
        self.os_down: set = set()
        self.hand: set = set()
        self.queued = 0

    def _apply(self, events):
        for scan, extended, down in events:
            (self.os_down.add if down else self.os_down.discard)((scan, extended))

    def flush(self):
        if self.queued:
            self.queued = 0
            self._apply(self.engine.take_resumes())

    def event(self, k, down):
        key = ident(k)
        repeat = down and key in self.hand
        (self.hand.add if down else self.hand.discard)(key)
        decision = self.engine.handle(ev(k, down))
        self._apply(decision.now)
        if not down:
            assert not decision.swallow, 'a physical release was swallowed'
        if decision.swallow:
            assert repeat, 'only a repeat may be swallowed'
        else:
            (self.os_down.add if down else self.os_down.discard)(key)
        if decision.later:
            self.queued += 1

    def check_one_per_pair(self):
        for first, second in self.pairs:
            assert not (first in self.os_down and second in self.os_down)
            active = [key for key in self.engine.active_keys() if key in (first, second)]
            assert len(active) <= 1


PAIRS = (('KEY_A', 'KEY_D'), ('KEY_W', 'KEY_S'))
POOL = (A, D, W, S, Q)


@pytest.mark.parametrize('seed', range(40))
def test_random_typing_keeps_every_promise(seed):
    random_source = random.Random(seed)
    screen = Screen(PAIRS)
    for _ in range(2000):
        k = random_source.choice(POOL)
        # Down when already held is a repeat, which is what a held key sends.
        screen.event(k, random_source.random() < 0.55)
        if random_source.random() < 0.6:
            screen.flush()
        screen.check_one_per_pair()
    for key in list(screen.hand):
        screen.event(screen.names.get(key, Q), False)
    screen.flush()
    assert screen.engine.active_keys() == []
    assert screen.engine.held_keys() == set()
    assert screen.os_down == set(), 'a key was left down with no hand on it'


@pytest.mark.parametrize('seed', range(40))
def test_nothing_stays_down_that_no_hand_holds(seed):
    """With window changes, shortcuts and key-ups the hook never sees thrown in."""
    random_source = random.Random(1000 + seed)
    screen = Screen(PAIRS)
    for _ in range(2000):
        roll = random_source.random()
        if roll < 0.04:
            screen.engine.reset()
            screen.flush()
        elif roll < 0.08 and screen.hand:
            # Released on a desktop the hook cannot see: the system saw it.
            key = random_source.choice(sorted(screen.hand))
            screen.hand.discard(key)
            screen.os_down.discard(key)
            screen.engine.reset()
            screen.flush()
        elif roll < 0.12:
            for down in (True, False):
                screen.engine.handle(ev(LALT, down))
            screen.flush()
        else:
            screen.event(random_source.choice(POOL), random_source.random() < 0.55)
            if random_source.random() < 0.5:
                screen.flush()
            else:
                continue
        assert screen.os_down <= screen.hand, 'pressed a key nobody is holding'
    for key in list(screen.hand):
        screen.event(screen.names.get(key, Q), False)
    screen.flush()
    assert screen.os_down == set()


# -- the hook being taken away ----------------------------------------------

def test_silence_gets_a_new_hook(rig):
    rig.hook.last_callback = time.perf_counter_ns() - SILENCE_NS - 1
    rig.host.check_hook()
    rig.hook.pump()
    assert rig.hook.rearmed == 1
    assert rig.host.status()['rearms'] == 1


def test_a_hook_heard_from_is_left_alone(rig):
    rig.hook.last_callback = time.perf_counter_ns()
    rig.host.check_hook()
    rig.hook.pump()
    assert rig.hook.rearmed == 0


def test_checking_the_hook_sends_no_keys(rig):
    rig.hook.last_callback = 0
    rig.host.check_hook()
    rig.hook.pump()
    assert rig.injector.sent == []


def test_no_phantom_press_after_the_hook_was_gone(rig):
    """While it was gone both keys were let go; nothing may press one again."""
    rig.key(A, True)
    rig.key(D, True)
    rig.hook.last_callback = 0
    rig.host.check_hook()
    rig.hook.pump()
    assert rig.host.engine.active_keys() == []
    assert rig.host.engine.held_keys() == set()
    rig.key(D, False)
    rig.key(A, False)
    rig.hook.pump()
    assert rig.batches() == [(release(A),)]


def test_the_active_key_stays_across_a_new_hook_if_windows_agrees(rig):
    rig.key(A, True)
    rig.key(D, True)
    rig.keys_down.add(D.vk)
    rig.hook.last_callback = 0
    rig.host.check_hook()
    rig.hook.pump()
    assert rig.host.engine.active_keys() == [ident(D)]
    assert rig.host.engine.held_keys() == {ident(D)}    # A is forgotten
    rig.key(D, False)
    rig.hook.pump()
    assert rig.batches() == [(release(A),)]


def test_windows_is_asked_with_the_virtual_key_the_layout_gave(rig):
    asked = []
    rig.host._is_down = lambda vk: asked.append(vk) or False
    azerty = KeyEvent(vk=0x51, scan=A.scan, extended=False, down=True, injected=False,
                      ours=False, time=0)
    rig.host.handle(azerty)
    rig.hook.last_callback = 0
    rig.host.check_hook()
    rig.hook.pump()
    assert 0x51 in asked


def test_a_new_hook_that_fails_leaves_the_state_alone(rig):
    rig.hook.rearm_works = False
    rig.key(A, True)
    rig.hook.last_callback = 0
    rig.host.check_hook()
    rig.hook.pump()
    assert rig.host.engine.active_keys() == [ident(A)]


# -- on and off -------------------------------------------------------------

@pytest.fixture
def known_interval():
    """A value no other test leaves behind, so that putting it back shows."""
    before = sys.getswitchinterval()
    sys.setswitchinterval(0.004)
    yield 0.004
    sys.setswitchinterval(before)


def test_the_switch_interval_is_lowered_while_on_and_put_back_after(tmp_path, known_interval):
    rig = Rig(tmp_path)
    assert sys.getswitchinterval() == pytest.approx(SWITCH_INTERVAL)
    rig.host.configure(enabled=False)
    assert sys.getswitchinterval() == pytest.approx(known_interval)


def test_stopping_puts_everything_back(tmp_path, known_interval):
    rig = Rig(tmp_path)
    hook, watch = rig.hook, FakeWatch.made[-1]
    assert watch.running
    rig.host.stop()
    assert sys.getswitchinterval() == pytest.approx(known_interval)
    assert not hook.running and not watch.running
    assert not any(thread.name == 'snaptap-guard' for thread in threading.enumerate())


def test_turning_off_mid_press_sends_nothing(rig):
    rig.key(A, True)
    rig.key(D, True)
    rig.key(D, False)
    rig.host.configure(enabled=False)
    rig.host.handle(ev(A, False))
    assert rig.batches() == [(release(A),)]


def test_settings_changes_reach_the_engine_through_the_hook_thread(tmp_path):
    """A running hook's thread is the only one to touch the engine."""
    rig = Rig(tmp_path)
    seen = []
    hook = rig.hook
    real_defer = hook.defer

    def defer(job):
        seen.append(job)
        return real_defer(job)

    hook.defer = defer
    try:
        rig.host.configure(pairs=[['KEY_W', 'KEY_S']])
        assert seen
        rig.key(W, True)
        rig.key(S, True)
        assert rig.batches() == [(release(W),)]
    finally:
        rig.host.stop()


def test_off_by_default_there_is_no_hook_and_nothing_changes(tmp_path):
    made = []
    before = sys.getswitchinterval()
    host = SnapTapHost(SnapTapSettings(str(tmp_path / 's.json')),
                       injector=RecordingInjector(), hook_factory=made.append,
                       watch_factory=FakeWatch)
    host.start()
    assert made == []
    assert sys.getswitchinterval() == before
    assert host.handle(ev(A, True)) is False
    assert host.handle(ev(D, True)) is False
    assert host.status()['enabled'] is False and host.status()['running'] is False


def test_a_hook_that_will_not_start_leaves_it_off_and_says_so(tmp_path):
    class Dead(FakeHook):
        def start(self):
            return False

    before = sys.getswitchinterval()
    settings = SnapTapSettings(str(tmp_path / 's.json'))
    settings.update(enabled=True, acknowledged=True)
    host = SnapTapHost(settings, injector=RecordingInjector(), hook_factory=Dead,
                       watch_factory=FakeWatch)
    host.start()
    status = host.status()
    assert status['enabled'] is True and status['running'] is False
    assert sys.getswitchinterval() == before


# -- beside the Fn layer and macros -----------------------------------------

@pytest.fixture
def keyboard(tmp_path):
    """A daemon with key handling and a BlackWidow, on a hook that is not real."""
    fake = FakeRazerDevice(BLACKWIDOW_CHROMA, 'Razer BlackWidow Chroma', interface=2, keys=True)
    backend = FakeHidBackend([fake])
    injector = RecordingInjector()
    hook = SyncHook()
    keys = KeyService(backend, macros=MacroStore(str(tmp_path / 'macros.json')),
                      injector=injector, hook=hook)
    settings = SnapTapSettings(str(tmp_path / 'snaptap.json'))
    settings.update(enabled=True, acknowledged=True, pairs=[['KEY_A', 'KEY_D'], ['KEY_F1', 'KEY_Q']])
    host = SnapTapHost(settings, keys=keys, watch_factory=FakeWatch,
                       foreground=lambda: 'notepad.exe', is_down=lambda vk: False)
    service = DaemonService(backend=backend, persistence=Persistence(str(tmp_path / 'p.json')),
                            enable_effects=False, keys=keys, macros=keys.macros, snaptap=host)
    service.start()
    yield service, keys, service.manager.devices[0], fake, injector, hook
    service.stop()


def wait_for_fn(keys, on=True):
    assert wait_for(lambda: any(b.fn == on for b in keys._bindings.values()))


def test_it_shares_the_key_service_s_hook(keyboard):
    service, keys, _, _, _, hook = keyboard
    assert service.snaptap._hook is hook is keys.hook
    assert keys.snap_tap is service.snaptap
    assert service.snaptap.status()['running'] is True


def test_snap_tap_works_through_the_key_service(keyboard):
    _, keys, _, _, injector, hook = keyboard
    assert keys.handle_key(ev(A, True)) is False
    assert keys.handle_key(ev(D, True)) is False
    assert injector.sent == [('batch', (release(A),))]
    assert keys.handle_key(ev(A, True)) is True         # a suppressed repeat
    keys.handle_key(ev(D, False))
    hook.pump()
    assert injector.sent[-1] == ('batch', (press(A),))


def test_a_key_the_fn_layer_swallowed_never_reaches_snap_tap(keyboard):
    service, keys, _, fake, injector, _ = keyboard
    fake.hold(FN)
    wait_for_fn(keys)
    assert keys.handle_key(ev(F1, True)) is True        # Mute instead
    assert service.snaptap.engine.active_keys() == []
    assert keys.handle_key(ev(F1, False)) is True
    assert not any(entry[0] == 'batch' for entry in injector.sent)


def test_the_fn_layer_still_translates_with_snap_tap_on(keyboard):
    _, keys, _, fake, injector, _ = keyboard
    fake.hold(FN)
    wait_for_fn(keys)
    keys.handle_key(ev(F1, True))
    keys.handle_key(ev(F1, False))
    assert [(entry[1], entry[2]) for entry in injector.sent if entry[0] == 'key'] == [
        (0xAD, True), (0xAD, False)]


def test_a_release_kept_back_by_the_fn_layer_does_not_leave_a_phantom(keyboard):
    """F1 down, Q down over it, Fn down: F1's repeat then belongs to the Fn layer."""
    service, keys, _, fake, injector, hook = keyboard
    keys.handle_key(ev(Q, True))
    keys.handle_key(ev(F1, True))                       # Q is suppressed, F1 active
    fake.hold(FN)
    wait_for_fn(keys)
    keys.handle_key(ev(Q, True))                        # swallowed as a suppressed repeat
    keys.handle_key(ev(F1, True))                       # now translated
    assert keys.handle_key(ev(F1, False)) is True       # kept back, and noted
    assert service.snaptap.engine.active_keys() == []
    hook.pump()
    assert ('batch', (press(Q),)) not in injector.sent


def test_recording_a_macro_gets_the_keys_typed_and_nothing_snap_tap_sent(keyboard):
    service, keys, device, _, injector, hook = keyboard
    service.rpc_macro_record_start(device.serial, 'M3')
    keys.handle_key(ev(A, True, t=0))
    keys.handle_key(ev(D, True, t=10))
    # What Snap Tap sent comes back through the hook, marked as injected.
    keys.handle_key(ev(A, False, injected=True, t=11))
    keys.handle_key(ev(D, False, t=20))
    hook.pump()
    keys.handle_key(ev(A, True, injected=True, t=21))
    keys.handle_key(ev(A, False, t=30))
    saved = service.rpc_macro_record_stop()
    assert [(e['scan'], e['down']) for e in saved['events']] == [
        (A.scan, True), (D.scan, True), (D.scan, False), (A.scan, False)]


def test_playing_a_macro_is_left_alone(keyboard):
    service, keys, _, _, injector, _ = keyboard
    assert keys.handle_key(ev(A, True, injected=True)) is False
    assert keys.handle_key(ev(D, True, injected=True)) is False
    assert service.snaptap.engine.active_keys() == []
    assert injector.sent == []


def test_without_a_key_service_it_has_a_hook_of_its_own(tmp_path):
    """``--no-keys``: nothing else is hooked, so Snap Tap brings its own."""
    made = []

    def factory(on_event):
        hook = SyncHook(on_event)
        hook.start()
        made.append(hook)
        return hook

    host = SnapTapHost(SnapTapSettings(str(tmp_path / 's.json')), injector=RecordingInjector(),
                       hook_factory=factory, watch_factory=FakeWatch,
                       foreground=lambda: 'notepad.exe', is_down=lambda vk: False)
    service = DaemonService(backend=build_demo_backend(), enable_effects=False,
                            persistence=Persistence(str(tmp_path / 'p.json')), snaptap=host)
    service.start()
    try:
        assert service.keys is None and made == []
        service.rpc_snaptap_set(enabled=True, acknowledged=True)
        assert len(made) == 1 and made[0].running
        assert made[0].on_event(ev(A, True)) is False
        assert made[0].on_event(ev(D, True)) is False
        assert host.injector.sent == [('batch', (release(A),))]
        service.rpc_snaptap_set(enabled=False)
        assert not made[0].running and host.status()['running'] is False
    finally:
        service.stop()


def test_a_daemon_with_no_key_service_makes_its_own_settings_file(tmp_path):
    service = DaemonService(backend=build_demo_backend(), enable_effects=False,
                            persistence=Persistence(str(tmp_path / 'p.json')))
    assert service.snaptap.settings.path == str(tmp_path / 'snaptap.json')
    assert service.rpc_snaptap_status()['enabled'] is False


# -- settings ---------------------------------------------------------------

def test_the_file_starts_as_the_design_says(tmp_path):
    settings = SnapTapSettings(str(tmp_path / 'snaptap.json'))
    settings.update(acknowledged=False)
    assert json.loads((tmp_path / 'snaptap.json').read_text()) == {
        'version': 1, 'enabled': False, 'pairs': [['KEY_A', 'KEY_D']], 'deny': ['cs2.exe'],
        'allow': [], 'acknowledged': False}


def test_settings_survive_a_restart(tmp_path):
    path = str(tmp_path / 'snaptap.json')
    SnapTapSettings(path).update(enabled=True, acknowledged=True, pairs=[['w', 's']],
                                 deny=['Game.EXE'], allow=['other.exe'])
    assert SnapTapSettings(path).as_dict() == {
        'version': 1, 'enabled': True, 'pairs': [['KEY_W', 'KEY_S']], 'deny': ['game.exe'],
        'allow': ['other.exe'], 'acknowledged': True}


def test_the_default_deny_entry_can_be_removed_for_good(tmp_path):
    path = str(tmp_path / 'snaptap.json')
    SnapTapSettings(path).update(deny=[])
    assert SnapTapSettings(path).as_dict()['deny'] == []


def test_a_broken_file_is_ignored(tmp_path):
    path = tmp_path / 'snaptap.json'
    path.write_text('{ not json')
    assert SnapTapSettings(str(path)).as_dict()['enabled'] is False


def test_a_file_from_another_version_is_ignored(tmp_path):
    path = tmp_path / 'snaptap.json'
    path.write_text(json.dumps({'version': 2, 'enabled': True, 'acknowledged': True}))
    assert SnapTapSettings(str(path)).enabled is False


def test_a_bad_field_in_the_file_falls_back_on_its_own(tmp_path):
    path = tmp_path / 'snaptap.json'
    path.write_text(json.dumps({'version': 1, 'pairs': [['KEY_A', 'KEY_LEFTMETA']],
                                'deny': ['x.exe']}))
    data = SnapTapSettings(str(path)).as_dict()
    assert data['pairs'] == [['KEY_A', 'KEY_D']]
    assert data['deny'] == ['x.exe']


def test_editing_the_file_is_no_way_around_the_warning(tmp_path):
    path = tmp_path / 'snaptap.json'
    path.write_text(json.dumps({'version': 1, 'enabled': True, 'acknowledged': False}))
    assert SnapTapSettings(str(path)).enabled is False


def test_a_refused_change_changes_nothing(tmp_path):
    path = str(tmp_path / 'snaptap.json')
    settings = SnapTapSettings(path)
    with pytest.raises(SnapTapError):
        settings.update(deny=['x.exe'], pairs=[['KEY_A', 'KEY_A']])
    assert settings.as_dict()['deny'] == ['cs2.exe']


def test_no_temporary_file_is_left_behind(tmp_path):
    SnapTapSettings(str(tmp_path / 'snaptap.json')).update(deny=[])
    assert [path.name for path in tmp_path.iterdir()] == ['snaptap.json']


@pytest.mark.parametrize('pairs, message', [
    ([['KEY_LEFTMETA', 'KEY_A']], 'cannot be in a pair'),
    ([['KEY_RIGHTMETA', 'KEY_A']], 'cannot be in a pair'),
    ([['win', 'KEY_A']], 'cannot be in a pair'),
    ([['KEY_PAUSE', 'KEY_A']], 'cannot be in a pair'),
    ([['KEY_LEFTALT', 'KEY_A']], 'cannot be in a pair'),
    ([['KEY_A', 'KEY_A']], 'itself'),
    ([['a', 'KEY_A']], 'itself'),
    ([['KEY_A', 'KEY_D'], ['KEY_D', 'KEY_W']], 'already in a pair'),
    ([['KEY_A', 'KEY_D'], ['KEY_D', 'KEY_A']], 'already in a pair'),
    ([['KEY_MENU', 'KEY_A']], 'no key called'),
    ([['KEY_FN', 'KEY_A']], 'no key called'),
    ([['KEY_MUTE', 'KEY_A']], 'no key called'),
    ([['KEY_A']], 'two keys'),
    ([['KEY_A', 'KEY_D', 'KEY_W']], 'two keys'),
    ([['KEY_A', 5]], 'key name'),
    ('KEY_A', 'list'),
])
def test_bad_pairs_are_refused(pairs, message):
    with pytest.raises(SnapTapError, match=message):
        clean_pairs(pairs)


def test_keys_can_be_given_as_people_write_them():
    assert clean_pairs([['a', 'D'], ['left', 'ctrl']]) == [
        ['KEY_A', 'KEY_D'], ['KEY_LEFT', 'KEY_LEFTCTRL']]


@pytest.mark.parametrize('names', [['cs2'], ['C:\\games\\cs2.exe'], ['a/b.exe'], [''], [3], 'x.exe'])
def test_bad_programs_are_refused(names):
    with pytest.raises(SnapTapError):
        clean_programs(names)


def test_programs_are_lower_cased_and_not_repeated():
    assert clean_programs(['CS2.exe', ' cs2.EXE ']) == ['cs2.exe']


# -- the daemon's API -------------------------------------------------------

@pytest.fixture
def service(tmp_path):
    hooks = []

    def factory(on_event):
        hook = SyncHook(on_event)
        hook.start()
        hooks.append(hook)
        return hook

    host = SnapTapHost(SnapTapSettings(str(tmp_path / 'snaptap.json')),
                       injector=RecordingInjector(), hook_factory=factory,
                       watch_factory=FakeWatch, foreground=lambda: 'notepad.exe',
                       is_down=lambda vk: False)
    made = DaemonService(backend=build_demo_backend(), enable_effects=False,
                         persistence=Persistence(str(tmp_path / 'p.json')), snaptap=host)
    made.start()
    yield made
    made.stop()


def test_status_says_what_it_is(service):
    status = service.rpc_snaptap_status()
    assert status['enabled'] is False and status['acknowledged'] is False
    assert status['pairs'] == [['KEY_A', 'KEY_D']] and status['deny'] == ['cs2.exe']
    assert status['scope'] == 'applies to every keyboard'
    assert status['warning'] == WARNING
    assert 'KEY_LEFTMETA' not in status['keys'] and 'KEY_PAUSE' not in status['keys']
    assert status['keys'][0] == 'KEY_A'
    assert status['latency_us'] == {'samples': 0, 'p50': None, 'p99': None, 'max': None}


def test_turning_it_on_needs_the_warning_acknowledged_once(service):
    with pytest.raises(RpcError) as raised:
        service.rpc_snaptap_set(enabled=True)
    assert raised.value.code == INVALID_PARAMS
    assert WARNING in str(raised.value)
    assert service.rpc_snaptap_status()['enabled'] is False
    assert service.rpc_snaptap_set(enabled=True, acknowledged=True)['running'] is True
    service.rpc_snaptap_set(enabled=False)
    assert service.rpc_snaptap_set(enabled=True)['enabled'] is True


@pytest.mark.parametrize('changes', [
    {'pairs': [['KEY_A', 'KEY_A']]}, {'deny': ['nope']}, {'allow': 'x.exe'},
    {'enabled': 'yes'}, {'acknowledged': 1},
])
def test_bad_values_are_refused_over_rpc(service, changes):
    with pytest.raises(RpcError) as raised:
        service.rpc_snaptap_set(**changes)
    assert raised.value.code == INVALID_PARAMS


def test_the_methods_are_on_the_allowlist(service):
    server = DaemonServer(service, port=0)
    try:
        assert server.methods['snaptap.status'] == service.rpc_snaptap_status
        assert server.methods['snaptap.set'] == service.rpc_snaptap_set
    finally:
        server.server_close()


def test_it_comes_back_on_with_the_daemon(tmp_path):
    settings = SnapTapSettings(str(tmp_path / 'snaptap.json'))
    settings.update(enabled=True, acknowledged=True)
    hook = SyncHook()

    def factory(on_event):
        hook.start()
        return hook

    host = SnapTapHost(settings, injector=RecordingInjector(), hook_factory=factory,
                       watch_factory=FakeWatch, foreground=lambda: 'x.exe',
                       is_down=lambda vk: False)
    made = DaemonService(backend=build_demo_backend(), enable_effects=False,
                         persistence=Persistence(str(tmp_path / 'p.json')), snaptap=host)
    made.start()
    try:
        assert host.status()['running'] is True
    finally:
        made.stop()


# -- the command line -------------------------------------------------------

@pytest.fixture
def daemon(tmp_path, monkeypatch, service):
    server = DaemonServer(service, port=0)
    path = str(tmp_path / 'daemon.json')
    server.endpoint.write(path)
    monkeypatch.setattr('openrazer_win.daemon.protocol.endpoint_path', lambda: path)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05},
                              daemon=True)
    thread.start()
    yield service
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_the_cli_shows_where_things_stand(daemon, capsys):
    assert main(['snaptap']) == 0
    output = capsys.readouterr().out
    assert 'off' in output and 'A + D' in output and 'cs2.exe' in output
    assert 'applies to every keyboard' in output


def test_the_cli_shows_the_warning_and_needs_an_answer(daemon, capsys, monkeypatch):
    monkeypatch.setattr('sys.stdin', type('NoTerminal', (), {'isatty': lambda self: False})())
    assert main(['snaptap', 'on']) == 2
    captured = capsys.readouterr()
    assert WARNING in captured.err and '--accept' in captured.err
    assert daemon.rpc_snaptap_status()['enabled'] is False


def test_the_cli_takes_no_for_an_answer(daemon, capsys, monkeypatch):
    monkeypatch.setattr('sys.stdin', type('Terminal', (), {'isatty': lambda self: True})())
    monkeypatch.setattr('builtins.input', lambda prompt: 'n')
    assert main(['snaptap', 'on']) == 0
    assert daemon.rpc_snaptap_status()['acknowledged'] is False


def test_the_cli_turns_it_on_and_off(daemon, capsys):
    assert main(['snaptap', 'on', '--accept']) == 0
    captured = capsys.readouterr()
    assert WARNING in captured.err
    assert 'Some games forbid this' in captured.out and 'working' in captured.out
    assert daemon.rpc_snaptap_status()['enabled'] is True
    assert main(['snaptap', 'off']) == 0
    assert daemon.rpc_snaptap_status()['enabled'] is False
    assert main(['snaptap', 'on']) == 0     # asked once only
    assert WARNING not in capsys.readouterr().err


def test_the_cli_manages_pairs(daemon, capsys):
    assert main(['snaptap', 'pair', 'add', 'w', 's']) == 0
    assert daemon.rpc_snaptap_status()['pairs'] == [['KEY_A', 'KEY_D'], ['KEY_W', 'KEY_S']]
    assert main(['snaptap', 'pair', 'remove', 's', 'w']) == 0
    assert main(['snaptap', 'pair', 'remove', 'a', 'd']) == 0
    assert daemon.rpc_snaptap_status()['pairs'] == []
    assert main(['snaptap', 'pair', 'remove', 'a', 'd']) == 2
    assert main(['snaptap', 'pair', 'add', 'a']) == 2
    assert main(['snaptap', 'pair', 'add', 'a', 'a']) == 2
    assert main(['snaptap', 'pair', 'add', 'win', 'a']) == 2


def test_the_cli_manages_the_program_lists(daemon, capsys):
    assert main(['snaptap', 'deny', 'remove', 'cs2.exe']) == 0
    assert daemon.rpc_snaptap_status()['deny'] == []
    assert main(['snaptap', 'deny', 'add', 'Game.exe']) == 0
    assert daemon.rpc_snaptap_status()['deny'] == ['game.exe']
    assert main(['snaptap', 'allow', 'add', 'other.exe']) == 0
    assert main(['snaptap', 'allow', 'remove', 'other.exe']) == 0
    assert daemon.rpc_snaptap_status()['allow'] == []
    assert main(['snaptap', 'allow', 'remove', 'other.exe']) == 2
    assert main(['snaptap', 'deny', 'add', 'nope']) == 2
    assert main(['snaptap', 'deny', 'add']) == 2


def test_the_cli_speaks_json(daemon, capsys):
    assert main(['snaptap', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['pairs'] == [['KEY_A', 'KEY_D']]


def test_the_cli_says_when_there_is_no_daemon(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('openrazer_win.daemon.protocol.endpoint_path',
                        lambda: str(tmp_path / 'absent.json'))
    assert main(['snaptap']) == 2
    assert 'daemon start' in capsys.readouterr().err


def test_on_takes_nothing_more(daemon, capsys):
    assert main(['snaptap', 'on', 'add']) == 2


# -- what is said about it --------------------------------------------------

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_studio_shows_the_same_warning():
    source = (ROOT / 'studio/app/src/ui/snaptap.rs').read_text(encoding='utf-8')
    assert WARNING in source


def test_nothing_says_it_is_safe_or_hidden():
    texts = [WARNING]
    for name in ('README.md', 'CHANGELOG.md', 'openrazer_win/keys/snaptap.py',
                 'openrazer_win/keys/snaptap_host.py', 'studio/app/src/ui/snaptap.rs'):
        texts.append((ROOT / name).read_text(encoding='utf-8'))
    for word in ('undetect', 'anti-cheat safe', 'safe to use', 'bypass anti'):
        assert not any(word in text.lower() for text in texts), word


def test_the_engine_touches_no_windows_api():
    source = (ROOT / 'openrazer_win/keys/snaptap.py').read_text(encoding='utf-8')
    assert 'ctypes' not in source


def test_the_latency_tool_only_works_when_asked():
    import importlib.util

    spec = importlib.util.spec_from_file_location('snaptap_latency',
                                                  ROOT / 'tools/snaptap_latency.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    summary = tool.summarise([0.1] * 98 + [0.5, 25.0])
    assert summary['count'] == 100 and summary['max_ms'] == 25.0
    assert not tool.passes_gate(summary)
    assert tool.passes_gate({'p99_ms': 0.9, 'max_ms': 19.0})
    assert not tool.passes_gate({'p99_ms': 1.0, 'max_ms': 19.0})


# -- timing and the real hook -----------------------------------------------

def test_timings_report_percentiles_in_microseconds():
    timings = Timings()
    for value in range(1, 1001):
        timings.add(value * 1000)
    summary = timings.summary()
    assert summary['samples'] == 1000
    assert summary['p50'] == 500.0 and summary['p99'] == 990.0 and summary['max'] == 1000.0


def test_timings_keep_only_the_latest():
    timings = Timings()
    for _ in range(Timings.SIZE):
        timings.add(1000)
    timings.add(5_000_000)
    assert timings.summary()['samples'] == Timings.SIZE
    assert timings.summary()['max'] == 5000.0


def test_status_carries_the_timings(rig):
    rig.hook.timings.add(2000)
    assert rig.host.status()['latency_us']['max'] == 2.0


def test_the_injector_sends_by_scan_code_with_the_marker(monkeypatch):
    from openrazer_win.keys import inject

    sent = []

    def fake_send(count, array, size):
        sent.extend(array[index].union.ki for index in range(count))
        return count

    injector = inject.Injector()
    injector._send = fake_send
    assert injector.batch([(0x1E, False, False), (0x48, True, True)]) == 2
    assert [(k.wVk, k.wScan, k.dwFlags, k.dwExtraInfo) for k in sent] == [
        (0, 0x1E, inject.KEYEVENTF_SCANCODE | inject.KEYEVENTF_KEYUP, inject.MARKER),
        (0, 0x48, inject.KEYEVENTF_SCANCODE | inject.KEYEVENTF_EXTENDEDKEY, inject.MARKER)]


def test_a_hook_with_no_thread_declines_deferred_work():
    assert FilterHook(lambda event: False).defer(lambda: None) is False


def test_the_real_hook_installs_runs_deferred_work_and_shuts_down(tmp_path):
    """Install the real hook and the window watch, for a moment, on Windows only.

    The pair is F23 and F24, on no keyboard here, and the injector only writes
    down what it would send, so whoever is typing meanwhile is not touched.
    """
    if not is_available():
        pytest.skip('the OS hook is a Windows feature')

    settings = SnapTapSettings(str(tmp_path / 'snaptap.json'))
    settings.update(enabled=True, acknowledged=True, pairs=[['KEY_F23', 'KEY_F24']])
    host = SnapTapHost(settings, injector=RecordingInjector())
    try:
        host.start()
        hook = host._hook
        assert hook is not None and hook.running, 'SetWindowsHookEx failed; see the log'
        assert host.status()['running'] is True
        assert host._watch._hooks, 'SetWinEventHook failed; see the log'

        ran = threading.Event()
        answers = []
        assert hook.defer(lambda: (answers.append(hook.rearm()), ran.set()))
        assert ran.wait(2.0), 'deferred work did not run on the hook thread'
        assert answers == [True]
    finally:
        host.stop()
    assert not hook.running
    assert hook._hook is None


def test_the_real_hook_can_be_started_again_at_once():
    """Regression guard: start() after stop() returned before the new hook existed."""
    if not is_available():
        pytest.skip('the OS hook is a Windows feature')

    hook = FilterHook(lambda event: False)
    try:
        assert hook.start()
        hook.stop()
        assert not hook.running and hook._thread_id is None
        assert hook.start()
        assert hook.running and hook._hook is not None
        ran = threading.Event()
        assert hook.defer(ran.set)
        assert ran.wait(2.0)
    finally:
        hook.stop()


def test_the_module_has_no_import_time_windows_needs():
    assert callable(snaptap_host.foreground_exe)
