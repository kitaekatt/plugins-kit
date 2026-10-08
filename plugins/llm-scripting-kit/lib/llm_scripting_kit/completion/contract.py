"""Output contracts at the completion seam: derive, prepare, judge.

An :class:`~.contract_types.OutputContract` rides on
``BackendOptions.output_contract``. Three things happen to it, and this module
owns all three so no adapter re-implements any of them:

1. BEFORE SELECTION -- :func:`contract_requirements` turns the contract into a
   requirement mapping for :func:`~.requirements.match_capabilities`, so a
   caller picks only an endpoint whose adapter advertises the policy.
   :func:`merge_requirements` joins it to the caller's own requirements.
2. BEFORE DISPATCH -- every adapter's ``complete()`` calls
   :func:`prepare_contract` right after resolving its options. With no
   contract it returns None and the call is unchanged. Otherwise it refuses,
   with :class:`OutputContractUnsatisfiable` and before any subprocess or HTTP
   call, a contract whose policy the adapter's record does not list, or one
   sent together with a legacy schema key in ``extras``. This is the last
   guard, and it holds for a caller that skipped selection entirely.
3. AFTER A COMPLETED CALL -- :func:`finalize_contract` judges the text with
   the pure :func:`evaluate_output`. A valid answer comes back with the
   validated object in ``structured`` and a :class:`ContractReport`; anything
   else raises :class:`OutputContractViolation`, carrying the full failed
   response.

Why a violation RAISES rather than returning ``status="error"``: the package
API signals failure by raising, and callers treat any returned response as
completed -- a consumer's response cache would store it. Raising keeps a
violation out of every success path.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Tuple

from .contract_types import (
    DELIVERIES,
    DELIVERY_NATIVE,
    DELIVERY_NONE,
    DELIVERY_PROMPT,
    DISPOSITION_SCHEMA_MISMATCH,
    DISPOSITION_TEXT_ONLY,
    DISPOSITION_UNPARSEABLE,
    DISPOSITION_VALID,
    DISPOSITIONS,
    POLICIES,
    POLICY_NATIVE_REQUIRED,
    POLICY_TEXT_ONLY,
    POLICY_VALIDATED_RESULT,
    SCHEMA_CLASS_OPENAI_STRICT,
    SCHEMA_CLASS_SUBSET,
    SCHEMA_CLASSES,
    SCHEMA_INSTRUCTION_PREFIX,
    SCHEMA_POLICIES,
    ContractReport,
    OutputContract,
    canonical_schema_json,
    strict_schema_violations,
)
from .json_repair import (
    STATUS_AMBIGUOUS,
    STATUS_REPAIRED,
    STATUS_UNRECOVERABLE,
    repair_json_structure,
)
from .json_schema import validate
from .types import ERROR, BackendOptions, LLMResponse, ResponseError

#: ``extras`` keys that are the LEGACY per-transport schema paths. Sending one
#: together with a contract would put two schema instructions on one call
#: with no rule for which wins, so :func:`prepare_contract` refuses the pair.
LEGACY_SCHEMA_EXTRAS = ("output_schema", "response_format")

#: ``ResponseError.code`` of a response that violated its contract.
VIOLATION_ERROR_CODE = "output-contract-violation"


class OutputContractUnsatisfiable(ValueError):
    """The contract cannot be honored by this adapter; nothing was dispatched."""


class OutputContractViolation(RuntimeError):
    """A completed call whose answer does not satisfy its output contract.

    ``response`` is the full failed :class:`~.types.LLMResponse`:
    ``status="error"``, the raw ``text``, usage, and the
    :class:`ContractReport`, with ``structured`` None. The token counts and
    ``model`` are mirrored as attributes so a caller that prices exceptions
    can price this billed call.

    The MESSAGE carries no model-authored text. Halt classification
    substring-matches exception messages, so an answer that merely mentions a
    rate limit must not make this read as a provider halt.
    """

    def __init__(self, response: LLMResponse, message: Optional[str] = None) -> None:
        if message is None:
            report = response.output_contract
            if report is None:
                message = "output contract violated"
            else:
                message = (
                    f"output contract {report.contract_id!r} violated: "
                    f"{report.disposition} ({len(report.errors)} schema error(s))"
                )
        super().__init__(message)
        self.response = response
        self.input_tokens = response.input_tokens
        self.output_tokens = response.output_tokens
        self.cache_hit_tokens = response.cache_hit_tokens
        self.model = response.model


def render_schema_instruction(contract: OutputContract) -> str:
    """The exact system-text suffix a prompt-delivering adapter appends.

    ``"\\n\\n" + SCHEMA_INSTRUCTION_PREFIX + canonical_schema_json(contract)``
    -- deterministic ASCII, so a test can assert the bytes rather than the
    presence of a channel.
    """
    if contract.schema is None:
        raise ValueError("a text-only output contract has no schema to render")
    return "\n\n" + SCHEMA_INSTRUCTION_PREFIX + canonical_schema_json(contract)


@dataclass(frozen=True)
class ContractOutcome:
    """The pure judgment of one answer against one contract.

    ``value`` is the validated object when ``disposition`` is ``valid`` and
    None otherwise; ``errors`` are sorted ``(json_pointer, keyword)`` pairs.
    """

    disposition: str
    value: Any = None
    errors: Tuple[Tuple[str, str], ...] = ()
    repaired: bool = False
    repair_edits: Tuple[Tuple[str, str, int], ...] = ()
    repair_note: str = ""


class _Unparseable(ValueError):
    pass


def _refuse_constant(token: str) -> Any:
    raise _Unparseable(f"non-JSON constant {token}")


def _refuse_duplicate_keys(pairs: Any) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise _Unparseable("duplicate object key")
        result[key] = value
    return result


def _strict_loads(text: str) -> Any:
    """``json.loads`` without its leniencies: no NaN/Infinity, no duplicate keys."""
    return json.loads(
        text,
        parse_constant=_refuse_constant,
        object_pairs_hook=_refuse_duplicate_keys,
    )


def evaluate_output(contract: OutputContract, text: Any) -> ContractOutcome:
    """Judge ``text`` against ``contract``. Pure: no I/O, no clock.

    ``text-only`` gives a ``text-only`` outcome. Otherwise ``text`` must parse
    as exactly one strict JSON value (``unparseable`` if not) that conforms to
    the schema (``schema-mismatch`` with the sorted errors if not).
    """
    if contract.policy == POLICY_TEXT_ONLY:
        return ContractOutcome(DISPOSITION_TEXT_ONLY)
    if not isinstance(text, str):
        return ContractOutcome(DISPOSITION_UNPARSEABLE)
    try:
        value = _strict_loads(text)
    except (ValueError, RecursionError):
        return ContractOutcome(DISPOSITION_UNPARSEABLE)
    errors = validate(contract.schema, value)
    if errors:
        return ContractOutcome(DISPOSITION_SCHEMA_MISMATCH, None, errors)
    return ContractOutcome(DISPOSITION_VALID, value, ())


def repair_and_evaluate_output(contract: OutputContract, text: Any) -> ContractOutcome:
    """:func:`evaluate_output`, then one structural repair of an unparseable answer.

    The strict judgment comes first; only an ``unparseable`` answer is
    repaired (code fences, text outside the root value, a dropped, swapped or
    surplus bracket, a missing comma, a key/colon slip -- see
    :mod:`.json_repair`). A repair is applied only when exactly one repaired
    document fits the schema's shape, and the repaired text is then judged by
    the same strict :func:`evaluate_output`, so validation stays mandatory.
    The outcome records whether a repair was applied and which edits.
    """
    outcome = evaluate_output(contract, text)
    if outcome.disposition != DISPOSITION_UNPARSEABLE or not isinstance(text, str):
        return outcome
    repair = repair_json_structure(contract.schema, text)
    if repair.status == STATUS_REPAIRED:
        again = evaluate_output(contract, repair.text)
        if again.disposition != DISPOSITION_UNPARSEABLE:
            return replace(
                again,
                repaired=True,
                repair_edits=tuple((e.op, e.token, e.offset) for e in repair.edits),
            )
        return replace(outcome, repair_note="repair-unparseable")
    if repair.status in (STATUS_AMBIGUOUS, STATUS_UNRECOVERABLE):
        return replace(outcome, repair_note=repair.status)
    return outcome


def contract_requirements(contract: Optional[OutputContract]) -> Dict[str, Any]:
    """The selection requirement a contract implies (``{}`` for no contract).

    ``{"structured_output": {"policies": [<policy>]}}`` for every policy,
    text-only included: an adapter that does not list a policy refuses it at
    dispatch, so selecting one would only move the refusal later.

    A schema contract whose schema is NOT strict-compatible
    (:attr:`OutputContract.strict_compatible`) also requires
    ``"contract_schema_class": "json-schema-subset"``: an adapter that accepts
    only ``openai-strict`` schemas would refuse it before dispatch. The
    requirement is positive, so a record declaring no class does not match
    it. A strict-compatible schema, and text-only, add nothing.
    """
    if contract is None:
        return {}
    if not isinstance(contract, OutputContract):
        raise TypeError(
            f"expected an OutputContract, got {type(contract).__name__}"
        )
    structured: Dict[str, Any] = {"policies": [contract.policy]}
    if contract.policy in SCHEMA_POLICIES and not contract.strict_compatible:
        structured["contract_schema_class"] = SCHEMA_CLASS_SUBSET
    return {"structured_output": structured}


_KEY_ALIASES = {"structured": "structured_output"}


def _normalized_requirements(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {_KEY_ALIASES.get(str(k), str(k)): v for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {"params": list(value)}
    raise ValueError("requirements must be a mapping or list")


def merge_requirements(a: Any, b: Any) -> Dict[str, Any]:
    """Join two requirement mappings, refusing a conflict rather than picking one.

    Accepts the same shapes ``match_capabilities`` does (``None``, a mapping,
    or a list as ``params`` shorthand). A key both sides set to DIFFERENT
    values raises :class:`ValueError` -- in particular two different
    ``structured_output`` requirements, which no single call could satisfy.
    Silently keeping one side would drop the other side's requirement.
    """
    left = _normalized_requirements(a)
    right = _normalized_requirements(b)
    merged = dict(left)
    for key, value in right.items():
        if key in merged and merged[key] != value:
            raise ValueError(
                f"conflicting requirements for {key!r}: {merged[key]!r} vs {value!r}"
            )
        merged[key] = value
    return merged


@dataclass(frozen=True)
class DeliveryPlan:
    """How one adapter will deliver one contract on one call.

    - ``delivery`` -- :data:`DELIVERY_NATIVE`, :data:`DELIVERY_PROMPT` or
      :data:`DELIVERY_NONE` (text-only).
    - ``instruction`` -- for prompt delivery, the exact string to append to
      the system text (:func:`render_schema_instruction`); else None.
    - ``schema_json`` -- for native delivery, the exact schema document to
      hand the native channel (:func:`canonical_schema_json`); else None.
    """

    contract: OutputContract
    adapter: str
    delivery: str
    instruction: Optional[str] = None
    schema_json: Optional[str] = None


def prepare_contract(capabilities: Any, options: Optional[BackendOptions]) -> Optional[DeliveryPlan]:
    """Refuse or plan a contract BEFORE anything is dispatched.

    ``capabilities`` is the record describing THIS call (a family record, or
    one specialized to an endpoint). Returns None when the options carry no
    contract, so an uncontracted call is unchanged. Raises
    :class:`OutputContractUnsatisfiable` when a legacy schema key is sent
    alongside the contract, when the record's
    ``structured_output.policies`` does not list the contract's policy, or
    when the record's ``contract_schema_class`` is ``openai-strict`` and the
    schema is not strict-compatible (the message names the offending schema
    pointers, sorted). Every refusal happens before the adapter writes any
    file or starts any call.
    """
    contract = getattr(options, "output_contract", None) if options is not None else None
    if contract is None:
        return None
    if not isinstance(contract, OutputContract):
        raise TypeError(
            f"options.output_contract must be an OutputContract, got "
            f"{type(contract).__name__}"
        )
    adapter = getattr(capabilities, "adapter", "<unknown adapter>")
    extras = getattr(options, "extras", None) or {}
    legacy = sorted(k for k in LEGACY_SCHEMA_EXTRAS if k in extras)
    if legacy:
        raise OutputContractUnsatisfiable(
            f"output contract {contract.id!r} cannot be combined with "
            + ", ".join(f"extras.{k}" for k in legacy)
            + "; the contract is the provider-independent schema path, so send "
            "one or the other. Nothing was dispatched"
        )
    structured = capabilities.structured_output
    advertised = tuple(getattr(structured, "policies", ()) or ())
    if contract.policy not in advertised:
        listed = ", ".join(advertised) if advertised else "none"
        raise OutputContractUnsatisfiable(
            f"{adapter} does not advertise output contract policy "
            f"{contract.policy!r} (advertised policies: {listed}); refused "
            f"contract {contract.id!r} before dispatch"
        )
    if contract.policy == POLICY_TEXT_ONLY:
        return DeliveryPlan(contract=contract, adapter=adapter, delivery=DELIVERY_NONE)
    if getattr(structured, "contract_schema_class", None) == SCHEMA_CLASS_OPENAI_STRICT:
        violations = contract.strict_violations
        if violations:
            raise OutputContractUnsatisfiable(
                f"{adapter} accepts only {SCHEMA_CLASS_OPENAI_STRICT} schemas, and "
                f"contract {contract.id!r} is not strict-compatible at "
                + ", ".join(p or "/" for p in violations)
                + " (each object schema needs additionalProperties false and "
                "every property listed in required); refused before dispatch"
            )
    delivery = getattr(structured, "contract_delivery", None)
    if delivery == DELIVERY_NATIVE:
        return DeliveryPlan(
            contract=contract,
            adapter=adapter,
            delivery=DELIVERY_NATIVE,
            schema_json=canonical_schema_json(contract),
        )
    if delivery == DELIVERY_PROMPT:
        if contract.policy == POLICY_NATIVE_REQUIRED:
            raise OutputContractUnsatisfiable(
                f"{adapter} lists {POLICY_NATIVE_REQUIRED!r} but delivers "
                "contracts by prompt; refused before dispatch"
            )
        return DeliveryPlan(
            contract=contract,
            adapter=adapter,
            delivery=DELIVERY_PROMPT,
            instruction=render_schema_instruction(contract),
        )
    raise OutputContractUnsatisfiable(
        f"{adapter} lists policy {contract.policy!r} but declares no "
        f"contract_delivery (native or prompt); refused before dispatch"
    )


def _report(plan: DeliveryPlan, outcome: ContractOutcome) -> ContractReport:
    contract = plan.contract
    return ContractReport(
        contract_id=contract.id,
        schema_version=contract.schema_version,
        schema_digest=contract.schema_digest,
        policy=contract.policy,
        delivery=plan.delivery,
        disposition=outcome.disposition,
        errors=outcome.errors,
        repaired=outcome.repaired,
        repair_edits=outcome.repair_edits,
        repair_note=outcome.repair_note,
    )


def finalize_contract(plan: Optional[DeliveryPlan], response: LLMResponse) -> LLMResponse:
    """Judge a COMPLETED call's answer; return it enriched, or raise.

    An unparseable answer gets one deterministic structural repair first
    (:func:`repair_and_evaluate_output`); the repaired text is validated like
    any answer. ``response.text`` stays the raw answer, and the report's
    ``repaired`` / ``repair_edits`` say what changed.

    No plan: the response is returned unchanged. Text-only: the response plus
    a ``text-only`` report. Valid: ``structured`` is the validated object and
    only that. Anything else raises :class:`OutputContractViolation` whose
    ``response`` keeps the raw text, usage and truthfulness fields, with
    ``status="error"``, ``structured=None`` and the report.
    """
    if plan is None:
        return response
    outcome = repair_and_evaluate_output(plan.contract, response.text)
    report = _report(plan, outcome)
    if outcome.disposition == DISPOSITION_TEXT_ONLY:
        return replace(response, output_contract=report)
    if outcome.disposition == DISPOSITION_VALID:
        return replace(response, structured=outcome.value, output_contract=report)
    detail = outcome.disposition
    if outcome.errors:
        detail += ": " + "; ".join(f"{p or '/'} {k}" for p, k in outcome.errors)
    failed = replace(
        response,
        status=ERROR,
        error=ResponseError(VIOLATION_ERROR_CODE, detail),
        structured=None,
        output_contract=report,
    )
    raise OutputContractViolation(failed)


__all__ = [
    # re-exported value types (defined in contract_types)
    "POLICY_NATIVE_REQUIRED",
    "POLICY_VALIDATED_RESULT",
    "POLICY_TEXT_ONLY",
    "POLICIES",
    "SCHEMA_POLICIES",
    "DELIVERY_NATIVE",
    "DELIVERY_PROMPT",
    "DELIVERY_NONE",
    "DELIVERIES",
    "DISPOSITION_VALID",
    "DISPOSITION_SCHEMA_MISMATCH",
    "DISPOSITION_UNPARSEABLE",
    "DISPOSITION_TEXT_ONLY",
    "DISPOSITIONS",
    "SCHEMA_INSTRUCTION_PREFIX",
    "SCHEMA_CLASS_SUBSET",
    "SCHEMA_CLASS_OPENAI_STRICT",
    "SCHEMA_CLASSES",
    "OutputContract",
    "ContractReport",
    "canonical_schema_json",
    "strict_schema_violations",
    # this module
    "LEGACY_SCHEMA_EXTRAS",
    "VIOLATION_ERROR_CODE",
    "OutputContractUnsatisfiable",
    "OutputContractViolation",
    "render_schema_instruction",
    "ContractOutcome",
    "evaluate_output",
    "repair_and_evaluate_output",
    "contract_requirements",
    "merge_requirements",
    "DeliveryPlan",
    "prepare_contract",
    "finalize_contract",
]
