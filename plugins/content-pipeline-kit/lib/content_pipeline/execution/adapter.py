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

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

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
    "require_compatible_adapter",
]
