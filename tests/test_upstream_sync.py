"""What the driver code does around the reports, not just the reports.

Syncing with upstream turned up kernel behaviour the recipes had been dropping
on the floor: helpers called from inside a handler (switching the device mode,
asking which profile is active), ``fallthrough`` between product-id cases, and
the device mode a driver leaves the hardware in when it binds.  Each of those
is pinned down here, through the transpiler on small C snippets and through
the public device API against the emulator.
"""
from __future__ import annotations

import os
import sys

import pytest

from openrazer_win.devices.recipes import Environment, RecipeTable, execute
from openrazer_win.protocol.report import RazerReport, RazerReportError

from .conftest import make_device

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))
import transpile_recipes as tr  # noqa: E402

CONSTS = {'USB_DEVICE_ID_RAZER_PAD': 0x0F26, 'USB_DEVICE_ID_RAZER_DOCK': 0x00A4,
          'USB_DEVICE_ID_RAZER_OTHER': 0x0C00, 'USB_DEVICE_ID_RAZER_V4_PRO': 0x00E2,
          'VARSTORE': 0x01, 'ZERO_LED': 0x00, 'BACKLIGHT_LED': 0x05, 'UNDERGLOW_LED': 0x0A}


def record(steps, env=None):
    sent = []

    def send(report, want_response):
        sent.append(report)
        if not want_response:
            return None
        reply = RazerReport.unpack(report.pack())
        reply.arguments[0] = 0x04       # what an "active profile" query answers
        return reply

    execute(steps, env or Environment(), send)
    return sent


def emit(c_body, pid, helpers=None):
    nodes = tr.parse_block(tr.strip_comments(c_body))
    helper_nodes = {name: tr.parse_block(body) for name, body in (helpers or {}).items()}
    return tr.Emitter(CONSTS, pid, set(), 'accessory', [], helper_nodes).emit(nodes)


SET_DEVICE_MODE = """
    request = razer_chroma_standard_set_device_mode(mode, param);
    request.transaction_id.id = 0x3F;
    return razer_send_payload(device, &request, &response);
"""


# -- the transpiler ---------------------------------------------------------

def test_a_case_that_falls_through_keeps_the_next_case_s_report():
    body = """
    switch (device->usb_pid) {
    case USB_DEVICE_ID_RAZER_PAD:
        direction ^= ((1<<0) | (1<<1));
        fallthrough;
    case USB_DEVICE_ID_RAZER_DOCK:
        request = razer_chroma_extended_matrix_effect_wave(VARSTORE, ZERO_LED, direction);
        break;
    default:
        return -EINVAL;
    }
    err = razer_send_payload(device, &request, &response);
    """
    pad = emit(body, 0x0F26)
    assert [step['op'] for step in pad] == ['xor', 'build', 'send']
    dock = emit(body, 0x00A4)
    assert [step['op'] for step in dock] == ['build', 'send']


def test_a_device_mode_call_inside_a_handler_is_kept():
    body = """
    err = razer_set_device_mode(device, 0x00, 0x00);
    if (err)
        return err;
    request = razer_chroma_extended_matrix_effect_spectrum(VARSTORE, ZERO_LED);
    err = razer_send_payload(device, &request, &response);
    """
    steps = emit(body, 0x0F26, {'razer_set_device_mode': SET_DEVICE_MODE})
    assert steps[0]['op'] == 'call'
    sent = record(steps)
    assert [(r.command_class, r.command_id) for r in sent] == [(0x00, 0x04), (0x0F, 0x02)]
    assert sent[0].transaction_id == 0x3F
    assert list(sent[0].arguments[:2]) == [0x00, 0x00]


def test_a_helper_s_return_value_becomes_a_variable():
    helper = """
    request = get_razer_report(0x05, 0x84, 0x01);
    request.transaction_id.id = 0x1F;
    err = razer_send_payload(device, &request, &response);
    if (err) {
        return VARSTORE;
    }
    return response.arguments[0];
    """
    body = """
    request = razer_chroma_extended_matrix_effect_static(razer_get_active_varstore(device), ZERO_LED, (struct razer_rgb*)&buf[0]);
    err = razer_send_payload(device, &request, &response);
    """
    steps = emit(body, 0x0C00, {'razer_get_active_varstore': helper})
    sent = record(steps, Environment(b'\x01\x02\x03'))
    assert (sent[0].command_class, sent[0].command_id) == (0x05, 0x84)
    assert sent[1].arguments[0] == 0x04          # the profile the device named


def test_the_catch_all_recipe_is_never_one_of_the_named_devices():
    body = """
    if (device->usb_pid == USB_DEVICE_ID_RAZER_PAD) {
        request = razer_chroma_standard_matrix_effect_none();
    } else {
        request = razer_chroma_standard_matrix_effect_spectrum();
    }
    err = razer_send_payload(device, &request, &response);
    """
    assert emit(body, None)[0]['fn'] == 'razer_chroma_standard_matrix_effect_spectrum'
    assert emit(body, 0x0F26)[0]['fn'] == 'razer_chroma_standard_matrix_effect_none'


def test_a_helper_does_not_leak_its_locals():
    steps = [{'op': 'let', 'name': 'mode', 'value': {'k': 'const', 'value': 7}},
             {'op': 'call', 'vars': {'mode': {'k': 'const', 'value': 3}},
              'steps': [{'op': 'let', 'name': 'scratch', 'value': {'k': 'const', 'value': 1}},
                        {'op': 'stop'}]}]
    env = Environment()
    record(steps, env)
    assert env.vars['mode'] == 7
    assert 'scratch' not in env.vars


# -- openrazer/openrazer#2975's helpers (the Basilisk V4 Pro) -----------------

VARIABLE_STORAGE = """
    struct razer_report request = {0};
    struct razer_report response = {0};

    switch (device->usb_pid) {
    case USB_DEVICE_ID_RAZER_V4_PRO:
        request = get_razer_report(0x05, 0x84, 0x01);
        request.transaction_id.id = 0x1f;

        if (razer_send_payload(device, &request, &response) == 0 &&
            response.arguments[0] >= 0x01 && response.arguments[0] <= 0x05)
            return response.arguments[0];
        break;
    }

    return VARSTORE;
"""

STATIC_ON_THE_ACTIVE_PROFILE = """
    request = razer_chroma_extended_matrix_effect_static(razer_get_variable_storage(device), ZERO_LED, (struct razer_rgb*)&buf[0]);
    request.transaction_id.id = 0x1f;
    err = razer_send_payload(device, &request, &response);
"""


def answering(profile=None, error=False):
    """A sender whose active-profile query answers `profile`, or fails."""
    sent = []

    def send(report, want_response):
        sent.append(report)
        if (report.command_class, report.command_id) == (0x05, 0x84):
            if error:
                raise RazerReportError('device replied not supported', status=0x05)
            reply = RazerReport.unpack(report.pack())
            reply.arguments[0] = profile
            return reply
        return RazerReport.unpack(report.pack()) if want_response else None
    return send, sent


@pytest.mark.parametrize('profile, error, storage', [
    (3, False, 3),          # what the mouse says
    (9, False, 0x01),       # out of range: VARSTORE
    (0, False, 0x01),
    (None, True, 0x01),     # the query failed: VARSTORE, and the effect still goes
])
def test_the_active_profile_is_asked_for_and_checked(profile, error, storage):
    steps = emit(STATIC_ON_THE_ACTIVE_PROFILE, 0x00E2,
                 {'razer_get_variable_storage': VARIABLE_STORAGE})
    assert steps[0]['op'] == 'call'
    send, sent = answering(profile or 0, error)
    execute(steps, Environment(b'\xff\x00\x00'), send)
    assert [(r.command_class, r.command_id) for r in sent] == [(0x05, 0x84), (0x0F, 0x02)]
    assert sent[1].arguments[0] == storage
    assert list(sent[1].arguments[6:9]) == [0xFF, 0x00, 0x00]


def test_a_helper_that_only_returns_a_constant_is_folded_away():
    # Every other mouse: razer_get_variable_storage() is VARSTORE, asked of nobody.
    steps = emit(STATIC_ON_THE_ACTIVE_PROFILE, 0x0C00,
                 {'razer_get_variable_storage': VARIABLE_STORAGE})
    assert [step['op'] for step in steps] == ['build', 'txid', 'send']
    assert steps[0]['args'][0] == {'k': 'const', 'value': 0x01}
    send, sent = answering(3)
    execute(steps, Environment(b'\x01\x02\x03'), send)
    assert [(r.command_class, r.command_id) for r in sent] == [(0x0F, 0x02)]


def test_a_native_helper_is_called_after_the_values_it_needs():
    body = """
    dpi_x = (buf[0] << 8) | (buf[1] & 0xFF);
    dpi_y = (count == 4) ? ((buf[2] << 8) | (buf[3] & 0xFF)) : dpi_x;
    err = razer_set_active_stage_dpi(device, dpi_x, dpi_y);
    if (err)
        return err;
    return count;
    """
    native = """
    unsigned char variable_storage = razer_get_variable_storage(device);
    request = razer_chroma_misc_get_dpi_stages(variable_storage);
    """
    helpers = {'razer_get_variable_storage': VARIABLE_STORAGE,
               'razer_set_active_stage_dpi': native}
    steps = emit(body, 0x00E2, helpers)
    assert [step['op'] for step in steps] == ['let', 'let', 'call', 'native', 'stop']
    assert steps[2]['result'] == 'variable_storage'
    assert steps[3] == {'op': 'native', 'fn': 'razer_set_active_stage_dpi',
                        'args': [{'k': 'var', 'name': 'dpi_x'}, {'k': 'var', 'name': 'dpi_y'}]}
    # The parenthesised test of the ternary is a ternary, not a raw C string.
    assert steps[1]['value']['k'] == 'ternary'
    env = Environment(b'\x03\x20')
    execute(steps[:2], env, answering()[0])
    assert (env.vars['dpi_x'], env.vars['dpi_y']) == (800, 800)
    env = Environment(b'\x03\x20\x06\x40')
    execute(steps[:2], env, answering()[0])
    assert (env.vars['dpi_x'], env.vars['dpi_y']) == (800, 1600)
    # Elsewhere the value it needs is a constant, bound without asking.
    other = emit(body, 0x0C00, helpers)
    assert other[2] == {'op': 'let', 'name': 'variable_storage',
                        'value': {'k': 'const', 'value': 0x01}}


def test_a_constant_picked_by_product_id_binds_a_wrapper_per_device():
    helper = """
    struct razer_mouse_device *device = dev_get_drvdata(dev);

    if (device->usb_pid == USB_DEVICE_ID_RAZER_V4_PRO ||
        device->usb_pid == USB_DEVICE_ID_RAZER_OTHER)
        return UNDERGLOW_LED;

    return BACKLIGHT_LED;
    """
    nodes = tr.parse_block(tr.strip_comments(helper))
    folded = tr.fold_pid_helper(nodes, {0x00E2, 0x0C00, 0x0F26}, CONSTS, set(), 'mouse', [])
    assert folded == (0x05, {'00e2': 0x0A, '0c00': 0x0A})
    wrapper = tr.parse_block('return razer_attr_write_led_brightness(dev, attr, buf, count, '
                             'razer_backlight_led_id(dev));')
    assert tr.find_delegation(wrapper, CONSTS, {'razer_backlight_led_id': None}) == (
        'razer_attr_write_led_brightness', [{'helper': 'razer_backlight_led_id'}])
    assert tr.find_delegation(wrapper, CONSTS) is None


def test_a_wrapper_bound_per_device_resolves_for_each_one():
    pool = [[{'op': 'build', 'target': 'request', 'fn': 'get_razer_report',
              'args': [{'k': 'const', 'value': 0x0F}, {'k': 'const', 'value': 0x04},
                       {'k': 'var', 'name': 'led_id'}]}, {'op': 'send'}]]
    table = RecipeTable({'pool': pool, 'drivers': {'mouse': {
        'write:led_brightness': {'default': 0, 'pids': {}},
        'write:backlight_led_brightness': {'alias': 'write:led_brightness',
                                           'bind': {'led_id': 0x05},
                                           'pid_bind': {'00e2': {'led_id': 0x0A}}},
        'write:outer': {'alias': 'write:backlight_led_brightness', 'bind': {'led_id': 0x01}},
    }}})
    assert table.lookup('mouse', 'write:backlight_led_brightness', 0x00E2)[1] == {'led_id': 0x0A}
    assert table.lookup('mouse', 'write:backlight_led_brightness', 0x0099)[1] == {'led_id': 0x05}
    # An outer wrapper's own binding still wins over the inner one's.
    assert table.lookup('mouse', 'write:outer', 0x00E2)[1] == {'led_id': 0x01}


def test_the_shipped_underglow_is_its_own_led_on_the_v4_pro_only(recipes):
    for attribute in ('write:backlight_led_brightness', 'read:backlight_led_brightness',
                      'write:backlight_matrix_effect_static'):
        assert recipes.lookup('mouse', attribute, 0x00E2)[1]['led_id'] == 0x0A
        assert recipes.lookup('mouse', attribute, 0x00E3)[1]['led_id'] == 0x0A
        assert recipes.lookup('mouse', attribute, 0x0099)[1]['led_id'] == 0x05


# -- the shipped recipes, through the device API ----------------------------

CHARGING_PAD = 0x0F26
FIREFLY_V2 = 0x0C04
KRAKEN_KITTY_EDITION = 0x0F19
BLACKWIDOW_CHROMA = 0x0203
HUNTSMAN_V3_PRO_8KHZ = 0x02CF


def mode_writes(fake):
    return [tuple(r.arguments[:2]) for r in fake.sent_reports
            if (r.command_class, r.command_id) == (0x00, 0x04)]


def test_an_accessory_enters_driver_mode_like_the_kernel_puts_it(persistence):
    device, fake = make_device(FIREFLY_V2, persistence)
    try:
        device.enter_driver_mode()
        assert fake.state.device_mode == (0x03, 0x00)
    finally:
        device.close()


@pytest.mark.parametrize('pid', [CHARGING_PAD, KRAKEN_KITTY_EDITION])
def test_the_accessories_the_kernel_leaves_alone_stay_in_normal_mode(pid, persistence):
    device, fake = make_device(pid, persistence)
    try:
        device.enter_driver_mode()
        assert mode_writes(fake) == []
    finally:
        device.close()


def test_the_charging_pad_switches_mode_around_its_effects(persistence):
    device, fake = make_device(CHARGING_PAD, persistence)
    try:
        device.set_spectrum()
        assert mode_writes(fake)[-1] == (0x00, 0x00)
        device.set_custom_frame([(0, 0, b'\xff\x00\x00' * 3)])
        assert mode_writes(fake)[-1] == (0x03, 0x00)
    finally:
        device.close()


def test_a_keyboard_only_enters_driver_mode_when_its_keys_are_handled(persistence):
    device, fake = make_device(BLACKWIDOW_CHROMA, persistence)
    try:
        device.enter_driver_mode(keys_handled=False)
        assert fake.state.device_mode == (0x00, 0x00)
        device.enter_driver_mode(keys_handled=True)
        assert fake.state.device_mode == (0x03, 0x00)
        device.leave_driver_mode()
        assert fake.state.device_mode == (0x00, 0x00)
    finally:
        device.close()


def test_leaving_driver_mode_is_a_no_op_when_it_was_never_entered(persistence):
    device, fake = make_device(BLACKWIDOW_CHROMA, persistence)
    try:
        device.leave_driver_mode()
        assert mode_writes(fake) == []
    finally:
        device.close()


def test_the_8khz_huntsman_lights_its_active_profile(persistence):
    device, fake = make_device(HUNTSMAN_V3_PRO_8KHZ, persistence)
    try:
        device.set_static(1, 2, 3)
        classes = [(r.command_class, r.command_id) for r in fake.sent_reports]
        assert (0x05, 0x84) in classes
        query = classes.index((0x05, 0x84))
        assert (0x0F, 0x02) in classes[query:]
    finally:
        device.close()
