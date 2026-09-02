"""Transport-neutral worker-pack and reap assets.

This module exists so the workflow lane (``workflows/run-ready-wave.js``
and its Python pack builder) can build a worker's invocation set and reap
abandoned units WITHOUT importing the background-session driver
(``execution/drivers/claude_bg.py``). Everything below was either MOVED
here verbatim from that module (the worker-pack and reap helpers the
background driver already built) or is NEW support the workflow lane needs
that the background driver never did, because that driver claims a unit
itself before launch (see ``claude_bg``'s module docstring) rather than
handing a worker a runnable claim command.

Import discipline (load-bearing): this module imports ONLY the standard
library plus :mod:`content_pipeline.execution.model`,
:mod:`content_pipeline.execution.store`, and
:mod:`content_pipeline.execution.adapter`. It must NEVER import
``content_pipeline.execution.drivers.claude_bg`` -- that driver imports
these names back out of this module (see its own docstring), and the reverse
edge would be an import cycle.

Moved verbatim from ``claude_bg.py``: :func:`_sanitize_path_component`,
:func:`_format_argv`, :class:`WorkerCommand`, :func:`answer_path_for`,
:func:`envelope_path_for`, :data:`ANSWER_FENCE_PREFIX`, the three fence
error classes (:class:`AnswerFenceError`, :class:`MissingAnswerFenceError`,
:class:`AnswerFenceMismatchError`), :func:`format_fenced_answer`,
:func:`parse_fenced_answer`, :func:`_envelope_payload_text`,
:func:`worker_envelopes_for`, :func:`enumerate_worker_invocations`,
:func:`reclaimable_units`, :func:`reclaim_attempt_count`,
:func:`_terminally_fail_exhausted_unit`, and
:data:`DEFAULT_MAX_RECLAIMS_PER_UNIT`. ``claude_bg.py`` now re-imports these
names rather than defining them, so ``claude_bg.X is workerpack.X`` for
every one of them (pinned by
``tests/content-pipeline-kit/test_workerpack_aliases.py``).

New here, C-specific: :func:`claim_envelope_path_for` (worker-scoped claim
envelope path -- see the design doc section 2's "self-claim ruling" for why
worker-scoping is load-bearing), :func:`claim_envelope_text` (the exact
``_claim`` payload, per ``execution/protocol.py``'s ``build_handlers``),
:func:`enumerate_workflow_invocations` (the six strings
:func:`enumerate_worker_invocations` already produces, plus a ``claimCmd``),
and :func:`build_wave_args` (reap-first candidate selection, the lease
refusal, batch-id minting, envelope pre-writes, and the args object the
workflow script consumes).
"""

from __future__ import annotations

import json
import math
import os
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.model import (
    AlreadyClaimedError,
    AttemptKind,
    ExecutionError,
    RunHaltedError,
    TerminalStateError,
    UnitRecord,
    UnitState,
)
from content_pipeline.execution.store import ExecutionStore

# ---------------------------------------------------------------------------
# Moved verbatim from claude_bg.py -- worker-pack and reap assets (B1)
# ---------------------------------------------------------------------------


def _sanitize_path_component(value: str) -> str:
    """A filesystem-safe fragment for :func:`answer_path_for` -- every
    non-alnum/``-``/``_`` character becomes ``_``. Deterministic, and never
    empty for a non-empty ``value`` (``run_id``/``unit_id`` are non-empty by
    convention -- see ``pipeline.workunit.WorkUnit``)."""
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in value)


@dataclass(frozen=True)
class WorkerCommand:
    """The consumer's declaration of how its protocol mount is invoked.

    ``argv`` is a TEMPLATE, not a full invocation: a tuple of argv tokens
    (e.g. ``("python", "mytool.py", "run")``) that :func:`enumerate_worker_invocations`
    extends with a verb (``claim``/``read``/``submit``/``fail``) and a fixed
    set of identifying flags. Any token containing the literal substrings
    ``{run_id}``/``{unit_id}``/``{worker_id}`` is substituted first (a
    consumer whose mount needs, say, a per-run ``--db`` path can embed
    ``{run_id}`` in one of its own tokens). B1 ships NO default template --
    the mount is the consumer's.

    ``answer_dir`` is the directory (a native path) a worker writes its
    deterministic per-unit answer file into, via the Write tool -- see
    :func:`answer_path_for`.

    ``envelope_dir`` is the directory (a native path) a worker's JSON
    protocol envelopes live in -- see :func:`envelope_path_for`. Additive
    and optional: ``None`` (the default) means "same directory as
    ``answer_dir``", via :attr:`resolved_envelope_dir`, so an existing
    caller that never sets this field keeps writing everything to one
    directory exactly as before this field existed.
    """

    argv: Tuple[str, ...]
    answer_dir: str
    envelope_dir: Optional[str] = None

    @property
    def resolved_envelope_dir(self) -> str:
        """``envelope_dir`` when set, else ``answer_dir`` -- the directory a
        caller should actually use for envelope paths. Kept as a property
        (never resolved into a stored field) so a caller that mutates
        ``answer_dir`` after construction -- there is none today, but the
        class is otherwise immutable-by-convention -- never leaves this
        derived value stale."""
        return self.envelope_dir if self.envelope_dir is not None else self.answer_dir


def _format_argv(argv: Sequence[str], **subs: str) -> Tuple[str, ...]:
    """Literal-substring substitution over every ``argv`` token -- never
    :meth:`str.format`, which would raise on a token that happens to contain
    an unrelated ``{...}`` (a Windows path, a JSON-shaped flag value)."""
    out: List[str] = []
    for token in argv:
        for key, value in subs.items():
            token = token.replace("{" + key + "}", value)
        out.append(token)
    return tuple(out)


def answer_path_for(worker_command: WorkerCommand, run_id: str, unit_id: str) -> str:
    """The deterministic per-unit answer-file path a worker writes its
    submission text to, and that :func:`enumerate_worker_invocations`'s
    ``submit --from-file`` invocation reads back. Deterministic in ``run_id``
    and ``unit_id`` alone -- computable before the run, which is what makes
    the invocation set enumerable ahead of time (the module docstring's whole
    reason for existing).

    Deliberately carries NO ``worker_id`` and no generation counter, so two
    successive dispatches of the same unit write the same file. The
    generation is fenced in the file's CONTENT instead, by
    :func:`format_fenced_answer` -- putting it in the path would make the
    path un-computable before the run and destroy exactly the pre-run
    enumerability this function exists for."""
    filename = (
        f"{_sanitize_path_component(run_id)}__{_sanitize_path_component(unit_id)}.answer.txt"
    )
    return os.path.join(worker_command.answer_dir, filename)


# ---------------------------------------------------------------------------
# The answer artifact's own fence -- content, never path
# ---------------------------------------------------------------------------

ANSWER_FENCE_PREFIX = "content-pipeline-fence:"


class AnswerFenceError(ExecutionError):
    """Base class for a refusal to read an answer artifact whose declared
    fencing token cannot be trusted for the submission presenting it."""


class MissingAnswerFenceError(AnswerFenceError):
    """The answer artifact's first line is not a fence declaration.

    Refused rather than treated as unfenced-and-fine: an artifact with no
    declared generation is exactly the artifact a previous dispatch's
    still-live session may have written, and accepting it would splice text
    of unknown provenance into a currently-valid submit envelope."""

    def __init__(self, first_line: str) -> None:
        self.first_line = first_line
        super().__init__(
            "answer artifact does not begin with a fence line "
            f"({ANSWER_FENCE_PREFIX!r} followed by the fencing token); its "
            f"first line was {first_line!r}"
        )


class AnswerFenceMismatchError(AnswerFenceError):
    """The answer artifact declares a DIFFERENT fencing token than the
    submit envelope presenting it -- either a stale artifact under a current
    envelope, or a current artifact under a stale envelope. Both are the
    same defect seen from opposite ends: the text and the standing to submit
    it came from different generations of the same unit."""

    def __init__(self, declared: int, expected: int) -> None:
        self.declared = declared
        self.expected = expected
        super().__init__(
            f"answer artifact declares fencing token {declared!r} but the "
            f"submission presents {expected!r}; refusing to submit text "
            "produced under a different claim"
        )


def format_fenced_answer(fencing_token: int, text: str) -> str:
    """The exact bytes a worker writes to :func:`answer_path_for`'s path.

    One fence line, then the answer text verbatim::

        content-pipeline-fence: 7
        <the answer text, exactly as produced>

    Only the FIRST line is ever interpreted, so the body may contain
    anything at all -- including further lines that look like fence lines,
    which :func:`parse_fenced_answer` returns untouched as part of the
    answer."""
    return f"{ANSWER_FENCE_PREFIX} {fencing_token}\n{text}"


def parse_fenced_answer(raw: str, expected_token: int) -> str:
    """The answer text out of ``raw``, or a typed refusal.

    Splits on the FIRST newline only: the first line must be
    :data:`ANSWER_FENCE_PREFIX` followed by an integer equal to
    ``expected_token``, and everything after that newline is the answer,
    returned byte-for-byte. Because only the first line is inspected, a body
    that itself contains ``content-pipeline-fence:`` is ordinary text and is
    neither re-parsed nor stripped.

    Raises :class:`MissingAnswerFenceError` when the first line is not a
    fence declaration at all, and :class:`AnswerFenceMismatchError` when it
    declares a different token."""
    first_line, separator, body = raw.partition("\n")
    declaration = first_line.rstrip("\r").strip()
    if not declaration.startswith(ANSWER_FENCE_PREFIX):
        raise MissingAnswerFenceError(first_line)
    token_text = declaration[len(ANSWER_FENCE_PREFIX):].strip()
    try:
        declared = int(token_text)
    except ValueError as exc:
        raise MissingAnswerFenceError(first_line) from exc
    if declared != expected_token:
        raise AnswerFenceMismatchError(declared, expected_token)
    return body if separator else ""


def envelope_path_for(
    worker_command: WorkerCommand, run_id: str, unit_id: str, verb: str
) -> str:
    """The deterministic per-unit, per-verb JSON protocol-envelope path --
    the file a ``read``/``submit``/``fail`` invocation's ``@<path>``
    argument names (see ``cli.run.build_commands``'s ``protocol`` command).
    Deterministic in ``(run_id, unit_id, verb)`` alone, mirroring
    :func:`answer_path_for`'s determinism in ``(run_id, unit_id)`` -- the
    same property that makes an enumerated invocation pre-allowlistable."""
    filename = (
        f"{_sanitize_path_component(run_id)}__{_sanitize_path_component(unit_id)}.{verb}.json"
    )
    return os.path.join(worker_command.resolved_envelope_dir, filename)


_ENVELOPE_VERBS: Tuple[str, ...] = ("read", "submit", "fail")


def _envelope_payload_text(verb: str, run_id: str, unit_id: str, worker_id: str) -> str:
    """The literal JSON (``read``) or JSON-shaped TEMPLATE
    (``submit``/``fail``) text for one verb's envelope.

    ``read`` needs no fencing token (its payload does not consume one -- see
    ``execution/protocol.py``'s ``_read``), so its text is ordinary, valid,
    ready-to-use JSON. ``submit``/``fail`` DO need a fencing token, but that
    value is not knowable when this function runs (P5's determinism
    constraint: an enumerated invocation string must be computable from
    ``(run_id, unit_id, worker_id)`` alone, before any unit is ever
    claimed). So their text carries the literal, unquoted placeholder token
    ``<FENCING_TOKEN>`` in place of a real value -- NOT valid JSON as
    written, and not meant to be parsed until a worker substitutes the real
    token, which its LAUNCH PROMPT names (the dispatcher claims the unit
    before launching; see :func:`dispatch_unit`). See
    :func:`worker_envelopes_for`'s docstring for who writes which of these
    to disk and when.
    """
    if verb == "read":
        envelope = {
            "protocol_version": "1",
            "verb": verb,
            "payload": {"run_id": run_id, "unit_id": unit_id, "worker_id": worker_id},
        }
        return json.dumps(envelope, indent=2) + "\n"
    # submit: JSON-shaped template text, fencing_token a literal placeholder
    # a worker fills in at runtime -- see docstring above.
    if verb == "submit":
        return (
            "{\n"
            '  "protocol_version": "1",\n'
            f"  \"verb\": {json.dumps(verb)},\n"
            '  "payload": {\n'
            f"    \"run_id\": {json.dumps(run_id)},\n"
            f"    \"unit_id\": {json.dumps(unit_id)},\n"
            f"    \"worker_id\": {json.dumps(worker_id)},\n"
            '    "fencing_token": <FENCING_TOKEN>\n'
            "  }\n"
            "}\n"
        )
    # fail: workers replace both placeholders. The detail placeholder is
    # unquoted so the worker supplies one JSON string literal via json.dumps.
    return (
        "{\n"
        '  "protocol_version": "1",\n'
        f"  \"verb\": {json.dumps(verb)},\n"
        '  "payload": {\n'
        f"    \"run_id\": {json.dumps(run_id)},\n"
        f"    \"unit_id\": {json.dumps(unit_id)},\n"
        f"    \"worker_id\": {json.dumps(worker_id)},\n"
        '    "fencing_token": <FENCING_TOKEN>,\n'
        '    "terminal": true,\n'
        '    "error": <FAILURE_DETAIL_JSON>\n'
        "  }\n"
        "}\n"
    )


def worker_envelopes_for(
    worker_command: WorkerCommand, run_id: str, unit_id: str, worker_id: str
) -> Dict[str, Tuple[str, str]]:
    """``{verb: (path, text)}`` for ``read``/``submit``/``fail`` -- the JSON
    protocol-envelope path and content for each verb this unit's worker ever
    needs. Pure function: no filesystem I/O here, deterministic in
    ``(run_id, unit_id, worker_id)`` alone (P5), same as every other
    function in this section.

    There is deliberately no ``claim`` entry. The dispatcher claims the unit
    itself before launching (:func:`dispatch_unit`), so a worker session has
    no claim envelope to run and no way to take a claim -- which is what
    stops a session left alive by an earlier dispatch from re-claiming a
    unit that has since been reclaimed and re-dispatched.

    A caller writes these to disk at two different TIMES, for two different
    reasons, per the D5/P5 design this module ships against:

    - ``read`` is written by the DISPATCHER, before the worker's session
      ever launches (:func:`build_launch_prompt` does this) -- its text
      needs no runtime information, so pre-writing it is what lets the
      dispatcher pre-authorize the ``read`` invocation (P5).
    - ``submit``/``fail`` are written by the WORKER itself, at runtime, via
      the Write tool -- their text needs the fencing token, which is not
      knowable when this function runs. The text this function returns for
      them is a TEMPLATE (see :func:`_envelope_payload_text`): the worker's
      only permitted edit is substituting the literal ``<FENCING_TOKEN>``
      token for the real value its launch prompt names; nothing else in the
      template may change.
    """
    return {
        verb: (envelope_path_for(worker_command, run_id, unit_id, verb),
               _envelope_payload_text(verb, run_id, unit_id, worker_id))
        for verb in _ENVELOPE_VERBS
    }


def enumerate_worker_invocations(
    worker_command: WorkerCommand, run_id: str, unit_id: str, worker_id: str
) -> Tuple[str, str, str, str, str, str]:
    """The EXACT command/Write-tool-target strings a worker for this unit
    may run or write, in order: ``read``, ``submit --text-file=<answer
    path>``, ``fail``, the Write-tool target for the answer file, the
    Write-tool target for the ``submit`` envelope, and the Write-tool target
    for the ``fail`` envelope.

    There is no ``claim`` entry: the dispatcher claims each unit before
    launching its worker (:func:`dispatch_unit`), so ``read`` is a worker's
    first invocation.

    Every returned string is deterministic given ``(run_id, unit_id,
    worker_id)`` -- no unit content, no timestamp, no random component, and
    (P5-critical) NO FENCING TOKEN, even though the dispatcher now knows the
    token before the launch. Keeping it out of these strings is what makes a
    pre-authorized allowlist entry possible: the same six strings can be
    computed, and allowlisted, before the worker ever runs. The token
    reaches the worker through the launch PROMPT and rides in file CONTENT
    (the submit/fail envelopes it authors, and the answer artifact's fence
    line), never in an invocation string.

    Each of ``read``/``submit``/``fail`` is ``<argv> protocol @<envelope
    path>`` (see ``cli.run.build_commands``'s ``protocol`` command and its
    ``@<path>`` envelope-sourcing form) -- never the old flag form
    (``claim --run-id ... --unit-id ...``), which
    ``cli.run.build_commands`` never registered as a command at all (only
    ``protocol`` is), so the flag form always failed as an unknown command
    for every verb except ``claim`` (whose flags accidentally parsed as
    positional argv and silently held the unit's lease forever without
    ever reaching ``read``/``submit``).
    """
    subs = {"run_id": run_id, "unit_id": unit_id, "worker_id": worker_id}
    base = _format_argv(worker_command.argv, **subs)
    envelopes = worker_envelopes_for(worker_command, run_id, unit_id, worker_id)
    answer_path = answer_path_for(worker_command, run_id, unit_id)

    read_path, _ = envelopes["read"]
    submit_path, _ = envelopes["submit"]
    fail_path, _ = envelopes["fail"]

    read_cmd = shlex.join(base + ("protocol", f"@{read_path}"))
    submit_cmd = shlex.join(
        base + ("protocol", f"@{submit_path}", f"--text-file={answer_path}")
    )
    fail_cmd = shlex.join(base + ("protocol", f"@{fail_path}"))
    write_answer_cmd = f"Write tool -> {answer_path}"
    write_submit_cmd = f"Write tool -> {submit_path}"
    write_fail_cmd = f"Write tool -> {fail_path}"
    return (
        read_cmd,
        submit_cmd,
        fail_cmd,
        write_answer_cmd,
        write_submit_cmd,
        write_fail_cmd,
    )


# ---------------------------------------------------------------------------
# Reclaim selection and bounded reclaims (driver-local; wave.py is
# untouched -- _flat_ready_wave returns only PENDING, so a unit whose worker
# died sits CLAIMED forever and never re-enters a wave through that module)
# ---------------------------------------------------------------------------


def reclaimable_units(store: ExecutionStore, run_id: str, *, at: Optional[float] = None) -> List[UnitRecord]:
    """Units in ``CLAIMED`` whose lease has expired, with NO open dispatch,
    ordinal order.

    ``no open dispatch`` is the guard that keeps this driver-local: a unit
    whose worker is still tracked (even if this dispatcher stopped renewing
    it, e.g. a ``blocked`` session -- see :func:`supervise_tick`) is not
    reclaimable until its dispatch has been settled, so a second launch is
    never dispatched on top of a still-open one.
    """
    now = time.time() if at is None else at
    open_unit_ids = {d.unit_id for d in store.open_dispatches(run_id)}
    units = sorted(store.list_units(run_id), key=lambda u: u.ordinal)
    return [
        u
        for u in units
        if u.state is UnitState.CLAIMED
        and u.lease_expires_at is not None
        and u.lease_expires_at <= now
        and u.unit_id not in open_unit_ids
    ]


def reclaim_attempt_count(store: ExecutionStore, run_id: str, unit_id: str) -> int:
    """How many :data:`~content_pipeline.execution.model.AttemptKind.EXPIRE`
    rows exist for this unit -- already durable via ``claim_unit``'s reclaim
    path, so this needs no new schema; it just counts."""
    return sum(
        1 for a in store.list_attempts(run_id, unit_id) if a.kind is AttemptKind.EXPIRE
    )


def _terminally_fail_exhausted_unit(
    store: ExecutionStore, run_id: str, unit_id: str, *, dispatcher_id: str, at: Optional[float]
) -> None:
    """Beyond ``max_reclaims_per_unit``: reclaim once more (bumping the
    fence, same as any reclaim) and immediately fail terminally, mirroring
    ``execution.controller``'s ``_record_terminal_skip`` claim-then-fail
    shape.

    THIS CLAIM CAN REFUSE, and its refusals are the caller's to interpret,
    not this function's: ``dispatch_wave``'s dispatch loop handles
    :class:`RunHaltedError`, :class:`TerminalStateError` and
    :class:`AlreadyClaimedError` around the call (see the CLAIM REFUSALS
    paragraph there), because what each one means is a control-flow decision
    -- end the wave, skip the unit -- that only the loop can make. Every
    other exception propagates unchanged.

    NOTHING HERE OPENS A DISPATCH ROW, which is why -- unlike
    :func:`dispatch_unit` -- there is no cleanup to guard: the unrecoverable
    CLAIMED-plus-open-dispatch state (:func:`reclaimable_units` excludes it
    forever) is structurally unreachable from this function. A unit is only
    a candidate for this path via :func:`reclaimable_units`, which already
    excludes units with an open dispatch, and no row is written between
    there and here. If ``fail_unit`` below raises after a SUCCESSFUL claim,
    the unit is left CLAIMED under this dispatcher's fresh token with no
    dispatch row -- reclaimable again the moment that lease expires, so it
    is recoverable and is deliberately left to propagate. (It is also all
    but unreachable: the claim just bumped the fence, so no other actor
    holds a token that could make the unit terminal or steal the claim
    before the next statement.)"""
    claim = store.claim_unit(run_id, unit_id, dispatcher_id, at=at)
    store.fail_unit(
        run_id, unit_id, claim.fencing_token, error="reclaim_exhausted", terminal=True, at=at
    )


DEFAULT_MAX_RECLAIMS_PER_UNIT = 2


# ---------------------------------------------------------------------------
# New, C-specific: the worker-scoped claim envelope
# ---------------------------------------------------------------------------
#
# The B driver's worker_envelopes_for deliberately has no "claim" entry
# (the dispatcher claims before launch, so a B worker never runs a claim
# command). The C lane's agent claims its OWN unit (design section 2's
# "self-claim ruling"), so it needs a runnable claim command and a claim
# envelope to run it against -- and that envelope path must be WORKER-
# SCOPED, not just (run_id, unit_id) like envelope_path_for's other verbs:
# a later batch reusing the shared path would overwrite an earlier stalled
# agent's claim envelope and hand it a different worker identity (design
# section 2, "Remedy adopted").


def claim_envelope_path_for(
    worker_command: WorkerCommand, run_id: str, unit_id: str, worker_id: str
) -> str:
    """The worker-scoped ``claim`` protocol-envelope path:
    ``<run>__<unit>__<worker>.claim.json``, using the same
    :func:`_sanitize_path_component` discipline as :func:`envelope_path_for`.

    Worker-scoped rather than keyed on ``(run_id, unit_id)`` alone (contrast
    :func:`envelope_path_for`): a claim envelope is the one C-lane artifact
    that grants standing to CLAIM a unit, so a later batch's claim envelope
    must never overwrite an earlier, still-live agent's -- see this module's
    docstring above for the residual this closes and does not close.
    """
    filename = (
        f"{_sanitize_path_component(run_id)}__{_sanitize_path_component(unit_id)}"
        f"__{_sanitize_path_component(worker_id)}.claim.json"
    )
    return os.path.join(worker_command.resolved_envelope_dir, filename)


def claim_envelope_text(run_id: str, unit_id: str, worker_id: str) -> str:
    """The exact ``claim`` envelope JSON a C-lane agent runs against
    :func:`claim_envelope_path_for`'s path -- valid JSON (unlike the
    ``submit``/``fail`` TEMPLATE text :func:`_envelope_payload_text`
    produces), carrying exactly the payload
    ``execution/protocol.py``'s ``build_handlers``'s ``_claim`` handler
    requires: ``run_id``, ``unit_id``, ``worker_id``. No fencing token (a
    claim RETURNS one, it does not consume one) and no ``lease_seconds``
    (an optional payload field that may only SHORTEN the derived lease, per
    ``_claim``'s ``_resolve_lease_seconds`` -- this lane never sends one, so
    the mount's own derivation/explicit ``lease_seconds`` governs)."""
    envelope = {
        "protocol_version": "1",
        "verb": "claim",
        "payload": {"run_id": run_id, "unit_id": unit_id, "worker_id": worker_id},
    }
    return json.dumps(envelope, indent=2) + "\n"


def enumerate_workflow_invocations(
    worker_command: WorkerCommand, run_id: str, unit_id: str, worker_id: str
) -> Tuple[str, str, str, str, str, str, str]:
    """The seven EXACT command/Write-tool-target strings a C-lane agent for
    this unit may run or write: the ``claim`` invocation, then the same six
    strings :func:`enumerate_worker_invocations` produces for
    ``read``/``submit``/``fail`` and their Write-tool targets.

    The six are obtained by CALLING :func:`enumerate_worker_invocations`,
    never by re-deriving them, so the two lanes can never drift apart on
    what a ``read``/``submit``/``fail`` invocation string looks like. Only
    ``claim_cmd`` is new: ``<argv> protocol @<worker-scoped claim path>``,
    built the same way ``enumerate_worker_invocations`` builds its own
    ``protocol @<path>`` invocations, against
    :func:`claim_envelope_path_for`'s path rather than
    :func:`envelope_path_for`'s.
    """
    read_cmd, submit_cmd, fail_cmd, write_answer_cmd, write_submit_cmd, write_fail_cmd = (
        enumerate_worker_invocations(worker_command, run_id, unit_id, worker_id)
    )
    subs = {"run_id": run_id, "unit_id": unit_id, "worker_id": worker_id}
    base = _format_argv(worker_command.argv, **subs)
    claim_path = claim_envelope_path_for(worker_command, run_id, unit_id, worker_id)
    claim_cmd = shlex.join(base + ("protocol", f"@{claim_path}"))
    return (
        claim_cmd,
        read_cmd,
        submit_cmd,
        fail_cmd,
        write_answer_cmd,
        write_submit_cmd,
        write_fail_cmd,
    )


def build_wave_args(
    store: ExecutionStore,
    run_id: str,
    adapter: RunAdapter,
    worker_command: WorkerCommand,
    max_agents: int,
    *,
    lease_seconds: Optional[float] = None,
    at: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the ``args`` object ``workflows/run-ready-wave.js`` consumes
    (design section 3) -- everything computed in Python by the invoking
    session, never by the script.

    ``lease_seconds`` is this call's own optional mount-fixed override,
    mirroring ``build_handlers``'s own parameter of the same name and
    default: not part of the five positional parameters design section 5
    names, but needed to satisfy requirement (b) below (a caller whose mount
    passes an explicit ``lease_seconds`` to ``build_handlers`` passes the
    same value here; a caller relying on per-unit derivation leaves it
    ``None``). ``at`` is the same injectable-clock convention every other
    function in this module and ``claude_bg.py`` uses.

    Five things, in order, per design section 5:

    (a) REAP-FIRST candidate selection. Candidates are the still-``PENDING``
        units of the run plus expired-``CLAIMED`` units (the
        :func:`reclaimable_units` predicate). Any candidate whose EXPIRE
        count (:func:`reclaim_attempt_count`) has already reached
        :data:`DEFAULT_MAX_RECLAIMS_PER_UNIT` is terminally failed via
        :func:`_terminally_fail_exhausted_unit` (a freshly minted reaper
        worker id -- this is the generalization design section 5 calls for)
        and excluded from the pack list. That call's claim CAN refuse, and
        its three refusals are handled PER UNIT here, matching
        ``dispatch_wave``'s exhaustion path: a refused unit is excluded from
        the wave (already terminal, or live-claimed by someone else -- either
        way not this build's to hand to a fresh agent), and a
        :class:`RunHaltedError` additionally stops further selection so
        assembly ends gracefully instead of aborting mid-reap. This runs
        REGARDLESS of (b) below: reaping is maintenance on the run, not
        something a lease refusal should block.
    (b) THE LEASE REFUSAL. Raises (never warns) when the mount has neither
        a finite-positive explicit ``lease_seconds`` (this call's own
        parameter) nor a positive ``adapter.resolve_expected_unit_seconds``
        for EVERY selected unit -- a mount with no sizing information for
        some unit must not silently get the store's bare 300s fallback in
        a lane with no renewer to correct it (D5: no renewer, no mid-flight
        reclaim in this lane).
    (c) Mints ``batch_id`` -- the only identity source for this call, along
        with ``run_id`` (Python-minted, never left to the script).
    (d) Pre-writes the ``claim`` and ``read`` envelope files for every
        selected unit, exactly as ``build_launch_prompt`` pre-writes the
        ``read`` envelope for a B-lane worker. ``submit``/``fail`` envelopes
        are NOT pre-written -- they are agent-authored at runtime from the
        verbatim template text, same as B1.
    (e) Returns packs with the fields design section 3 names
        (``unitId``/``ordinal``/``workerId``/``claimCmd``/``readCmd``/
        ``submitCmd``/``failCmd``/``answerPath``/``writeSubmitPath``/
        ``writeFailPath``/``submitTemplate``/``failTemplate``),
        ordinal-sorted, under ``{"runId", "batchId", "maxAgents", "units"}``.

    ``workerId`` is ``wf-<batchId>-<unitId>``, sanitized as one unit via
    :func:`_sanitize_path_component` (design section 3) -- this is what
    makes the claim envelope WORKER-scoped (design section 2).
    """
    now = time.time() if at is None else at
    reaper_id = f"reap-{uuid.uuid4().hex[:12]}"

    # (a) reap-first candidate selection.
    all_units = {u.unit_id: u for u in store.list_units(run_id)}
    pending = [u for u in all_units.values() if u.state is UnitState.PENDING]
    combined: Dict[str, UnitRecord] = {u.unit_id: u for u in pending}
    for u in reclaimable_units(store, run_id, at=now):
        combined.setdefault(u.unit_id, u)

    selected: List[UnitRecord] = []
    for u in sorted(combined.values(), key=lambda r: r.ordinal):
        if (
            u.state is UnitState.CLAIMED
            and reclaim_attempt_count(store, run_id, u.unit_id) >= DEFAULT_MAX_RECLAIMS_PER_UNIT
        ):
            # CLAIM REFUSALS ARE PER-UNIT, NOT BUILD-FATAL. The claim inside
            # _terminally_fail_exhausted_unit really does refuse on this path
            # (its own docstring assigns all three refusals to the caller),
            # and ``dispatch_wave`` already treats each of them as routine --
            # this loop follows that policy rather than inventing a second
            # one. Letting one refusal propagate would abort the whole build
            # AFTER earlier units in this loop had already been terminally
            # failed: a durable partial reap with no wave to show for it.
            #
            # All three refusals EXCLUDE the unit from the wave, because each
            # one says the unit is not this build's to hand out: it is
            # already terminal, or another actor holds a live claim on it. A
            # unit another worker just settled must never be handed to a
            # fresh agent.
            try:
                _terminally_fail_exhausted_unit(
                    store, run_id, u.unit_id, dispatcher_id=reaper_id, at=now
                )
            except RunHaltedError:
                # The RUN is halted, so no further claim can succeed and
                # emitting more work is wrong. End assembly GRACEFULLY, the
                # same way ``dispatch_wave``'s exhaustion path ends its wave:
                # stop selecting, keep what was already selected, and let the
                # agents' own claims report the halt.
                break
            except TerminalStateError:
                # Benign here, exactly as on ``dispatch_wave``'s exhaustion
                # path: this call exists only to drive an exhausted unit
                # terminal, and the refusal says it already is.
                pass
            except AlreadyClaimedError:
                # Someone re-claimed it with a live lease between
                # ``reclaimable_units`` and this claim, so it is no longer
                # abandoned and is not this build's to fail -- nor to
                # dispatch.
                pass
            continue
        selected.append(u)

    # (b) the lease refusal.
    explicit_ok = (
        lease_seconds is not None and math.isfinite(lease_seconds) and lease_seconds > 0
    )
    if not explicit_ok:
        for u in selected:
            seconds = adapter.resolve_expected_unit_seconds(adapter.unit_for(u.unit_id))
            if seconds is None or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError(
                    "build_wave_args: mount has neither a finite-positive explicit "
                    "lease_seconds nor a positive resolve_expected_unit_seconds for "
                    f"unit {u.unit_id!r}; refusing to emit a wave"
                )

    # (c) mint the batch id -- the only identity source, alongside run_id.
    batch_id = uuid.uuid4().hex[:12]

    # (d) pre-write claim + read envelopes; (e) assemble ordinal-sorted packs.
    os.makedirs(worker_command.resolved_envelope_dir, exist_ok=True)
    packs: List[Dict[str, Any]] = []
    for u in selected:
        worker_id = _sanitize_path_component(f"wf-{batch_id}-{u.unit_id}")

        envelopes = worker_envelopes_for(worker_command, run_id, u.unit_id, worker_id)
        read_path, read_text = envelopes["read"]
        Path(read_path).write_text(read_text, encoding="utf-8")

        claim_path = claim_envelope_path_for(worker_command, run_id, u.unit_id, worker_id)
        claim_text = claim_envelope_text(run_id, u.unit_id, worker_id)
        Path(claim_path).write_text(claim_text, encoding="utf-8")

        (
            claim_cmd,
            read_cmd,
            submit_cmd,
            fail_cmd,
            _write_answer_cmd,
            _write_submit_cmd,
            _write_fail_cmd,
        ) = enumerate_workflow_invocations(worker_command, run_id, u.unit_id, worker_id)

        answer_path = answer_path_for(worker_command, run_id, u.unit_id)
        submit_path, submit_template = envelopes["submit"]
        fail_path, fail_template = envelopes["fail"]

        packs.append(
            {
                "unitId": u.unit_id,
                "ordinal": u.ordinal,
                "workerId": worker_id,
                "claimCmd": claim_cmd,
                "readCmd": read_cmd,
                "submitCmd": submit_cmd,
                "failCmd": fail_cmd,
                "answerPath": answer_path,
                "writeSubmitPath": submit_path,
                "writeFailPath": fail_path,
                "submitTemplate": submit_template,
                "failTemplate": fail_template,
            }
        )

    return {
        "runId": run_id,
        "batchId": batch_id,
        "maxAgents": max_agents,
        "units": packs,
    }


__all__ = [
    "ANSWER_FENCE_PREFIX",
    "AnswerFenceError",
    "AnswerFenceMismatchError",
    "DEFAULT_MAX_RECLAIMS_PER_UNIT",
    "MissingAnswerFenceError",
    "WorkerCommand",
    "answer_path_for",
    "build_wave_args",
    "claim_envelope_path_for",
    "claim_envelope_text",
    "enumerate_workflow_invocations",
    "enumerate_worker_invocations",
    "envelope_path_for",
    "format_fenced_answer",
    "parse_fenced_answer",
    "reclaim_attempt_count",
    "reclaimable_units",
    "worker_envelopes_for",
]
