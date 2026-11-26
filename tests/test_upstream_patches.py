"""Devices taken from upstream pull requests that are still open.

``tools/upstream_patches/`` holds those pull requests' driver and daemon
changes, and ``tools/upstream_source.py`` applies them to a scratch copy of the
upstream checkout before the generators read it.
"""
from __future__ import annotations

import os
import sys

import pytest

from .conftest import last_report, make_device

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))
import upstream_source  # noqa: E402

PATCH = """diff --git a/driver/x.c b/driver/x.c
--- a/driver/x.c
+++ b/driver/x.c
@@ -1,3 +1,4 @@
 a
 b
+new
 c
"""


@pytest.fixture
def patches(tmp_path, monkeypatch):
    folder = tmp_path / 'patches'
    folder.mkdir()
    monkeypatch.setattr(upstream_source, 'PATCH_DIR', str(folder))
    return folder


def _add_patch(folder, name='one.patch'):
    with open(str(folder / name), 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(PATCH)


def _upstream(root, text: bytes) -> str:
    os.makedirs(str(root / 'driver'))
    os.makedirs(str(root / 'daemon'))
    with open(str(root / 'driver' / 'x.c'), 'wb') as handle:
        handle.write(text)
    return str(root)


def _read(root: str) -> bytes:
    """The file's lines; git writes them back with whatever core.autocrlf says."""
    with open(os.path.join(root, 'driver', 'x.c'), 'rb') as handle:
        return handle.read().replace(b'\r\n', b'\n')


def test_without_patches_the_checkout_is_read_in_place(tmp_path, patches):
    upstream = _upstream(tmp_path / 'up', b'a\nb\nc\n')
    with upstream_source.checkout(upstream) as checkout:
        assert checkout == upstream


def test_a_pending_patch_applies_to_a_windows_checkout(tmp_path, patches):
    _add_patch(patches)
    # git on Windows checks out with core.autocrlf, the patches are LF.
    upstream = _upstream(tmp_path / 'up', b'a\r\nb\r\nc\r\n')
    with upstream_source.checkout(upstream) as checkout:
        assert checkout != upstream
        assert _read(checkout) == b'a\nb\nnew\nc\n'
    assert _read(upstream) == b'a\nb\nc\n'          # the checkout itself is left alone


def test_a_patch_upstream_already_carries_is_skipped(tmp_path, patches, capsys):
    _add_patch(patches)
    upstream = _upstream(tmp_path / 'up', b'a\nb\nnew\nc\n')
    with upstream_source.checkout(upstream) as checkout:
        assert _read(checkout) == b'a\nb\nnew\nc\n'
    assert 'already carries one.patch' in capsys.readouterr().err


def test_a_patch_that_no_longer_applies_stops_the_sync(tmp_path, patches):
    _add_patch(patches)
    upstream = _upstream(tmp_path / 'up', b'x\ny\nz\n')
    with pytest.raises(SystemExit, match='no longer applies'):
        with upstream_source.checkout(upstream):
            pass


# -- what the shipped patches bring ------------------------------------------

@pytest.mark.parametrize('pid, transaction_id', [(0x0277, 0x1F), (0x027B, 0x9F)])
def test_the_pro_type_ultra_is_driven_on_its_third_interface(pid, transaction_id,
                                                             recipes, persistence):
    # openrazer-win#11: wired and wireless both carry the 91-byte feature
    # report on mi=02, which is the interface openrazer#2888 addresses.
    assert recipes.transport_params('kbd', pid)['index'] == 2
    device, fake = make_device(pid, persistence)
    try:
        device.set_static(255, 255, 255)
        report = last_report(fake, 0x0F, 0x02)
        assert report.transaction_id == transaction_id
        # Backlight LED, static, and the pull request's 0x01 in argument 4.
        assert list(report.arguments[:9]) == [0x01, 0x05, 0x01, 0x00, 0x01,
                                              0x01, 0xFF, 0xFF, 0xFF]
    finally:
        device.close()


# -- the Mouse Dock Pro's transaction id (issue #22) -------------------------

@pytest.mark.parametrize('apply', [
    lambda device: device.set_static(255, 0, 0),
    lambda device: device.set_spectrum(),
    lambda device: device.set_wave(1),
    lambda device: device.set_breath_random(),
    lambda device: device.set_breath_single(0, 255, 0),
    lambda device: device.set_none(),
    lambda device: device.set_brightness(40),
])
def test_the_mouse_dock_pro_is_only_ever_spoken_to_with_0xff(apply, persistence):
    # It answers "timeout" to anything else, and upstream sends the mode
    # command every effect starts with as 0x3F.
    device, fake = make_device(0x00A4, persistence)
    try:
        fake.state.sent.clear()
        apply(device)
        assert fake.sent_reports
        assert {report.transaction_id for report in fake.sent_reports} == {0xFF}
    finally:
        device.close()


def test_other_accessories_keep_their_transaction_ids(persistence):
    device, fake = make_device(0x0F26, persistence)          # Charging Pad Chroma
    try:
        fake.state.sent.clear()
        device.set_spectrum()
        ids = [report.transaction_id for report in fake.sent_reports
               if (report.command_class, report.command_id) in ((0x00, 0x04), (0x0F, 0x02))]
        assert ids == [0x3F, 0x1F]
    finally:
        device.close()


# -- the wave's speed (issue #22) ---------------------------------------------

def test_a_wave_of_the_newer_kind_takes_a_speed(persistence):
    device, fake = make_device(0x00A4, persistence)
    try:
        assert device.has_wave_speed()
        device.set_wave(1)
        assert list(last_report(fake, 0x0F, 0x02).arguments[:5]) == [0x01, 0x00, 0x04, 0x02, 0x28]
        device.set_wave(1, speed=80)
        assert last_report(fake, 0x0F, 0x02).arguments[4] == 80
        assert persistence.get(device.serial, 'backlight', 'wave_speed') == 80
        fake.state.sent.clear()
        device.restore()
        assert last_report(fake, 0x0F, 0x02).arguments[4] == 80
        # Without one it is the driver's own again, and that is remembered too.
        device.set_wave(1)
        assert last_report(fake, 0x0F, 0x02).arguments[4] == 0x28
        assert persistence.get(device.serial, 'backlight', 'wave_speed') is None
    finally:
        device.close()


def test_a_wave_of_the_older_kind_has_no_speed(persistence):
    from openrazer_win.devices.recipes import NotSupported
    device, fake = make_device(0x0203, persistence)          # BlackWidow Chroma
    try:
        assert not device.has_wave_speed()
        with pytest.raises(NotSupported):
            device.set_wave(1, speed=80)
        device.set_wave(1)
        assert device.capabilities()['zones']['backlight']['wave_speed'] is False
    finally:
        device.close()


def test_studio_is_told_which_waves_take_a_speed_and_can_give_one(persistence):
    device, fake = make_device(0x00A4, persistence)          # Mouse Dock Pro
    try:
        zones = device.capabilities()['zones']
        assert zones['backlight']['wave_speed'] is True
        assert not any(z['wave_speed'] for name, z in zones.items() if 'wave' not in z['effects'])
        # The zone is a keyword over RPC, so the speed comes second.
        device.set_wave_with_speed(2, 10, zone='backlight')
        assert last_report(fake, 0x0F, 0x02).arguments[4] == 10
        assert persistence.get(device.serial, 'backlight', 'wave_speed') == 10
        assert persistence.get(device.serial, 'backlight', 'wave_dir') == 2
    finally:
        device.close()


def test_a_speed_with_the_wave_is_allowed_over_rpc():
    from openrazer_win.daemon.server import DEVICE_METHODS, HARDWARE_EFFECTS
    assert 'set_wave_with_speed' in DEVICE_METHODS & HARDWARE_EFFECTS


# -- the retail DeathAdder 2000 (issue #20, openrazer/openrazer#2931) ---------
# Upstream master carries it since 7dd238c, so its patch is gone; this checks
# that the sync kept it the twin of the other DeathAdder 2000.

def test_the_retail_deathadder_2000_is_the_other_one_under_a_second_id(recipes, persistence):
    for attribute in ('read:dpi', 'write:dpi', 'write:poll_rate', 'write:logo_matrix_effect_on',
                      'write:scroll_matrix_effect_breath', 'write:logo_led_brightness'):
        assert recipes.lookup('mouse', attribute, 0x005F) == recipes.lookup('mouse', attribute, 0x004F)
