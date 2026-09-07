"""The recipe table and its interpreter."""
from __future__ import annotations

import pytest

from openrazer_win.devices.recipes import (
    Environment, NotSupported, RecipeError, evaluate, execute, is_actionable,
)
from openrazer_win.protocol.report import RazerReport


def collect(steps, env=None):
    """Run `steps` against a recorder, returning the reports it would send."""
    sent = []
    env = env or Environment()

    def send(report, want_response):
        sent.append(report)
        if not want_response:
            return None
        reply = RazerReport.unpack(report.pack())
        reply.status = 0x02
        return reply

    execute(steps, env, send)
    return sent, env


# -- expression evaluation --------------------------------------------------

def test_constants_and_buffer_reads():
    env = Environment(b'\x10\x20\x30')
    assert evaluate({'k': 'const', 'value': 7}, env) == 7
    assert evaluate({'k': 'buf', 'offset': 1}, env) == 0x20
    assert evaluate({'k': 'buf', 'offset': 99}, env) == 0
    assert evaluate({'k': 'rgb', 'offset': 0}, env) == (0x10, 0x20, 0x30)


def test_variables_must_be_bound():
    env = Environment(b'', speed=3)
    assert evaluate({'k': 'var', 'name': 'speed'}, env) == 3
    with pytest.raises(RecipeError, match='unbound'):
        evaluate({'k': 'var', 'name': 'direction'}, env)


def test_count_is_the_payload_length():
    env = Environment(b'\x01\x02\x03\x04')
    assert evaluate({'k': 'var', 'name': 'count'}, env) == 4


def test_big_endian_pair_expression():
    env = Environment(b'\x07\x08')
    node = {'k': 'raw', 'expr': '(buf[0] << 8) | (buf[1] & 0xFF)'}
    assert evaluate(node, env) == 0x0708


def test_response_arguments_expression():
    env = Environment()
    env.response = RazerReport()
    env.response.set_arguments(1, (0x0C, 0x80))
    node = {'k': 'raw', 'expr': '(response.arguments[1] << 8) | (response.arguments[2] & 0xFF)'}
    assert evaluate(node, env) == 0x0C80


def test_pointer_arithmetic_returns_a_slice():
    env = Environment()
    env.response = RazerReport()
    env.response.set_arguments(0, bytes(range(10)))
    node = {'k': 'raw', 'expr': 'response.arguments + 4'}
    assert evaluate(node, env)[:3] == bytes((4, 5, 6))


def test_c_negation_is_translated():
    env = Environment(b'', state=1)
    assert evaluate({'k': 'raw', 'expr': '!state'}, env) == 0
    env.vars['state'] = 0
    assert evaluate({'k': 'raw', 'expr': '!state'}, env) == 1


def test_arithmetic_expression():
    env = Environment(b'', start_col=2, stop_col=9)
    assert evaluate({'k': 'raw', 'expr': '(stop_col - start_col) + 1'}, env) == 8


def test_ternary_expression():
    env = Environment(b'', channel=2, sz=17)
    env.response = RazerReport()
    env.response.set_argument(4, 99)
    node = {'k': 'ternary',
            'test': {'left': {'k': 'var', 'name': 'channel'}, 'op': '==',
                     'right': {'k': 'const', 'value': 2}},
            'then': {'k': 'var', 'name': 'sz'},
            'else': {'k': 'response', 'offset': 4}}
    assert evaluate(node, env) == 17
    env.vars['channel'] = 3
    assert evaluate(node, env) == 99


def test_expressions_cannot_call_functions():
    env = Environment()
    with pytest.raises(RecipeError):
        evaluate({'k': 'raw', 'expr': '__import__("os").system("echo hi")'}, env)


def test_unparseable_expression_raises():
    env = Environment()
    with pytest.raises(RecipeError, match='unsupported expression'):
        evaluate({'k': 'raw', 'expr': 'def broken('}, env)


# -- control flow -----------------------------------------------------------

BUILD_STATIC = {'op': 'build', 'target': 'request',
                'fn': 'razer_chroma_standard_matrix_effect_static',
                'args': [{'k': 'rgb', 'offset': 0}]}
SEND = {'op': 'send'}


def test_build_then_send():
    sent, env = collect([BUILD_STATIC, {'op': 'txid', 'target': 'request',
                                        'value': {'k': 'const', 'value': 0xFF}}, SEND],
                        Environment(b'\x01\x02\x03'))
    assert len(sent) == 1
    assert sent[0].transaction_id == 0xFF
    assert bytes(sent[0].arguments[1:4]) == b'\x01\x02\x03'
    assert env.sent == 1


def test_send_without_a_report_is_not_supported():
    with pytest.raises(NotSupported):
        collect([SEND])


def test_switch_count_picks_the_matching_arm():
    steps = [{'op': 'switch_count',
              'cases': [{'count': 3, 'steps': [{'op': 'let', 'name': 'picked',
                                                'value': {'k': 'const', 'value': 'single'}}]},
                        {'count': 6, 'steps': [{'op': 'let', 'name': 'picked',
                                                'value': {'k': 'const', 'value': 'dual'}}]}],
              'default': [{'op': 'let', 'name': 'picked',
                           'value': {'k': 'const', 'value': 'random'}}]}]
    assert collect(steps, Environment(b'abc'))[1].vars['picked'] == 'single'
    assert collect(steps, Environment(b'abcdef'))[1].vars['picked'] == 'dual'
    assert collect(steps, Environment(b'x'))[1].vars['picked'] == 'random'


def test_if_count_takes_the_else_branch():
    steps = [{'op': 'if_count', 'test': {'op': '==', 'value': 2},
              'then': [{'op': 'let', 'name': 'n', 'value': {'k': 'const', 'value': 2}}],
              'else': [{'op': 'let', 'name': 'n', 'value': {'k': 'const', 'value': 4}}]}]
    assert collect(steps, Environment(b'\x00' * 4))[1].vars['n'] == 4


def test_stop_ends_the_recipe():
    steps = [BUILD_STATIC, {'op': 'stop'}, SEND]
    sent, _ = collect(steps, Environment(b'\x01\x02\x03'))
    assert sent == []


def test_stop_inside_a_branch_skips_the_shared_tail():
    steps = [{'op': 'if_count', 'test': {'op': '==', 'value': 1},
              'then': [{'op': 'stop'}], 'else': []},
             BUILD_STATIC, SEND]
    assert collect(steps, Environment(b'\x00'))[0] == []
    assert len(collect(steps, Environment(b'\x00\x00\x00'))[0]) == 1


def test_for_each_row_walks_the_payload():
    steps = [{'op': 'for_each_row', 'steps': [
        {'op': 'build', 'target': 'request',
         'fn': 'razer_chroma_standard_matrix_set_custom_frame',
         'args': [{'k': 'var', 'name': 'row_id'}, {'k': 'var', 'name': 'start_col'},
                  {'k': 'var', 'name': 'stop_col'},
                  {'k': 'blob', 'offset': {'k': 'var', 'name': 'offset'}}]},
        SEND]}]
    payload = bytes((0, 0, 1)) + bytes(range(6)) + bytes((1, 0, 0)) + bytes((9, 9, 9))
    sent, _ = collect(steps, Environment(payload))
    assert len(sent) == 2
    assert bytes(sent[0].arguments[1:4]) == bytes((0, 0, 1))
    assert bytes(sent[0].arguments[4:10]) == bytes(range(6))
    assert bytes(sent[1].arguments[1:4]) == bytes((1, 0, 0))
    assert bytes(sent[1].arguments[4:7]) == bytes((9, 9, 9))


def test_for_each_row_rejects_a_truncated_frame():
    steps = [{'op': 'for_each_row', 'steps': [BUILD_STATIC]}]
    with pytest.raises(RecipeError, match='colour bytes'):
        collect(steps, Environment(bytes((0, 0, 5)) + bytes(3)))


def test_for_each_row_rejects_reversed_columns():
    steps = [{'op': 'for_each_row', 'steps': [BUILD_STATIC]}]
    with pytest.raises(RecipeError, match='past stop column'):
        collect(steps, Environment(bytes((0, 4, 1)) + bytes(9)))


def test_unknown_op_is_reported():
    with pytest.raises(RecipeError, match='unknown recipe op'):
        collect([{'op': 'teleport'}])


# -- table lookups ----------------------------------------------------------

def test_actionability_ignores_bare_sends():
    assert not is_actionable([{'op': 'send'}])
    assert is_actionable([BUILD_STATIC, SEND])
    assert is_actionable([{'op': 'if_count', 'test': {'op': '==', 'value': 3},
                           'then': [BUILD_STATIC], 'else': []}])


def test_zone_attributes_resolve_through_their_alias(recipes):
    steps, bindings = recipes.lookup('mouse', 'write:logo_led_brightness', 0x0078)
    assert bindings == {'led_id': 0x04}
    assert any(step['op'] == 'build' for step in steps)


def test_unknown_attribute_raises(recipes):
    with pytest.raises(NotSupported):
        recipes.lookup('mouse', 'write:teleport', 0x0078)


def test_product_specific_recipe_wins_over_the_default(recipes):
    classic, _ = recipes.lookup('kbd', 'write:matrix_effect_static', 0x0203)
    extended, _ = recipes.lookup('kbd', 'write:matrix_effect_static', 0x0257)
    assert classic[0]['fn'] == 'razer_chroma_standard_matrix_effect_static'
    assert extended[0]['fn'] == 'razer_chroma_extended_matrix_effect_static'


def test_transaction_ids_are_per_device(recipes):
    classic, _ = recipes.lookup('kbd', 'write:matrix_effect_static', 0x0203)
    extended, _ = recipes.lookup('kbd', 'write:matrix_effect_static', 0x0257)
    assert classic[1]['value']['value'] == 0xFF
    assert extended[1]['value']['value'] == 0x3F


def test_transport_parameters_have_sane_defaults(recipes):
    params = recipes.transport_params('kbd', 0x0203)
    assert params['index'] == 1
    assert params['wait_us'] >= 600
    # A device the table has never heard of still gets usable parameters.
    assert recipes.transport_params('kbd', 0xFFFF)['index'] == 1
