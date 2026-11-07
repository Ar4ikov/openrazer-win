"""Snap Tap: of two opposing keys, the one pressed last wins.

The Huntsman V2 Tenkeyless has no Snap Tap in its firmware -- with Synapse
closed the keyboard does nothing of the kind, so there is no USB command to
send.  Synapse does it on the PC, and so does this.

The engine is additive.  Physical keys go on as they are, and all it does is
add events: holding A and pressing D sends A's release just before D, and
letting go of D sends A's press again if A is still held.  The one thing it
ever holds back is the auto-repeat of a key that lost to its partner, because
a repeat is a press to anything reading it.  It never holds back a release: a
release that goes missing is a key stuck down, while one too many does nothing.

This module knows nothing of Windows.  Keys are ``(scan code, extended flag)``
pairs, which name a position on the board whatever the layout says it types,
and events are anything with the fields of :class:`~.hook.KeyEvent`.  What
surrounds it on Windows is in ``snaptap_host``; another host could take its
place.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from typing import Callable, Iterable, NamedTuple, Optional, Tuple

from ..core.persistence import default_config_dir
from .codes import KEYS, canonical_name

logger = logging.getLogger(__name__)

#: Shown before Snap Tap is first turned on, by the command line and by Studio.
#: Studio has its own copy to translate; a test holds the two together.
WARNING = (
    'Snap Tap changes your key presses in software. Some games forbid this: '
    'Counter-Strike 2 may kick you from official matches, and tournaments or other '
    'platforms may go further. Turn it off in those games. It does nothing in games '
    'running as administrator, and it may not work in every game.')

#: What sits beside the switch.
WARNING_SHORT = 'Some games forbid this. Turn it off in those games.'

#: The hook sees every keyboard as one: it cannot tell which sent a key.
SCOPE = 'applies to every keyboard'

SETTINGS_VERSION = 1
DEFAULT_PAIRS = (('KEY_A', 'KEY_D'),)
#: A starting point and nothing more: the user may take it out, and it stays out.
DEFAULT_DENY = ('cs2.exe',)

#: Keys a pair cannot use.  The Windows keys and Alt belong to Windows' own
#: shortcuts, and holding either turns Snap Tap off, so a pair of them could
#: never work; Pause is reported as Num Lock without its extended flag.
UNPAIRABLE = frozenset(('KEY_LEFTMETA', 'KEY_RIGHTMETA', 'KEY_LEFTALT', 'KEY_RIGHTALT',
                        'KEY_PAUSE'))
PAIRABLE = frozenset(name for name in KEYS if name not in UNPAIRABLE)

#: Left and right Alt and Windows, as virtual keys.  While one is held the keys
#: are a shortcut, not movement: Alt+D must not release a held A.  Ctrl is not
#: here on purpose, because crouching while strafing is Ctrl and two opposing
#: keys.
BYPASS_VKS = frozenset((0xA4, 0xA5, 0x5B, 0x5C))

_EXE_NAME = re.compile(r'^[^\\/:*?"<>|]+\.exe$')

KeyId = Tuple[int, bool]


class SnapTapError(ValueError):
    """Settings that cannot be used; the message says why."""


class Decision(NamedTuple):
    """What to do about one event."""

    #: Keep the event from everything after the hook.
    swallow: bool = False
    #: ``(scan, extended, down)`` to send before the event goes on.
    now: tuple = ()
    #: Ask :meth:`SnapTapEngine.take_resumes` once the callback has returned.
    #: Not sooner: the release of the key that was on top has to arrive first,
    #: or the two are both down for a moment.
    later: bool = False


PASS = Decision()
SWALLOW = Decision(swallow=True)
RESUME = Decision(later=True)


class _Pair:
    __slots__ = ('partner', 'release', 'active', 'held', 'vks', 'pending')

    def __init__(self, first: KeyId, second: KeyId):
        self.partner = {first: second, second: first}
        #: Built once: the callback should not be allocating.
        self.release = {key: Decision(now=(key + (False,),)) for key in (first, second)}
        #: The key that is winning.  One key, not a flag per key, so the two
        #: cannot both be active.
        self.active: Optional[KeyId] = None
        #: The keys physically down, as far as this has seen.
        self.held: set = set()
        #: The virtual key each key last came with.  Asking Windows about a key
        #: takes one, and which one depends on the layout.
        self.vks: dict = {}
        #: The key waiting to be pressed again.
        self.pending: Optional[KeyId] = None

    def clear(self) -> None:
        self.active = None
        self.pending = None
        self.held.clear()


class SnapTapEngine:
    """The state of every pair.  Not thread safe: one thread owns it."""

    def __init__(self, pairs: Iterable = ()):
        self._pairs: dict = {}
        self._all: list = []
        self._bypass: set = set()
        #: Set while the program in front is one Snap Tap stays out of.
        self.paused = False
        self.configure(pairs)

    def configure(self, pairs: Iterable) -> None:
        """Start over with `pairs`, each two :data:`KeyId`."""
        self._all = [_Pair(first, second) for first, second in pairs]
        self._pairs = {key: pair for pair in self._all for key in pair.partner}
        self._bypass = set()

    # -- the callback ------------------------------------------------------
    def handle(self, event) -> Decision:
        if event.injected or self.paused:
            return PASS
        vk = event.vk
        if vk in BYPASS_VKS:
            if not event.down:
                self._bypass.discard(vk)
                return PASS
            # Everything is forgotten rather than carried across, so nothing
            # known from before the shortcut can be pressed again after it.  A
            # key still held simply goes unnoticed until it next repeats.
            if not self._bypass:
                self._forget()
            self._bypass.add(vk)
            return PASS
        if self._bypass:
            return PASS
        key = (event.scan, event.extended)
        pair = self._pairs.get(key)
        if pair is None:
            return PASS
        partner = pair.partner[key]
        if event.down:
            return self._down(pair, key, partner, vk)
        return self._up(pair, key, partner)

    def _down(self, pair: _Pair, key: KeyId, partner: KeyId, vk: int) -> Decision:
        if key in pair.held:
            if pair.active == key:
                return PASS
            if pair.active is not None:
                return SWALLOW
            pair.active = key
            return PASS
        pair.held.add(key)
        pair.vks[key] = vk
        taken_over = pair.active == partner
        pair.active = key
        if taken_over:
            pair.pending = None
            return pair.release[partner]
        return PASS

    def _up(self, pair: _Pair, key: KeyId, partner: KeyId) -> Decision:
        pair.held.discard(key)
        if pair.pending == key:
            # Let go before its press could be sent: sending it now would hold
            # down a key nobody is holding.
            pair.pending = None
        if pair.active != key:
            return PASS
        if partner in pair.held:
            pair.active = partner
            pair.pending = partner
            return RESUME
        pair.active = None
        return PASS

    def take_resumes(self) -> list:
        """The presses that are due: ``(scan, extended, True)`` for each.

        Asked for after the callback, so anything that has happened since --
        the key let go, the partner back, a reset -- has already cancelled it.
        """
        due = []
        for pair in self._all:
            if pair.pending is not None:
                due.append(pair.pending + (True,))
                pair.pending = None
        return due

    def swallowed(self, event) -> None:
        """Note a release something else kept back, so it is not waited for.

        The Fn layer holds back a key that was down when Fn went down.  Taken
        for still held, that key would be pressed again when its partner goes.
        """
        if event.down or event.injected:
            return
        self._bypass.discard(event.vk)
        key = (event.scan, event.extended)
        pair = self._pairs.get(key)
        if pair is None:
            return
        pair.held.discard(key)
        if pair.pending == key:
            pair.pending = None
        if pair.active == key:
            pair.active = None

    # -- the world changing ------------------------------------------------
    def _forget(self) -> None:
        for pair in self._all:
            pair.clear()

    def reset(self, is_down: Optional[Callable[[int], bool]] = None) -> None:
        """Forget every key, sending nothing.

        After a switch of window or desktop a key's release may have gone
        somewhere the hook cannot see.  Remembering such a key as held is the
        one real way to a stuck key: its partner's release would press it
        again, with no hand on it to ever let go.

        Alt and the Windows keys are asked about rather than forgotten: Alt is
        still down in the middle of Alt+Tab, and that is when the window
        changes.
        """
        self._forget()
        self._bypass = {vk for vk in BYPASS_VKS if is_down(vk)} if is_down else set()

    def rearm(self, is_down: Callable[[int], bool]) -> None:
        """Pick up again after a time when the hook may have heard nothing.

        The active key stays only if Windows agrees that it is down.  A key
        that lost to it does not stay, because Windows has it as released
        either way, and only the hook could have seen the hand come off it.
        """
        for pair in self._all:
            active = pair.active
            still_down = active is not None and is_down(pair.vks.get(active, 0))
            pair.clear()
            if still_down:
                pair.active = active
                pair.held.add(active)
        self._bypass = {vk for vk in BYPASS_VKS if is_down(vk)}

    # -- for tests and status ----------------------------------------------
    def active_keys(self) -> list:
        return [pair.active for pair in self._all if pair.active is not None]

    def held_keys(self) -> set:
        return {key for pair in self._all for key in pair.held}


def pause_reason(exe: Optional[str], deny: Iterable, allow: Iterable) -> Optional[str]:
    """Why Snap Tap should stay out of the program `exe`, or ``None``.

    A program with no name to check is not given the benefit of the doubt: a
    game that hides itself would otherwise slip past the deny list.
    """
    if not exe:
        return 'unknown program'
    if exe in deny:
        return 'denied'
    if allow and exe not in allow:
        return 'not allowed'
    return None


# -- settings ---------------------------------------------------------------

def _key_name(text) -> str:
    if not isinstance(text, str) or not text.strip():
        raise SnapTapError('a key name is needed, not {0!r}'.format(text))
    name = canonical_name(text)
    if name in UNPAIRABLE:
        raise SnapTapError('{0} cannot be in a pair'.format(name))
    if name not in PAIRABLE:
        raise SnapTapError('no key called {0}'.format(text.strip()))
    return name


def clean_pairs(pairs) -> list:
    """`pairs` as canonical key names, or :class:`SnapTapError`."""
    if not isinstance(pairs, (list, tuple)):
        raise SnapTapError('pairs must be a list of two-key lists')
    cleaned, used = [], set()
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise SnapTapError('a pair is two keys, not {0!r}'.format(pair))
        first, second = _key_name(pair[0]), _key_name(pair[1])
        if first == second:
            raise SnapTapError('{0} cannot be paired with itself'.format(first))
        for name in (first, second):
            if name in used:
                raise SnapTapError('{0} is already in a pair'.format(name))
            used.add(name)
        cleaned.append([first, second])
    return cleaned


def clean_programs(names) -> list:
    """Executable names, lower-cased and without repeats."""
    if not isinstance(names, (list, tuple)):
        raise SnapTapError('programs must be a list of executable names')
    cleaned = []
    for name in names:
        if not isinstance(name, str):
            raise SnapTapError('not an executable name: {0!r}'.format(name))
        name = name.strip().lower()
        if not _EXE_NAME.match(name):
            raise SnapTapError('{0!r} is not an executable name such as game.exe'.format(name))
        if name not in cleaned:
            cleaned.append(name)
    return cleaned


def key_ids(pairs: Iterable) -> list:
    """Canonical pairs as the engine wants them."""
    return [tuple((KEYS[name].scan, KEYS[name].extended) for name in pair) for pair in pairs]


class SnapTapSettings:
    """``snaptap.json``, beside the daemon's other state."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(default_config_dir(), 'snaptap.json')
        self._lock = threading.Lock()
        self._data = self._defaults()
        self._load()

    @staticmethod
    def _defaults() -> dict:
        return {'version': SETTINGS_VERSION, 'enabled': False,
                'pairs': [list(pair) for pair in DEFAULT_PAIRS],
                'deny': list(DEFAULT_DENY), 'allow': [], 'acknowledged': False}

    def _load(self) -> None:
        try:
            with open(self.path, encoding='utf-8') as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning('ignoring unreadable %s', self.path, exc_info=True)
            return
        if not isinstance(data, dict) or data.get('version') != SETTINGS_VERSION:
            logger.warning('ignoring %s: not a settings file this version can read', self.path)
            return
        for field, clean in (('pairs', clean_pairs), ('deny', clean_programs),
                             ('allow', clean_programs)):
            # Missing means the file is older than the field.  An empty list
            # is the user's own choice and is left empty.
            if field not in data:
                continue
            try:
                self._data[field] = clean(data[field])
            except SnapTapError as error:
                logger.warning('ignoring %s in %s: %s', field, self.path, error)
        self._data['acknowledged'] = data.get('acknowledged') is True
        # Editing the file is no way around the warning.
        self._data['enabled'] = data.get('enabled') is True and self._data['acknowledged']

    def update(self, enabled: Optional[bool] = None, pairs=None, deny=None, allow=None,
               acknowledged: Optional[bool] = None) -> None:
        """Change what is given, all of it or none of it."""
        if enabled is not None and not isinstance(enabled, bool):
            raise SnapTapError('enabled is true or false')
        if acknowledged is not None and not isinstance(acknowledged, bool):
            raise SnapTapError('acknowledged is true or false')
        with self._lock:
            data = dict(self._data)
            if pairs is not None:
                data['pairs'] = clean_pairs(pairs)
            if deny is not None:
                data['deny'] = clean_programs(deny)
            if allow is not None:
                data['allow'] = clean_programs(allow)
            if acknowledged:
                data['acknowledged'] = True
            if enabled is not None:
                if enabled and not data['acknowledged']:
                    raise SnapTapError('the warning has to be acknowledged first: ' + WARNING)
                data['enabled'] = enabled
            self._write(data)
            self._data = data

    def _write(self, data: dict) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory,
                                             delete=False, suffix='.tmp')
        try:
            json.dump(data, handle, indent=1, sort_keys=True)
        finally:
            handle.close()
        os.replace(handle.name, self.path)

    def as_dict(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._data['enabled']
