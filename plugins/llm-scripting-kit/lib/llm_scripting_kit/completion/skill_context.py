"""Skill context at the completion seam: materialize, prepare, compose.

A caller names skills by path; ``bootstrap_lib.skill_material`` (bootstrap's
shared library) reads them once and returns one token-bounded text block plus a
provenance report. This module is llm-scripting-kit's side of that edge:

1. BEFORE THE CALL -- :func:`materialize_skill_context` builds a
   :class:`~.skill_context_types.SkillContext` from a library ``SkillSelection``
   or its JSON mapping. Every read happens here, so the block and its
   ``digest`` are fixed before anything is dispatched.
2. BEFORE DISPATCH -- every adapter's ``complete()`` calls
   :func:`prepare_skill_context` right after ``prepare_contract``. With no
   skill context it returns None and the call is unchanged. Otherwise it checks
   the record's digest and refuses, with
   :class:`~.skill_context_types.SkillContextUnsatisfiable`, on an adapter
   whose capability record has no ``skill_context`` block. The three harness
   adapters have none: a harness loads skills itself, and the seam cannot see
   what it loaded.
3. DELIVERY -- a delivering adapter composes the system text with
   :func:`compose_system`: the block first, then the caller's system text; the
   output-contract instruction, when there is one, is appended after both.

The edge to ``bootstrap_lib`` is REQUIRED with a floor (``requires_bootstrap``
in this plugin's ``bootstrap.json``) and refuses at the call for the states a
floor cannot exclude: :func:`_skill_material` diagnoses an absent library and
one too old to make the calls this module makes, and a missing PyYAML is a
third state. Each message names what a user runs; the version in a message is
this module's own constant, never read from the library.
"""
from __future__ import annotations

import hashlib
import inspect
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional

from .skill_context_types import (
    DELIVERY_SYSTEM_MESSAGE,
    RESOLVER,
    SkillContext,
    SkillContextError,
    SkillContextReport,
    SkillContextSupportError,
    SkillContextUnsatisfiable,
)

#: The bootstrap version that first ships ``bootstrap_lib.skill_material`` with
#: the calls this module makes. This plugin's own constant: a stale library
#: cannot know the version that replaced it.
SKILL_MATERIAL_BOOTSTRAP = "0.138.0"

#: The library report schema this module reads.
_REQUIRED_REPORT_SCHEMA = "plugins-kit.skill-material-report/v1"

STATE_ABSENT = "absent"
STATE_TOO_OLD = "too-old"
STATE_NO_PYYAML = "no-pyyaml"

_ABSENT_MESSAGE = (
    "skill context needs bootstrap_lib, which is not importable in this "
    "interpreter; run `claude plugin install bootstrap@plugins-kit`. Nothing "
    "was dispatched. To send the request without it, omit skill_context and "
    "place your own text in system."
)
_TOO_OLD_MESSAGE = (
    "the linked bootstrap_lib does not provide the skill-material calls this "
    f"seam makes; skill context needs bootstrap >= {SKILL_MATERIAL_BOOTSTRAP}. "
    "Run `claude plugin update bootstrap@plugins-kit`. A copy left behind by an "
    "uninstall reads the same way. Nothing was dispatched."
)
_NO_PYYAML_MESSAGE = (
    "the interpreter running this call has no PyYAML, which "
    "bootstrap_lib.skill_material needs to read a skill; llm-scripting-kit's "
    "own environment has it, so run the call through the `llm-scripting-kit` "
    "command or from a plugin that declares PyYAML. Nothing was dispatched."
)


def _too_old(detail: str = "") -> SkillContextSupportError:
    message = _TOO_OLD_MESSAGE if not detail else f"{_TOO_OLD_MESSAGE} ({detail})"
    return SkillContextSupportError(message, state=STATE_TOO_OLD)


def _skill_material() -> Any:
    """Return ``bootstrap_lib.skill_material``, probed for every call made here.

    The probe binds the report schema this module reads, the two exception
    classes it catches, ``SkillSelection.from_json(mapping)``,
    ``materialize(selection, base_dir=...)`` and
    ``SkillMaterialReport.to_json(self)``. Only a ``ModuleNotFoundError`` for
    the named module is caught, so a syntax error in a half-synced copy
    surfaces as itself.
    """
    try:
        import bootstrap_lib  # noqa: F401, PLC0415
    except ModuleNotFoundError as exc:
        raise SkillContextSupportError(_ABSENT_MESSAGE, state=STATE_ABSENT) from exc
    try:
        import bootstrap_lib.skill_material as module  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        if exc.name != "bootstrap_lib.skill_material":
            raise
        raise _too_old() from exc
    supported = getattr(module, "SUPPORTED_REPORT_SCHEMAS", None)
    from_json = getattr(getattr(module, "SkillSelection", None), "from_json", None)
    materialize = getattr(module, "materialize", None)
    to_json = getattr(getattr(module, "SkillMaterialReport", None), "to_json", None)
    if (
        not isinstance(supported, (set, frozenset))
        or _REQUIRED_REPORT_SCHEMA not in supported
        or not isinstance(getattr(module, "SkillMaterialError", None), type)
        or not isinstance(getattr(module, "PyYamlUnavailableError", None), type)
    ):
        raise _too_old()
    try:
        inspect.signature(from_json).bind({})
        inspect.signature(materialize).bind(object(), base_dir=None)
        inspect.signature(to_json).bind(object())
    except (TypeError, ValueError) as exc:
        raise _too_old() from exc
    return module


def _is_json_native(value: Any) -> bool:
    """True when ``value`` is a JSON document at every depth.

    A ``dict`` with ``str`` keys, a ``list``, a ``str``, an ``int``, a finite
    ``float``, a ``bool`` or ``None``. A tuple, a set or any other object
    anywhere fails. Walked with an explicit stack, so depth is not bounded by
    the recursion limit.
    """
    stack = [value]
    while stack:
        item = stack.pop()
        if item is None or isinstance(item, (str, bool, int)):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                return False
            continue
        if type(item) is dict:
            for key, child in item.items():
                if not isinstance(key, str):
                    return False
                stack.append(child)
            continue
        if type(item) is list:
            stack.extend(item)
            continue
        return False
    return True


def skill_context_from(materialized: Any) -> SkillContext:
    """Adapt the library's ``MaterializedSkills`` to this seam's record.

    The only place that reads the library's shape. A missing attribute, a
    report schema other than the one this module knows, a ``to_json()`` that
    raises ``TypeError``, or a result that is not a JSON-native mapping at every
    depth is the ``too-old`` state: the library in hand is not the one this
    module was written against.
    """
    try:
        text = materialized.text
        report = materialized.report
        schema = report.schema
        format_version = report.format_version
        digest = report.digest
        estimated_tokens = report.estimated_tokens
        token_budget = report.token_budget
        token_estimate = report.token_estimate
        skills = len(report.skills)
    except (AttributeError, TypeError) as exc:
        raise _too_old(f"unexpected result shape: {exc}") from exc
    if schema != _REQUIRED_REPORT_SCHEMA:
        raise _too_old(
            f"report schema {schema!r}, expected {_REQUIRED_REPORT_SCHEMA!r}"
        )
    try:
        provenance = report.to_json()
    except (AttributeError, TypeError) as exc:
        raise _too_old(f"report.to_json() failed: {exc}") from exc
    if type(provenance) is not dict or not _is_json_native(provenance):
        raise _too_old("report.to_json() did not return a JSON-native mapping")
    if not isinstance(text, str):
        raise _too_old("the materialized text is not a str")
    return SkillContext(
        text=text,
        report=SkillContextReport(
            resolver=RESOLVER,
            report_schema=schema,
            format_version=format_version,
            digest=digest,
            estimated_tokens=estimated_tokens,
            token_budget=token_budget,
            token_estimate=token_estimate,
            skills=skills,
            provenance=provenance,
        ),
    )


def materialize_skill_context(
    selection: Any, *, base_dir: Optional[Path] = None
) -> SkillContext:
    """Read the selected skills once and return the seam's record.

    ``selection`` is a ``bootstrap_lib.skill_material.SkillSelection`` or its
    JSON mapping (built with the library's own ``SkillSelection.from_json``,
    which refuses an unknown key). A relative skill path resolves against
    ``base_dir``, default the process working directory.

    Raises :class:`~.skill_context_types.SkillContextError` with the library's
    message for a refused selection, skill or resource (``from`` the library's
    error), and :class:`~.skill_context_types.SkillContextSupportError` when
    the library is absent, too old, or cannot parse for want of PyYAML.
    """
    module = _skill_material()
    if not isinstance(selection, (Mapping, module.SkillSelection)):
        raise TypeError(
            "materialize_skill_context takes a SkillSelection or its JSON "
            f"mapping, got {type(selection).__name__}"
        )
    try:
        if isinstance(selection, Mapping):
            selection = module.SkillSelection.from_json(selection)
        result = module.materialize(selection, base_dir=base_dir)
    except module.PyYamlUnavailableError as exc:
        raise SkillContextSupportError(_NO_PYYAML_MESSAGE, state=STATE_NO_PYYAML) from exc
    except module.SkillMaterialError as exc:
        raise SkillContextError(str(exc)) from exc
    return skill_context_from(result)


@dataclass(frozen=True)
class SkillContextPlan:
    """How one adapter will deliver one skill context on one call."""

    context: SkillContext
    adapter: str
    delivery: str
    emits: str

    def delivered_report(self) -> SkillContextReport:
        """The report as it goes on ``LLMResponse.skill_context``."""
        return replace(
            self.context.report,
            adapter=self.adapter,
            delivery=self.delivery,
            emits=self.emits,
        )


def prepare_skill_context(capabilities: Any, options: Any) -> Optional[SkillContextPlan]:
    """Refuse or plan a skill context BEFORE anything is dispatched.

    Returns None when the options carry no skill context. Raises
    :class:`TypeError` for a value that is not a
    :class:`~.skill_context_types.SkillContext`,
    :class:`~.skill_context_types.SkillContextError` when the record's text
    and digest disagree, and
    :class:`~.skill_context_types.SkillContextUnsatisfiable` when
    ``capabilities`` has no ``skill_context`` block.
    """
    context = getattr(options, "skill_context", None) if options is not None else None
    if context is None:
        return None
    if not isinstance(context, SkillContext):
        raise TypeError(
            "options.skill_context must be a SkillContext built by "
            "llm_scripting_kit.completion.materialize_skill_context, got "
            f"{type(context).__name__}"
        )
    actual = hashlib.sha256(context.text.encode("utf-8")).hexdigest()
    if actual != context.report.digest:
        raise SkillContextError(
            "skill context text and report disagree: sha256 of the text is "
            f"{actual}, the report says {context.report.digest}; refused before "
            "dispatch. Materialize the selection again rather than editing the "
            "text."
        )
    adapter = getattr(capabilities, "adapter", "<unknown adapter>")
    block = getattr(capabilities, "skill_context", None)
    if block is None:
        raise SkillContextUnsatisfiable(
            f"{adapter} does not deliver skill context: a harness loads skills "
            "itself, and this seam cannot see what it loads. Nothing was "
            "dispatched. Use a transport entry (adapter openrouter), or omit "
            "skill_context and place your own text in system."
        )
    return SkillContextPlan(
        context=context, adapter=adapter, delivery=block.delivery, emits=block.emits
    )


def compose_system(plan: Optional[SkillContextPlan], system: str) -> str:
    """The block first, then the caller's system text, joined by a blank line.

    With no plan, ``system`` unchanged; with an empty ``system``, the block
    alone.
    """
    if plan is None:
        return system
    if not system:
        return plan.context.text
    return plan.context.text + "\n\n" + system


def skill_context_requirements(context: Optional[SkillContext]) -> Dict[str, Any]:
    """The selection requirement a skill context implies (``{}`` for None).

    ``{"skill_context": {"delivery": "system-message"}}``: only an adapter
    whose record carries that block delivers the context, and any other would
    refuse it before dispatch.
    """
    if context is None:
        return {}
    if not isinstance(context, SkillContext):
        raise TypeError(
            f"expected a SkillContext, got {type(context).__name__}"
        )
    return {"skill_context": {"delivery": DELIVERY_SYSTEM_MESSAGE}}


__all__ = [
    "SKILL_MATERIAL_BOOTSTRAP",
    "STATE_ABSENT",
    "STATE_TOO_OLD",
    "STATE_NO_PYYAML",
    "SkillContextPlan",
    "materialize_skill_context",
    "skill_context_from",
    "prepare_skill_context",
    "compose_system",
    "skill_context_requirements",
]
