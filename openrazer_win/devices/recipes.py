"""Interpreter for the transpiled kernel-driver recipes.

``tools/transpile_recipes.py`` turns each ``switch (device->usb_pid)`` in the
OpenRazer kernel drivers into a short program: build report X, stamp
transaction id Y, send it.  This module runs those programs.

Keeping the device quirks as data rather than hand-written Python means the
port tracks upstream exactly -- re-running the transpiler against a newer
OpenRazer release picks up new devices without touching any code here.
"""
from __future__ import annotations

import ast
import json
import os
import re
from functools import lru_cache
from typing import Any, Callable, Optional

from ..protocol.chroma import get_builder
from ..protocol.report import RazerReport

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
RECIPES_PATH = os.path.join(DATA_DIR, 'recipes.json')


class RecipeError(Exception):
    """A recipe could not be executed."""


class NotSupported(RecipeError):
    """The device has no recipe for this attribute."""


# ---------------------------------------------------------------------------
# Expression evaluation
# ---------------------------------------------------------------------------

_BLOB_RE = re.compile(r'^(response\.arguments|buf)\s*\+\s*(\d+)$')

#: The C subset the drivers use in expressions maps cleanly onto Python's
#: grammar once ``!`` becomes ``not``, so a restricted AST walk evaluates it
#: without ever calling :func:`eval`.
_ALLOWED_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a // b if isinstance(a, int) and isinstance(b, int) else a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.LShift: lambda a, b: a << b,
    ast.RShift: lambda a, b: a >> b,
    ast.BitOr: lambda a, b: a | b,
    ast.BitAnd: lambda a, b: a & b,
    ast.BitXor: lambda a, b: a ^ b,
}

_ALLOWED_COMPARE = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}


class Environment:
    """Named values a recipe reads from and writes to."""

    __slots__ = ('buf', 'vars', 'reports', 'response', 'out', 'sent')

    def __init__(self, buf: bytes = b'', **variables):
        self.buf = bytes(buf)
        self.vars: dict = dict(variables)
        self.reports: dict = {}
        self.response: Optional[RazerReport] = None
        self.out = bytearray(16)
        self.sent = 0

    @property
    def count(self) -> int:
        return len(self.buf)

    def buf_byte(self, offset: int) -> int:
        return self.buf[offset] if offset < len(self.buf) else 0

    def response_arg(self, offset: int) -> int:
        if self.response is None:
            return 0
        return self.response.arguments[offset]


def evaluate(node: Any, env: Environment) -> Any:
    """Evaluate one value node produced by the transpiler."""
    if not isinstance(node, dict):
        return node
    kind = node.get('k')

    if kind == 'const':
        return node['value']
    if kind == 'buf':
        return env.buf_byte(node['offset'])
    if kind == 'response':
        return env.response_arg(node['offset'])
    if kind == 'rgb':
        offset = node['offset']
        return tuple(env.buf_byte(offset + i) for i in range(3))
    if kind == 'blob':
        offset = node['offset']
        if isinstance(offset, dict):
            offset = int(evaluate(offset, env) or 0)
        return env.buf[offset:]
    if kind == 'var':
        name = node['name']
        if name == 'count':
            return env.count
        if name not in env.vars:
            raise RecipeError('recipe needs an unbound variable: {0}'.format(name))
        return env.vars[name]
    if kind == 'ternary':
        test = node['test']
        left = evaluate(test['left'], env)
        right = evaluate(test['right'], env)
        chosen = 'then' if _COMPARISONS[test['op']](left, right) else 'else'
        return evaluate(node[chosen], env)
    if kind == 'raw':
        return _evaluate_raw(node['expr'], env)
    raise RecipeError('unknown value node: {0!r}'.format(node))


def _evaluate_raw(expr: str, env: Environment) -> Any:
    expr = expr.strip()

    if expr.startswith('"'):
        return expr.strip('"')

    # ``response.arguments + 4`` is pointer arithmetic, not addition.
    match = _BLOB_RE.match(expr)
    if match:
        source, offset = match.group(1), int(match.group(2))
        if source == 'buf':
            return env.buf[offset:]
        if env.response is None:
            return b''
        return bytes(env.response.arguments[offset:])

    # C's logical negation, spelled the way Python's parser expects.  The
    # result has to be stripped: eval-mode parsing rejects leading whitespace.
    python_expr = re.sub(r'!(?!=)', ' not ', expr).strip()
    try:
        tree = ast.parse(python_expr, mode='eval')
    except SyntaxError as error:
        raise RecipeError('unsupported expression: {0}'.format(expr)) from error
    return _eval_node(tree.body, env, expr)


def _eval_node(node: ast.AST, env: Environment, source: str) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str)):
            return node.value
        raise RecipeError('unsupported literal in {0}'.format(source))

    if isinstance(node, ast.Name):
        if node.id == 'count':
            return env.count
        if node.id in env.vars:
            return env.vars[node.id]
        raise RecipeError('expression {0} needs unbound variable {1}'.format(
            source, node.id))

    if isinstance(node, ast.Attribute):
        if _dotted(node) == 'response.arguments':
            return bytes(env.response.arguments) if env.response else b''
        raise RecipeError('unsupported attribute in {0}'.format(source))

    if isinstance(node, ast.Subscript):
        index = _eval_node(node.slice, env, source)
        target = node.value
        if isinstance(target, ast.Name) and target.id == 'buf':
            return env.buf_byte(int(index))
        if isinstance(target, ast.Attribute) and _dotted(target) == 'response.arguments':
            return env.response_arg(int(index))
        raise RecipeError('unsupported subscript in {0}'.format(source))

    if isinstance(node, ast.BinOp):
        handler = _ALLOWED_BINOPS.get(type(node.op))
        if handler is None:
            raise RecipeError('unsupported operator in {0}'.format(source))
        return handler(_eval_node(node.left, env, source),
                       _eval_node(node.right, env, source))

    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand, env, source)
        if isinstance(node.op, ast.Not):
            return 0 if value else 1
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.Invert):
            return ~value
        raise RecipeError('unsupported unary operator in {0}'.format(source))

    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        handler = _ALLOWED_COMPARE.get(type(node.ops[0]))
        if handler is None:
            raise RecipeError('unsupported comparison in {0}'.format(source))
        return handler(_eval_node(node.left, env, source),
                       _eval_node(node.comparators[0], env, source))

    if isinstance(node, ast.IfExp):
        chosen = node.body if _eval_node(node.test, env, source) else node.orelse
        return _eval_node(chosen, env, source)

    raise RecipeError('unsupported expression: {0}'.format(source))


def _dotted(node: ast.Attribute) -> str:
    parts = [node.attr]
    current = node.value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return '.'.join(reversed(parts))


_COMPARISONS: dict = {
    '==': lambda a, b: a == b,
    '!=': lambda a, b: a != b,
    '<': lambda a, b: a < b,
    '>': lambda a, b: a > b,
    '<=': lambda a, b: a <= b,
    '>=': lambda a, b: a >= b,
}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

#: ``send(report, want_response) -> RazerReport | None``
Sender = Callable[[RazerReport, bool], Optional[RazerReport]]


class _Stop(Exception):
    """Raised internally when a recipe hits the kernel's ``return count``."""


def execute(steps: list, env: Environment, send: Sender) -> Environment:
    """Run `steps`, using `send` for every transport operation."""
    try:
        _execute(steps, env, send)
    except _Stop:
        pass
    return env


def _execute(steps: list, env: Environment, send: Sender) -> Environment:
    for step in steps:
        op = step['op']

        if op == 'stop':
            raise _Stop()

        if op == 'build':
            builder = get_builder(step['fn'])
            args = [evaluate(a, env) for a in step['args']]
            env.reports[step['target']] = builder(*args)

        elif op == 'txid':
            report = _report(env, step['target'])
            report.transaction_id = int(evaluate(step['value'], env)) & 0xFF

        elif op == 'set_arg':
            report = _report(env, step['target'])
            index = step['index']
            if isinstance(index, dict):
                index = int(evaluate(index, env))
            value = evaluate(step['value'], env)
            if isinstance(value, (bytes, bytearray, tuple, list)):
                report.set_arguments(index, bytes(value))
            else:
                report.set_argument(index, int(value))

        elif op == 'set_field':
            report = _report(env, step['target'])
            value = int(evaluate(step['value'], env))
            field = step['field']
            if field == 'command_id':
                report.command_id = value & 0xFF
            elif field == 'command_class':
                report.command_class = value & 0xFF
            elif field == 'data_size':
                report.data_size = value & 0xFF
            elif field == 'remaining_packets':
                report.remaining_packets = value & 0xFFFF

        elif op in ('send', 'send_no_response'):
            report = env.reports.get('request')
            if report is None:
                raise NotSupported('recipe reached a send with no report built')
            response = send(report, op == 'send')
            env.sent += 1
            if response is not None:
                env.response = response

        elif op == 'let':
            env.vars[step['name']] = evaluate(step['value'], env)

        elif op == 'xor':
            current = int(env.vars.get(step['name'], 0))
            env.vars[step['name']] = current ^ int(step['value'])

        elif op == 'out':
            env.out[step['index']] = env.response_arg(step['arg'])

        elif op == 'if_count':
            test = step['test']
            compare = _COMPARISONS[test['op']]
            branch = step['then'] if compare(env.count, test['value']) else step.get('else', [])
            _execute(branch, env, send)

        elif op == 'switch_count':
            branch = step.get('default', [])
            for case in step['cases']:
                if case['count'] == env.count:
                    branch = case['steps']
                    break
            _execute(branch, env, send)

        elif op == 'for_each_row':
            # The payload is a run of ``row, start_col, stop_col, r, g, b, ...``
            # records -- the same format the sysfs matrix_custom_frame takes.
            cursor = 0
            while cursor + 3 <= len(env.buf):
                row_id = env.buf[cursor]
                start_col = env.buf[cursor + 1]
                stop_col = env.buf[cursor + 2]
                if start_col > stop_col:
                    raise RecipeError(
                        'start column {0} is past stop column {1}'.format(
                            start_col, stop_col))
                row_length = ((stop_col + 1) - start_col) * 3
                if cursor + 3 + row_length > len(env.buf):
                    raise RecipeError(
                        'frame row {0} promised {1} colour bytes but only {2} remain'
                        .format(row_id, row_length, len(env.buf) - cursor - 3))
                env.vars.update({
                    'row_id': row_id,
                    'start_col': start_col,
                    'stop_col': stop_col,
                    'row_length': row_length,
                    'offset': cursor + 3,
                })
                _execute(step['steps'], env, send)
                cursor += 3 + row_length

        elif op == 'send_argb':
            channel = int(evaluate(step['channel'], env))
            data = evaluate(step['data'], env)
            if not isinstance(data, (bytes, bytearray)):
                raise RecipeError('ARGB payload must be bytes')
            size = int(evaluate(step['size'], env))
            send_argb = getattr(send, 'send_argb', None)
            if send_argb is None:
                raise NotSupported('this transport cannot send ARGB frames')
            send_argb(channel, bytes(data)[:size * 3])
            env.sent += 1

        elif op == 'switch_var':
            try:
                value = _evaluate_raw(step['var'], env)
            except RecipeError:
                value = env.vars.get(step['var'])
            branch = step.get('default', [])
            for case in step['cases']:
                if case['value'] == value:
                    branch = case['steps']
                    break
            _execute(branch, env, send)

        else:
            raise RecipeError('unknown recipe op: {0}'.format(op))
    return env


def _report(env: Environment, target: str) -> RazerReport:
    """The named report register.

    ``response`` is special: a few handlers pre-seed it from cached driver
    state and then read it back, so it must be the same object the interpreter
    exposes as ``env.response``.
    """
    if target == 'response':
        if env.response is None:
            env.response = RazerReport()
        return env.response
    report = env.reports.get(target)
    if report is None:
        report = RazerReport()
        env.reports[target] = report
    return report


# ---------------------------------------------------------------------------
# The recipe table
# ---------------------------------------------------------------------------

#: Ops that actually do something.  A recipe whose product-id switch had no
#: matching case can degrade to a bare ``send`` with nothing built -- that means
#: the device does not implement the attribute, not that the call should fail
#: at transport time.
ACTIONABLE_OPS = frozenset(('build', 'for_each_row', 'send_argb'))


def is_actionable(steps: list) -> bool:
    for step in steps:
        if step['op'] in ACTIONABLE_OPS:
            return True
        if step['op'] == 'if_count':
            if is_actionable(step['then']) or is_actionable(step.get('else', [])):
                return True
        elif step['op'] in ('switch_count', 'switch_var'):
            if any(is_actionable(case['steps']) for case in step['cases']):
                return True
            if is_actionable(step.get('default', [])):
                return True
    return False


class RecipeTable:
    """Per-driver, per-attribute, per-product-id recipe lookup."""

    def __init__(self, payload: dict):
        self._pool: list = payload['pool']
        self._actionable: list = [is_actionable(steps) for steps in self._pool]
        self._drivers: dict = payload['drivers']
        self._transport: dict = payload.get('transport', {})
        self.blade_pids = {int(p, 16) for p in payload.get('blade_pids', [])}
        self.constants: dict = payload.get('constants', {})

    def attributes(self, driver: str) -> list:
        return sorted(self._drivers.get(driver, {}))

    def has(self, driver: str, attribute: str, pid: int) -> bool:
        try:
            self.lookup(driver, attribute, pid)
        except NotSupported:
            return False
        return True

    def lookup(self, driver: str, attribute: str, pid: int) -> tuple:
        """Return ``(steps, bindings)`` for `attribute` on `pid`.

        `bindings` carries the values a delegating sysfs wrapper supplies -- for
        example ``logo_led_brightness`` forwards to the shared
        ``led_brightness`` handler with ``led_id`` pinned to the logo LED.

        Raises :class:`NotSupported` when neither the device nor the driver
        default implements the attribute.
        """
        table = self._drivers.get(driver)
        if table is None:
            raise NotSupported('no recipes for driver {0!r}'.format(driver))

        bindings: dict = {}
        seen = set()
        while True:
            entry = table.get(attribute)
            if entry is None:
                raise NotSupported(
                    'driver {0} has no attribute {1!r}'.format(driver, attribute))
            if 'alias' not in entry:
                break
            if attribute in seen:
                raise RecipeError('alias loop at {0}'.format(attribute))
            seen.add(attribute)
            # An outer wrapper's bindings win over the inner one's defaults.
            for key, value in entry.get('bind', {}).items():
                bindings.setdefault(key, value)
            attribute = entry['alias']

        ref = entry['pids'].get('{0:04x}'.format(pid), entry['default'])
        if not self._actionable[ref]:
            raise NotSupported(
                'device 0x{0:04x} does not implement {1}'.format(pid, attribute))
        return self._pool[ref], bindings

    def transport_params(self, driver: str, pid: int) -> dict:
        """USB interface index and inter-transfer delay for this device."""
        entry = self._transport.get(driver)
        if entry is None:
            return {'index': 0, 'wait_us': 600}
        return entry['pids'].get('{0:04x}'.format(pid), entry['default'])


@lru_cache(maxsize=1)
def get_recipes() -> RecipeTable:
    """The recipe table, parsed once per process."""
    with open(RECIPES_PATH, encoding='utf-8') as handle:
        return RecipeTable(json.load(handle))
