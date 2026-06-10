"""The `{{ ... }}` expression mini-language and its compilation to JavaScript.

workflow-kit does NOT render templates at compile time. Instead it compiles each
`{{ expr }}` into a live JS expression so interpolation happens inside the running
Workflow engine with real values. A prompt string becomes a JS template literal;
a bare value (an `over:` or `output:`) becomes a JS expression.

Supported expressions (anything else is a compile error -- v1 stays tight):

    {{ inputs.X[.Y...] }}        -> inputs.X[.Y...]  (the compiler emits a normalized
                                    `const inputs` from the runtime `args`, which the
                                    Workflow tool delivers as a JSON string)
    {{ steps.ID[.Y...] }}        -> <stepvar>[.Y...]
    {{ steps.ID[*].F[.Y...] }}   -> <stepvar>.flatMap((r) => r.F[.Y...])
    {{ <local> [.Y...] }}        -> the item identifier in scope (pipeline `as`,
                                    fan_out `as`, or the implicit `item` of a flat
                                    for_each)
    {{ <prevStageId> [.Y...] }}  -> <prevVar>[.Y...]  (only the immediately
                                    preceding stage of a pipeline is addressable)

A Scope carries the bindings available at one point in the document.
"""

from __future__ import annotations

import re
from typing import Optional

from .errors import WorkflowError

_EXPR_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_SINGLE_RE = re.compile(r"^\s*\{\{(.*)\}\}\s*$", re.DOTALL)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STEPS_RE = re.compile(
    r"^steps\.([A-Za-z_][A-Za-z0-9_]*)(\[\*\])?(.*)$", re.DOTALL
)
_INPUTS_RE = re.compile(r"^inputs\.(.+)$", re.DOTALL)


class Scope:
    """Bindings in effect where an expression is compiled.

    step_vars  : {step_id -> js variable name} for steps already defined.
    locals     : {local_name -> js identifier} for in-scope items (the `as` names
                 of an enclosing pipeline/fan_out, or "item" for a flat for_each).
    prev_stage : (stage_id, js_var) for the immediately preceding pipeline stage,
                 or None.
    inputs     : set of input names `inputs.X` heads may use, or None to skip the
                 check. The compiler passes the declared `inputs:` names (plus the
                 reserved node args when node steps exist); an unknown head is a
                 compile error instead of a silent runtime `undefined`.
    """

    def __init__(self, step_vars=None, locals=None, prev_stage=None, inputs=None):
        self.step_vars = dict(step_vars or {})
        self.locals = dict(locals or {})
        self.prev_stage = prev_stage
        self.inputs = set(inputs) if inputs is not None else None

    def available(self) -> str:
        names = (
            [f"inputs.*"]
            + [f"steps.{k}" for k in self.step_vars]
            + list(self.locals)
            + ([self.prev_stage[0]] if self.prev_stage else [])
        )
        return ", ".join(names) if names else "(nothing)"


def _split_idents(s: str, full: str) -> list:
    parts = [p for p in s.split(".") if p != ""]
    for p in parts:
        if not _IDENT.match(p):
            raise WorkflowError(f"invalid identifier {p!r} in {{{{ {full.strip()} }}}}")
    return parts


def _member(parts) -> str:
    return "".join(f".{p}" for p in parts)


def compile_expr(expr: str, scope: Scope) -> str:
    """Compile one `{{ }}`-interior expression to a JS expression string."""
    e = expr.strip()
    if not e:
        raise WorkflowError("empty {{ }} expression")

    m = _STEPS_RE.match(e)
    if m:
        sid, star, rest = m.group(1), m.group(2), m.group(3)
        if sid not in scope.step_vars:
            raise WorkflowError(
                f"unknown step reference {sid!r} in {{{{ {e} }}}}; "
                f"available: {scope.available()}"
            )
        var = scope.step_vars[sid]
        rest = rest.strip()
        if rest and not rest.startswith("."):
            raise WorkflowError(f"expected '.' after step reference in {{{{ {e} }}}}")
        tail = _split_idents(rest, e)
        if star:
            if not tail:
                raise WorkflowError(f"`[*]` must be followed by a field in {{{{ {e} }}}}")
            return f"{var}.flatMap((r) => r{_member(tail)})"
        return f"{var}{_member(tail)}"

    m = _INPUTS_RE.match(e)
    if m:
        parts = _split_idents(m.group(1), e)
        if scope.inputs is not None and parts[0] not in scope.inputs:
            raise WorkflowError(
                f"unknown input {parts[0]!r} in {{{{ {e} }}}}; "
                f"declared inputs: {sorted(scope.inputs)}"
            )
        return "inputs" + _member(parts)

    parts = _split_idents(e, e)
    head, tail = parts[0], parts[1:]
    if head in scope.locals:
        return scope.locals[head] + _member(tail)
    if scope.prev_stage and head == scope.prev_stage[0]:
        return scope.prev_stage[1] + _member(tail)
    raise WorkflowError(
        f"unknown reference {head!r} in {{{{ {e} }}}}; available: {scope.available()}"
    )


def _escape_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def compile_template(template: str, scope: Scope) -> str:
    """Compile a string with embedded `{{ }}` into a JS template literal (backticks included)."""
    out = ["`"]
    pos = 0
    for m in _EXPR_RE.finditer(template):
        out.append(_escape_literal(template[pos:m.start()]))
        out.append("${" + compile_expr(m.group(1), scope) + "}")
        pos = m.end()
    out.append(_escape_literal(template[pos:]))
    out.append("`")
    return "".join(out)


def compile_single(value, scope: Scope) -> str:
    """Compile a value that must be exactly one `{{ }}` expression to a JS expression."""
    if not isinstance(value, str):
        raise WorkflowError(f"expected a {{{{ ... }}}} expression string, got {value!r}")
    m = _SINGLE_RE.match(value)
    if not m:
        raise WorkflowError(
            f"expected a single {{{{ ... }}}} expression, got {value!r}"
        )
    return compile_expr(m.group(1), scope)
