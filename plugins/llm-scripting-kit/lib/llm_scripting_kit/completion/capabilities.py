"""Adapter-owned capability advertisement.

Endpoints behind the completion seam are not equal: an HTTP model and a codex
subprocess differ in which params they honor, what they can constrain, whether a
caller schema is enforced, and how system text reaches the model. This module
does not make them equal -- it makes their inequality legible BEFORE the call.

The one rule every value here obeys: **a capability describes what the adapter
EMITS, never what the provider or CLI does with it.** ``ExecutionControl.emits``
names the concrete argv element, environment key, or request field the adapter
produces, so a seam test can assert it. Nothing in this module promises that a
target honors a control -- no fake-seam test can establish that, and advertising
it would be exactly the overclaim the advertisement exists to prevent.

Two consequences worth stating, because both were mis-designed once:

- Suppressing a flag is NOT a control. ``codex`` omits its network-enabling
  ``-c`` pair when ``network=False``; nothing is emitted, so no control is
  advertised for that case.
- A value menu is advertised only where the code that builds the request
  validates it. ``CodexCliBackend`` calls the shared argv builder directly and
  bypasses ``CodexAdapter``'s effort validation, so codex advertises no effort
  ``values``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

# -- param handling --------------------------------------------------------

MAPPED = "mapped"
"""The adapter reads the param and changes an observable request element."""

PASSTHROUGH = "passthrough"
"""The adapter copies the value into a generic downstream field, unvalidated."""

# -- control effects -------------------------------------------------------

ALLOW = "allow"
"""The emission asks the target to permit the named subjects."""

DENY = "deny"
"""The emission asks the target to reject the named subjects."""

CONFINE = "confine"
"""The emission asks the target to limit an activity to a stated boundary."""

DISABLE = "disable"
"""The emission asks the target not to load or expose the named subjects."""

BYPASS = "bypass"

# -- canonical guarantee subjects -------------------------------------------
#
# A SUBJECT names a capability of the running agent in adapter-neutral terms, so
# a caller can require an OUTCOME ("this job must not mutate the working tree")
# without naming the mechanism any one harness happens to use for it. Each
# adapter says how it delivers the subject: via an ExecutionControl that denies
# or confines it, or -- when the adapter has no such capability at all -- by
# listing it in Capabilities.guarantees.
#
# Add a subject only when a caller needs it, and only with an empirical check
# behind it: ExecutionControl.effect records what an emission ASKS for, never
# that the target complied.
FILESYSTEM_WRITE = "filesystem-write"
SHELL_EXEC = "shell-exec"
SUBAGENT_SPAWN = "subagent-spawn"

# A deny floor is expressed in BackendOptions' only tool-deny vocabulary, which
# is claude-cli's tool NAMES. Requiring an outcome from it means knowing what
# those names denote, so the mapping lives here beside the subjects rather than
# in any one consumer: job-kit reads it to derive what a floor requires, and the
# opencode adapter reads it to translate the same list into permission scalars.
# Two consumers deriving it separately is how they drift.
_TOOL_SUBJECTS = {
    FILESYSTEM_WRITE: frozenset(
        {"write", "edit", "multiedit", "notebookedit", "patch", "apply_patch"}
    ),
    SHELL_EXEC: frozenset({"bash", "shell", "run", "execute"}),
    SUBAGENT_SPAWN: frozenset({"task", "agent", "subagent"}),
}


def subjects_for_disallowed_tools(disallowed_tools):
    """The canonical subjects a claude-shaped deny list is asking to withhold.

    Names match case-insensitively, and a name carrying a claude-style argument
    filter (``Bash(git *)``) is read by its head. An unrecognized name maps to
    no subject: it must neither widen the ask nor silently narrow it, so a
    caller naming only unknown tools gets an empty set and requires nothing.
    """
    if not disallowed_tools:
        return frozenset()
    names = set()
    for token in str(disallowed_tools).replace(",", " ").split():
        token = token.strip().lower()
        if not token:
            continue
        names.add(token.split("(", 1)[0])
    return frozenset(
        subject for subject, aliases in _TOOL_SUBJECTS.items() if names & aliases
    )

"""The emission asks the target not to require interactive approval."""

# -- control provenance ----------------------------------------------------

FIXED = "fixed"
"""Every invocation of this adapter emits the control."""

REQUEST = "request"
"""The caller selects the control through ``parameter``."""

ALWAYS = "always"
"""The mapping applies on every platform."""

WINDOWS = "windows"
"""The mapping applies only when building a Windows invocation."""

# -- structured output -----------------------------------------------------

NATIVE = "native"
"""A dedicated first-class provider or CLI schema control carries the schema."""

NONE = "none"
"""No caller-schema parameter is bound; such a request is dropped."""

TEXT_RESULT = "text"
"""The normalized response stays text even when a schema control was sent."""

PARSED_RESULT = "parsed"
"""Schema-backed output is parsed into the normalized ``structured`` field."""

# -- system prompt handling ------------------------------------------------

NATIVE_ROLE = "native-role"
"""System text is sent as a distinct system-role message."""

REPLACE = "replace"
"""System text goes through a native whole-system-prompt replacement channel."""

APPEND = "append"
"""System text goes through a distinct native append channel."""

PROMPT_FOLD = "prompt-fold"
"""System and user text are concatenated into one model-visible prompt."""


@dataclass(frozen=True)
class ParamCapability:
    """How one param reaches the request.

    ``type`` is a DECLARED expectation, not an enforced one: ``BackendOptions``
    is a plain frozen dataclass with annotations and every adapter passes raw
    values through, so nothing rejects a wrong type before dispatch. Validation
    belongs at the CLI request boundary, not here.

    ``values`` is present only where the code building the request actually
    validates the menu. Its absence means the adapter claims no exhaustive menu,
    NOT that any value is invalid downstream.
    """

    type: str
    handling: str = MAPPED
    values: Optional[Tuple[str, ...]] = None
    default: Optional[Any] = None
    emits: Optional[str] = None
    note: str = ""

    def to_json(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"type": self.type, "handling": self.handling}
        if self.values is not None:
            result["values"] = list(self.values)
        if self.default is not None:
            # tuple -> list so the payload is JSON-native; a caller mutating the
            # result must not be able to reach the shared record through it
            result["default"] = (
                list(self.default) if isinstance(self.default, tuple) else self.default
            )
        if self.emits is not None:
            result["emits"] = self.emits
        if self.note:
            result["note"] = self.note
        return result


@dataclass(frozen=True)
class ExecutionControl:
    """One constraint the adapter emits into the request.

    ``emits`` is the load-bearing field and the falsifiable one: it names the
    exact argv element, environment key path, or request field produced. A seam
    test asserts that string appears. ``effect`` says what the emission ASKS
    for; it is not a claim that the target complies.

    ``subjects`` carries exact native identifiers where the mechanism names them
    (``permission.task``); it is empty where the control has no subject list and
    the emission itself is the whole control (a sandbox mode selector).
    """

    id: str
    emits: str
    effect: str
    subjects: Tuple[str, ...] = ()
    source: str = FIXED
    parameter: Optional[str] = None
    when_value: Optional[str] = None
    platform: str = ALWAYS
    note: str = ""

    def to_json(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "id": self.id,
            "emits": self.emits,
            "effect": self.effect,
            "source": self.source,
        }
        if self.subjects:
            result["subjects"] = list(self.subjects)
        if self.parameter is not None:
            result["parameter"] = self.parameter
        if self.when_value is not None:
            result["when_value"] = self.when_value
        if self.platform != ALWAYS:
            result["platform"] = self.platform
        if self.note:
            result["note"] = self.note
        return result


@dataclass(frozen=True)
class StructuredOutputCapability:
    """Whether a caller schema reaches the target, and how the result comes back."""

    mode: str = NONE
    request_param: Optional[str] = None
    result: str = TEXT_RESULT
    note: str = ""

    def to_json(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"mode": self.mode, "result": self.result}
        if self.request_param is not None:
            result["request_param"] = self.request_param
        if self.note:
            result["note"] = self.note
        return result


@dataclass(frozen=True)
class SystemPromptCapability:
    """How system text reaches the model. Only ``append`` claims an append.

    ``mode`` is the mode a caller who selects nothing gets, and ``emits`` is
    what THAT mode produces. Where the caller can choose, ``modes`` lists every
    mode the adapter can emit (including the default), ``parameter`` names the
    :class:`~.types.BackendOptions` field that selects one, and
    ``emits_by_mode`` gives the concrete emission per mode -- one falsifiable
    string each, exactly as :attr:`ExecutionControl.emits` is.

    An empty ``modes`` means the adapter emits ``mode`` and nothing else; it is
    not a claim that the target supports no other mode, only that this adapter
    reaches none. Same rule as everywhere here: a mode is advertised where the
    adapter EMITS it, never where a CLI merely documents a flag.
    """

    mode: str
    separator: Optional[str] = None
    emits: Optional[str] = None
    modes: Tuple[str, ...] = ()
    parameter: Optional[str] = None
    emits_by_mode: Mapping[str, str] = field(default_factory=dict)
    note: str = ""

    def to_json(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"mode": self.mode}
        if self.separator is not None:
            result["separator"] = self.separator
        if self.emits is not None:
            result["emits"] = self.emits
        if self.modes:
            result["modes"] = list(self.modes)
        if self.parameter is not None:
            result["parameter"] = self.parameter
        if self.emits_by_mode:
            result["emits_by_mode"] = dict(self.emits_by_mode)
        if self.note:
            result["note"] = self.note
        return result


@dataclass(frozen=True)
class Capabilities:
    """What one adapter family can honor, as the adapter itself declares it.

    ``adapter`` is the backend's own ``name`` -- the label the factory actually
    produces -- never a family name invented for the advertisement.

    ``dropped_params`` names every :class:`BackendOptions` field this adapter
    does not read. It is the advertised half of the truthfulness guarantee: a
    param listed here is applied never and reported always.

    ``guarantees`` names the canonical subjects this adapter denies STRUCTURALLY
    -- because it has no such capability to begin with, so no flag, sandbox or
    permission entry is involved and there is nothing that could be configured
    back on. A transport that exposes no tools guarantees ``FILESYSTEM_WRITE``
    that way. A subject delivered by an emitted flag belongs in
    ``execution_controls`` instead, where ``emits`` keeps it falsifiable.
    """

    adapter: str
    params: Mapping[str, ParamCapability] = field(default_factory=dict)
    dropped_params: Tuple[str, ...] = ()
    execution_controls: Tuple[ExecutionControl, ...] = ()
    guarantees: Tuple[str, ...] = ()
    structured_output: StructuredOutputCapability = field(
        default_factory=StructuredOutputCapability
    )
    system_prompt: SystemPromptCapability = field(
        default_factory=lambda: SystemPromptCapability(mode=NONE)
    )

    def honors(self, param: str) -> bool:
        """True when this adapter reads ``param`` at all."""
        return param in self.params

    def denies(self, subject: str) -> bool:
        """True when this adapter can guarantee ``subject`` is unavailable.

        Satisfied either structurally (``guarantees``) or by an execution
        control that denies or confines the subject. A control whose ``source``
        is ``REQUEST`` still counts: the advertisement says the mechanism
        EXISTS, and the caller requiring the subject is the one that must pass
        the parameter which arms it.
        """
        if subject in self.guarantees:
            return True
        return any(
            subject in control.subjects and control.effect in (DENY, CONFINE)
            for control in self.execution_controls
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "adapter": self.adapter,
            "params": {k: v.to_json() for k, v in self.params.items()},
            "dropped_params": list(self.dropped_params),
            "execution_controls": [c.to_json() for c in self.execution_controls],
            "guarantees": list(self.guarantees),
            "structured_output": self.structured_output.to_json(),
            "system_prompt": self.system_prompt.to_json(),
        }


__all__ = [
    "Capabilities",
    "ParamCapability",
    "ExecutionControl",
    "StructuredOutputCapability",
    "SystemPromptCapability",
    "MAPPED",
    "PASSTHROUGH",
    "ALLOW",
    "DENY",
    "CONFINE",
    "DISABLE",
    "BYPASS",
    "FILESYSTEM_WRITE",
    "SHELL_EXEC",
    "SUBAGENT_SPAWN",
    "subjects_for_disallowed_tools",
    "FIXED",
    "REQUEST",
    "ALWAYS",
    "WINDOWS",
    "NATIVE",
    "NONE",
    "TEXT_RESULT",
    "PARSED_RESULT",
    "NATIVE_ROLE",
    "REPLACE",
    "APPEND",
    "PROMPT_FOLD",
]
