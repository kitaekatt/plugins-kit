"""``RunAdapter`` (A-min.3): the consumer's full worker-facing contract.

Canonical home -- widened in place, not duplicated
------------------------------------------------------
``execution.controller`` used to define :class:`RunAdapter` locally as a
"local, minimal seam" (see that module's docstring, now updated to point
here). This module is where that class now LIVES; ``execution.controller``
imports and re-exports it unchanged, so every existing import path
(``from content_pipeline.execution.controller import RunAdapter``) and every
existing call site (:func:`~content_pipeline.execution.drivers.inline.run_wave`,
:func:`~content_pipeline.execution.controller.finalize_run`) keeps working
with no signature change -- only new, optional, trailing fields (the plan's
"Honour... widen, do not break existing callers" instruction). Both call
sites still share the identical ``parse_fn`` field for the same reason as
before: D1's "finalize re-parses with the SAME function the driver submitted
under" requirement holds BY CONSTRUCTION, not by convention.

Five responsibilities (plan of record, phase A-min.3)
----------------------------------------------------------
1. **Reconstruct a unit by id** -- :attr:`RunAdapter.unit_for`. Unchanged
   from A-min.2.
2. **Build a prepared request** -- :meth:`RunAdapter.resolve_prepared_request`.
   New first-class step: :attr:`RunAdapter.build_request` when supplied,
   otherwise composed from the existing :attr:`system_for`/:attr:`user_for`
   fields (so an A-min.2 adapter that never touches ``build_request``
   resolves to the identical prompt it always built).
3. **Provide the ``ValidationSpec``** -- :meth:`RunAdapter.resolve_validation_spec`.
   Reuses :class:`content_pipeline.llm.platform.ValidationSpec` -- the SAME
   type :func:`~content_pipeline.llm.platform.submit_validated` builds
   internally and :func:`~content_pipeline.llm.platform.evaluate_submission`
   consumes (plan D1) -- rather than inventing a second, adapter-local type
   for the identical contract; that module's own docstring names this
   widening and explicitly declines to replace itself with it.
4. **Apply a payload** -- :attr:`RunAdapter.apply`. Unchanged from A-min.2.
5. **Optionally reconcile an ``apply_unknown``** -- :attr:`RunAdapter.reconcile`.
   Unchanged from A-min.2 (D6, fail closed absent this hook).

Adapter identity/version and incompatible resume (D1)
-----------------------------------------------------------
:attr:`RunAdapter.adapter_version` is the consumer's own identity/version tag
for ITS adapter code (parser, prompt builder, validators) -- distinct from,
and compared against, ``RunRecord.adapter_version``, the value the run was
created with (``store.create_run(..., adapter_version=...)``). They are
expected to be the identical string for a given adapter build.
:func:`require_compatible_adapter` raises :class:`AdapterVersionMismatchError`
when they disagree, BEFORE any unit is touched -- the plan's "adapter
identity/version is recorded in the run... an incompatible resume is
refused" requirement. This check is NOT run automatically by
:func:`~content_pipeline.execution.controller.prepare_run` or
:func:`~content_pipeline.execution.controller.finalize_run` (their A-min.2
behavior, and the tests that pin it, are unchanged) -- it is invoked by
``execution.protocol``'s ``prepare``/``resume``/``finalize`` verbs, the
out-of-process entry points a resumed worker actually calls through.

``parse_fn`` MUST be deterministic and store-independent for tracked runs
(D1's adapter contract, restated from ``execution.controller``): finalize
re-runs it on text recorded at submit time, potentially long after and in a
different process.
"""

from __future__ import annotations

import ntpath
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from content_pipeline.execution.model import ExecutionError, RunRecord
from content_pipeline.llm.platform import ValidationSpec
from content_pipeline.pipeline.workunit import WorkUnit
from content_pipeline.validate import contract


def _default_unit_for(unit_id: str) -> WorkUnit:
    """The ``RunAdapter.unit_for`` default: a bare ``WorkUnit`` carrying only
    the id, no payload or context -- sufficient for a ``generate`` callable
    that needs neither."""
    return WorkUnit(id=unit_id)


@dataclass(frozen=True)
class PreparedRequest:
    """The consumer's built prompt for one unit (responsibility 2).

    ``unit`` is the ``WorkUnit`` it was built from, carried alongside so a
    caller (a worker skill, a protocol ``read`` reply) has the source unit
    without re-deriving it via a second ``unit_for`` call.
    """

    unit: WorkUnit
    system: str
    user: str


class AdapterVersionMismatchError(ExecutionError):
    """Raised by :func:`require_compatible_adapter` when ``adapter.adapter_version``
    disagrees with the run's recorded ``RunRecord.adapter_version`` (D1: an
    incompatible resume is refused, never guessed)."""

    def __init__(self, run_id: str, run_adapter_version: str, adapter_version: str) -> None:
        self.run_id = run_id
        self.run_adapter_version = run_adapter_version
        self.adapter_version = adapter_version
        super().__init__(
            f"run {run_id!r} was created with adapter_version "
            f"{run_adapter_version!r}, but this adapter reports "
            f"{adapter_version!r}; refusing to resume with a mismatched "
            "adapter (D1) rather than guess it is compatible"
        )


_GIT_BASH_POSIX_ABS_PATH = re.compile(r"^/([A-Za-z])/(.*)$")

_RESERVED_CWD_KEY = "__cwd__"


def _normalize_native_path(path: str) -> str:
    """Case/separator/trailing-slash-insensitive normalization via pure
    ``ntpath`` computation -- no OS calls, so it depends only on the
    ``cwd`` string it is handed (in particular, it respects a monkeypatched
    ``os.getcwd()`` return value the same way any other pure computation
    would; ``os.path.abspath`` does NOT on Windows, since it resolves via
    ``nt._getfullpathname``, a WinAPI call that reads the REAL process cwd
    and ignores a monkeypatched ``os.getcwd`` -- this helper exists
    specifically to avoid that trap)."""
    return ntpath.normcase(ntpath.normpath(path))


def _resolve_against_cwd(cwd: str, value: str) -> str:
    """Resolve ``value`` against ``cwd`` as Windows path components (pure
    ``ntpath.join``, no OS calls) and normalize the result. A driveless
    rooted value (the Git Bash POSIX shape, ``/d/dev/x``) takes its drive
    from ``cwd`` -- ``ntpath.join(cwd, "/d/dev/x")`` where ``cwd`` is
    ``D:\\dev\\x`` yields ``D:\\d\\dev\\x``, NOT ``D:\\dev\\x`` -- so
    the Git Bash case still resolves to a DIFFERENT location than ``cwd``
    itself, preserving the refusal this feature exists to produce. An
    already-absolute, same-location value (a trailing separator, a
    different drive-letter case) resolves to the identical normalized
    string as ``cwd``."""
    return _normalize_native_path(ntpath.join(cwd, value))


def _to_native_flavour(value: str) -> str:
    """Best-effort normalization for the ``likely_path_flavour_mismatch``
    hint ONLY -- a Git Bash POSIX-style absolute path (``/d/dev/x``) is
    rewritten to its native Windows equivalent (``D:\\dev\\x``) before
    case/separator normalization. Never used to decide pass/fail."""
    match = _GIT_BASH_POSIX_ABS_PATH.match(value)
    if match:
        drive, rest = match.groups()
        value = f"{drive.upper()}:\\{rest.replace('/', chr(92))}"
    return os.path.normcase(os.path.normpath(value))


def _same_location_different_flavour(recorded: str, actual: str) -> bool:
    """True when ``recorded`` and ``actual`` differ as raw strings, EXACTLY
    ONE of them looks like a Git Bash POSIX-style drive path
    (``_GIT_BASH_POSIX_ABS_PATH``), and they normalize (best-effort) to the
    same filesystem location once that one side is rewritten to its native
    equivalent -- the Git Bash ``PWD`` vs native ``os.getcwd()`` case this
    feature exists to diagnose.

    Requiring the asymmetry is load-bearing, not decorative: without it, a
    same-flavour difference (a trailing separator, a drive-letter case
    difference -- BOTH values native, neither POSIX-style) would ALSO
    normalize equal and get mislabeled a "path flavour" mismatch, which is
    simply false -- nothing POSIX was ever involved. Purely informational:
    never used to decide pass/fail (see the module docstring's "comparison
    is exact string equality" decision)."""
    if not recorded or not actual or recorded == actual:
        return False
    recorded_is_posix = bool(_GIT_BASH_POSIX_ABS_PATH.match(recorded))
    actual_is_posix = bool(_GIT_BASH_POSIX_ABS_PATH.match(actual))
    if recorded_is_posix == actual_is_posix:
        # Both native-style or both POSIX-style: whatever the difference
        # is, it is not a FLAVOUR difference.
        return False
    try:
        return _to_native_flavour(recorded) == _to_native_flavour(actual)
    except Exception:  # noqa: BLE001 -- best-effort hint, never fatal
        return False


class WorkerEnvironmentDeclarationError(ExecutionError):
    """Raised by :meth:`WorkerEnvironment.__post_init__` when a name appears
    in BOTH ``forbidden_vars`` and (``required_vars`` | ``cwd_vars``) -- an
    incoherent declaration (a name that must simultaneously be captured/
    matched and be absent from a worker). This is a distinct shape from
    :class:`WorkerEnvironmentMismatchError`: that class reports a *runtime*
    disagreement between a recorded snapshot and a live process; this one
    reports a *construction-time* contradiction in the declaration itself,
    knowable with no environment at all -- so it is raised eagerly rather
    than deferred to point-of-use (a deliberate, narrow exception to this
    class's usual "fields default so a call site fails at the point of use"
    posture; do not "fix" this back to deferred validation).

    Names every offending variable name, never a value -- there is no value
    to name yet at construction time, and the whole point of the check is
    that a forbidden variable's value must never surface anywhere.
    """

    def __init__(self, names: Sequence[str]) -> None:
        self.names = tuple(names)
        joined = ", ".join(repr(name) for name in self.names)
        super().__init__(
            f"WorkerEnvironment declares {joined} both forbidden and "
            "required/cwd-tracked; a variable cannot be both captured (or "
            "worker-side matched) and forbidden -- remove it from one side"
        )


class WorkerEnvironmentMismatchError(ExecutionError):
    """Raised by :meth:`WorkerEnvironment.check` (a worker-side environment
    mismatch) and by :func:`require_creatable_environment` (a create-run-time
    refusal) -- distinct call sites, same shape.

    ``likely_path_flavour_mismatch`` (a computed attribute, following the
    ``likely_reasoning_exhausted`` precedent) is ``True`` when ``recorded``
    and ``actual`` differ as raw strings but resolve to the same location
    under a best-effort Git-Bash-POSIX-to-native normalization -- it never
    changes the pass/fail outcome, only the message and this attribute.

    ``worker_cwd`` is set only by :meth:`WorkerEnvironment.check`'s
    ``cwd_vars`` loop (the worker-side self-check: is THIS worker in the
    right place). When set, it takes the place ``recorded_value`` would
    otherwise hold -- there is no "recorded" value in that comparison at
    all, since it never consults the run's stored snapshot.
    """

    def __init__(
        self,
        *,
        run_id: str,
        var_name: str,
        recorded_value: Optional[str] = None,
        actual_value: Optional[str] = None,
        forbidden: bool = False,
        worker_cwd: Optional[str] = None,
    ) -> None:
        self.run_id = run_id
        self.var_name = var_name
        self.recorded_value = recorded_value
        self.actual_value = actual_value
        self.forbidden = forbidden
        self.worker_cwd = worker_cwd
        self.likely_path_flavour_mismatch = False

        if forbidden:
            message = (
                f"run {run_id!r} declares {var_name!r} forbidden in a worker "
                "environment, but this process has it set; refusing to run a "
                "worker with a forbidden environment variable present"
            )
        elif worker_cwd is not None:
            # The worker-side cwd_vars check (WorkerEnvironment.check): this
            # is NOT a recorded-snapshot-vs-live comparison like the other
            # branches -- there is no "recorded" value here at all. `var_name`
            # names a declared cwd_var, `actual_value` is ITS live value in
            # this worker process, and `worker_cwd` is this SAME worker's own
            # os.getcwd(). The two disagree by RESOLVED LOCATION.
            self.likely_path_flavour_mismatch = _same_location_different_flavour(
                worker_cwd, actual_value or ""
            )
            flavour_note = (
                " (same location, different path flavour -- a Git Bash POSIX "
                "path where a native Windows path was expected)"
                if self.likely_path_flavour_mismatch
                else ""
            )
            message = (
                f"run {run_id!r} worker process has {var_name!r}={actual_value!r} "
                f"but this worker's own cwd is {worker_cwd!r}{flavour_note}; the "
                "worker is running in the wrong directory"
            )
        else:
            display_name = "cwd" if var_name == _RESERVED_CWD_KEY else var_name
            self.likely_path_flavour_mismatch = _same_location_different_flavour(
                recorded_value or "", actual_value or ""
            )
            flavour_note = (
                " (same location, different path flavour -- a Git Bash POSIX "
                "path where a native Windows path was recorded)"
                if self.likely_path_flavour_mismatch
                else ""
            )
            message = (
                f"run {run_id!r} recorded {display_name} {recorded_value!r} but "
                f"this process sees {actual_value!r}{flavour_note}; refusing to "
                "run a worker against an environment the run was not created "
                "in rather than resolve against the wrong root"
            )
        super().__init__(message)


@dataclass(frozen=True)
class WorkerEnvironment:
    """The consumer's declared worker-process environment contract (item 5).

    ``required_vars`` -- names whose VALUE must equal the run's recorded
    snapshot exactly (string equality, never resolved-location equality --
    see :meth:`check`). ``forbidden_vars`` -- NAMES only, never values; a
    worker process must not have any of these set (non-empty) at all.
    ``require_cwd`` -- the worker's ``os.getcwd()`` must equal the run's
    recorded ``os.getcwd()`` (worker-side, exact string equality, same as
    ``required_vars``).

    ``cwd_vars`` -- names whose value is meant to REPRESENT the working
    directory (the canonical example: ``PWD``), enforced in TWO places: by
    :func:`require_creatable_environment`'s create-run-time anchor check (in
    the ORCHESTRATOR's process, against ITS ``os.getcwd()``), and by
    :meth:`check`'s worker-side loop (in the WORKER's own process, against
    ITS OWN ``os.getcwd()`` -- not the recorded snapshot). This is a
    SEPARATE, explicit declaration from ``required_vars``/``require_cwd`` on
    purpose: a var naming the cwd is expected to vary in STRING FORM across
    environments that agree on location (native vs Git Bash POSIX spelling
    of the same directory), so it must never be compared by exact string
    equality the way ``required_vars`` is -- both enforcement points compare
    it to the enforcing process's own ``os.getcwd()`` by RESOLVED LOCATION
    instead. A name may appear in both ``required_vars`` and ``cwd_vars``
    (then it is ALSO worker-side exact-matched against the recorded
    snapshot, in addition to the resolved-location self-check below) or
    only in ``cwd_vars`` (then it gets the create-run anchor check AND the
    worker-side resolved-location self-check, but never an exact-match
    against a recorded value).

    An adapter that declares nothing (the default, every field empty/False)
    must behave exactly as before this feature existed: an empty
    :meth:`snapshot`, and :meth:`check` a no-op against any recorded value.
    """

    required_vars: Tuple[str, ...] = ()
    forbidden_vars: Tuple[str, ...] = ()
    require_cwd: bool = False
    cwd_vars: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse an incoherent declaration eagerly, at construction time
        (see :class:`WorkerEnvironmentDeclarationError`'s docstring for why
        this is a deliberate exception to the module's usual
        fail-at-point-of-use habit): a name in ``forbidden_vars`` that is
        ALSO in ``required_vars`` or ``cwd_vars`` would otherwise have its
        live value captured by :meth:`snapshot` and persisted -- exactly the
        leak this declaration claims to forbid."""
        overlap = set(self.forbidden_vars) & (set(self.required_vars) | set(self.cwd_vars))
        if overlap:
            raise WorkerEnvironmentDeclarationError(sorted(overlap))

    def snapshot(self) -> Dict[str, str]:
        """The current process's values for every declared ``required_var``
        AND every declared ``cwd_var`` (the union -- a ``cwd_var`` need not
        also be a ``required_var`` to be captured here, since
        :func:`require_creatable_environment` reads it from this same
        snapshot), plus ``os.getcwd()`` under a reserved key when
        ``require_cwd``. Never includes ``forbidden_vars`` -- their values
        are never recorded, only their presence is later checked (item 5's
        decision). A name declared in BOTH ``forbidden_vars`` and
        ``required_vars``/``cwd_vars`` cannot reach this method at all:
        :meth:`__post_init__` refuses that declaration at construction."""
        names = set(self.required_vars) | set(self.cwd_vars)
        result: Dict[str, str] = {name: os.environ.get(name, "") for name in names}
        if self.require_cwd:
            result[_RESERVED_CWD_KEY] = os.getcwd()
        return result

    def check(self, recorded: Mapping[str, str], *, run_id: str = "") -> None:
        """Refuse (:class:`WorkerEnvironmentMismatchError`) when the CURRENT
        process's environment disagrees with ``recorded`` (the run's stored
        snapshot). Exact string equality throughout for ``required_vars`` /
        ``require_cwd`` -- never resolved-location equality (DECIDED, module
        docstring). A default ``WorkerEnvironment()`` (nothing declared) is
        always a no-op, regardless of ``recorded``'s content.

        Deliberate asymmetry, worth restating so a later reader does not
        "simplify" it away: ``required_vars`` (and ``require_cwd``) compare
        this worker against the RECORDED SNAPSHOT -- did the environment
        change since the run was created. ``cwd_vars``, below, compares this
        worker against ITS OWN ``os.getcwd()`` -- is THIS worker in the right
        place, regardless of what was recorded. Different questions, so
        different comparisons: the former is exact string equality against
        ``recorded``; the latter is resolved-location equality against this
        process's own cwd, via the same ``_resolve_against_cwd`` helper
        :func:`require_creatable_environment` uses at create-run time.
        """
        for name in self.required_vars:
            recorded_value = recorded.get(name, "")
            actual_value = os.environ.get(name, "")
            if recorded_value != actual_value:
                raise WorkerEnvironmentMismatchError(
                    run_id=run_id,
                    var_name=name,
                    recorded_value=recorded_value,
                    actual_value=actual_value,
                )
        if self.require_cwd:
            recorded_cwd = recorded.get(_RESERVED_CWD_KEY, "")
            actual_cwd = os.getcwd()
            if recorded_cwd != actual_cwd:
                raise WorkerEnvironmentMismatchError(
                    run_id=run_id,
                    var_name=_RESERVED_CWD_KEY,
                    recorded_value=recorded_cwd,
                    actual_value=actual_cwd,
                )
        if self.cwd_vars:
            # Worker-side cwd_vars enforcement (closes the gap: previously
            # cwd_vars was only checked once, at create-run time, in the
            # ORCHESTRATOR's process -- a worker declaring only cwd_vars
            # (no required_vars, require_cwd=False) got zero worker-side
            # enforcement). Same relationship require_creatable_environment
            # enforces, now also enforced where the work actually happens:
            # the declared var's LIVE value must resolve to THIS worker's
            # own os.getcwd(). Reuses the same resolution helpers
            # (_resolve_against_cwd / _normalize_native_path) rather than a
            # second comparison, and skips an unset/empty value exactly as
            # require_creatable_environment does (nothing to compare).
            worker_cwd = os.getcwd()
            resolved_worker_cwd = _normalize_native_path(worker_cwd)
            for name in self.cwd_vars:
                actual_value = os.environ.get(name, "")
                if not actual_value:
                    continue
                if _resolve_against_cwd(worker_cwd, actual_value) != resolved_worker_cwd:
                    raise WorkerEnvironmentMismatchError(
                        run_id=run_id,
                        var_name=name,
                        actual_value=actual_value,
                        worker_cwd=worker_cwd,
                    )
        for name in self.forbidden_vars:
            if os.environ.get(name):
                raise WorkerEnvironmentMismatchError(
                    run_id=run_id, var_name=name, forbidden=True
                )

    def materialize(self, recorded: Mapping[str, str]) -> Tuple[Dict[str, str], Optional[str]]:
        """Pure dict math for a FUTURE spawner (no subprocess here): an env
        overlay (only the declared ``required_vars`` present in ``recorded``)
        and a ``cwd`` (the recorded cwd when ``require_cwd``, else ``None``).
        """
        overlay = {name: recorded[name] for name in self.required_vars if name in recorded}
        cwd = recorded.get(_RESERVED_CWD_KEY) if self.require_cwd else None
        return overlay, cwd


def require_compatible_environment(run: RunRecord, adapter: "RunAdapter") -> None:
    """The protocol-mount enforcement half (item 5): refuse a worker verb
    when ``adapter.environment`` disagrees with ``run.environment`` (the
    snapshot recorded at create-run time). A ``run.environment`` of ``None``
    (no snapshot recorded -- an adapter-less create, or an adapter that
    declared nothing) is treated as an empty recorded snapshot; a
    ``WorkerEnvironment()`` default checks nothing regardless."""
    recorded = run.environment or {}
    adapter.environment.check(recorded, run_id=run.id)


def require_creatable_environment(
    run_id: str, environment: WorkerEnvironment, snapshot: Mapping[str, str]
) -> None:
    """The create-run anchor half (item 5, DECIDED point 3): refuse to
    create a run when a declared ``cwd_var`` does not RESOLVE to
    ``os.getcwd()``'s location, checked in the orchestrator's own shell at
    create-run time -- before any worker ever runs.

    Checks ONLY ``environment.cwd_vars`` -- never a heuristic over
    ``required_vars``. A var not named there is never compared against
    ``os.getcwd()`` at all, no matter how path-like its value looks (a
    legitimate content root or asset directory that simply is not the cwd
    must never be refused here).

    Comparison is BY RESOLVED LOCATION (:func:`_resolve_against_cwd`, pure
    ``ntpath`` computation -- never ``os.path.abspath``, which resolves via
    a WinAPI call that reads the REAL process cwd and ignores this
    function's own ``cwd`` value), NOT raw string equality -- unlike the
    worker-side :meth:`WorkerEnvironment.check`, which stays exact string
    equality on purpose (a `cwd_var`'s spelling is expected to vary across
    environments that agree on location). So a trailing separator or a
    drive-letter case difference PASSES here.

    This is what still catches the Git Bash case: under Git Bash, a
    declared ``PWD`` snapshots as a POSIX-style path
    (``/d/dev/spiritcrossing/main``) with no drive letter. Resolved against
    ``cwd`` (``D:\\dev\\spiritcrossing\\main``), ``ntpath.join`` takes the
    drive from ``cwd`` and appends the POSIX value's path components
    UNCHANGED -- ``D:\\d\\dev\\spiritcrossing\\main`` -- a DIFFERENT
    location than ``cwd`` itself, so the refusal survives moving from exact
    string equality to resolved-location equality.
    """
    cwd = os.getcwd()
    resolved_cwd = _normalize_native_path(cwd)
    for name in environment.cwd_vars:
        value = snapshot.get(name, "")
        if not value:
            continue
        if _resolve_against_cwd(cwd, value) != resolved_cwd:
            raise WorkerEnvironmentMismatchError(
                run_id=run_id, var_name=name, recorded_value=cwd, actual_value=value
            )


@dataclass
class RunAdapter:
    """The consumer's full worker-facing contract -- see the module
    docstring's "Five responsibilities" section for which field covers which
    A-min.3 responsibility.

    Production fields (consumed by ``drivers.inline.run_wave``,
    A-min.2, unchanged):

    - ``unit_for`` -- ``unit_id -> WorkUnit``. Reconstructs the work unit a
      driver claimed, by id. Defaults to a bare ``WorkUnit(id=unit_id)``, the
      right shape for a ``generate`` callable that needs neither payload nor
      context.
    - ``system_for`` / ``user_for`` -- optional ``WorkUnit -> str``. Build the
      backend-path prompt; ``user_for`` is required (and ``system_for``
      defaults to an empty system prompt) whenever ``run_wave`` is given a
      ``backend`` rather than a plain ``generate`` callable, or when
      :meth:`resolve_prepared_request` falls back to them (no
      ``build_request`` supplied).
    - ``validators`` -- passed straight through to ``submit_validated``'s
      validate-until-valid loop, and folded into
      :meth:`resolve_validation_spec`'s default ``ValidationSpec`` when
      ``validation_spec_for`` is not supplied.

    Finalize fields (consumed by
    :func:`~content_pipeline.execution.controller.finalize_run`, A-min.2,
    unchanged):

    - ``parse_fn`` -- ``text -> payload``. Called mechanically on the durably
      recorded ``accepted_text``; never re-validates (D1). THE SAME callable
      a ``backend``-path ``run_wave`` call (or a protocol ``submit`` verb)
      evaluated the response under -- not a second, independently-supplied
      copy (D1's re-parse requirement).
    - ``apply`` -- ``(unit_id, payload) -> None``. The consumer's delivery
      side effect (e.g. a ``deliver.*`` write).
    - ``reconcile`` -- optional ``unit_id -> bool``. Answers "did this
      unit's apply already land" for a unit found ``apply_unknown``. Absent
      means finalize refuses to proceed past any ``apply_unknown`` unit
      (D6, fail closed).

    A-min.3 widenings (new, optional, trailing fields -- see the module
    docstring; every A-min.2 caller that never sets these observes no
    behavior change):

    - ``build_request`` -- optional ``WorkUnit -> PreparedRequest``. The
      first-class "build a prepared request" step. When absent,
      :meth:`resolve_prepared_request` composes one from ``system_for``/
      ``user_for`` instead -- the identical text an A-min.2 ``backend``-path
      ``run_wave`` call already builds.
    - ``validation_spec_for`` -- optional ``WorkUnit -> ValidationSpec``. The
      first-class "provide the ValidationSpec" step, for a consumer whose
      validators or parse behavior vary per unit. When absent,
      :meth:`resolve_validation_spec` composes one from ``parse_fn``,
      ``validators``, and ``validation_context``.
    - ``validation_context`` -- the ``context`` a composed ``ValidationSpec``
      carries (validators receive it; ``parse_fn`` does not). ``None`` by
      default, matching ``submit_validated``'s own default.
    - ``adapter_version`` -- this adapter's own identity/version tag,
      compared against ``RunRecord.adapter_version`` by
      :func:`require_compatible_adapter`. Empty string by default (matching
      an A-min.1/A-min.2 run that never populated a real value on either
      side, so an adapter that ignores this feature stays compatible with
      itself).

    Every field defaults so a consumer exercising only one call site (e.g. a
    ``generate``-only ``run_wave`` call that never finalizes, or a protocol
    mount that never calls ``read``) supplies only what it uses; a call site
    that actually needs a field it was not given fails at the point of use,
    not at construction time.
    """

    unit_for: Callable[[str], WorkUnit] = _default_unit_for
    system_for: Optional[Callable[[WorkUnit], str]] = None
    user_for: Optional[Callable[[WorkUnit], str]] = None
    parse_fn: Optional[Callable[[str], Any]] = None
    validators: Sequence[contract.Validator] = field(default_factory=tuple)
    apply: Optional[Callable[[str, Any], None]] = None
    reconcile: Optional[Callable[[str], bool]] = None
    build_request: Optional[Callable[[WorkUnit], PreparedRequest]] = None
    validation_spec_for: Optional[Callable[[WorkUnit], ValidationSpec]] = None
    validation_context: Any = None
    adapter_version: str = ""
    environment: WorkerEnvironment = WorkerEnvironment()
    expected_unit_seconds: Optional[float] = None
    unit_seconds_for: Optional[Callable[[WorkUnit], Optional[float]]] = None

    def resolve_expected_unit_seconds(self, unit: Optional[WorkUnit] = None) -> Optional[float]:
        """Item 2: the adapter declares COST, the lane's store owns the
        lease formula (:func:`~content_pipeline.execution.store.lease_for`).

        Uses ``unit_seconds_for(unit)`` when supplied and ``unit`` is given
        AND it returns a non-``None`` value; otherwise falls back to
        ``expected_unit_seconds``; otherwise ``None`` (undeclared -- the
        caller falls back to the unchanged 300s default, no warning)."""
        if self.unit_seconds_for is not None and unit is not None:
            per_unit = self.unit_seconds_for(unit)
            if per_unit is not None:
                return per_unit
        return self.expected_unit_seconds

    def resolve_prepared_request(self, unit: WorkUnit) -> PreparedRequest:
        """Responsibility 2: build the prepared request for ``unit``.

        Uses ``build_request`` when supplied. Otherwise composes one from
        ``system_for``/``user_for`` (``system_for`` defaulting to an empty
        system prompt) -- the identical shape ``drivers.inline.run_wave``
        already builds for its ``backend`` path, so a protocol ``read`` verb
        and the inline driver never disagree about what a given unit's
        request looks like. Raises ``ValueError`` when neither is supplied.
        """
        if self.build_request is not None:
            return self.build_request(unit)
        if self.user_for is None:
            raise ValueError(
                "RunAdapter has neither `build_request` nor `user_for` set; "
                "cannot build a prepared request for unit "
                f"{unit.id!r}"
            )
        system = self.system_for(unit) if self.system_for is not None else ""
        user = self.user_for(unit)
        return PreparedRequest(unit=unit, system=system, user=user)

    def resolve_validation_spec(self, unit: WorkUnit) -> ValidationSpec:
        """Responsibility 3: provide the ``ValidationSpec`` for ``unit``.

        Uses ``validation_spec_for`` when supplied. Otherwise composes one
        from ``parse_fn``, ``validators``, and ``validation_context`` -- the
        SAME three inputs ``submit_validated`` already threads into its own
        internally-built ``ValidationSpec`` (plan D1), so a protocol
        ``submit`` verb judges a response exactly as the inline driver's
        ``backend`` path would have. Raises ``ValueError`` when neither
        ``validation_spec_for`` nor ``parse_fn`` is supplied.
        """
        if self.validation_spec_for is not None:
            return self.validation_spec_for(unit)
        if self.parse_fn is None:
            raise ValueError(
                "RunAdapter has neither `validation_spec_for` nor `parse_fn` "
                f"set; cannot build a ValidationSpec for unit {unit.id!r}"
            )
        return ValidationSpec(
            parse_fn=self.parse_fn,
            validators=self.validators,
            context=self.validation_context,
        )


def require_compatible_adapter(run: RunRecord, adapter: RunAdapter) -> None:
    """Refuse an incompatible resume (D1).

    Compares ``adapter.adapter_version`` against ``run.adapter_version`` --
    equal (including both ``""``, the "identity not tracked" default) passes;
    anything else raises :class:`AdapterVersionMismatchError` before any unit
    is touched. Called by ``execution.protocol``'s ``prepare``/``resume``/
    ``finalize`` verbs, never automatically by
    :func:`~content_pipeline.execution.controller.prepare_run` or
    :func:`~content_pipeline.execution.controller.finalize_run` themselves
    (see the module docstring).
    """
    if adapter.adapter_version != run.adapter_version:
        raise AdapterVersionMismatchError(
            run.id, run.adapter_version, adapter.adapter_version
        )


__all__ = [
    "AdapterVersionMismatchError",
    "PreparedRequest",
    "RunAdapter",
    "WorkerEnvironment",
    "WorkerEnvironmentDeclarationError",
    "WorkerEnvironmentMismatchError",
    "require_compatible_adapter",
    "require_compatible_environment",
    "require_creatable_environment",
]
