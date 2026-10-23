"""Per-call truthfulness derivations shared by every adapter.

The advertisement (:mod:`.capabilities`) says what an adapter can honor in
general. The response (:class:`~.types.LLMResponse`) says what happened on ONE
call. This module is the bridge, and it exists so the per-call half is DERIVED
from the advertisement rather than restated beside it -- a second
hand-maintained list is exactly the drift the SSOT rule forbids.

Division of labour, because the two halves are not symmetric:

- ``dropped_params`` is derived generically here. The advertisement already
  names every :class:`~.types.BackendOptions` field an adapter does not read,
  so the only per-call question is which of those the caller actually SET.
- ``extras`` needs a THIRD rule, because its keys are not fields and the
  advertisement carries them one at a time. See
  :func:`derive_extras_report`; the important part is that a key an adapter
  forwards without validating is NOT dropped, and saying so would be as
  untrue as the silence it replaced.
- ``execution_controls_applied`` is reported BY the adapter, because only the
  code building the request knows what it emitted. A record's ``source`` does
  not settle it: codex's ``sandbox-mode`` is ``source=REQUEST`` yet emitted on
  every call because it has a default, so a rule keyed on ``source`` alone
  would under-report it. :func:`check_applied_controls` is the guard against
  the drift that hands the adapter: it refuses an id the advertisement does not
  carry, so a reported control is always one a reader can look up.
"""
from __future__ import annotations

from dataclasses import MISSING, fields
from datetime import datetime, timezone
from typing import Any, Iterable, Tuple

from .capabilities import FIXED, PASSTHROUGH, Capabilities
from .types import BackendOptions


def utc_now_iso() -> str:
    """Current UTC time as a second-precision ISO-8601 string with ``Z``.

    One formatter for every adapter so ``started_at``/``ended_at`` are
    comparable across a mixed-backend run without a consumer normalizing them.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _default_for(name: str) -> Any:
    """The declared default of one :class:`BackendOptions` field."""
    for f in fields(BackendOptions):
        if f.name != name:
            continue
        if f.default is not MISSING:
            return f.default
        if f.default_factory is not MISSING:  # type: ignore[misc]
            return f.default_factory()  # type: ignore[misc]
        return MISSING
    return MISSING


def caller_set_params(options: BackendOptions) -> Tuple[str, ...]:
    """Names of the options fields whose value differs from the declared default.

    "Set" is inferred by comparison, not recorded at construction:
    ``BackendOptions`` is a frozen dataclass with no sentinel per field, so a
    caller passing a value equal to the default is indistinguishable from one
    who passed nothing. That collision is harmless HERE -- a param left at its
    default was not being relied on, so omitting it from ``dropped_params``
    withholds nothing the caller needs.
    """
    out = []
    for f in fields(options):
        default = _default_for(f.name)
        if default is MISSING:
            continue
        value = getattr(options, f.name)
        if value != default:
            out.append(f.name)
    return tuple(out)


def derive_extras_report(
    capabilities: Capabilities, options: BackendOptions
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Classify each key the caller put in ``extras`` as dropped or forwarded.

    Returns ``(dropped, forwarded)``, both as ``extras.<key>`` names sorted so
    two calls with the same keys report the same sequence -- unlike a field,
    an extras key has no advertisement order to borrow.

    Three outcomes, decided per key against the advertisement alone:

    - The advertisement carries ``extras.<key>`` as a param: the adapter reads
      it, so nothing is reported (codex's ``output_schema``, ``sandbox``, ...).
    - The advertisement carries a generic ``extras`` param with
      ``handling=PASSTHROUGH``: every key rides into a downstream field
      unvalidated, so the key is FORWARDED, not dropped. Calling it dropped
      would be a fresh lie in place of the old silence -- openrouter really
      does send it, it just makes no claim the provider accepts it.
    - Neither: the key reaches nothing and is DROPPED. This covers claude-cli
      and opencode-cli, which read no extras at all, and it covers codex's
      unrecognised keys, which its argv builder ignores.

    Note what is deliberately NOT consulted: ``capabilities.dropped_params``.
    That tuple is about ``BackendOptions`` FIELDS, and the bare word ``extras``
    in it says "this adapter reads no extras key" -- a statement about the map,
    not about any key in it. The per-key answer comes from ``params``, which is
    where per-key advertisement lives.
    """
    extras = options.extras or {}
    generic = capabilities.params.get("extras")
    forwards_everything = generic is not None and generic.handling == PASSTHROUGH
    dropped = []
    forwarded = []
    for key in extras:
        name = f"extras.{key}"
        if name in capabilities.params:
            continue
        (forwarded if forwards_everything else dropped).append(name)
    return tuple(sorted(dropped)), tuple(sorted(forwarded))


def derive_dropped_params(
    capabilities: Capabilities, options: BackendOptions
) -> Tuple[str, ...]:
    """Params this call requested that the adapter does not read.

    The intersection of what the caller set with what the adapter advertises as
    dropped, in the advertisement's own order so two calls against the same
    adapter report the same sequence, followed by the per-key extras verdicts.

    The bare word ``extras`` never survives into the report. An adapter that
    advertises it as dropped is saying it reads no extras key at all, which is
    true but useless to a caller holding four of them: the field name does not
    say WHICH keys went nowhere, and on codex -- where some keys are read and
    some are not -- there is no single answer for the field. So it is replaced
    by the ``extras.<key>`` entries :func:`derive_extras_report` derives.
    """
    was_set = set(caller_set_params(options))
    fields_dropped = tuple(
        p for p in capabilities.dropped_params if p in was_set and p != "extras"
    )
    extras_dropped, _ = derive_extras_report(capabilities, options)
    return fields_dropped + extras_dropped


def derive_forwarded_params(
    capabilities: Capabilities, options: BackendOptions
) -> Tuple[str, ...]:
    """Params sent downstream without the adapter validating them.

    Distinct from ``dropped_params`` because the two carry opposite advice: a
    dropped param had no effect and the caller should stop sending it, while a
    forwarded one reached the provider and may well have worked. Merging them
    would tell an openrouter caller to remove a param that is doing its job.
    """
    _, forwarded = derive_extras_report(capabilities, options)
    return forwarded


def fixed_control_ids(capabilities: Capabilities) -> Tuple[str, ...]:
    """Ids of the controls this adapter emits on EVERY invocation.

    ``source=FIXED`` is defined as "every invocation of this adapter emits the
    control", so an adapter whose controls are all fixed can report its applied
    set straight from the advertisement with no second list to keep in step.
    An adapter with conditional controls cannot use this -- it must report what
    it actually emitted.
    """
    return tuple(c.id for c in capabilities.execution_controls if c.source == FIXED)


def check_applied_controls(
    capabilities: Capabilities, applied: Iterable[str]
) -> Tuple[str, ...]:
    """Return ``applied`` as a tuple, rejecting any id not advertised.

    Raises :class:`ValueError` rather than dropping the unknown id: silently
    filtering would turn an adapter/advertisement drift into a quiet
    under-report, which is the failure this whole contract exists to prevent.
    """
    known = {c.id for c in capabilities.execution_controls}
    applied = tuple(applied)
    unknown = [i for i in applied if i not in known]
    if unknown:
        raise ValueError(
            f"{capabilities.adapter} reported execution controls it does not "
            f"advertise: {', '.join(sorted(unknown))}"
        )
    return applied


__all__ = [
    "utc_now_iso",
    "caller_set_params",
    "derive_extras_report",
    "derive_dropped_params",
    "derive_forwarded_params",
    "fixed_control_ids",
    "check_applied_controls",
]
