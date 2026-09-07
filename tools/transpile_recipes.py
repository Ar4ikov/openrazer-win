"""Transpile the OpenRazer kernel drivers into per-device JSON "recipes".

The Linux drivers express every device quirk as a ``switch (device->usb_pid)``
inside a sysfs attribute handler.  This script walks those switches and, for
each (attribute, product id) pair, folds the PID-dependent branches away and
emits the remaining steps -- which report builder to call, with which
arguments, and which transaction id to stamp on it.

``openrazer_win.devices.recipes`` interprets the result at runtime, so the
Windows port stays faithful to the kernel driver without duplicating 18k lines
of C by hand.

Usage::

    python tools/transpile_recipes.py <path-to-openrazer-checkout> <out.json>
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Optional

DRIVERS = {
    'kbd': 'razerkbd_driver.c',
    'mouse': 'razermouse_driver.c',
    'accessory': 'razeraccessory_driver.c',
}

# Constants defined across razercommon.h and the per-driver headers.
CONST_FILES = ['razercommon.h', 'razerkbd_driver.h', 'razermouse_driver.h',
               'razeraccessory_driver.h', 'razerkraken_driver.h']

# Constants that live in razercommon.h as C enums rather than #defines.
ENUM_CONSTS = {
    'CLASSIC_EFFECT_STATIC': 0x00, 'CLASSIC_EFFECT_BLINKING': 0x01,
    'CLASSIC_EFFECT_BREATHING': 0x02, 'CLASSIC_EFFECT_SPECTRUM': 0x04,
    'MATRIX_EFFECT_OFF': 0x00, 'MATRIX_EFFECT_WAVE': 0x01,
    'MATRIX_EFFECT_REACTIVE': 0x02, 'MATRIX_EFFECT_BREATHING': 0x03,
    'MATRIX_EFFECT_SPECTRUM': 0x04, 'MATRIX_EFFECT_CUSTOMFRAME': 0x05,
    'MATRIX_EFFECT_STATIC': 0x06, 'MATRIX_EFFECT_STARLIGHT': 0x19,
    'true': 1, 'false': 0, 'ON': 1, 'OFF': 0,
}


def strip_comments(src: str) -> str:
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    src = re.sub(r'//[^\n]*', '', src)
    return src


def load_constants(driver_dir: str) -> tuple[dict[str, int], dict[str, int]]:
    """Return (all constants, USB_DEVICE_ID_* only)."""
    consts: dict[str, int] = dict(ENUM_CONSTS)
    pids: dict[str, int] = {}
    define_re = re.compile(r'^#define\s+(\w+)\s+(0x[0-9A-Fa-f]+|\d+)\s*$', re.M)
    for name in CONST_FILES:
        path = os.path.join(driver_dir, name)
        if not os.path.exists(path):
            continue
        text = strip_comments(open(path, encoding='utf-8').read())
        for match in define_re.finditer(text):
            key, raw = match.group(1), match.group(2)
            value = int(raw, 0)
            consts[key] = value
            if key.startswith('USB_DEVICE_ID_'):
                pids[key] = value
    return consts, pids


# --------------------------------------------------------------------------
# A very small C statement splitter.  It only understands the subset the
# drivers actually use: blocks, if/else, switch/case and plain statements.
# --------------------------------------------------------------------------

class Node:
    __slots__ = ('kind', 'text', 'children', 'cases')

    def __init__(self, kind: str, text: str = '', children=None, cases=None):
        self.kind = kind          # stmt | if | switch | block | loop
        self.text = text
        self.children = children or []
        self.cases = cases or []  # switch: list of (labels, [Node])

    def __repr__(self):  # pragma: no cover - debugging aid
        return '<{0} {1!r}>'.format(self.kind, self.text[:40])


def _match_brace(src: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    while i < len(src):
        ch = src[i]
        if ch == '"' or ch == "'":
            quote = ch
            i += 1
            while i < len(src) and src[i] != quote:
                i += 2 if src[i] == '\\' else 1
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError('unbalanced braces')


def _match_paren(src: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    while i < len(src):
        if src[i] == '(':
            depth += 1
        elif src[i] == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError('unbalanced parens')


def parse_block(src: str) -> list[Node]:
    """Parse a sequence of C statements into Nodes."""
    nodes: list[Node] = []
    i = 0
    n = len(src)
    while i < n:
        while i < n and src[i] in ' \t\r\n;':
            i += 1
        if i >= n:
            break
        node, i = _parse_statement(src, i)
        if node is not None:
            nodes.append(node)
    return nodes


def _parse_statement(src: str, i: int) -> tuple[Optional[Node], int]:
    """Parse exactly one statement starting at `i`; return it and the next index.

    Compound statements (``if`` / ``switch`` / loops / blocks) are consumed
    whole.  Getting this right matters: an unbraced ``else`` may be followed by
    a nested ``if`` whose own body contains semicolons, and scanning naively to
    the next ``;`` would swallow the statements that follow the chain.
    """
    n = len(src)
    while i < n and src[i] in ' \t\r\n;':
        i += 1
    if i >= n:
        return None, n

    word = re.match(r'(\w+)', src[i:])
    keyword = word.group(1) if word else ''

    if keyword == 'if':
        paren = src.index('(', i)
        close = _match_paren(src, paren)
        cond = src[paren + 1:close]
        then_body, after = _read_statement(src, close + 1)
        else_body: list[Node] = []
        match = re.match(r'\s*else\b', src[after:])
        if match:
            else_body, after = _read_statement(src, after + match.end())
        return Node('if', cond, [then_body, else_body]), after

    if keyword == 'switch':
        paren = src.index('(', i)
        close = _match_paren(src, paren)
        expr = src[paren + 1:close].strip()
        brace = src.index('{', close)
        end = _match_brace(src, brace)
        return Node('switch', expr, cases=parse_cases(src[brace + 1:end])), end + 1

    if keyword in ('for', 'while'):
        paren = src.index('(', i)
        close = _match_paren(src, paren)
        body, after = _read_statement(src, close + 1)
        return Node('loop', src[paren + 1:close], [body]), after

    if keyword == 'do':
        body, after = _read_statement(src, i + 2)
        match = re.match(r'\s*while\s*\(', src[after:])
        if match:
            close = _match_paren(src, after + match.end() - 1)
            after = close + 1
        return Node('loop', '', [body]), after

    if src[i] == '{':
        end = _match_brace(src, i)
        return Node('block', '', [parse_block(src[i + 1:end])]), end + 1

    # Plain statement: read to the next top-level semicolon.
    j = i
    depth = 0
    while j < n:
        ch = src[j]
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            if depth == 0 and ch == '}':
                break
            depth -= 1
        elif ch == ';' and depth == 0:
            break
        j += 1
    stmt = src[i:j].strip()
    node = Node('stmt', ' '.join(stmt.split())) if stmt else None
    return node, j + 1


def _read_statement(src: str, start: int) -> tuple[list[Node], int]:
    """Read a single statement or brace-delimited block starting at `start`."""
    i = start
    while i < len(src) and src[i] in ' \t\r\n':
        i += 1
    if i < len(src) and src[i] == '{':
        end = _match_brace(src, i)
        return parse_block(src[i + 1:end]), end + 1
    node, after = _parse_statement(src, i)
    return ([node] if node is not None else []), after


def parse_cases(src: str) -> list[tuple[list[str], list[Node]]]:
    """Split a switch body into (labels, statements) groups."""
    label_re = re.compile(r'\b(case\s+([^:]+?)\s*:|default\s*:)')
    positions = []
    depth = 0
    i = 0
    while i < len(src):
        ch = src[i]
        if ch in '({[':
            depth += 1
        elif ch in ')}]':
            depth -= 1
        elif depth == 0:
            m = label_re.match(src, i)
            if m:
                label = m.group(2).strip() if m.group(2) else 'default'
                positions.append((i, m.end(), label))
                i = m.end()
                continue
        i += 1

    groups: list[tuple[list[str], list[Node]]] = []
    pending: list[str] = []
    for idx, (_start, end, label) in enumerate(positions):
        next_start = positions[idx + 1][0] if idx + 1 < len(positions) else len(src)
        body_src = src[end:next_start]
        pending.append(label)
        if body_src.strip():
            groups.append((pending, parse_block(body_src)))
            pending = []
    if pending:
        groups.append((pending, []))
    return groups


# --------------------------------------------------------------------------
# Expression -> JSON value
# --------------------------------------------------------------------------

CAST_RE = re.compile(r'^\(\s*unsigned\s+(?:char|short|int)\s*\)\s*')
ADDRESS_OF_RE = re.compile(r'^&\s*(\w+)$')
MEMBER_RE = re.compile(r'^device->([\w.]+)$')
TERNARY_RE = re.compile(r'^(.+?)\s*\?\s*(.+?)\s*:\s*(.+)$')
COMPARE_RE = re.compile(r'^(\w+)\s*(==|!=|<=|>=|<|>)\s*(\w+)$')
RGB_PTR_RE = re.compile(r'^\(\s*struct\s+razer_rgb\s*\*\s*\)\s*&\s*buf\s*\[\s*(\d+)\s*\]$')
BLOB_PTR_RE = re.compile(r'^\(\s*unsigned\s+char\s*\*\s*\)\s*&\s*buf\s*\[\s*(\w+)\s*\]$')
BUF_IDX_RE = re.compile(r'^buf\s*\[\s*(\d+)\s*\]$')


def parse_expr(expr: str, consts: dict[str, int]) -> Any:
    """Convert a C expression into a JSON-serialisable value node."""
    expr = ' '.join(expr.split())
    while expr.startswith('(') and expr.endswith(')') and _match_paren(expr, 0) == len(expr) - 1:
        expr = expr[1:-1].strip()
    # Casts and address-of operators are not valid Python, so peel them off
    # with dedicated patterns before anything else looks at the expression.
    m = re.match(r'^\(\s*(?:struct\s+)?razer_rgb\s*\*\s*\)\s*&\s*buf\s*\[\s*(\d+)\s*\]$', expr)
    if m:
        return {'k': 'rgb', 'offset': int(m.group(1))}

    m = re.match(r'^\(\s*unsigned\s+char\s*\*\s*\)\s*&\s*buf\s*\[\s*(\w+)\s*\]$', expr)
    if m:
        token = m.group(1)
        offset: Any = int(token) if token.isdigit() else {'k': 'var', 'name': token}
        return {'k': 'blob', 'offset': offset}

    m = re.match(r'^&\s*buf\s*\[\s*(\w+)\s*\]$', expr)
    if m:
        token = m.group(1)
        offset = int(token) if token.isdigit() else {'k': 'var', 'name': token}
        return {'k': 'blob', 'offset': offset}

    expr = CAST_RE.sub('', expr).strip()

    m = RESPONSE_ARG_RE.match(expr)
    if m:
        return {'k': 'response', 'offset': int(m.group(1))}

    m = BUF_IDX_RE.match(expr)
    if m:
        return {'k': 'buf', 'offset': int(m.group(1))}

    if re.fullmatch(r'0x[0-9A-Fa-f]+|\d+', expr):
        return {'k': 'const', 'value': int(expr, 0)}

    if expr in consts:
        return {'k': 'const', 'value': consts[expr]}

    if re.fullmatch(r'\w+', expr):
        return {'k': 'var', 'name': expr}

    # ``&rgb1`` -- a local ``struct razer_rgb`` declared with an initialiser.
    m = ADDRESS_OF_RE.match(expr)
    if m:
        return {'k': 'var', 'name': m.group(1)}

    # ``device->orochi2011.poll`` -- cached driver state the device layer owns.
    m = MEMBER_RE.match(expr)
    if m:
        return {'k': 'var', 'name': m.group(1).replace('.', '_')}

    m = TERNARY_RE.match(expr)
    if m:
        cond = COMPARE_RE.match(m.group(1).strip())
        if cond:
            return {'k': 'ternary',
                    'test': {'left': parse_expr(cond.group(1), consts),
                             'op': cond.group(2),
                             'right': parse_expr(cond.group(3), consts)},
                    'then': parse_expr(m.group(2), consts),
                    'else': parse_expr(m.group(3), consts)}

    return {'k': 'raw', 'expr': expr}


def split_args(text: str) -> list[str]:
    args: list[str] = []
    depth = 0
    current = ''
    for ch in text:
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        if ch == ',' and depth == 0:
            args.append(current.strip())
            current = ''
        else:
            current += ch
    if current.strip():
        args.append(current.strip())
    return args


# --------------------------------------------------------------------------
# Statement -> step
# --------------------------------------------------------------------------

BUILDER_RE = re.compile(
    r'^(\w+)\s*=\s*(razer_chroma_\w+|razer_naga_\w+|razer_basilisk_\w+|get_razer_report)\s*\((.*)\)$')
TXID_RE = re.compile(r'^(\w+)\.transaction_id\.id\s*=\s*(.+)$')
ARG_RE = re.compile(r'^(\w+)\.arguments\[\s*([\w+\- ]+)\s*\]\s*=\s*(.+)$')
FIELD_RE = re.compile(r'^(\w+)\.(data_size|command_class|command_id\.id|remaining_packets)\s*=\s*(.+)$')
SEND_RE = re.compile(r'^(?:\w+\s*=\s*)?razer_send_payload(_no_response)?\s*\((.*)\)$')
LET_RE = re.compile(r'^(\w+)\s*=\s*(.+)$')

IGNORED_PREFIXES = (
    'dev_warn', 'dev_err', 'dev_info', 'printk', 'hid_warn', 'hid_err', 'hid_info',
    'mutex_lock', 'mutex_unlock', 'return', 'break', 'continue', 'memset', 'memcpy',
    'WARN', 'msleep', 'usleep', 'print_erroneous_report', 'goto', 'kfree',
    'fallthrough', 'strscpy', 'strncpy', 'serial_string[', 'device->',
    'exit:', 'buf++', 'remaining++', 'dev_get_drvdata',
)

RGB_DECL_RE = re.compile(r'^struct razer_rgb (\w+)\s*=\s*\{(.+)\}$')

#: Row-walking bookkeeping the interpreter performs itself.
ROW_CURSOR_RE = re.compile(
    r'^(?:row_id|start_col|stop_col)\s*=\s*buf\[offset\+\+\]$'
    r'|^offset\s*[+]=|^row_length\s*=')

ARGB_RE = re.compile(r'^(?:\w+\s*=\s*)?razer_send_argb_msg\s*\(\s*[^,]+,\s*([^,]+),\s*([^,]+),\s*(.+)\)$')

# ``buf[N] = response.arguments[M]`` -- a read attribute handing raw bytes back.
OUT_RE = re.compile(r'^buf\[\s*(\d+)\s*\]\s*=\s*response\.arguments\[\s*(\d+)\s*\]$')
XOR_RE = re.compile(r'^(\w+)\s*\^=\s*(.+)$')
RESPONSE_ARG_RE = re.compile(r'^response\.arguments\[\s*(\d+)\s*\]$')

# Conditions that only guard error handling or input validation in the kernel;
# the Python layer validates its own arguments, so they carry no information.
DROP_CONDITIONS = re.compile(
    r'response\.status|\bwant_response\b|kstrto|\bremaining\b|active_stage|'
    r'count\s*!=\s*\d+\s*&&|\bdmi_|\bmutex')

DECL_RE = re.compile(
    r'^(?:const\s+)?(?:unsigned\s+|signed\s+)?'
    r'(?:struct\s+\w+|union\s+\w+|char|int|short|long|size_t|bool|u8|u16|u32|ssize_t)'
    r'[\s*]+\w+')


class Emitter:
    def __init__(self, consts: dict[str, int], pid: Optional[int], blade_pids: set,
                 driver: str, unhandled: list):
        self.consts = consts
        self.pid = pid
        self.blade_pids = blade_pids
        self.driver = driver
        self.unhandled = unhandled

    # -- condition folding -------------------------------------------------
    def fold_condition(self, cond: str) -> Optional[bool]:
        """Return True/False when the condition is decidable for this PID."""
        text = ' '.join(cond.split())
        if 'is_blade_laptop' in text:
            if self.pid is None:
                return None
            base = self.pid in self.blade_pids
            return (not base) if text.startswith('!') else base
        if 'usb_pid' in text or 'idProduct' in text:
            if self.pid is None:
                return None
            names = re.findall(r'USB_DEVICE_ID_\w+', text)
            if not names or '&&' in text:
                return None
            values = {self.consts.get(name) for name in names}
            present = self.pid in values
            if '!=' in text and '==' not in text:
                return not present
            return present
        return None

    # -- main walker -------------------------------------------------------
    def emit(self, nodes: list[Node]) -> list[dict]:
        steps: list[dict] = []
        for node in nodes:
            steps.extend(self.emit_node(node))
        return steps

    def emit_node(self, node: Node) -> list[dict]:
        if node.kind == 'stmt':
            return self.emit_stmt(node.text)

        if node.kind == 'block':
            return self.emit(node.children[0])

        if node.kind == 'loop':
            # ``while (offset < count)`` walks the payload one matrix row at a
            # time.  Emit the body once; the interpreter re-runs it per row and
            # binds row_id / start_col / stop_col / offset itself.
            condition = ' '.join(node.text.split())
            if 'offset' in condition and 'count' in condition:
                body = self.emit(node.children[0])
                return [{'op': 'for_each_row', 'steps': body}] if body else []
            return []

        if node.kind == 'if':
            cond = ' '.join(node.text.split())
            then_nodes, else_nodes = node.children[0], node.children[1]
            if re.search(r'\berr\b|\bretval\b', cond) and 'count' not in cond:
                return []
            if DROP_CONDITIONS.search(cond):
                # Error/validation guard: keep the mainline branch only.
                return self.emit(then_nodes) if 'want_response' in cond else []
            folded = self.fold_condition(cond)
            if folded is True:
                return self.emit(then_nodes)
            if folded is False:
                return self.emit(else_nodes)
            test = self.count_condition(cond)
            if test is not None:
                then_steps = self.emit(then_nodes)
                else_steps = self.emit(else_nodes)
                if not then_steps and not else_steps:
                    return []
                return [{'op': 'if_count', 'test': test,
                         'then': then_steps, 'else': else_steps}]
            self.unhandled.append('{0}: if ({1})'.format(self.driver, cond))
            return self.emit(then_nodes)

        if node.kind == 'switch':
            expr = node.text
            if 'usb_pid' in expr or 'idProduct' in expr:
                return self.emit_pid_switch(node)

            cases: list[dict] = []
            default: list[dict] = []
            key = 'count' if expr.strip() == 'count' else 'value'
            for labels, body in node.cases:
                body_steps = self.emit(body)
                if 'default' in labels:
                    default = body_steps
                for label in labels:
                    if label == 'default':
                        continue
                    try:
                        cases.append({key: int(label, 0), 'steps': body_steps})
                    except ValueError:
                        self.unhandled.append('{0}: case {1}'.format(self.driver, label))
            if expr.strip() == 'count':
                return [{'op': 'switch_count', 'cases': cases, 'default': default}]
            return [{'op': 'switch_var', 'var': expr.strip(),
                     'cases': cases, 'default': default}]

        return []

    @staticmethod
    def count_condition(cond: str) -> Optional[dict]:
        m = re.match(r'^count\s*(==|!=|<|>|<=|>=)\s*(\d+)$', cond.strip())
        if m:
            return {'op': m.group(1), 'value': int(m.group(2))}
        return None

    def emit_pid_switch(self, node: Node) -> list[dict]:
        default_body: Optional[list[Node]] = None
        for labels, body in node.cases:
            if 'default' in labels:
                default_body = body
            if self.pid is None:
                continue
            names = [lab for lab in labels if lab != 'default']
            values = {self.consts.get(name) for name in names}
            if self.pid in values:
                return self.emit(body)
        if default_body is not None:
            return self.emit(default_body)
        return []

    def emit_stmt(self, stmt: str) -> list[dict]:
        text = stmt.strip().rstrip(';').strip()
        if not text:
            return []
        # ``return count;`` is the kernel's "handled, we're done" exit.  It
        # matters: the ARGB controller returns there and must NOT fall through
        # to the shared razer_send_payload tail.  Error returns carry no such
        # meaning and are dropped with the rest of the error handling.
        if re.fullmatch(r'return\s+count', text):
            return [{'op': 'stop'}]

        if any(text.startswith(prefix) for prefix in IGNORED_PREFIXES):
            return []
        if ROW_CURSOR_RE.match(text):
            return []

        m = ARGB_RE.match(text)
        if m:
            return [{'op': 'send_argb',
                     'channel': parse_expr(m.group(1), self.consts),
                     'size': parse_expr(m.group(2), self.consts),
                     'data': parse_expr(m.group(3), self.consts)}]

        # ``struct razer_rgb rgb1 = {.r = 0x00, .g = 0xFF, .b = 0x00};``
        m = RGB_DECL_RE.match(text)
        if m:
            channels = {key: int(value, 0) for key, value in
                        re.findall(r'\.(\w)\s*=\s*(0x[0-9A-Fa-f]+|\d+)', m.group(2))}
            return [{'op': 'let', 'name': m.group(1),
                     'value': {'k': 'const', 'value': [channels.get('r', 0),
                                                       channels.get('g', 0),
                                                       channels.get('b', 0)]}}]

        m = SEND_RE.match(text)
        if m:
            return [{'op': 'send_no_response' if m.group(1) else 'send'}]

        m = OUT_RE.match(text)
        if m:
            return [{'op': 'out', 'index': int(m.group(1)), 'arg': int(m.group(2))}]

        m = XOR_RE.match(text)
        if m:
            expr = m.group(2).replace(' ', '')
            if re.fullmatch(r'\(?\(1<<0\)\|\(1<<1\)\)?', expr):
                return [{'op': 'xor', 'name': m.group(1), 'value': 3}]

        m = BUILDER_RE.match(text)
        if m:
            target, fn, raw_args = m.group(1), m.group(2), m.group(3)
            args = [parse_expr(a, self.consts) for a in split_args(raw_args)]
            return [{'op': 'build', 'target': target, 'fn': fn, 'args': args}]

        m = TXID_RE.match(text)
        if m:
            return [{'op': 'txid', 'target': m.group(1),
                     'value': parse_expr(m.group(2), self.consts)}]

        m = ARG_RE.match(text)
        if m:
            idx = m.group(2).strip()
            index: Any = (int(idx, 0) if re.fullmatch(r'0x[0-9A-Fa-f]+|\d+', idx)
                          else parse_expr(idx, self.consts))
            return [{'op': 'set_arg', 'target': m.group(1), 'index': index,
                     'value': parse_expr(m.group(3), self.consts)}]

        m = FIELD_RE.match(text)
        if m:
            return [{'op': 'set_field', 'target': m.group(1),
                     'field': m.group(2).replace('.id', ''),
                     'value': parse_expr(m.group(3), self.consts)}]

        if DECL_RE.match(text):
            return []

        m = LET_RE.match(text)
        if m:
            name, value = m.group(1), m.group(2)
            if 'razer_' in value or 'kstrto' in value or name in ('err', 'retval', 'ret'):
                return []
            return [{'op': 'let', 'name': name, 'value': parse_expr(value, self.consts)}]

        if 'kstrto' in text:
            return []

        self.unhandled.append('{0}: {1}'.format(self.driver, text[:90]))
        return []


# --------------------------------------------------------------------------

FUNC_RE = re.compile(
    r'static\s+(?:__must_check\s+)?ssize_t\s+(razer_attr_(read|write)_(\w+))\s*\(([^)]*)\)\s*\{')

#: Parameters every sysfs handler takes; anything beyond them is a bound value
#: supplied by a delegating wrapper such as ``razer_attr_write_logo_...``.
STANDARD_PARAMS = ('dev', 'attr', 'buf', 'count')

DELEGATE_RE = re.compile(r'^return\s+(razer_attr_(?:read|write)_\w+)\s*\((.*)\)$')


def parse_signature(raw_params: str) -> list:
    """Return the handler's extra parameter names, in declaration order."""
    names = []
    for param in split_args(raw_params):
        match = re.search(r'(\w+)\s*$', param.strip())
        if match and match.group(1) not in STANDARD_PARAMS:
            names.append(match.group(1))
    return names


def find_delegation(nodes: list, consts: dict) -> Optional[tuple]:
    """Detect a wrapper whose whole body is ``return other_handler(...)``."""
    statements = [n for n in nodes if n.kind == 'stmt' and n.text]
    if len(statements) != 1:
        return None
    match = DELEGATE_RE.match(statements[0].text.rstrip(';'))
    if not match:
        return None
    target = match.group(1)
    extra = [a for a in split_args(match.group(2)) if a not in STANDARD_PARAMS]
    values = []
    for arg in extra:
        arg = arg.strip()
        if arg in consts:
            values.append(consts[arg])
        elif re.fullmatch(r'0x[0-9A-Fa-f]+|\d+', arg):
            values.append(int(arg, 0))
        else:
            return None
    return target, values


def extract_transport(driver_dir: str, consts: dict) -> dict:
    """Per-PID USB interface index and post-write delay.

    Windows exposes each USB interface as its own HID collection, so the
    interface index the kernel passes to ``usb_control_msg`` tells us which
    collection to open.  The delay is how long the device needs between
    SET_REPORT and GET_REPORT.
    """
    transport: dict[str, Any] = {}

    def scan(filename: str, func_pattern: str, extractor) -> dict:
        src = strip_comments(open(os.path.join(driver_dir, filename), encoding='utf-8').read())
        match = re.search(func_pattern, src)
        if not match:
            return {'default': None, 'pids': {}}
        brace = src.index('{', match.end() - 1)
        body = src[brace + 1:_match_brace(src, brace)]
        nodes = parse_block(body)
        switches = [n for n in nodes if n.kind == 'switch']
        result: dict[str, Any] = {'default': None, 'pids': {}}
        if not switches:
            result['default'] = extractor(nodes, consts)
            return result
        for labels, case_body in switches[0].cases:
            params = extractor(case_body, consts)
            if params is None:
                continue
            if 'default' in labels:
                result['default'] = params
            for label in labels:
                if label == 'default' or label not in consts:
                    continue
                result['pids']['{0:04x}'.format(consts[label])] = params
        return result

    def kbd_extractor(nodes, consts_map):
        index = wait = None
        for node in nodes:
            if node.kind != 'stmt':
                continue
            m = re.match(r'\*report_index\s*=\s*(\S+)', node.text)
            if m:
                index = int(m.group(1).rstrip(';'), 0)
            m = re.match(r'\*wait\s*=\s*(\w+)', node.text)
            if m:
                wait = consts_map.get(m.group(1).rstrip(';'))
        if index is None and wait is None:
            return None
        return {'index': index or 0, 'wait_us': wait or 600}

    def call_extractor(nodes, consts_map):
        index = None
        wait = None
        for node in nodes:
            if node.kind != 'stmt':
                continue
            m = re.match(r'index\s*=\s*(\S+)', node.text)
            if m:
                index = int(m.group(1).rstrip(';'), 0)
            m = re.search(r'razer_get_usb_response\(hdev,\s*([^,]+),\s*request,\s*[^,]+,\s*response,\s*(\w+)\)',
                          node.text)
            if m:
                token = m.group(1).strip()
                if re.fullmatch(r'0x[0-9A-Fa-f]+|\d+', token):
                    index = int(token, 0)
                wait = consts_map.get(m.group(2))
        if wait is None and index is None:
            return None
        return {'index': index or 0, 'wait_us': wait or 600}

    transport['kbd'] = scan('razerkbd_driver.c',
                            r'static void razer_get_report_params\([^)]*\)\s*\{',
                            kbd_extractor)
    transport['mouse'] = scan('razermouse_driver.c',
                              r'static int razer_get_report\([^)]*\)\s*\{',
                              call_extractor)
    transport['accessory'] = scan('razeraccessory_driver.c',
                                  r'static int razer_get_report\([^)]*\)\s*\{',
                                  call_extractor)
    for entry in transport.values():
        if entry['default'] is None:
            entry['default'] = {'index': 0, 'wait_us': 600}
    return transport


def transpile(upstream: str, out_path: str) -> int:
    driver_dir = os.path.join(upstream, 'driver')
    consts, _pid_map = load_constants(driver_dir)

    kbd_src = strip_comments(
        open(os.path.join(driver_dir, 'razerkbd_driver.c'), encoding='utf-8').read())
    blade_block = re.search(r'static bool is_blade_laptop\(.*?\n\}', kbd_src, re.S)
    blade_pids = set()
    if blade_block:
        for name in re.findall(r'USB_DEVICE_ID_\w+', blade_block.group(0)):
            if name in consts:
                blade_pids.add(consts[name])

    unhandled: list[str] = []
    result: dict[str, Any] = {'schema': 1, 'drivers': {}}

    # Most product ids share a handful of distinct step lists, so intern them
    # into a pool and reference by index.  Cuts the on-disk size ~8x.
    pool: list[list[dict]] = []
    pool_index: dict[str, int] = {}

    def intern(steps: list[dict]) -> int:
        key = json.dumps(steps, separators=(',', ':'), sort_keys=True)
        if key not in pool_index:
            pool_index[key] = len(pool)
            pool.append(steps)
        return pool_index[key]

    for driver, filename in DRIVERS.items():
        src = strip_comments(open(os.path.join(driver_dir, filename), encoding='utf-8').read())
        attrs: dict[str, Any] = {}

        # Pass 1: record every handler's extra parameters so a delegating
        # wrapper can bind them by name.
        signatures: dict[str, list] = {}
        for match in FUNC_RE.finditer(src):
            signatures[match.group(1)] = parse_signature(match.group(4))

        for match in FUNC_RE.finditer(src):
            attr_name = match.group(3)
            direction = match.group(2)
            brace = match.end() - 1
            body_src = src[brace + 1:_match_brace(src, brace)]
            nodes = parse_block(body_src)

            delegation = find_delegation(nodes, consts)
            if delegation is not None:
                target, values = delegation
                target_params = signatures.get(target, [])
                if len(values) == len(target_params):
                    target_attr = target[len('razer_attr_'):]
                    target_dir, _, target_name = target_attr.partition('_')
                    attrs['{0}:{1}'.format(direction, attr_name)] = {
                        'alias': '{0}:{1}'.format(target_dir, target_name),
                        'bind': dict(zip(target_params, values)),
                    }
                    continue

            pids = {consts[name] for name in re.findall(r'USB_DEVICE_ID_\w+', body_src)
                    if name in consts}

            default_steps = Emitter(consts, None, blade_pids, driver, unhandled).emit(nodes)
            default_ref = intern(default_steps)

            per_pid: dict[str, int] = {}
            for pid in sorted(pids):
                steps = Emitter(consts, pid, blade_pids, driver, unhandled).emit(nodes)
                if not steps:
                    continue
                ref = intern(steps)
                if ref != default_ref:
                    per_pid['{0:04x}'.format(pid)] = ref

            attrs['{0}:{1}'.format(direction, attr_name)] = {
                'default': default_ref, 'pids': per_pid}
        result['drivers'][driver] = attrs

    # The Kraken family speaks a different protocol entirely (address writes to
    # RAM rather than the 90-byte control report), so it has no recipes.  Record
    # which product ids belong to it so the port routes them to its own device
    # class instead of the accessory tables.
    kraken_src = strip_comments(
        open(os.path.join(driver_dir, 'razerkraken_driver.c'), encoding='utf-8').read())
    kraken_table = re.search(r'razer_devices\[\]\s*=\s*\{(.*?)\};', kraken_src, re.S)
    kraken_pids = sorted({
        consts[name] for name in re.findall(
            r'USB_DEVICE_ID_\w+', kraken_table.group(1) if kraken_table else kraken_src)
        if name in consts})
    result['kraken_pids'] = ['{0:04x}'.format(pid) for pid in kraken_pids]

    result['transport'] = extract_transport(driver_dir, consts)
    result['pool'] = pool
    result['blade_pids'] = sorted('{0:04x}'.format(p) for p in blade_pids)
    result['constants'] = {k: v for k, v in consts.items() if not k.startswith('USB_DEVICE_ID_')}

    with open(out_path, 'w', encoding='utf-8') as handle:
        json.dump(result, handle, separators=(',', ':'), sort_keys=False)
        handle.write('\n')

    counts = {d: len(a) for d, a in result['drivers'].items()}
    print('wrote {0}; attributes per driver: {1}; pooled recipes: {2}'.format(
        out_path, counts, len(pool)))
    if unhandled:
        seen: dict[str, int] = {}
        for item in unhandled:
            seen[item] = seen.get(item, 0) + 1
        print('--- {0} distinct unhandled constructs ---'.format(len(seen)), file=sys.stderr)
        for item, n in sorted(seen.items(), key=lambda kv: -kv[1])[:60]:
            print('  {0:5d}  {1}'.format(n, item), file=sys.stderr)
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(transpile(sys.argv[1], sys.argv[2]))
