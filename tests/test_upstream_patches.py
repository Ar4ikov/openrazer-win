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


# -- the Basilisk V4 Pro (openrazer-win#31, openrazer/openrazer#2975) ---------

BASILISK_V4_PRO = (0x00E2, 0x00E3)


@pytest.fixture(params=BASILISK_V4_PRO, ids=['wired', 'receiver'])
def basilisk_v4_pro(request, persistence):
    device, fake = make_device(request.param, persistence)
    fake.state.onboard['active'] = 3         # not profile 1, so VARSTORE would show
    _ = device.serial
    fake.state.sent.clear()
    yield device, fake
    device.close()


@pytest.mark.parametrize('pid', BASILISK_V4_PRO)
def test_the_basilisk_v4_pro_is_reached_on_its_fourth_interface(pid, recipes):
    from openrazer_win.core.transport import select_control_interface
    from openrazer_win.hid.base import HidDeviceInfo
    assert recipes.transport_params('mouse', pid)['index'] == 3

    def collection(interface, usage_page, usage, feature_length):
        return HidDeviceInfo(path='mi_{0:02x}'.format(interface), vendor_id=0x1532,
                             product_id=pid, interface=interface, usage_page=usage_page,
                             usage=usage, feature_length=feature_length)
    # What Windows lists for the receiver: the control report sits on a
    # consumer-control collection of interface 3.
    control = collection(3, 0x000C, 0x0001, 91)
    found = select_control_interface([collection(0, 0x0001, 0x0002, 0),
                                      collection(1, 0x0001, 0x0006, 0), control], 3)
    assert found is control


def test_every_basilisk_v4_pro_report_carries_transaction_id_0x1f(basilisk_v4_pro):
    device, fake = basilisk_v4_pro
    device.set_static(255, 0, 0)
    device.set_wave(1, zone='logo')
    device.set_spectrum(zone='scroll')
    device.set_brightness(40)
    device.get_brightness('scroll')
    device.set_dpi(1600)
    device.get_dpi()
    device.get_dpi_stages()
    device.set_poll_rate(8000)
    device.get_poll_rate()
    device.set_scroll_mode(2)
    device.set_scroll_acceleration(True)
    device.get_scroll_smart_reel()
    device.get_battery_level()
    device.set_idle_time(300)
    device.set_custom_frame([(0, 0, b'\x00\xff\x00' * 7)])
    assert fake.sent_reports
    assert {report.transaction_id for report in fake.sent_reports} == {0x1F}


def test_the_basilisk_v4_pro_lights_its_active_profile(basilisk_v4_pro):
    device, fake = basilisk_v4_pro
    device.set_static(1, 2, 3)
    query, effect = fake.sent_reports
    assert (query.command_class, query.command_id) == (0x05, 0x84)
    assert (effect.command_class, effect.command_id) == (0x0F, 0x02)
    # The active profile, every LED, static, then the colour.
    assert list(effect.arguments[:9]) == [0x03, 0x00, 0x01, 0x00, 0x00, 0x01, 1, 2, 3]
    device.set_brightness(50, zone='logo')
    assert list(last_report(fake, 0x0F, 0x04).arguments[:3]) == [0x03, 0x04, 0x80]
    device.set_scroll_smart_reel(True)
    assert list(last_report(fake, 0x02, 0x17).arguments[:2]) == [0x03, 0x01]


def test_the_basilisk_v4_pro_underglow_is_its_backlight(basilisk_v4_pro):
    device, fake = basilisk_v4_pro
    device.set_brightness(100)
    assert list(last_report(fake, 0x0F, 0x04).arguments[:3]) == [0x03, 0x0A, 0xFF]
    assert device.get_brightness() == 100.0


def test_a_basilisk_v4_pro_that_cannot_say_its_profile_falls_back_to_varstore(basilisk_v4_pro):
    device, fake = basilisk_v4_pro
    fake.state.onboard['active'] = None      # answered "not supported"
    device.set_static(1, 2, 3)
    assert last_report(fake, 0x0F, 0x02).arguments[0] == 0x01


def test_the_basilisk_v4_pro_dpi_is_its_active_stage(basilisk_v4_pro):
    device, fake = basilisk_v4_pro
    fake.state.dpi_stages = (2, [(400, 400), (800, 800), (1600, 1600)])
    device.set_dpi(2400, 2600)
    # The plain DPI command does nothing on this mouse, so it is never sent.
    assert not [r for r in fake.sent_reports if (r.command_class, r.command_id) == (0x04, 0x05)]
    read = last_report(fake, 0x04, 0x86)
    assert read.arguments[0] == 0x03
    assert fake.state.dpi_stages == (2, [(400, 400), (2400, 2600), (1600, 1600)])
    assert last_report(fake, 0x04, 0x06).arguments[0] == 0x03
    assert device.get_dpi() == (2400, 2600)
    assert device.get_dpi_stages() == (2, [(400, 400), (2400, 2600), (1600, 1600)])


def test_the_basilisk_v4_pro_wheel_has_three_modes(basilisk_v4_pro):
    device, fake = basilisk_v4_pro
    for mode in (2, 1, 0):
        device.set_scroll_mode(mode)
        report = last_report(fake, 0x02, 0x30)
        assert list(report.arguments[:2]) == [0x00, mode]
        assert device.get_scroll_mode() == mode
    # Not the Basilisk V3's commands, which this one does not take.
    assert not [r for r in fake.sent_reports if (r.command_class, r.command_id) in
                ((0x02, 0x14), (0x02, 0x94))]


def test_the_basilisk_v4_pro_polls_up_to_8000_hz(basilisk_v4_pro):
    device, fake = basilisk_v4_pro
    assert device.supported_poll_rates() == [125, 500, 1000, 2000, 4000, 8000]
    device.set_poll_rate(8000)
    writes = [r for r in fake.sent_reports if (r.command_class, r.command_id) == (0x00, 0x40)]
    # Sent twice, as the driver does: to argument 0x00 and then 0x01.
    assert [list(r.arguments[:2]) for r in writes] == [[0x00, 0x01], [0x01, 0x01]]
    assert device.get_poll_rate() == 8000


def test_what_the_basilisk_v4_pro_offers(basilisk_v4_pro):
    device, _fake = basilisk_v4_pro
    caps = device.capabilities()
    assert sorted(caps['zones']) == ['backlight', 'logo', 'scroll']
    assert all(zone['brightness'] for zone in caps['zones'].values())
    assert caps['matrix_dimensions'] == [1, 7]
    assert caps['max_dpi'] == 50000
    for feature in ('dpi', 'dpi_stages', 'poll_rate', 'battery', 'idle_time', 'scroll_mode',
                    'custom_frame', 'software_effects'):
        assert caps[feature] is True, feature


def test_other_mice_keep_their_scroll_wheel_commands(persistence):
    device, fake = make_device(0x00AA, persistence)       # Basilisk V3 Pro (Wired)
    try:
        device.set_scroll_mode(1)
        assert last_report(fake, 0x02, 0x14).arguments[0] == 0x01
        device.set_scroll_acceleration(True)
        # VARSTORE, and no profile question for it: that is the V4 Pro's alone.
        assert not [r for r in fake.sent_reports if (r.command_class, r.command_id) == (0x05, 0x84)]
        assert list(last_report(fake, 0x02, 0x16).arguments[:2]) == [0x01, 0x01]
    finally:
        device.close()
