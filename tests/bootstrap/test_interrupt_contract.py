"""Tests for bootstrap_lib.interrupt_contract -- the shared interrupt contract.

The module holds the store-independent rules of a durable interrupt: the
request shape, the field rules and limits, the canonical form, the decision
and outcome words, the replay test, the lapse rule and the resolution
document. It is stdlib-only, imports nothing from bootstrap_lib and no
plugin, and never imports the schema validator: a caller passes one in. The
validators here are stand-ins defined in this file, so no test imports
llm-scripting-kit.

Every expected value below is written out as a literal, never read back from
the module under test.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
import types
from pathlib import Path
from typing import Any, Callable

import pytest

from bootstrap_lib import interrupt_contract as ic


MODULE_PATH = Path(ic.__file__)
PLUGIN_DEV = MODULE_PATH.parent.parent / "skills" / "plugin-dev"

SUBSET = "llm-scripting-kit.json-schema-subset/v1"
JOB_KIT_REQUEST = "job-kit.interrupt-request/v1"
SHARED_REQUEST = "plugins-kit.interrupt-request/v1"

APPROVAL_SCHEMA = {
    "type": "object",
    "required": ["approved"],
    "properties": {"approved": {"const": True}},
}

_ABSENT = object()


class StubValidator:
    """A recording stand-in for the validator a caller passes in."""

    def __init__(
        self,
        subsets: Any = frozenset({SUBSET}),
        *,
        schema_error: str | None = None,
        errors: tuple = (),
    ) -> None:
        if subsets is not _ABSENT:
            self.SUPPORTED_SUBSETS = subsets
        self.calls: list[tuple] = []
        self._schema_error = schema_error
        self._errors = errors

    def check_schema(self, schema: Any, *, subset: Any = None) -> None:
        self.calls.append(("check_schema", schema, subset))
        if self._schema_error is not None:
            raise ValueError(self._schema_error)

    def validate(self, schema: Any, value: Any, *, subset: Any = None) -> tuple:
        self.calls.append(("validate", schema, value, subset))
        return self._errors


def _check(**overrides: Any) -> dict:
    """``check_request`` for a valid job-kit request, with overrides."""
    kwargs: dict[str, Any] = {
        "envelope": JOB_KIT_REQUEST,
        "kind": "approval",
        "request_schema": dict(APPROVAL_SCHEMA),
        "payload": {"target": "v1.2.0"},
        "accepted_envelopes": frozenset({JOB_KIT_REQUEST}),
        "owner": "job-kit",
        "validator": StubValidator(),
    }
    kwargs.update(overrides)
    return ic.check_request(**kwargs)


def _mapping(raw: Any, **overrides: Any) -> dict:
    kwargs: dict[str, Any] = {
        "accepted_envelopes": frozenset({JOB_KIT_REQUEST}),
        "owner": "job-kit",
        "validator": StubValidator(),
    }
    kwargs.update(overrides)
    return ic.check_request_mapping(raw, **kwargs)


def _document(data: bytes, **overrides: Any) -> dict:
    kwargs: dict[str, Any] = {
        "accepted_envelopes": frozenset({JOB_KIT_REQUEST}),
        "owner": "job-kit",
        "validator": StubValidator(),
    }
    kwargs.update(overrides)
    return ic.parse_request_document(data, **kwargs)


def _raw(**overrides: Any) -> dict:
    """A valid request mapping, with top-level overrides."""
    value: dict[str, Any] = {
        "schema": JOB_KIT_REQUEST,
        "kind": "approval",
        "request_schema": dict(APPROVAL_SCHEMA),
        "payload": {"target": "v1.2.0"},
    }
    value.update(overrides)
    return value


def _refusal(exc_type: type, call: Callable[[], Any]) -> BaseException:
    """The exception ``call`` raises, which must be exactly ``exc_type``."""
    with pytest.raises(exc_type) as caught:
        call()
    assert type(caught.value) is exc_type
    return caught.value


# --------------------------------------------------------------------------
# Frozen literals, limits and words
# --------------------------------------------------------------------------


def test_contract_v1_literal_is_frozen() -> None:
    assert ic.CONTRACT_V1 == "plugins-kit.interrupt-contract/v1"


def test_supported_contracts_is_v1() -> None:
    assert ic.SUPPORTED_CONTRACTS == frozenset({"plugins-kit.interrupt-contract/v1"})
    assert isinstance(ic.SUPPORTED_CONTRACTS, frozenset)


def test_shared_envelope_literals_are_frozen() -> None:
    assert ic.REQUEST_ENVELOPE_V1 == "plugins-kit.interrupt-request/v1"
    assert ic.RESOLUTION_ENVELOPE_V1 == "plugins-kit.interrupt-resolution/v1"


def test_validator_subset_literal_is_frozen() -> None:
    assert ic.VALIDATOR_SUBSET == "llm-scripting-kit.json-schema-subset/v1"
    assert ic.VALIDATOR_MODULE == "llm_scripting_kit.completion.json_schema"


def test_limits_are_the_documented_values() -> None:
    assert ic.REQUEST_DOCUMENT_LIMIT == 131072
    assert ic.INPUT_LIMIT == 65536
    assert ic.KIND_LIMIT == 64
    assert ic.EXPIRES_IN_S_MAX == 2147483647
    assert ic.REASON_LIMIT == 2000


def test_words_and_key_sets_are_the_documented_values() -> None:
    assert ic.OWNER == "bootstrap@plugins-kit"
    assert ic.REQUEST_KEYS == ("schema", "kind", "request_schema", "payload")
    assert ic.OPTIONAL_REQUEST_KEYS == ("expires_in_s",)
    assert ic.OUTCOMES == frozenset({"answered", "rejected", "expired"})
    assert ic.DECISIONS == (("answer", "answered"), ("reject", "rejected"))


# --------------------------------------------------------------------------
# Module boundary
# --------------------------------------------------------------------------


def _imports() -> tuple[set[str], list[int]]:
    """Every absolute dotted import name in the module, and relative-import lines."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    relative: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.append(node.lineno)
            elif node.module:
                imported.add(node.module)
    return imported, relative


def test_module_imports_stdlib_only() -> None:
    imported, _relative = _imports()
    roots = {name.split(".")[0] for name in imported}
    assert roots <= set(sys.stdlib_module_names), roots - set(sys.stdlib_module_names)


def test_module_imports_no_bootstrap_lib_and_no_plugin() -> None:
    imported, relative = _imports()
    roots = {name.split(".")[0] for name in imported}
    assert not relative, f"relative imports at lines {relative}"
    first_party = {
        "bootstrap_lib",
        "content_pipeline",
        "hue_kit",
        "job_kit",
        "llm_scripting_kit",
        "secrets_kit",
        "workflow_kit_lib",
    }
    assert not roots & first_party, roots & first_party


def test_validator_is_injected_not_imported() -> None:
    imported, _relative = _imports()
    roots = {name.split(".")[0] for name in imported}
    assert "llm_scripting_kit" not in roots
    # No dynamic import either: the name in VALIDATOR_MODULE is only a name.
    assert "importlib" not in roots
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "__import__" not in names
    before = {name for name in sys.modules if name.startswith("llm_scripting_kit")}
    validator = StubValidator()
    _check(validator=validator)
    ic.validate_input(APPROVAL_SCHEMA, {"approved": True}, validator=validator)
    after = {name for name in sys.modules if name.startswith("llm_scripting_kit")}
    assert after == before
    assert [call[0] for call in validator.calls] == ["check_schema", "validate"]


def test_public_surface_is_the_documented_one() -> None:
    documented = [
        "OWNER", "CONTRACT_V1", "SUPPORTED_CONTRACTS", "REQUEST_ENVELOPE_V1",
        "RESOLUTION_ENVELOPE_V1", "VALIDATOR_MODULE", "VALIDATOR_SUBSET",
        "REQUEST_KEYS", "OPTIONAL_REQUEST_KEYS", "REQUEST_DOCUMENT_LIMIT",
        "INPUT_LIMIT", "KIND_LIMIT", "EXPIRES_IN_S_MAX", "REASON_LIMIT",
        "OUTCOMES", "DECISIONS",
        "ContractError", "RequestError", "InputError", "DecisionError",
        "ValidatorError",
        "check_validator", "canonical_json", "check_request",
        "check_request_mapping", "parse_request_document", "validate_input",
        "expiry", "lapsed", "bound_reason", "decision_outcome",
        "same_resolution", "resolution_document",
    ]
    assert sorted(ic.__all__) == sorted(documented)
    # Nothing public is defined outside __all__: the module holds no event
    # function (event phases belong to each store), and no record type.
    defined = {
        name
        for name, value in vars(ic).items()
        if not name.startswith("_")
        and (
            name.isupper()
            or (
                (inspect.isfunction(value) or inspect.isclass(value))
                and value.__module__ == ic.__name__
            )
        )
    }
    assert defined == set(documented)
    assert not hasattr(ic, "event_phase")
    assert not [name for name in vars(ic) if "event" in name.lower()]
    assert issubclass(ic.ContractError, ValueError)
    for name in ("RequestError", "InputError", "DecisionError", "ValidatorError"):
        assert issubclass(getattr(ic, name), ic.ContractError), name
    assert ic.InputError("x").errors == ()


# --------------------------------------------------------------------------
# The validator is held to a named subset, not to its signatures
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda validator: _check(validator=validator), id="check_request"),
        pytest.param(
            lambda validator: ic.validate_input(
                APPROVAL_SCHEMA, {"approved": True}, validator=validator
            ),
            id="validate_input",
        ),
    ],
)
def test_validator_without_subset_marker_is_refused(call: Callable[[Any], Any]) -> None:
    validator = StubValidator(_ABSENT)
    _refusal(ic.ValidatorError, lambda: call(validator))
    assert validator.calls == []


def test_validator_advertising_another_subset_is_refused() -> None:
    other = StubValidator(frozenset({"llm-scripting-kit.json-schema-subset/v2"}))
    _refusal(ic.ValidatorError, lambda: ic.check_validator(other))
    _refusal(ic.ValidatorError, lambda: _check(validator=other))
    assert other.calls == []
    # A bare string is not a collection: substring membership must not pass.
    text_marker = StubValidator("llm-scripting-kit.json-schema-subset/v1 and more")
    _refusal(ic.ValidatorError, lambda: ic.check_validator(text_marker))
    # The literal itself, in any supported collection, is accepted.
    for subsets in (frozenset({SUBSET}), {SUBSET, "x/v2"}, (SUBSET,), [SUBSET]):
        assert ic.check_validator(StubValidator(subsets)) is None


def test_signature_compatible_validator_is_not_enough() -> None:
    calls: list[str] = []

    def check_schema(schema: Any, *, subset: Any = None) -> None:
        calls.append("check_schema")

    def validate(schema: Any, value: Any, *, subset: Any = None) -> tuple:
        calls.append("validate")
        return ()

    unmarked = types.SimpleNamespace(check_schema=check_schema, validate=validate)
    _refusal(ic.ValidatorError, lambda: ic.check_validator(unmarked))
    _refusal(ic.ValidatorError, lambda: _check(validator=unmarked))
    _refusal(
        ic.ValidatorError,
        lambda: ic.validate_input(APPROVAL_SCHEMA, {"approved": True}, validator=unmarked),
    )
    assert calls == []
    # The marker alone is not enough either: both functions must be callable.
    marked = types.SimpleNamespace(
        SUPPORTED_SUBSETS=frozenset({SUBSET}), check_schema=check_schema, validate=None
    )
    error = _refusal(ic.ValidatorError, lambda: ic.check_validator(marked))
    assert str(error) == (
        "interrupt contract plugins-kit.interrupt-contract/v1 requires a schema "
        "validator with callable check_schema and validate; this validator lacks "
        "a callable validate"
    )


def test_validator_refusal_names_required_and_advertised_subsets() -> None:
    required = (
        "interrupt contract plugins-kit.interrupt-contract/v1 requires a schema "
        "validator that advertises 'llm-scripting-kit.json-schema-subset/v1' in "
        "SUPPORTED_SUBSETS; this validator "
    )
    missing = _refusal(ic.ValidatorError, lambda: ic.check_validator(StubValidator(_ABSENT)))
    assert str(missing) == required + "has no SUPPORTED_SUBSETS collection"
    other = _refusal(
        ic.ValidatorError,
        lambda: ic.check_validator(StubValidator(frozenset({"b.subset/v2", "a.subset/v9"}))),
    )
    assert str(other) == required + "advertises 'a.subset/v9', 'b.subset/v2'"
    empty = _refusal(ic.ValidatorError, lambda: ic.check_validator(StubValidator(frozenset())))
    assert str(empty) == required + "advertises nothing"


def test_validator_is_checked_before_the_schema_is_read() -> None:
    validator = StubValidator(frozenset({"other.subset/v1"}))
    _refusal(ic.ValidatorError, lambda: _check(validator=validator))
    assert validator.calls == []
    # It also precedes every field rule: an invalid request under an unusable
    # validator reports the validator.
    _refusal(ic.ValidatorError, lambda: _check(validator=validator, kind="NOT A KIND"))
    assert validator.calls == []


@pytest.mark.parametrize("function", ["check_schema", "validate"])
def test_validator_calls_carry_the_subset(function: str) -> None:
    validator = StubValidator()
    if function == "check_schema":
        _check(validator=validator, request_schema={"type": "object"})
        assert validator.calls == [
            ("check_schema", {"type": "object"}, "llm-scripting-kit.json-schema-subset/v1")
        ]
    else:
        ic.validate_input({"type": "object"}, {"a": 1}, validator=validator)
        assert validator.calls == [
            ("validate", {"type": "object"}, {"a": 1}, "llm-scripting-kit.json-schema-subset/v1")
        ]


# --------------------------------------------------------------------------
# Canonical JSON
# --------------------------------------------------------------------------


def test_canonical_json_ignores_key_order() -> None:
    assert ic.canonical_json({"b": [1, {"d": 1, "c": 2}], "a": None}) == (
        '{"a":null,"b":[1,{"c":2,"d":1}]}'
    )
    assert ic.canonical_json({"a": None, "b": [1, {"c": 2, "d": 1}]}) == (
        '{"a":null,"b":[1,{"c":2,"d":1}]}'
    )
    assert ic.canonical_json({"s": chr(0xE9)}) == '{"s":"\\u00e9"}'


def test_canonical_json_refuses_nan_and_infinity() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            ic.canonical_json({"x": value})


# --------------------------------------------------------------------------
# Request fields
# --------------------------------------------------------------------------


def test_request_refuses_a_literal_outside_the_accepted_set() -> None:
    assert _check()["envelope"] == "job-kit.interrupt-request/v1"
    _refusal(ic.RequestError, lambda: _check(envelope=SHARED_REQUEST))
    _refusal(ic.RequestError, lambda: _check(envelope="job-kit.interrupt-request/v2"))
    _refusal(ic.RequestError, lambda: _check(envelope=7))
    _refusal(ic.RequestError, lambda: _check(envelope=None))
    # The converse store: it accepts the shared literal and refuses job-kit's.
    shared = frozenset({SHARED_REQUEST})
    assert _check(envelope=SHARED_REQUEST, accepted_envelopes=shared)["envelope"] == (
        "plugins-kit.interrupt-request/v1"
    )
    _refusal(ic.RequestError, lambda: _check(accepted_envelopes=shared))
    # A bare string would turn membership into a substring test.
    with pytest.raises(TypeError):
        _check(envelope="job-kit", accepted_envelopes=JOB_KIT_REQUEST)


def test_envelope_refusal_names_the_owner_and_its_literals() -> None:
    error = _refusal(
        ic.RequestError,
        lambda: _check(
            envelope="x.interrupt-request/v9",
            owner="content-pipeline-kit",
            accepted_envelopes=frozenset({SHARED_REQUEST, "a.interrupt-request/v1"}),
        ),
    )
    assert str(error) == (
        "interrupt request schema 'x.interrupt-request/v9' is not accepted; this "
        "content-pipeline-kit accepts 'a.interrupt-request/v1', "
        "'plugins-kit.interrupt-request/v1'"
    )


@pytest.mark.parametrize(
    "kind, shown",
    [
        pytest.param("Approval", "'Approval'", id="pattern"),
        pytest.param("a" * 65, repr("a" * 65), id="length"),
        pytest.param(7, "7", id="type"),
    ],
)
def test_request_kind_is_validated(kind: Any, shown: str) -> None:
    error = _refusal(ic.RequestError, lambda: _check(kind=kind))
    assert str(error) == (
        "interrupt kind must match [a-z][a-z0-9-]* and be at most 64 characters, "
        f"got {shown}"
    )
    assert _check(kind="a" * 64)["kind"] == "a" * 64
    assert _check(kind="needs-input-2")["kind"] == "needs-input-2"


def test_request_schema_and_payload_must_be_objects() -> None:
    for value in ([], "text", None, 7):
        error = _refusal(ic.RequestError, lambda: _check(request_schema=value))
        assert str(error) == "interrupt request_schema must be a JSON object"
        error = _refusal(ic.RequestError, lambda: _check(payload=value))
        assert str(error) == "interrupt payload must be a JSON object"


def test_request_values_must_be_json_native() -> None:
    cases = [
        ({"payload": {"x": float("nan")}}, "interrupt payload is not JSON-native: /x: nan is not finite"),
        (
            {"request_schema": {"enum": ("a", "b")}},
            "interrupt request_schema is not JSON-native: /enum: tuple is not a JSON value",
        ),
        ({"payload": {1: "x"}}, "interrupt payload is not JSON-native: /: key 1 is not a string"),
        (
            {"payload": {"a/b": [0, {"c~d": {1, 2}}]}},
            "interrupt payload is not JSON-native: /a~1b/1/c~0d: set is not a JSON value",
        ),
    ]
    for overrides, message in cases:
        validator = StubValidator()
        error = _refusal(ic.RequestError, lambda: _check(validator=validator, **overrides))
        assert str(error) == message
        assert validator.calls == []


@pytest.mark.parametrize(
    "value, shown",
    [
        pytest.param(0, "0", id="zero"),
        pytest.param(-1, "-1", id="negative"),
        pytest.param(True, "True", id="bool"),
        pytest.param(1.5, "1.5", id="float"),
        pytest.param(2147483648, "2147483648", id="too_large"),
    ],
)
def test_request_expiry_range(value: Any, shown: str) -> None:
    error = _refusal(ic.RequestError, lambda: _check(expires_in_s=value))
    assert str(error) == (
        f"interrupt expires_in_s must be an int from 1 to 2147483647, got {shown}"
    )
    assert _check(expires_in_s=1)["expires_in_s"] == 1
    assert _check(expires_in_s=2147483647)["expires_in_s"] == 2147483647
    assert _check()["expires_in_s"] is None


def test_request_schema_is_checked_by_the_validator() -> None:
    validator = StubValidator(schema_error="unsupported keyword 'pattern'")
    error = _refusal(
        ic.RequestError,
        lambda: _check(validator=validator, request_schema={"pattern": "^a"}),
    )
    assert str(error) == (
        "interrupt request_schema is outside the supported JSON Schema subset: "
        "unsupported keyword 'pattern'"
    )
    assert isinstance(error.__cause__, ValueError)
    assert [call[:2] for call in validator.calls] == [("check_schema", {"pattern": "^a"})]
    accepting = StubValidator()
    _check(validator=accepting)
    assert [call[0] for call in accepting.calls] == ["check_schema"]


def test_request_returns_canonical_copies() -> None:
    schema = {"type": "object", "properties": {"b": {"type": "string"}, "a": {}}}
    payload = {"z": [1, {"y": 2, "x": 3}], "a": "v"}
    result = _check(request_schema=schema, payload=payload, expires_in_s=30)
    assert result == {
        "envelope": "job-kit.interrupt-request/v1",
        "kind": "approval",
        "request_schema": {"type": "object", "properties": {"b": {"type": "string"}, "a": {}}},
        "payload": {"z": [1, {"y": 2, "x": 3}], "a": "v"},
        "expires_in_s": 30,
    }
    assert list(result) == ["envelope", "kind", "request_schema", "payload", "expires_in_s"]
    assert result["request_schema"] is not schema
    assert result["payload"] is not payload
    # Deep copies: changing what the caller passed leaves the result alone.
    schema["properties"]["b"]["type"] = "number"
    payload["z"][1]["y"] = 99
    assert result["request_schema"]["properties"]["b"] == {"type": "string"}
    assert result["payload"]["z"][1] == {"x": 3, "y": 2}
    # Canonical: keys come back sorted at every depth.
    assert list(result["payload"]) == ["a", "z"]
    assert list(result["payload"]["z"][1]) == ["x", "y"]
    assert list(result["request_schema"]) == ["properties", "type"]


# --------------------------------------------------------------------------
# Request mappings and documents
# --------------------------------------------------------------------------


def test_request_mapping_refuses_unknown_and_missing_keys() -> None:
    assert _mapping(_raw(expires_in_s=60)) == {
        "envelope": "job-kit.interrupt-request/v1",
        "kind": "approval",
        "request_schema": APPROVAL_SCHEMA,
        "payload": {"target": "v1.2.0"},
        "expires_in_s": 60,
    }
    assert _mapping(_raw())["expires_in_s"] is None
    error = _refusal(ic.RequestError, lambda: _mapping(_raw(zeta=1, extra=2)))
    assert str(error) == (
        "interrupt request has unknown keys ['extra', 'zeta']; allowed: schema, "
        "kind, request_schema, payload, expires_in_s"
    )
    partial = _raw()
    del partial["payload"]
    del partial["kind"]
    error = _refusal(ic.RequestError, lambda: _mapping(partial))
    assert str(error) == "interrupt request is missing keys ['kind', 'payload']"
    # The key set is judged before any field, and before the validator.
    validator = StubValidator(_ABSENT)
    error = _refusal(ic.RequestError, lambda: _mapping(_raw(extra=1), validator=validator))
    assert str(error).startswith("interrupt request has unknown keys ['extra']")
    for value in ([], "text", None):
        error = _refusal(ic.RequestError, lambda: _mapping(value))
        assert str(error) == "interrupt request must be a JSON object"


def _document_bytes(size: int) -> bytes:
    """A valid request document padded with trailing spaces to ``size`` bytes."""
    body = json.dumps(_raw()).encode("utf-8")
    assert len(body) < size
    return body + b" " * (size - len(body))


def test_document_over_cap_is_refused() -> None:
    assert _document(_document_bytes(131072))["kind"] == "approval"
    validator = StubValidator()
    error = _refusal(
        ic.RequestError, lambda: _document(_document_bytes(131073), validator=validator)
    )
    assert str(error) == "interrupt request file is larger than 131072 bytes"
    assert validator.calls == []


@pytest.mark.parametrize(
    "data, message",
    [
        pytest.param(
            b"\xff",
            "interrupt request file is not UTF-8: 'utf-8' codec can't decode byte "
            "0xff in position 0: invalid start byte",
            id="utf8",
        ),
        pytest.param(
            b"nope",
            "interrupt request file is not JSON: Expecting value: line 1 column 1 (char 0)",
            id="json",
        ),
        pytest.param(
            b'{"schema": "job-kit.interrupt-request/v1", "kind": "approval", '
            b'"request_schema": {}, "payload": {"x": NaN}}',
            "interrupt request holds NaN, which is not JSON",
            id="nan",
        ),
        pytest.param(
            b'{"schema": "job-kit.interrupt-request/v1", "kind": "approval", '
            b'"request_schema": {}, "payload": {"x": 1, "x": 2}}',
            "interrupt request repeats the key 'x'",
            id="repeated_key",
        ),
        pytest.param(b"[]", "interrupt request must be a JSON object", id="not_object"),
    ],
)
def test_document_faults(data: bytes, message: str) -> None:
    error = _refusal(ic.RequestError, lambda: _document(data))
    assert str(error) == message


def test_document_parses_a_valid_request() -> None:
    data = (
        b'{"schema": "job-kit.interrupt-request/v1", "kind": "approval", '
        b'"request_schema": {"type": "object"}, "payload": {"b": 1, "a": 2}, '
        b'"expires_in_s": 3600}'
    )
    assert _document(data) == {
        "envelope": "job-kit.interrupt-request/v1",
        "kind": "approval",
        "request_schema": {"type": "object"},
        "payload": {"a": 2, "b": 1},
        "expires_in_s": 3600,
    }
    for constant in (b"Infinity", b"-Infinity"):
        error = _refusal(
            ic.RequestError, lambda: _document(data.replace(b"3600", constant))
        )
        assert str(error) == (
            f"interrupt request holds {constant.decode()}, which is not JSON"
        )


# --------------------------------------------------------------------------
# Resolution input
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, message",
    [
        pytest.param(
            {"approved": {1, 2}},
            "resolution input is not JSON-native: /approved: set is not a JSON value",
            id="non_native",
        ),
        pytest.param(
            "x" * 65535,
            "resolution input is 65537 bytes as canonical JSON; the cap is 65536",
            id="oversized",
        ),
    ],
)
def test_input_refused_before_validation(value: Any, message: str) -> None:
    validator = StubValidator(errors=(("", "type"),))
    error = _refusal(
        ic.InputError, lambda: ic.validate_input(APPROVAL_SCHEMA, value, validator=validator)
    )
    assert str(error) == message
    assert error.errors == ()
    assert validator.calls == []
    # One byte under: exactly at the cap is accepted.
    accepting = StubValidator()
    text = ic.validate_input({}, "x" * 65534, validator=accepting)
    assert len(text) == 65536


def test_input_errors_are_the_validators_tuples() -> None:
    reported = (("/approved", "const"), ("", "required"), ("/approved", "const"))
    validator = StubValidator(errors=reported)
    error = _refusal(
        ic.InputError,
        lambda: ic.validate_input(APPROVAL_SCHEMA, {"approved": False}, validator=validator),
    )
    assert str(error) == "resolution input does not satisfy the request schema"
    assert error.errors == (("/approved", "const"), ("", "required"), ("/approved", "const"))
    assert validator.calls == [
        ("validate", APPROVAL_SCHEMA, {"approved": False}, "llm-scripting-kit.json-schema-subset/v1")
    ]


def test_input_returns_canonical_text() -> None:
    text = ic.validate_input(
        APPROVAL_SCHEMA, {"note": chr(0xE9), "approved": True}, validator=StubValidator()
    )
    assert text == '{"approved":true,"note":"\\u00e9"}'
    assert ic.validate_input({}, None, validator=StubValidator()) == "null"


# --------------------------------------------------------------------------
# Expiry, reason, decisions, replay
# --------------------------------------------------------------------------


def test_expiry_is_created_plus_seconds() -> None:
    assert ic.expiry(1000.5, 30) == 1030.5
    assert ic.expiry(1790762400.0, 3600) == 1790766000.0
    assert ic.expiry(1000.5, None) is None


def test_lapse_is_inclusive_at_expires_at() -> None:
    assert ic.lapsed(1030.5, 1030.5) is True
    assert ic.lapsed(1030.5, 1030.75) is True
    assert ic.lapsed(1030.5, 1030.25) is False


def test_no_expiry_never_lapses() -> None:
    assert ic.lapsed(None, 0.0) is False
    assert ic.lapsed(None, 1e18) is False


def test_decision_outcome_maps_answer_and_reject() -> None:
    assert ic.decision_outcome("answer", input={"approved": True}) == "answered"
    assert ic.decision_outcome("answer") == "answered"
    assert ic.decision_outcome("reject", reason="not now") == "rejected"
    assert ic.decision_outcome("reject") == "rejected"
    error = _refusal(ic.DecisionError, lambda: ic.decision_outcome("approve"))
    assert str(error) == "decision must be one of answer, reject, got 'approve'"
    for word in ("answered", "expired", "expire", "", None, ["answer"]):
        _refusal(ic.DecisionError, lambda: ic.decision_outcome(word))


@pytest.mark.parametrize(
    "call, message",
    [
        pytest.param(
            lambda: ic.decision_outcome("reject", input={"approved": True}),
            "a rejection carries a reason, not an input",
            id="reject_with_input",
        ),
        pytest.param(
            lambda: ic.decision_outcome("answer", input={"approved": True}, reason="why"),
            "an answer carries an input, not a reason",
            id="answer_with_reason",
        ),
    ],
)
def test_decision_refuses_crossed_arguments(call: Callable[[], Any], message: str) -> None:
    error = _refusal(ic.DecisionError, call)
    assert str(error) == message


def _same(**overrides: Any) -> bool:
    kwargs: dict[str, Any] = {
        "stored_outcome": "answered",
        "stored_input_json": '{"a":1,"b":2}',
        "stored_reason": None,
        "outcome": "answered",
        "input": {"b": 2, "a": 1},
        "reason": None,
    }
    kwargs.update(overrides)
    return ic.same_resolution(**kwargs)


def test_same_resolution_ignores_key_order() -> None:
    assert _same() is True
    assert _same(input={"a": 1, "b": 2}) is True
    assert _same(input={"a": 1, "b": 3}) is False
    assert _same(input={"a": 1}) is False
    # A value with no canonical form is a conflict, not an error.
    assert _same(input={"a": {1, 2}}) is False
    assert _same(input={"a": float("nan")}) is False


def test_same_resolution_bounds_the_reason() -> None:
    rejected = {
        "stored_outcome": "rejected",
        "stored_input_json": None,
        "outcome": "rejected",
        "input": None,
    }
    assert _same(**rejected, stored_reason="r" * 2000, reason="r" * 2500) is True
    assert _same(**rejected, stored_reason="r" * 2000, reason="r" * 1999) is False
    assert _same(**rejected, stored_reason="not now", reason="not now") is True
    assert _same(**rejected, stored_reason="not now", reason="later") is False
    assert _same(**rejected, stored_reason=None, reason=None) is True
    assert _same(**rejected, stored_reason=None, reason="") is False
    assert _same(**rejected, stored_reason="7", reason=7) is True


def test_same_resolution_refuses_other_outcome() -> None:
    # Stored answered; a rejection with no reason must not replay it.
    assert _same(stored_input_json="null", outcome="rejected", input=None, reason=None) is False
    # Stored rejected; an answer whose text equals the stored text must not.
    assert (
        _same(stored_outcome="rejected", stored_input_json="null", outcome="answered", input=None)
        is False
    )
    # Stored expired; neither decision replays it.
    assert _same(stored_outcome="expired", stored_input_json=None, outcome="rejected") is False


def test_bound_reason_cuts_at_the_limit() -> None:
    assert ic.bound_reason("x" * 2001) == "x" * 2000
    assert ic.bound_reason("x" * 2000) == "x" * 2000
    assert ic.bound_reason("short") == "short"
    assert ic.bound_reason("") == ""
    assert ic.bound_reason(None) is None
    assert ic.bound_reason(5) == "5"


# --------------------------------------------------------------------------
# Resolution document
# --------------------------------------------------------------------------


def _resolution(**overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "resolution_envelope": "job-kit.interrupt-resolution/v1",
        "interrupt_id": "7",
        "kind": "approval",
        "outcome": "answered",
        "input": {"approved": True},
        "payload": {"target": "v1.2.0", "action": "push release tag"},
        "resolved_at": 1790762400.0,
    }
    kwargs.update(overrides)
    return ic.resolution_document(**kwargs)


def test_resolution_document_bytes_are_fixed() -> None:
    # The line job-kit wrote at dev HEAD 959811c2 for the same recorded row.
    assert _resolution() == (
        '{"input":{"approved":true},"interrupt_id":"7","kind":"approval",'
        '"outcome":"answered","payload":{"action":"push release tag",'
        '"target":"v1.2.0"},"resolved_at":"2026-09-30T10:00:00Z",'
        '"schema":"job-kit.interrupt-resolution/v1"}'
    )
    assert _resolution() == _resolution()
    assert _resolution(
        resolution_envelope="plugins-kit.interrupt-resolution/v1",
        outcome="rejected",
        input=None,
        payload={},
    ) == (
        '{"input":null,"interrupt_id":"7","kind":"approval","outcome":"rejected",'
        '"payload":{},"resolved_at":"2026-09-30T10:00:00Z",'
        '"schema":"plugins-kit.interrupt-resolution/v1"}'
    )


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param("", id="empty"),
        pytest.param("plugins-kit.interrupt-request/v1", id="request_literal"),
        pytest.param("plugins-kit.interrupt-resolution/v2", id="other_version"),
        pytest.param(7, id="non_string"),
    ],
)
def test_resolution_document_refuses_a_non_resolution_envelope(envelope: Any) -> None:
    error = _refusal(ic.RequestError, lambda: _resolution(resolution_envelope=envelope))
    assert str(error) == (
        f"interrupt resolution schema {envelope!r} is not a v1 resolution literal: "
        "<name>.interrupt-resolution/v1, where <name> matches [a-z][a-z0-9-]*"
    )
    for near_miss in (
        "Job-Kit.interrupt-resolution/v1",
        "job-kit.interrupt-resolution/v1 ",
        "x job-kit.interrupt-resolution/v1",
        ".interrupt-resolution/v1",
        "job-kitXinterrupt-resolution/v1",
    ):
        _refusal(ic.RequestError, lambda: _resolution(resolution_envelope=near_miss))


def test_resolution_document_refuses_an_unknown_outcome() -> None:
    for outcome in ("answer", "resolved", "", None, 1):
        error = _refusal(ic.ContractError, lambda: _resolution(outcome=outcome))
        assert str(error) == (
            f"interrupt outcome must be one of answered, expired, rejected, got {outcome!r}"
        )
    for outcome in ("answered", "rejected", "expired"):
        assert json.loads(_resolution(outcome=outcome))["outcome"] == outcome


@pytest.mark.parametrize(
    "epoch, text",
    [
        pytest.param(1790762400.0, "2026-09-30T10:00:00Z", id="whole"),
        pytest.param(1790762400.5, "2026-09-30T10:00:00.500000Z", id="fraction"),
    ],
)
def test_resolution_document_renders_utc_z(epoch: float, text: str) -> None:
    assert json.loads(_resolution(resolved_at=epoch))["resolved_at"] == text


# --------------------------------------------------------------------------
# Message text: job-kit's wording at dev HEAD 959811c2, under owner="job-kit"
# --------------------------------------------------------------------------

_MESSAGES = [
    (
        "envelope",
        lambda: _check(envelope="other.interrupt-request/v1"),
        "interrupt request schema 'other.interrupt-request/v1' is not accepted; "
        "this job-kit accepts 'job-kit.interrupt-request/v1'",
    ),
    (
        "kind",
        lambda: _check(kind="Bad Kind"),
        "interrupt kind must match [a-z][a-z0-9-]* and be at most 64 characters, "
        "got 'Bad Kind'",
    ),
    (
        "request_schema_object",
        lambda: _check(request_schema=[]),
        "interrupt request_schema must be a JSON object",
    ),
    (
        "payload_object",
        lambda: _check(payload=[]),
        "interrupt payload must be a JSON object",
    ),
    (
        "not_finite",
        lambda: _check(payload={"x": float("inf")}),
        "interrupt payload is not JSON-native: /x: inf is not finite",
    ),
    (
        "key_not_string",
        lambda: _check(request_schema={"properties": {2: {}}}),
        "interrupt request_schema is not JSON-native: /properties: key 2 is not a string",
    ),
    (
        "not_a_json_value",
        lambda: _check(payload={"when": b"now"}),
        "interrupt payload is not JSON-native: /when: bytes is not a JSON value",
    ),
    (
        "expires_in_s",
        lambda: _check(expires_in_s=0),
        "interrupt expires_in_s must be an int from 1 to 2147483647, got 0",
    ),
    (
        "schema_subset",
        lambda: _check(validator=StubValidator(schema_error="/: unknown keyword 'pattern'")),
        "interrupt request_schema is outside the supported JSON Schema subset: "
        "/: unknown keyword 'pattern'",
    ),
    (
        "constant",
        lambda: _document(b'{"schema": Infinity}'),
        "interrupt request holds Infinity, which is not JSON",
    ),
    (
        "repeated_key",
        lambda: _document(b'{"kind": "a", "kind": "b"}'),
        "interrupt request repeats the key 'kind'",
    ),
    (
        "document_size",
        lambda: _document(b" " * 131073),
        "interrupt request file is larger than 131072 bytes",
    ),
    (
        "utf8",
        lambda: _document(b'{"kind": "\xe9"}'),
        "interrupt request file is not UTF-8: 'utf-8' codec can't decode byte 0xe9 "
        "in position 10: invalid continuation byte",
    ),
    (
        "json",
        lambda: _document(b'{"kind": }'),
        "interrupt request file is not JSON: Expecting value: line 1 column 10 (char 9)",
    ),
    (
        "not_object",
        lambda: _document(b'"text"'),
        "interrupt request must be a JSON object",
    ),
    (
        "unknown_keys",
        lambda: _mapping(_raw(note="x")),
        "interrupt request has unknown keys ['note']; allowed: schema, kind, "
        "request_schema, payload, expires_in_s",
    ),
    (
        "missing_keys",
        lambda: _mapping({"schema": JOB_KIT_REQUEST}),
        "interrupt request is missing keys ['kind', 'request_schema', 'payload']",
    ),
    (
        "input_native",
        lambda: ic.validate_input({}, object, validator=StubValidator()),
        "resolution input is not JSON-native: /: type is not a JSON value",
    ),
    (
        "input_size",
        lambda: ic.validate_input({}, ["x" * 70000], validator=StubValidator()),
        "resolution input is 70004 bytes as canonical JSON; the cap is 65536",
    ),
    (
        "input_schema",
        lambda: ic.validate_input({}, 1, validator=StubValidator(errors=(("", "type"),))),
        "resolution input does not satisfy the request schema",
    ),
    (
        "decision_unknown",
        lambda: ic.decision_outcome("maybe"),
        "decision must be one of answer, reject, got 'maybe'",
    ),
    (
        "reject_with_input",
        lambda: ic.decision_outcome("reject", input=1),
        "a rejection carries a reason, not an input",
    ),
    (
        "answer_with_reason",
        lambda: ic.decision_outcome("answer", reason="r"),
        "an answer carries an input, not a reason",
    ),
]


@pytest.mark.parametrize(
    "call, message",
    [pytest.param(call, message, id=name) for name, call, message in _MESSAGES],
)
def test_messages_match_job_kit_wording(call: Callable[[], Any], message: str) -> None:
    with pytest.raises(ic.ContractError) as caught:
        call()
    assert str(caught.value) == message


# --------------------------------------------------------------------------
# The consumer probe
# --------------------------------------------------------------------------

# Every call shape the two planned consumers bind, with the exact arguments.
_PROBED_CALLS = (
    ("check_request", (), {
        "envelope": "", "kind": "", "request_schema": {}, "payload": {},
        "expires_in_s": None, "accepted_envelopes": (), "owner": "",
        "validator": None,
    }),
    ("check_request_mapping", ({},), {
        "accepted_envelopes": (), "owner": "", "validator": None,
    }),
    ("parse_request_document", (b"",), {
        "accepted_envelopes": (), "owner": "", "validator": None,
    }),
    ("validate_input", ({}, None), {"validator": None}),
    ("decision_outcome", ("answer",), {"input": None, "reason": None}),
    ("same_resolution", (), {
        "stored_outcome": "", "stored_input_json": None, "stored_reason": None,
        "outcome": "", "input": None, "reason": None,
    }),
    ("expiry", (0.0, None), {}),
    ("lapsed", (None, 0.0), {}),
    ("bound_reason", (None,), {}),
    ("canonical_json", (None,), {}),
    ("resolution_document", (), {
        "resolution_envelope": "", "interrupt_id": "", "kind": "",
        "outcome": "", "input": None, "payload": {}, "resolved_at": 0.0,
    }),
)


def _probe(module: Any) -> Any:
    """The probe of references/interrupt-contract.md, over a given module."""
    if "plugins-kit.interrupt-contract/v1" not in getattr(module, "SUPPORTED_CONTRACTS", ()):
        raise ImportError("too old: contract literal")
    for name, args, kwargs in _PROBED_CALLS:
        function = getattr(module, name, None)
        if not callable(function):
            raise ImportError(f"too old: {name}")
        try:
            inspect.signature(function).bind(*args, **kwargs)
        except (TypeError, ValueError) as exc:
            raise ImportError(f"too old: {name} call shape") from exc
    return module


def test_consumer_probe_shape_binds_real_functions() -> None:
    assert _probe(ic) is ic
    # The probe can fail: a module without the literal, without a function,
    # or with another call shape is told apart from the real one.
    surface = {name: getattr(ic, name) for name, _args, _kwargs in _PROBED_CALLS}
    no_literal = types.SimpleNamespace(SUPPORTED_CONTRACTS=frozenset({"x/v0"}), **surface)
    with pytest.raises(ImportError, match="contract literal"):
        _probe(no_literal)
    lacking = dict(surface)
    del lacking["same_resolution"]
    with pytest.raises(ImportError, match="too old: same_resolution$"):
        _probe(types.SimpleNamespace(SUPPORTED_CONTRACTS=ic.SUPPORTED_CONTRACTS, **lacking))

    def check_request(*, envelope: Any, kind: Any, request_schema: Any, payload: Any) -> dict:
        return {}

    reshaped = dict(surface, check_request=check_request)
    with pytest.raises(ImportError, match="check_request call shape"):
        _probe(types.SimpleNamespace(SUPPORTED_CONTRACTS=ic.SUPPORTED_CONTRACTS, **reshaped))
    # Keyword-only where the contract says so: the per-store inputs and the
    # validator cannot be passed by position.
    for name in ("check_request", "same_resolution", "resolution_document"):
        parameters = inspect.signature(getattr(ic, name)).parameters.values()
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters), name
    for name in ("check_request_mapping", "parse_request_document", "validate_input"):
        parameters = inspect.signature(getattr(ic, name)).parameters
        for keyword in ("accepted_envelopes", "owner", "validator"):
            if keyword in parameters:
                assert parameters[keyword].kind is inspect.Parameter.KEYWORD_ONLY, (name, keyword)
        assert "validator" in parameters, name


# --------------------------------------------------------------------------
# The specification names what the module freezes
# --------------------------------------------------------------------------


def test_reference_names_the_frozen_literals_and_limits() -> None:
    reference = (PLUGIN_DEV / "references" / "interrupt-contract.md").read_text(
        encoding="utf-8"
    )
    assert reference.isascii()
    for literal in (
        "plugins-kit.interrupt-contract/v1",
        "plugins-kit.interrupt-request/v1",
        "plugins-kit.interrupt-resolution/v1",
        "llm-scripting-kit.json-schema-subset/v1",
        "llm_scripting_kit.completion.json_schema",
        "bootstrap_lib.interrupt_contract",
        "131072",
        "65536",
        "2147483647",
        "2000",
    ):
        assert literal in reference, literal
    for name in ic.__all__:
        assert f"`{name}" in reference, name
    skill = (PLUGIN_DEV / "SKILL.md").read_text(encoding="utf-8")
    assert "path: references/interrupt-contract.md" in skill
