"""The one API over a model declaration: ``describe`` and ``run``.

A model declaration is a list of registry ids naming which model(s) may do a
unit of work (the format is specified in bootstrap's plugin-dev skill,
``references/model-declaration.md``, and validated structurally by
``bootstrap_lib.model_declaration``). This module answers the runtime
question the validator deliberately does not: on THIS machine, for THIS
caller, which declared entries can run, in what order, and what happens when
none can.

Three surfaces, kept apart:

- **RENDER** -- :func:`describe` returns the rendered subset: usable entries,
  out-of-quota entries (with their reset time) and unreachable entries, which
  are all real on this machine. Unresolved, unroutable-here,
  requirements-mismatch, excluded and shadowed-core entries are HIDDEN.
- **SKIP** -- selection passes over every non-usable entry silently: no
  notice, no warning, no log line.
- **FLOOR** -- when no usable rendered entry remains, :func:`describe` raises
  :class:`NoUsableRoutingTarget`, which itemises EVERY declared entry and its
  disposition in declaration order. That is where a typo surfaces.

Ordering is one rule for every caller (:func:`order_by_pace`): entries with a
pace reading are re-sorted by pace, highest first, among their own working
positions; entries without one keep their places; ties keep declaration
order. The first usable entry of the ordered list is the default. An
unattended caller takes it; an in-session caller may choose any usable entry
and announces the choice.

``pace = remaining / window_remaining``: 100% is exactly on pace. The number
comes from an UNPINNED read at render time, while the status, ``usable`` and
the default come from the session-pinned verdict
(:func:`~.usage_budget.pinned_evaluate`).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence

from .completion.halt import HALT_INSUFFICIENT_CREDIT, HALT_QUOTA
from .completion.types import BackendOptions
from .model_endpoints import HARNESS_KIND, TRANSPORT_KIND, EndpointRegistryError
from .models import EndpointResolveError
from .reachability import (
    DEFAULT_VERIFY_TIMEOUT_S,
    STATUS_UNKNOWN,
    STATUS_UNREACHABLE,
    Reachability,
    check_many,
)
from .usage_budget import (
    STATUS_AVAILABLE,
    STATUS_NO_DATA,
    STATUS_OUT_OF_QUOTA,
    STATUS_UNDER_QUOTA,
    POOL_SEVEN_DAY,
    Budget,
    ConserveSpec,
    evaluate,
    pinned_evaluate,
    record_observed_halt,
)

CALLER_SESSION = "session"
CALLER_PROCESS = "process"
_CALLERS = (CALLER_SESSION, CALLER_PROCESS)

DISPOSITION_USABLE = "usable"
DISPOSITION_OUT_OF_QUOTA = "out-of-quota"
DISPOSITION_UNREACHABLE = "unreachable"
DISPOSITION_UNRESOLVED = "unresolved"
DISPOSITION_UNROUTABLE = "unroutable"
DISPOSITION_REQUIREMENTS_MISMATCH = "requirements-mismatch"
DISPOSITION_EXCLUDED = "excluded"
DISPOSITION_SHADOWED_CORE = "shadowed-core"

#: Dispositions that stay in the render. Everything else is hidden.
_RENDERED = (DISPOSITION_USABLE, DISPOSITION_OUT_OF_QUOTA, DISPOSITION_UNREACHABLE)

#: Harnesses a process caller drives through the completion seam, and the
#: adapter family name each one's backend carries.
_HARNESS_ADAPTERS = {"claude": "claude-cli", "codex": "codex-cli", "opencode": "opencode-cli"}

#: How an in-session caller drives each harness (D1's caller table).
_SESSION_DRIVE = {"claude": "agent", "codex": "codex exec", "opencode": "opencode run"}

#: Below this fraction of the window left, a pace is not meaningful.
_ABOUT_TO_RESET = 0.01

#: The bootstrap release that shipped ``bootstrap_lib.model_declaration``.
_MODEL_DECLARATION_BOOTSTRAP = "0.129.0"

RULE_CHOICE_SESSION = (
    "Rule: any usable entry may be chosen; the default applies only when you "
    "have no preference. Announce every choice from a multi-entry declaration: "
    '"route: <unit> -> <entry>; <reason>", reason one of default, higher pace, '
    "independence, <prior entry> failed: <kind>, or your own clause."
)
RULE_CHOICE_PROCESS = (
    "Rule: take the first usable entry of the ordered list (the default); a run "
    "is explainable from the declared list plus the logged pace readings."
)
RULE_TRIGGER_SESSION = (
    "Re-select: any dispatch failure the launch-correction rule does not "
    "explain -- a non-zero exit, an Agent-tool error, a launch that produced no "
    "output -- moves to another usable entry; announce it with the failure kind "
    '(a classified marker, else "unexplained exit"). A schema-invalid or wrong '
    "result from a run that exited 0 is a task failure, not a trigger. Before "
    "re-selecting a unit that may have written, reset its workspace to its "
    "launch state or re-run it in a fresh worktree; read-only review lanes skip "
    "this step."
)
RULE_TRIGGER_PROCESS = (
    "Re-select: only a classified halt (quota, rate limit, auth, insufficient "
    "credit, launch/transport) moves to the next usable entry; a quota or "
    "credit halt records the entry out of quota until its reset. A task error, "
    "schema-invalid output, or an unclassified timeout stays a failed attempt."
)
RULE_INDEPENDENCE = (
    "Independence: prefer a non-author entry; the author may review when no "
    "other usable entry exists, and says so."
)


class DeclarationSupportError(ImportError):
    """``bootstrap_lib.model_declaration`` is absent or too old here.

    llm-scripting-kit links ``bootstrap_lib`` (its ``shared_lib_imports``) and
    bootstrap is a declared dependency of every plugin, so the posture is
    REQUIRED: without the structural validator there is no declaration to
    describe. The two states are diagnosed apart because they have different
    remedies.
    """


def _model_declaration() -> Any:
    """Return ``bootstrap_lib.model_declaration``, probed for the symbols used."""
    try:
        import bootstrap_lib  # noqa: F401, PLC0415
    except ImportError as exc:
        raise DeclarationSupportError(
            "bootstrap_lib is not linked into this environment; declare "
            "'bootstrap_lib' in the consuming plugin's bootstrap.json "
            "shared_lib_imports (llm-scripting-kit's describe/run need "
            f"bootstrap >= {_MODEL_DECLARATION_BOOTSTRAP})"
        ) from exc
    try:
        from bootstrap_lib import model_declaration  # noqa: PLC0415
    except ImportError:
        model_declaration = None
    if (
        model_declaration is None
        or not callable(getattr(model_declaration, "parse", None))
        or not isinstance(getattr(model_declaration, "CORE_IDS", None), frozenset)
    ):
        raise DeclarationSupportError(
            "the linked bootstrap_lib predates model_declaration.parse/CORE_IDS; "
            f"llm-scripting-kit's describe/run need bootstrap >= "
            f"{_MODEL_DECLARATION_BOOTSTRAP} -- update the bootstrap plugin"
        )
    return model_declaration


def check_registry_entry(entry_id: str, merged: Any) -> Optional[str]:
    """Classify a reserved core id whose MERGED entry is not a Claude harness.

    ``fable``, ``opus``, ``sonnet`` and ``haiku`` are reserved for
    ``harness: claude``. A config layer or registry entry of the same id
    shadows the shipped one, so a merged entry carrying another harness or a
    ``base_url`` is shadowed and cannot route as the core id. Returns the
    reason, or None when the entry is fine, absent, or not a core id.

    It classifies; it never raises. The finding reaches the user through the
    floor diagnostic only. The check reads the MERGED entry because a partial
    layer is legitimate: an owner entry carrying only ``conserve_usage``
    merges over the shipped harness entry and is still a Claude harness.
    """
    if merged is None or entry_id not in _model_declaration().CORE_IDS:
        return None
    base_url = getattr(merged, "base_url", None)
    harness = (getattr(merged, "harness", None) or "").strip().lower()
    if base_url:
        return f"reserved core id shadowed by a transport entry (base_url {base_url})"
    if harness != "claude":
        return f"reserved core id shadowed by harness {harness or '<none>'!r} (must be claude)"
    return None


def order_by_pace(
    items: Sequence[Any], *, pace: Optional[Callable[[Any], Optional[float]]] = None
) -> List[Any]:
    """Apply D5's one ordering rule. Pure; returns a new list.

    Items with a pace are re-sorted by pace, highest first, among the
    positions paced items occupy; items without a pace keep their positions;
    ties keep the input (declaration) order.
    """
    read = pace or (lambda item: getattr(item, "pace", None))
    ordered = list(items)
    paced_slots = [index for index, item in enumerate(ordered) if read(item) is not None]
    paced = sorted(
        (ordered[index] for index in paced_slots),
        key=lambda item: -float(read(item)),  # sorted() is stable: ties keep order
    )
    for slot, item in zip(paced_slots, paced):
        ordered[slot] = item
    return ordered


def _format_reset(resets_at: Optional[int]) -> str:
    if resets_at is None:
        return "an unknown time"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(resets_at))


@dataclass(frozen=True)
class Disposition:
    """What happened to one declared entry. Read only by floor handling."""

    id: str
    declared_index: int
    disposition: str
    detail: str = ""
    resets_at: Optional[int] = None

    def describe_line(self) -> str:
        text = self.disposition
        if self.disposition == DISPOSITION_OUT_OF_QUOTA:
            text += f" until {_format_reset(self.resets_at)}"
        return f"{self.id}: {text}" + (f" ({self.detail})" if self.detail else "")

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "declared_index": self.declared_index,
            "disposition": self.disposition,
            "detail": self.detail,
            "resets_at": self.resets_at,
        }


@dataclass(frozen=True)
class EntryState:
    """One rendered entry. ``usable`` gates selection; the rest is information."""

    id: str
    declared_index: int
    resolved: bool
    kind: str
    harness: Optional[str]
    model: Optional[str]
    family: Optional[str]
    tier: Optional[int]
    drive: str
    reachability: str
    usability: Optional[str]
    pace: Optional[float]
    shares_quota_with: tuple = ()
    usable: bool = False
    default: bool = False
    is_self: bool = False
    pace_note: str = ""
    remaining: Optional[float] = None
    window_remaining: Optional[float] = None
    resets_at: Optional[int] = None
    pool: Optional[str] = None
    reachability_detail: str = ""

    @property
    def status_text(self) -> str:
        if self.usability == STATUS_OUT_OF_QUOTA:
            return f"out of quota until {_format_reset(self.resets_at)}"
        if self.reachability == STATUS_UNREACHABLE:
            return f"unreachable ({self.reachability_detail})" if self.reachability_detail else "unreachable"
        if self.usability is None:
            return "n/a (unpaced)"
        label = {
            STATUS_AVAILABLE: "available",
            STATUS_UNDER_QUOTA: "under quota",
            STATUS_NO_DATA: "n/a",
        }.get(self.usability, self.usability)
        if self.pace is not None and self.remaining is not None and self.window_remaining is not None:
            return (
                f"{label}  {self.remaining * 100:.0f}% left, "
                f"{self.window_remaining * 100:.0f}% of window   pace {self.pace * 100:.0f}%"
            )
        if self.pace_note:
            return f"{label}  {self.pace_note}"
        return label

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "declared_index": self.declared_index,
            "resolved": self.resolved,
            "kind": self.kind,
            "harness": self.harness,
            "model": self.model,
            "family": self.family,
            "tier": self.tier,
            "drive": self.drive,
            "reachability": self.reachability,
            "usability": self.usability,
            "pace": self.pace,
            "pace_note": self.pace_note,
            "resets_at": self.resets_at,
            "shares_quota_with": list(self.shares_quota_with),
            "usable": self.usable,
            "default": self.default,
            "is_self": self.is_self,
        }


class NoUsableRoutingTarget(Exception):
    """The floor: no usable rendered entry remains.

    Carries EVERY declared entry and its disposition in declaration order,
    including the hidden ones. It is the only selection error; every caller
    propagates it.
    """

    def __init__(self, names: Sequence[str], dispositions: Sequence[Disposition], caller: str) -> None:
        self.names = tuple(names)
        self.dispositions = tuple(dispositions)
        self.caller = caller
        lines = "\n".join("  " + d.describe_line() for d in self.dispositions)
        super().__init__(
            f"no usable routing target in [{', '.join(self.names)}] "
            f"for a {caller} caller:\n{lines}"
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "kind": "no-usable-routing-target",
            "message": str(self),
            "caller": self.caller,
            "dispositions": [d.to_json() for d in self.dispositions],
        }


@dataclass(frozen=True)
class Ranking:
    """The result of :func:`describe`."""

    names: tuple
    caller: str
    rendered_entries: tuple
    dispositions: tuple
    rule: str

    @property
    def default(self) -> Optional[EntryState]:
        return next((entry for entry in self.rendered_entries if entry.default), None)

    def render(self) -> str:
        width = max(len(entry.id) for entry in self.rendered_entries)
        # The header names no ids: a hidden entry must not appear anywhere in
        # the render (direction 17); the floor is where hidden ids surface.
        lines = ["Declared models (ordered by pace): choose one; default is marked."]
        for entry in self.rendered_entries:
            if entry.kind == TRANSPORT_KIND:
                how = f"transport/{entry.drive}"
            elif (entry.harness or "") == "claude":
                how = f"claude/{entry.drive}"
            else:
                how = entry.harness or entry.kind
            marks = " ".join(
                mark for mark, on in (("[default]", entry.default), ("[author]", entry.is_self)) if on
            )
            line = f"  {entry.id:<{width}}  {how:<14} {entry.status_text}"
            if marks:
                line += f"   {marks}"
            if entry.shares_quota_with:
                line += f"  shares {entry.pool} with {', '.join(entry.shares_quota_with)}"
            lines.append(line)
        lines.append(self.rule)
        return "\n".join(lines)

    def to_json(self) -> Dict[str, Any]:
        default = self.default
        return {
            "names": list(self.names),
            "caller": self.caller,
            "default": default.id if default is not None else None,
            "rendered_entries": [entry.to_json() for entry in self.rendered_entries],
            "dispositions": [d.to_json() for d in self.dispositions],
            "rule": self.rule,
        }


def _fresh_reading(entry_id: str, spec: ConserveSpec, harness: Optional[str]) -> Budget:
    """Unpinned read for the pace NUMBER. ``entry_id`` names it for tests."""
    return evaluate(spec, harness)


def _pace(fresh: Optional[Budget]) -> "tuple[Optional[float], str]":
    """D5's "has a pace" table, for an entry whose pinned verdict is usable."""
    if fresh is None:
        return None, "n/a (unpaced)"
    if fresh.remaining is None or fresh.window_remaining is None:
        return None, "n/a (no reading)"
    if fresh.window_remaining < _ABOUT_TO_RESET:
        return None, "about to reset"
    if fresh.remaining <= 0.0:
        return 0.0, ""
    return fresh.remaining / fresh.window_remaining, ""


def _call_factory(factory: Callable[..., Any], name: str, project_root: Optional[str]) -> Any:
    if project_root is None:
        return factory(name)
    return factory(name, project_root=project_root)


def _adapter_name(kind: str, harness: Optional[str]) -> Optional[str]:
    if kind == TRANSPORT_KIND:
        return "openrouter"
    return _HARNESS_ADAPTERS.get((harness or "").lower())


def _drive(caller: str, kind: str, harness: Optional[str], backend_name: Optional[str]) -> str:
    if caller == CALLER_SESSION:
        return _SESSION_DRIVE.get((harness or "").lower(), "unroutable")
    return backend_name or _adapter_name(kind, harness) or "unroutable"


def _rule(caller: str, has_author: bool) -> str:
    if caller == CALLER_SESSION:
        parts = [RULE_CHOICE_SESSION, RULE_TRIGGER_SESSION]
    else:
        parts = [RULE_CHOICE_PROCESS, RULE_TRIGGER_PROCESS]
    if has_author:
        parts.append(RULE_INDEPENDENCE)
    return "\n".join(parts)


def describe(
    names: Any,
    *,
    project_root: Optional[str | Path] = None,
    caller: str,
    self_ref: Optional[str] = None,
    requirements: Any = None,
    capabilities: Optional[Mapping[str, Any]] = None,
    backend_factory: Optional[Callable[..., Any]] = None,
    exclude: Iterable[str] = (),
    reachability_cache: Optional[MutableMapping[str, Reachability]] = None,
    entries: Optional[Mapping[str, Any]] = None,
) -> Ranking:
    """Classify, filter, pace-order and default a declaration.

    ``caller`` is ``"session"`` (an agent that drives the harness itself:
    Agent tool, rendered ``codex exec``/``opencode run``; a transport entry
    has no agent loop and is unroutable here) or ``"process"`` (the completion
    seam: every resolvable entry routes). ``requirements`` is matched with
    ``completion.match_capabilities`` against ``capabilities`` (default: the
    shipped advertisement) keyed by the RESOLVED backend's name, the same
    lookup job-kit's execution makes. ``backend_factory`` resolves an id
    (a raised ``EndpointResolveError`` means it does not); without one an id
    resolves through the merged entry map, falling back to ``create_backend``
    for ids the map does not carry (the legacy ``openrouter`` wrapper).
    ``reachability_cache`` is read first and receives every probe made, so a
    caller-scoped mapping probes each entry once; without it every call
    probes live. ``entries`` injects the merged entry map.

    Raises :class:`NoUsableRoutingTarget` when nothing usable remains, and
    ``bootstrap_lib.model_declaration.DeclarationError`` for a structurally
    invalid declaration (empty, duplicate, non-string). Skipping is silent.
    """
    if caller not in _CALLERS:
        raise ValueError(f"caller must be one of {_CALLERS}, got {caller!r}")
    declared = _model_declaration().parse(names)
    root = str(project_root) if project_root is not None else None
    injected = entries is not None
    if entries is None:
        from .models import discover_model_entries  # noqa: PLC0415 -- import cycle

        entries = discover_model_entries(project_root=root)
    excluded = set(exclude)
    matcher = None
    if requirements:
        from .completion import adapter_capabilities, match_capabilities  # noqa: PLC0415

        advertised = dict(capabilities if capabilities is not None else adapter_capabilities())
        matcher = match_capabilities

    dispositions: Dict[int, Disposition] = {}
    candidates: List[Dict[str, Any]] = []
    for index, name in enumerate(declared):
        def settle(disposition: str, detail: str = "", resets_at: Optional[int] = None) -> None:
            dispositions[index] = Disposition(name, index, disposition, detail, resets_at)

        if name in excluded:
            settle(DISPOSITION_EXCLUDED, "ruled out by the caller")
            continue
        merged = entries.get(name)
        shadow = check_registry_entry(name, merged)
        if shadow is not None:
            settle(DISPOSITION_SHADOWED_CORE, shadow)
            continue
        selection = None
        if backend_factory is not None:
            try:
                selection = _call_factory(backend_factory, name, root)
            except (EndpointResolveError, EndpointRegistryError) as exc:
                settle(DISPOSITION_UNROUTABLE if merged is not None else DISPOSITION_UNRESOLVED, str(exc))
                continue
        elif merged is None:
            if injected:
                settle(DISPOSITION_UNRESOLVED, "no registry entry")
                continue
            from .completion.factory import create_backend  # noqa: PLC0415

            try:
                selection = _call_factory(create_backend, name, root)
            except (EndpointResolveError, EndpointRegistryError) as exc:
                settle(DISPOSITION_UNRESOLVED, str(exc))
                continue
        kind = getattr(merged, "kind", None) or getattr(selection, "kind", None) or TRANSPORT_KIND
        harness = getattr(merged, "harness", None)
        backend_name = getattr(getattr(selection, "backend", None), "name", None)
        if caller == CALLER_SESSION and (
            kind != HARNESS_KIND or (harness or "").lower() not in _SESSION_DRIVE
        ):
            settle(
                DISPOSITION_UNROUTABLE,
                "a transport entry has no agent loop" if kind != HARNESS_KIND
                else f"harness {harness!r} has no in-session drive",
            )
            continue
        if caller == CALLER_PROCESS and kind == HARNESS_KIND and selection is None and (
            (harness or "").lower() not in _HARNESS_ADAPTERS
        ):
            settle(DISPOSITION_UNROUTABLE, f"harness {harness!r} has no completion backend")
            continue
        if matcher is not None:
            adapter = backend_name if isinstance(backend_name, str) else _adapter_name(kind, harness)
            record = advertised.get(adapter) if adapter else None
            if record is None or not matcher(record, requirements):
                settle(DISPOSITION_REQUIREMENTS_MISMATCH, f"adapter {adapter!r}")
                continue
        spec = getattr(merged, "conserve_usage", None)
        pinned = pinned_evaluate(name, spec, harness) if spec is not None else None
        candidate = {
            "index": index, "name": name, "merged": merged, "kind": kind,
            "harness": harness, "backend_name": backend_name, "spec": spec,
            "pinned": pinned, "model": getattr(merged, "model", None) or getattr(selection, "model", None),
        }
        if pinned is not None and not pinned.usable:
            settle(DISPOSITION_OUT_OF_QUOTA, pinned.detail, pinned.resets_at)
        candidates.append(candidate)

    # Probe only what can still be dispatched: hidden and out-of-quota
    # entries gain nothing from a reachability verdict.
    to_probe = {
        c["name"]: {"kind": c["kind"], "harness": c["harness"]}
        for c in candidates
        if c["index"] not in dispositions
        and (reachability_cache is None or c["name"] not in reachability_cache)
    }
    probed = check_many(to_probe, timeout=DEFAULT_VERIFY_TIMEOUT_S, project_root=root) if to_probe else {}
    if reachability_cache is not None:
        reachability_cache.update(probed)
    known: Mapping[str, Reachability] = {**(reachability_cache or {}), **probed}

    self_model = None
    if self_ref is not None:
        self_entry = entries.get(self_ref)
        self_model = getattr(self_entry, "model", None) or self_ref

    states: List[EntryState] = []
    for c in candidates:
        index, name, merged = c["index"], c["name"], c["merged"]
        pinned = c["pinned"]
        out_of_quota = index in dispositions
        reach = None if out_of_quota else known.get(name)
        reach_status = reach.status if reach is not None else STATUS_UNKNOWN
        if not out_of_quota:
            if reach_status == STATUS_UNREACHABLE:
                dispositions[index] = Disposition(name, index, DISPOSITION_UNREACHABLE, reach.detail)
            else:
                dispositions[index] = Disposition(name, index, DISPOSITION_USABLE)
        fresh = None
        if c["spec"] is not None and not out_of_quota:
            fresh = _fresh_reading(name, c["spec"], c["harness"])
        pace, note = (None, "") if out_of_quota else _pace(fresh)
        states.append(EntryState(
            id=name,
            declared_index=index,
            resolved=True,
            kind=c["kind"],
            harness=c["harness"],
            model=c["model"],
            family=getattr(merged, "family", None),
            tier=getattr(merged, "tier", None),
            drive=_drive(caller, c["kind"], c["harness"], c["backend_name"]),
            reachability=reach_status,
            usability=pinned.status if pinned is not None else None,
            pace=pace,
            usable=dispositions[index].disposition == DISPOSITION_USABLE,
            is_self=self_ref is not None and (name == self_ref or c["model"] == self_model),
            pace_note=note,
            remaining=fresh.remaining if fresh is not None else None,
            window_remaining=fresh.window_remaining if fresh is not None else None,
            resets_at=pinned.resets_at if pinned is not None else None,
            pool=c["spec"].pool if c["spec"] is not None else None,
            reachability_detail=reach.detail if reach is not None else "",
        ))

    ordered_dispositions = tuple(dispositions[i] for i in range(len(declared)))
    ordered = order_by_pace(states)
    if not any(state.usable for state in ordered):
        raise NoUsableRoutingTarget(declared, ordered_dispositions, caller)

    # Shared quota is information only (D3): same harness, same declared pool.
    def pool_key(state: EntryState, spec: Any) -> Any:
        return (state.harness, spec.pool, spec.display_name) if spec is not None else None

    specs = {c["name"]: c["spec"] for c in candidates}
    first_usable = next(state.id for state in ordered if state.usable)
    finished = []
    for state in ordered:
        key = pool_key(state, specs[state.id])
        shares = tuple(
            other.id for other in ordered
            if other.id != state.id and key is not None and pool_key(other, specs[other.id]) == key
        )
        finished.append(replace(state, shares_quota_with=shares, default=state.id == first_usable))

    return Ranking(
        names=tuple(declared),
        caller=caller,
        rendered_entries=tuple(finished),
        dispositions=ordered_dispositions,
        rule=_rule(caller, any(state.is_self for state in finished)),
    )


# ---------------------------------------------------------------------------
# run(): unattended dispatch for callers with no loop of their own
# ---------------------------------------------------------------------------

RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_ATTEMPT_LIMIT = "attempt-limit"

#: Halts that move an unattended run to the next entry (D6, process set).
_QUOTA_HALTS = (HALT_QUOTA, HALT_INSUFFICIENT_CREDIT)

#: Launch/transport failures carrying no halt marker that still move on: the
#: entry is marked unreachable for the rest of the run.
_LAUNCH_ERRORS = (FileNotFoundError, ConnectionError)


@dataclass(frozen=True)
class RunRequest:
    """One unit of work. ``workspace`` names a git work tree the unit may write."""

    system: str
    prompt: str
    options: Optional[BackendOptions] = None
    workspace: Optional[Path] = None


@dataclass(frozen=True)
class Attempt:
    """One execution, reported through ``on_attempt`` with its pace reading."""

    entry: str
    number: int
    pace: Optional[float]
    halt: Optional[str] = None
    error: Optional[str] = None
    outcome: str = ""
    workspace_action: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "entry": self.entry, "number": self.number, "pace": self.pace,
            "halt": self.halt, "error": self.error, "outcome": self.outcome,
            "workspace_action": self.workspace_action,
        }


@dataclass(frozen=True)
class RunResult:
    """How an unattended run ended. The floor is raised, never returned."""

    status: str
    entry: Optional[str] = None
    response: Any = None
    attempts: tuple = ()
    detail: str = ""


class _WorkspaceSnapshot:
    """The launch state of a git work tree, restorable without touching history.

    The working tree (tracked plus untracked, non-ignored files) is captured
    as a tree object through a TEMPORARY index, so the user's own index is
    never rewritten to take the snapshot. Restoring puts every snapshot file
    back, removes non-ignored files the unit created, restores the real
    index, and moves HEAD back (``reset --soft``, so a unit's commit stays in
    the reflog) when the unit committed.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.head = self._git("rev-parse", "--verify", "-q", "HEAD", check=False).strip() or None
        self.index_tree = self._git("write-tree").strip()
        self.tree = self._tree_of_worktree()

    def _git(self, *args: str, env: Optional[Mapping[str, str]] = None, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, env={**os.environ, **(env or {})},
        )
        if check and proc.returncode != 0:
            raise OSError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def _tree_of_worktree(self) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self._git("add", "-A", ".", env=env)
            return self._git("write-tree", env=env).strip()

    def restore(self) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            now_env = {"GIT_INDEX_FILE": str(Path(tmp) / "now")}
            self._git("add", "-A", ".", env=now_env)
            present = set(self._git("ls-files", "-z", env=now_env).split("\0")) - {""}
            wanted = set(self._git("ls-tree", "-r", "-z", "--name-only", self.tree).split("\0")) - {""}
            for rel in sorted(present - wanted):
                try:
                    (self.root / rel).unlink()
                except OSError:
                    pass
            snap_env = {"GIT_INDEX_FILE": str(Path(tmp) / "snap")}
            self._git("read-tree", self.tree, env=snap_env)
            self._git("checkout-index", "-a", "-f", env=snap_env)
        moved = ""
        current = self._git("rev-parse", "--verify", "-q", "HEAD", check=False).strip() or None
        if self.head is not None and current != self.head:
            self._git("reset", "-q", "--soft", self.head)
            moved = f"; HEAD moved back to {self.head[:12]}"
        self._git("read-tree", self.index_tree)
        return f"workspace reset to its launch state (tree {self.tree[:12]}){moved}"


def run(
    names: Any,
    request: RunRequest,
    *,
    project_root: Optional[str | Path] = None,
    requirements: Any = None,
    exclude: Iterable[str] = (),
    max_attempts: int = 1,
    on_attempt: Optional[Callable[[Attempt], None]] = None,
    capabilities: Optional[Mapping[str, Any]] = None,
    backend_factory: Optional[Callable[..., Any]] = None,
    reachability_cache: Optional[MutableMapping[str, Reachability]] = None,
    entries: Optional[Mapping[str, Any]] = None,
) -> RunResult:
    """Dispatch a unit to the first usable entry, moving on only on a classified halt.

    For callers with NO loop of their own. Each execution is reported through
    ``on_attempt`` and counted against ``max_attempts``, which limits
    executions only: reaching it is ``RUN_ATTEMPT_LIMIT``, never the floor.
    A quota or credit halt writes the entry's verdict back
    (:func:`~.usage_budget.record_observed_halt`, when it declares
    ``conserve_usage``); every classified halt and every launch failure
    excludes the entry for the rest of the run, and :func:`describe` is called
    again. A task error stays a failed attempt. When ``request.workspace``
    names a git work tree, it is reset to its launch state before any
    re-selection, and a workspace that cannot be reset ends the run rather
    than layering a second model on the first one's partial edits.
    :class:`NoUsableRoutingTarget` propagates.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    root = str(project_root) if project_root is not None else None
    if backend_factory is None:
        from .completion.factory import create_backend as backend_factory  # noqa: PLC0415
    cache: MutableMapping[str, Reachability] = {} if reachability_cache is None else reachability_cache
    excluded = set(exclude)
    attempts: List[Attempt] = []
    snapshot: Optional[_WorkspaceSnapshot] = None
    snapshot_error = ""
    if request.workspace is not None:
        try:
            snapshot = _WorkspaceSnapshot(Path(request.workspace))
        except (OSError, subprocess.SubprocessError) as exc:
            snapshot_error = str(exc)

    def report(attempt: Attempt) -> None:
        attempts.append(attempt)
        if on_attempt is not None:
            on_attempt(attempt)

    while True:
        ranking = describe(
            names, project_root=root, caller=CALLER_PROCESS, requirements=requirements,
            capabilities=capabilities, backend_factory=backend_factory, exclude=excluded,
            reachability_cache=cache, entries=entries,
        )
        chosen = ranking.default
        assert chosen is not None  # describe() raises the floor otherwise
        number = len(attempts) + 1
        selection = _call_factory(backend_factory, chosen.id, root)
        options = request.options or BackendOptions()
        if options.effort is None and getattr(selection, "effort", None):
            options = replace(options, effort=selection.effort)
        try:
            response = selection.backend.complete(
                request.system, request.prompt, model=selection.model, options=options
            )
        except Exception as exc:  # noqa: BLE001 -- transports raise heterogeneous types
            halt = selection.backend.classify_halt(exc)
            launch = halt is None and isinstance(exc, _LAUNCH_ERRORS)
            if halt is None and not launch:
                report(Attempt(chosen.id, number, chosen.pace, error=str(exc), outcome="failed"))
                return RunResult(RUN_FAILED, chosen.id, None, tuple(attempts), f"task error: {exc}")
            if halt in _QUOTA_HALTS:
                spec = getattr((entries or {}).get(chosen.id), "conserve_usage", None)
                if spec is None and entries is None:
                    from .models import discover_model_entries  # noqa: PLC0415

                    found = discover_model_entries(project_root=root).get(chosen.id)
                    spec = getattr(found, "conserve_usage", None)
                if spec is not None:
                    record_observed_halt(chosen.id, spec, resets_at=getattr(exc, "resets_at", None))
            if launch:
                cache[chosen.id] = Reachability(
                    status=STATUS_UNREACHABLE, checked="dispatch",
                    detail=f"launch failed: {type(exc).__name__}: {exc}",
                )
            excluded.add(chosen.id)
            action = ""
            if request.workspace is not None:
                try:
                    if snapshot is None:
                        raise OSError(snapshot_error or "no launch snapshot")
                    action = snapshot.restore()
                except (OSError, subprocess.SubprocessError) as reset_exc:
                    action = f"workspace could not be reset: {reset_exc}"
                    report(Attempt(chosen.id, number, chosen.pace, halt=halt or "launch",
                                   error=str(exc), outcome="halted", workspace_action=action))
                    return RunResult(
                        RUN_FAILED, chosen.id, None, tuple(attempts),
                        f"{chosen.id} halted and the workspace could not be reset to its "
                        f"launch state, so no other entry was dispatched: {reset_exc}",
                    )
            report(Attempt(chosen.id, number, chosen.pace, halt=halt or "launch",
                           error=str(exc), outcome="halted", workspace_action=action))
            if len(attempts) >= max_attempts:
                return RunResult(
                    RUN_ATTEMPT_LIMIT, chosen.id, None, tuple(attempts),
                    f"attempt limit reached ({max_attempts})",
                )
            continue
        report(Attempt(chosen.id, number, chosen.pace, outcome="completed"))
        return RunResult(RUN_COMPLETED, chosen.id, response, tuple(attempts))


__all__ = [
    "CALLER_PROCESS",
    "CALLER_SESSION",
    "DISPOSITION_EXCLUDED",
    "DISPOSITION_OUT_OF_QUOTA",
    "DISPOSITION_REQUIREMENTS_MISMATCH",
    "DISPOSITION_SHADOWED_CORE",
    "DISPOSITION_UNREACHABLE",
    "DISPOSITION_UNRESOLVED",
    "DISPOSITION_UNROUTABLE",
    "DISPOSITION_USABLE",
    "RUN_ATTEMPT_LIMIT",
    "RUN_COMPLETED",
    "RUN_FAILED",
    "Attempt",
    "DeclarationSupportError",
    "Disposition",
    "EntryState",
    "NoUsableRoutingTarget",
    "Ranking",
    "RunRequest",
    "RunResult",
    "check_registry_entry",
    "describe",
    "order_by_pace",
    "run",
]
