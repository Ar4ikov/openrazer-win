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

from openrazer_win.devices.recipes import Environment, execute
from openrazer_win.protocol.report import RazerReport

from .conftest import make_device

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))
import transpile_recipes as tr  # noqa: E402

CONSTS = {'USB_DEVICE_ID_RAZER_PAD': 0x0F26, 'USB_DEVICE_ID_RAZER_DOCK': 0x00A4,
          'USB_DEVICE_ID_RAZER_OTHER': 0x0C00, 'VARSTORE': 0x01, 'ZERO_LED': 0x00}


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
