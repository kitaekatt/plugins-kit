"""Typed node contracts: the `provides` / `requires` artifact surface and its checks.

A script or openrouter step may PROVIDE one named artifact -- its `$OUT` file,
of a declared type -- and any step may REQUIRE artifacts earlier steps provide.
An artifact spec is exactly one of `schema: <name>` (a key of `schemas:`) or
`type: opaque-file`; a requirement may add `each: <bool>`.

This module owns:

- spec parsing (:func:`parse_artifact_spec`), called by ``model.Step.parse``;
- the document checks (:func:`analyze`), called at the end of
  ``WorkflowDoc._validate_cross_refs`` so ``--validate-only`` runs them too,
  and again by the compiler, which needs the resolved providers:
  1. spec cross-references (the schema name exists) and the ``artifacts``
     name reservation in contract documents;
  2. provider schema admissibility: every provider schema must construct
     llm-scripting-kit's ``OutputContract`` with policy ``validated-result``
     (inside its closed subset, JSON-native, non-null root) and its canonical
     JSON must fit :data:`NODE_SCHEMA_MAX_BYTES`, because it travels on the
     node's command line;
  3. duplicate providers; 4. missing providers; 5. provider-before-consumer
     order (a step requiring its own artifact is an order error);
  6. compatibility: an opaque-file requirement accepts any provider; a schema
     requirement accepts only a schema provider whose
     ``OutputContract.schema_digest`` equals its own; cardinality must match
     (``each: true`` iff the provider fans out with ``for_each``).

llm-scripting-kit is imported lazily, and only for a document that names at
least one schema artifact. An absent and a too-old library are diagnosed
apart. The one check that needs expression compilation -- a
``{{ artifacts.X }}`` use without a matching ``requires`` -- runs in the
compiler only, like the unknown ``steps.ID`` check.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from dataclasses import dataclass
from typing import Any, Optional

from .declarations import OUTPUT_CONTRACT_LSK
from .errors import WorkflowError

KIND_SCHEMA = "schema"
KIND_OPAQUE = "opaque-file"

#: A provider's cardinality: one result, or one per ``for_each`` item.
ONE = "one"
EACH = "each"

#: The canonical JSON of a provider schema travels on the node's command line.
NODE_SCHEMA_MAX_BYTES = 8192

#: The expression head a contract document reserves (stage ids and ``as`` names).
ARTIFACTS_HEAD = "artifacts"

_PROVIDES = "provides"
_REQUIRES = "requires"


@dataclass(frozen=True)
class ArtifactSpec:
    """One declared artifact: its kind, schema name (kind ``schema``), and ``each``."""

    kind: str
    schema: Optional[str] = None
    each: bool = False


@dataclass(frozen=True)
class Provision:
    """A resolved provider: the step that provides the artifact and its type."""

    name: str
    step_id: str
    index: int
    kind: str
    cardinality: str
    schema_name: Optional[str] = None
    schema_text: Optional[str] = None  # canonical JSON, emitted as a string literal
    digest: Optional[str] = None       # OutputContract.schema_digest


@dataclass(frozen=True)
class ContractPlan:
    """What the compiler needs: the providers, keyed by artifact name."""

    providers: dict

    def provision_of(self, step_id: str) -> Optional[Provision]:
        for prov in self.providers.values():
            if prov.step_id == step_id:
                return prov
        return None


# --------------------------------------------------------------------------- #
# spec parsing (step level)
# --------------------------------------------------------------------------- #
def parse_artifact_spec(raw: Any, where: str, role: str) -> ArtifactSpec:
    """Parse one spec under ``provides`` (role ``provides``) or ``requires``."""
    if not isinstance(raw, dict):
        raise WorkflowError(f"{where}: an artifact spec must be a mapping, got {type(raw).__name__}")
    allowed = {"schema", "type"} | ({"each"} if role == _REQUIRES else set())
    extra = sorted(set(raw) - allowed)
    if extra:
        hint = (
            " (`each` is only for requires; a provider's cardinality comes from its for_each)"
            if "each" in extra and role == _PROVIDES
            else ""
        )
        raise WorkflowError(
            f"{where}: unknown field(s) {extra}; allowed: {sorted(allowed)}{hint}"
        )
    has_schema = raw.get("schema") is not None
    has_type = raw.get("type") is not None
    if has_schema == has_type:
        raise WorkflowError(
            f"{where}: an artifact spec takes exactly one of `schema: <name>` or "
            "`type: opaque-file`"
        )
    each = raw.get("each", False)
    if not isinstance(each, bool):
        raise WorkflowError(f"{where}: 'each' must be a boolean, got {type(each).__name__}")
    if has_type:
        if raw["type"] != KIND_OPAQUE:
            raise WorkflowError(
                f"{where}: 'type' must be {KIND_OPAQUE!r} (the only artifact type), "
                f"got {raw['type']!r}; name a schema with `schema:` for typed JSON"
            )
        return ArtifactSpec(kind=KIND_OPAQUE, each=each)
    if not isinstance(raw["schema"], str):
        raise WorkflowError(
            f"{where}: 'schema' must name a block in `schemas:`, got {type(raw['schema']).__name__}"
        )
    return ArtifactSpec(kind=KIND_SCHEMA, schema=raw["schema"], each=each)


def is_contract_document(doc) -> bool:
    """True when any step declares ``provides`` or ``requires``."""
    return any(step.provides or step.requires for step in doc.steps)


def has_provides(doc) -> bool:
    return any(step.provides for step in doc.steps)


# --------------------------------------------------------------------------- #
# llm-scripting-kit (lazy)
# --------------------------------------------------------------------------- #
def _output_contract():
    """``(OutputContract, POLICY_VALIDATED_RESULT)``, probed; WorkflowError when unusable."""
    try:
        import llm_scripting_kit  # noqa: F401, PLC0415
    except ImportError as exc:
        raise WorkflowError(
            "a schema-typed artifact is checked at compile time with llm-scripting-kit's "
            "OutputContract, but llm_scripting_kit is not importable. Enable the "
            "llm-scripting-kit plugin and run the compiler with workflow-kit's venv interpreter "
            "(bootstrap links llm_scripting_kit onto it via the shared-libs .pth)."
        ) from exc
    reason = None
    try:
        from llm_scripting_kit.completion import (  # noqa: PLC0415
            POLICY_VALIDATED_RESULT,
            OutputContract,
        )
    except ImportError:
        reason = "no completion.OutputContract / POLICY_VALIDATED_RESULT"
    if reason is None and not callable(OutputContract):
        reason = "OutputContract is not callable"
    if reason is None:
        try:
            inspect.signature(OutputContract).bind(
                id="x", policy=POLICY_VALIDATED_RESULT, schema={}
            )
        except (TypeError, ValueError):
            reason = "OutputContract(id=, policy=, schema=) does not bind"
    if reason is None:
        try:
            fields = {f.name for f in dataclasses.fields(OutputContract)}
        except TypeError:
            fields = set()
        if "schema_digest" not in fields:
            reason = "OutputContract has no schema_digest"
    if reason is not None:
        raise WorkflowError(
            f"the linked llm_scripting_kit predates the output contract a schema-typed "
            f"artifact is checked with ({reason}); this needs llm-scripting-kit >= "
            f"{OUTPUT_CONTRACT_LSK}. Run `claude plugin update llm-scripting-kit@plugins-kit` "
            "and start a new session so bootstrap re-links the newer shared lib onto "
            "workflow-kit's venv."
        )
    return OutputContract, POLICY_VALIDATED_RESULT


def canonical_json(value: Any) -> str:
    """Sorted keys, compact separators, ASCII: the text ``schema_digest`` is computed over."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


class _Schemas:
    """Per-document cache: schema name -> (digest, canonical text) or the refusal reason."""

    def __init__(self, doc):
        self._doc = doc
        self._api = None
        self._cache = {}

    def resolve(self, name: str):
        """``(digest, text, None)`` when the schema constructs, else ``(None, None, reason)``."""
        if name not in self._cache:
            if self._api is None:
                self._api = _output_contract()
            output_contract, policy = self._api
            body = self._doc.schemas[name]
            try:
                contract = output_contract(
                    id=f"workflow-kit.artifact-schema.{name}", policy=policy, schema=body
                )
            except (TypeError, ValueError) as exc:
                self._cache[name] = (None, None, str(exc))
            else:
                self._cache[name] = (contract.schema_digest, canonical_json(body), None)
        return self._cache[name]


# --------------------------------------------------------------------------- #
# document checks
# --------------------------------------------------------------------------- #
def _step_where(step) -> str:
    return f"step {step.id!r}"


def _check_reserved_head(doc) -> None:
    """In a contract document, `artifacts` may not be a stage id or an `as` name."""
    for step in doc.steps:
        where = _step_where(step)
        pipe = step.pipeline
        if pipe is None:
            continue
        if pipe.as_ == ARTIFACTS_HEAD:
            raise WorkflowError(
                f"{where}.pipeline: 'as' must not be {ARTIFACTS_HEAD!r} in a document that "
                f"declares provides/requires ({{{{ {ARTIFACTS_HEAD}.NAME }}}} is the artifact "
                "expression head there); rename it"
            )
        for stage in pipe.stages:
            if stage.id == ARTIFACTS_HEAD:
                raise WorkflowError(
                    f"{where}.pipeline: stage id must not be {ARTIFACTS_HEAD!r} in a document "
                    f"that declares provides/requires ({{{{ {ARTIFACTS_HEAD}.NAME }}}} is the "
                    "artifact expression head there); rename it"
                )
            if stage.fan_out is not None and stage.fan_out.as_ == ARTIFACTS_HEAD:
                raise WorkflowError(
                    f"{where}.pipeline.stage {stage.id!r}.fan_out: 'as' must not be "
                    f"{ARTIFACTS_HEAD!r} in a document that declares provides/requires "
                    f"({{{{ {ARTIFACTS_HEAD}.NAME }}}} is the artifact expression head "
                    "there); rename it"
                )


def _check_schema_names(doc) -> None:
    names = set(doc.schemas)
    for step in doc.steps:
        for role in (_PROVIDES, _REQUIRES):
            for art, spec in getattr(step, role).items():
                if spec.kind == KIND_SCHEMA and spec.schema not in names:
                    raise WorkflowError(
                        f"{_step_where(step)}.{role}.{art}: unknown schema {spec.schema!r}; "
                        f"declared schemas: {sorted(names)}"
                    )


def analyze(doc) -> ContractPlan:
    """Run every compile-time contract check; return the resolved providers."""
    if not is_contract_document(doc):
        return ContractPlan(providers={})

    # 1. spec cross-references and the reserved head
    _check_schema_names(doc)
    _check_reserved_head(doc)

    needs_lsk = any(
        spec.kind == KIND_SCHEMA
        for step in doc.steps
        for block in (step.provides, step.requires)
        for spec in block.values()
    )
    schemas = _Schemas(doc) if needs_lsk else None

    # 2. provider schema admissibility (and every provider's resolved type)
    provisions = []
    for index, step in enumerate(doc.steps):
        for art, spec in step.provides.items():
            where = f"{_step_where(step)}.provides.{art}"
            digest = text = None
            if spec.kind == KIND_SCHEMA:
                digest, text, reason = schemas.resolve(spec.schema)
                if reason is not None:
                    raise WorkflowError(
                        f"{where}: schema {spec.schema!r} cannot type a node artifact: {reason}. "
                        "A provider schema must be inside llm-scripting-kit's validated-result "
                        "subset (for example no `pattern`, `format` or `oneOf`) and its root "
                        "must refuse null"
                    )
                size = len(text.encode("ascii"))
                if size > NODE_SCHEMA_MAX_BYTES:
                    raise WorkflowError(
                        f"{where}: schema {spec.schema!r} is {size} bytes of canonical JSON, over "
                        f"the {NODE_SCHEMA_MAX_BYTES}-byte limit for a node artifact schema (it "
                        "travels on the node's command line). Split the schema, or use "
                        "`type: opaque-file` and validate downstream"
                    )
            provisions.append(Provision(
                name=art, step_id=step.id, index=index, kind=spec.kind,
                cardinality=EACH if step.for_each is not None else ONE,
                schema_name=spec.schema, schema_text=text, digest=digest,
            ))

    # 3. duplicate providers
    providers = {}
    for prov in provisions:
        first = providers.get(prov.name)
        if first is not None:
            raise WorkflowError(
                f"artifact {prov.name!r} is provided by two steps, {first.step_id!r} and "
                f"{prov.step_id!r}; an artifact has exactly one provider"
            )
        providers[prov.name] = prov

    # 4. missing providers
    for step in doc.steps:
        for art in step.requires:
            if art not in providers:
                raise WorkflowError(
                    f"{_step_where(step)}.requires.{art}: no step provides artifact {art!r}; "
                    f"provided: {sorted(providers)}"
                )

    # 5. order: the provider runs strictly before the consumer (document order)
    for index, step in enumerate(doc.steps):
        for art in step.requires:
            prov = providers[art]
            if prov.index == index:
                raise WorkflowError(
                    f"{_step_where(step)}.requires.{art}: a step cannot require the artifact "
                    "it provides itself"
                )
            if prov.index > index:
                raise WorkflowError(
                    f"{_step_where(step)}.requires.{art}: provider step {prov.step_id!r} comes "
                    f"after consumer step {step.id!r}; steps run in document order, so move "
                    "the provider earlier"
                )

    # 6. compatibility: kind, schema digest, cardinality
    for step in doc.steps:
        for art, spec in step.requires.items():
            prov = providers[art]
            where = f"{_step_where(step)}.requires.{art}"
            if spec.kind == KIND_SCHEMA:
                if prov.kind != KIND_SCHEMA:
                    raise WorkflowError(
                        f"{where}: requires schema {spec.schema!r}, but provider step "
                        f"{prov.step_id!r} provides an opaque file; require "
                        "`type: opaque-file` or make the provider schema-typed"
                    )
                digest, _text, reason = schemas.resolve(spec.schema)
                if digest != prov.digest:
                    detail = (
                        f"schema {spec.schema!r} cannot type a node artifact: {reason}"
                        if reason is not None
                        else f"schema {spec.schema!r} differs from the provider's schema "
                        f"{prov.schema_name!r} (digest {digest[:12]} vs {prov.digest[:12]})"
                    )
                    raise WorkflowError(
                        f"{where}: incompatible with provider step {prov.step_id!r}: {detail}; "
                        "compatibility is schema digest equality -- name the same schema"
                    )
            want = EACH if spec.each else ONE
            if want != prov.cardinality:
                raise WorkflowError(
                    f"{where}: cardinality mismatch with provider step {prov.step_id!r}: the "
                    f"provider yields {'one file per for_each item' if prov.cardinality == EACH else 'one file'}, "
                    f"so the requirement needs `each: {'true' if prov.cardinality == EACH else 'false'}`"
                )

    return ContractPlan(providers=providers)
