"""The output-contract value types: what a caller declares, and what came back.

A LEAF module: it imports the standard library and :mod:`.json_schema` and
nothing else from this package. :mod:`.types` imports it at runtime so
``BackendOptions.output_contract`` and ``LLMResponse.output_contract`` resolve
under ``typing.get_type_hints``; a reverse edge from here to :mod:`.types`
would be an import cycle. ``test_contract_types_is_a_leaf_module`` pins that.

An :class:`OutputContract` is provider-independent. It names what a VALID
answer is and which of three policies applies:

- :data:`POLICY_NATIVE_REQUIRED` -- the schema must reach the target through a
  first-class schema channel, and the answer is validated.
- :data:`POLICY_VALIDATED_RESULT` -- the answer is validated; the schema may
  reach the target through any channel the adapter advertises.
- :data:`POLICY_TEXT_ONLY` -- an explicit declaration that the answer is text.

Which adapters can satisfy which policy is the adapter's own advertisement
(``StructuredOutputCapability.policies``); this module holds no adapter facts.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from .json_schema import check_schema, resolve_local_ref

POLICY_NATIVE_REQUIRED = "native-required"
POLICY_VALIDATED_RESULT = "validated-result"
POLICY_TEXT_ONLY = "text-only"
POLICIES = (POLICY_NATIVE_REQUIRED, POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY)
SCHEMA_POLICIES = (POLICY_NATIVE_REQUIRED, POLICY_VALIDATED_RESULT)

#: How the contract reached the target on one call.
DELIVERY_NATIVE = "native"
DELIVERY_PROMPT = "prompt"
DELIVERY_NONE = "none"
DELIVERIES = (DELIVERY_NATIVE, DELIVERY_PROMPT, DELIVERY_NONE)

#: What the answer was judged to be.
DISPOSITION_VALID = "valid"
DISPOSITION_SCHEMA_MISMATCH = "schema-mismatch"
DISPOSITION_UNPARSEABLE = "unparseable"
DISPOSITION_TEXT_ONLY = "text-only"
DISPOSITIONS = (
    DISPOSITION_VALID,
    DISPOSITION_SCHEMA_MISMATCH,
    DISPOSITION_UNPARSEABLE,
    DISPOSITION_TEXT_ONLY,
)

#: The fixed lead-in of a prompt-delivered schema instruction. The schema's
#: canonical JSON follows it directly, so the whole instruction is
#: deterministic ASCII a test can find byte for byte.
SCHEMA_INSTRUCTION_PREFIX = (
    "Respond with only one JSON value, with no prose and no code fence, that "
    "conforms to this JSON Schema:\n"
)

_NULL_ROOT_MESSAGE = "root schema may accept null; declare a non-null root type"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _require_json_native(value: Any, pointer: str = "") -> None:
    """Refuse any value ``json.dumps`` would silently normalize.

    ``json.dumps`` turns a tuple into a list and an int or bool key into a
    string, so serializing first and checking afterwards would accept a schema
    that is not the one the caller wrote. This walk runs BEFORE any
    serialization and names the JSON pointer of the first offending value.
    """
    where = pointer or "/"
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"schema value at {where} is not a finite number")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"schema object at {where} has a non-string key {key!r}"
                )
            token = key.replace("~", "~0").replace("/", "~1")
            _require_json_native(item, f"{pointer}/{token}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_native(item, f"{pointer}/{index}")
        return
    raise ValueError(
        f"schema value at {where} is a {type(value).__name__}, not a JSON value "
        "(dict with str keys, list, str, bool, int, finite float or None)"
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


def _excludes_null(schema: Mapping, root: Mapping, seen: frozenset = frozenset()) -> bool:
    """Whether ``schema`` refuses a ``null`` instance.

    The keywords at one level are conjunctive, so a level excludes null when
    ANY of its null-relevant constraints does: ``type`` without ``"null"``,
    ``const`` other than null, ``enum`` without null, a local ``$ref`` whose
    target excludes null, or an ``anyOf`` whose EVERY branch does. No other
    supported keyword applies to a null instance. Returns False when none of
    those constraints excludes null; raises :class:`ValueError` when the
    answer cannot be decided (a ``$ref`` cycle reached while deciding).
    """
    if "type" in schema:
        names = (schema["type"],) if isinstance(schema["type"], str) else schema["type"]
        if "null" not in names:
            return True
    if "const" in schema and schema["const"] is not None:
        return True
    if "enum" in schema and not any(member is None for member in schema["enum"]):
        return True
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            raise ValueError(
                f"cannot decide whether the root excludes null: $ref cycle at {ref}"
            )
        target = resolve_local_ref(root, ref)
        if _excludes_null(target, root, seen | {ref}):
            return True
    if "anyOf" in schema:
        if all(_excludes_null(branch, root, seen) for branch in schema["anyOf"]):
            return True
    return False


@dataclass(frozen=True)
class OutputContract:
    """What a valid answer is, and how strictly it must be delivered.

    - ``id`` -- a caller-stable name (``"cpk.summary"``).
    - ``policy`` -- one of :data:`POLICIES`.
    - ``schema`` -- a JSON Schema in the :mod:`.json_schema` subset; required
      for the two schema policies and forbidden for ``text-only``.
    - ``schema_version`` -- a caller LABEL. Defaults to the first 12 hex
      characters of ``schema_digest``.
    - ``schema_digest`` -- derived: the sha256 hex of the schema's canonical
      JSON (``null`` for ``text-only``). Independent of the label, so two
      schemas sharing one label stay distinguishable.

    Construction validates everything and raises :class:`ValueError`: the
    schema must be JSON-native (checked before any serialization), inside the
    supported subset, and its root must refuse ``null``. It is then deep-copied
    and stored read-only (nested ``MappingProxyType`` and tuples), so mutating
    the caller's dict afterwards cannot reach the contract.
    """

    id: str
    policy: str
    schema: Optional[Mapping[str, Any]] = field(default=None, hash=False)
    schema_version: Optional[str] = None
    schema_digest: str = field(init=False, default="")

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("output contract id must be a non-empty string")
        if self.policy not in POLICIES:
            raise ValueError(
                f"unknown output contract policy {self.policy!r}; expected one "
                f"of {', '.join(POLICIES)}"
            )
        if self.schema_version is not None and (
            not isinstance(self.schema_version, str) or not self.schema_version
        ):
            raise ValueError("schema_version must be a non-empty string when set")

        if self.policy == POLICY_TEXT_ONLY:
            if self.schema is not None:
                raise ValueError("a text-only output contract takes no schema")
            frozen = None
            canonical = _canonical_json(None)
        else:
            if self.schema is None:
                raise ValueError(f"a {self.policy} output contract requires a schema")
            if not isinstance(self.schema, dict):
                raise ValueError(
                    f"schema must be a dict, got {type(self.schema).__name__}"
                )
            _require_json_native(self.schema)
            copied = json.loads(json.dumps(self.schema, allow_nan=False))
            check_schema(copied)
            if not _excludes_null(copied, copied):
                raise ValueError(_NULL_ROOT_MESSAGE)
            frozen = _freeze(copied)
            canonical = _canonical_json(copied)

        digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
        object.__setattr__(self, "schema", frozen)
        object.__setattr__(self, "schema_digest", digest)
        if self.schema_version is None:
            object.__setattr__(self, "schema_version", digest[:12])

    def identity(self) -> Tuple[str, str, str, str]:
        """``(id, policy, schema_digest, schema_version)`` -- never the schema body.

        The digest keeps two schemas that share a label apart; the label is
        included too, because a record carrying the old label must not stand
        in for a contract that carries a new one.
        """
        return (self.id, self.policy, self.schema_digest, self.schema_version)

    def schema_json(self) -> Any:
        """A fresh plain-dict deep copy of the schema (None for text-only)."""
        return _thaw(self.schema)

    def to_json(self) -> dict:
        """The request form: a fresh plain dict, inverse of :meth:`from_json`."""
        result: dict = {
            "id": self.id,
            "policy": self.policy,
            "schema_version": self.schema_version,
        }
        if self.schema is not None:
            result["schema"] = _thaw(self.schema)
        return result

    @classmethod
    def from_json(cls, data: Any) -> "OutputContract":
        """Build a contract from its request form; :class:`ValueError` on any fault."""
        if not isinstance(data, Mapping):
            raise ValueError(
                f"output contract must be a JSON object, got {type(data).__name__}"
            )
        known = ("id", "policy", "schema", "schema_version")
        unknown = sorted(set(data) - set(known))
        if unknown:
            raise ValueError(
                "unknown output contract key(s): "
                + ", ".join(unknown)
                + "; known keys are "
                + ", ".join(known)
            )
        for required in ("id", "policy"):
            if required not in data:
                raise ValueError(f"output contract is missing {required!r}")
        return cls(
            id=data["id"],
            policy=data["policy"],
            schema=data.get("schema"),
            schema_version=data.get("schema_version"),
        )

    # The stored schema is a MappingProxyType, which neither pickles nor
    # deep-copies. The contract is immutable, so a copy may be itself, and a
    # pickle round-trips through the request form.
    def __copy__(self) -> "OutputContract":
        return self

    def __deepcopy__(self, memo: Any) -> "OutputContract":
        return self

    def __reduce__(self) -> Any:
        return (_contract_from_json, (self.to_json(),))


def _contract_from_json(data: Mapping) -> OutputContract:
    return OutputContract.from_json(data)


def canonical_schema_json(contract: OutputContract) -> str:
    """The contract schema's canonical JSON (``"null"`` for text-only).

    Sorted keys, compact separators, ASCII only -- the exact bytes the digest
    is computed over.
    """
    return _canonical_json(_thaw(contract.schema))


@dataclass(frozen=True)
class ContractReport:
    """What one call did with its output contract. Names, not magnitudes.

    - ``contract_id`` / ``schema_version`` / ``schema_digest`` / ``policy`` --
      copied from the contract, so a stored report identifies exactly which
      contract it judged.
    - ``delivery`` -- :data:`DELIVERY_NATIVE`, :data:`DELIVERY_PROMPT` or
      :data:`DELIVERY_NONE`: the channel the adapter used.
    - ``disposition`` -- one of :data:`DISPOSITIONS`.
    - ``errors`` -- sorted ``(json_pointer, keyword)`` pairs.
    """

    contract_id: str
    schema_version: str
    schema_digest: str
    policy: str
    delivery: str
    disposition: str
    errors: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.policy not in POLICIES:
            raise ValueError(f"unknown output contract policy {self.policy!r}")
        if self.delivery not in DELIVERIES:
            raise ValueError(f"unknown output contract delivery {self.delivery!r}")
        if self.disposition not in DISPOSITIONS:
            raise ValueError(
                f"unknown output contract disposition {self.disposition!r}"
            )
        object.__setattr__(
            self, "errors", tuple(sorted((str(p), str(k)) for p, k in self.errors))
        )

    def identity(self) -> Tuple[str, str, str, str]:
        """The judged contract's ``identity()``, for comparison against one."""
        return (self.contract_id, self.policy, self.schema_digest, self.schema_version)

    def to_json(self) -> dict:
        return {
            "contract_id": self.contract_id,
            "schema_version": self.schema_version,
            "schema_digest": self.schema_digest,
            "policy": self.policy,
            "delivery": self.delivery,
            "disposition": self.disposition,
            "errors": [[pointer, keyword] for pointer, keyword in self.errors],
        }


__all__ = [
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
    "OutputContract",
    "ContractReport",
    "canonical_schema_json",
]
