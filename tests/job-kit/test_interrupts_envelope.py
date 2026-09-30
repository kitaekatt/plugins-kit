"""Tests for job_kit.interrupts: the request envelope, input validation, the
resolution document, and the llm-scripting-kit json_schema probe."""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import job_kit.interrupts as interrupts
from job_kit.interrupts import (
    INPUT_LIMIT,
    REQUEST_ENVELOPE_V1,
    REQUEST_FILE_LIMIT,
    InterruptInputError,
    InterruptRequestError,
    JsonSchemaSupportError,
    canonical_json,
    parse_request,
    resolution_document,
    validate_input,
)
from job_kit.model import InterruptRecord, InterruptRequest, InterruptResolution

from llm_scripting_kit.completion import json_schema as real_json_schema


APPROVAL_SCHEMA = {
    "type": "object",
    "required": ["approved"],
    "properties": {"approved": {"const": True}},
}


def _request(**overrides: Any) -> dict:
    document: dict = {
        "schema": REQUEST_ENVELOPE_V1,
        "kind": "approval",
        "request_schema": APPROVAL_SCHEMA,
        "payload": {"action": "push release tag", "target": "v1.2.0"},
        "expires_in_s": 86400,
    }
    for key, value in overrides.items():
        if value is _DROP:
            document.pop(key, None)
        else:
            document[key] = value
    return document


_DROP = object()


def _write(tmp_path: Path, document: object, *, raw: str | None = None) -> Path:
    path = tmp_path / "request.json"
    path.write_text(raw if raw is not None else json.dumps(document), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# The request envelope
# --------------------------------------------------------------------------


def test_valid_request_parses_to_canonical_copies(tmp_path: Path) -> None:
    request = parse_request(_write(tmp_path, _request()))
    assert request == InterruptRequest(
        envelope=REQUEST_ENVELOPE_V1,
        kind="approval",
        request_schema=APPROVAL_SCHEMA,
        payload={"action": "push release tag", "target": "v1.2.0"},
        expires_in_s=86400,
    )
    no_expiry = parse_request(_write(tmp_path, _request(expires_in_s=_DROP)))
    assert no_expiry.expires_in_s is None


@pytest.mark.parametrize(
    "value",
    ["job-kit.interrupt-request/v2", "job-kit.interrupt-request/V1", "", 1, None, []],
)
def test_request_envelope_requires_exact_schema_literal(tmp_path: Path, value: object) -> None:
    with pytest.raises(InterruptRequestError) as excinfo:
        parse_request(_write(tmp_path, _request(schema=value)))
    assert REQUEST_ENVELOPE_V1 in str(excinfo.value)


def test_request_envelope_refuses_unknown_and_missing_keys(tmp_path: Path) -> None:
    with pytest.raises(InterruptRequestError, match="unknown keys"):
        parse_request(_write(tmp_path, _request(reason="free text")))
    for key in ("schema", "kind", "request_schema", "payload"):
        with pytest.raises(InterruptRequestError, match="missing keys"):
            parse_request(_write(tmp_path, _request(**{key: _DROP})))


@pytest.mark.parametrize(
    "kind",
    ["Approval", "1approval", "approval_now", "", "a" * 65, 7, None],
)
def test_request_kind_is_validated(tmp_path: Path, kind: object) -> None:
    with pytest.raises(InterruptRequestError, match="kind"):
        parse_request(_write(tmp_path, _request(kind=kind)))
    assert parse_request(_write(tmp_path, _request(kind="a" * 64))).kind == "a" * 64


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema": "%s", "kind": "approval", "request_schema": {}, '
        '"payload": {"x": NaN}}' % REQUEST_ENVELOPE_V1,
        '{"schema": "%s", "kind": "approval", "request_schema": {}, '
        '"payload": {"x": Infinity}}' % REQUEST_ENVELOPE_V1,
        '{"schema": "%s", "kind": "approval", "request_schema": {}, '
        '"payload": {"x": 1e400}}' % REQUEST_ENVELOPE_V1,
        '{"schema": "%s", "kind": "approval", "request_schema": {}, '
        '"payload": ["not", "a", "mapping"]}' % REQUEST_ENVELOPE_V1,
    ],
    ids=["nan", "infinity", "overflow", "list"],
)
def test_request_payload_must_be_json_native(tmp_path: Path, raw: str) -> None:
    with pytest.raises(InterruptRequestError):
        parse_request(_write(tmp_path, None, raw=raw))


@pytest.mark.parametrize(
    "value",
    [0, -5, True, 1.5, 2**31],
    ids=["zero", "negative", "bool", "float", "too_large"],
)
def test_request_expiry_must_be_positive_int(tmp_path: Path, value: object) -> None:
    with pytest.raises(InterruptRequestError, match="expires_in_s"):
        parse_request(_write(tmp_path, _request(expires_in_s=value)))
    assert parse_request(_write(tmp_path, _request(expires_in_s=1))).expires_in_s == 1


def test_request_file_over_cap_is_refused(tmp_path: Path) -> None:
    document = _request(payload={"blob": ""})
    base = len(json.dumps(document).encode("utf-8"))
    document["payload"]["blob"] = "x" * (REQUEST_FILE_LIMIT - base + 1)
    path = _write(tmp_path, document)
    assert path.stat().st_size == REQUEST_FILE_LIMIT + 1
    with pytest.raises(InterruptRequestError, match=str(REQUEST_FILE_LIMIT)):
        parse_request(path)
    document["payload"]["blob"] = "x" * (REQUEST_FILE_LIMIT - base)
    assert parse_request(_write(tmp_path, document)).payload["blob"]


def test_request_schema_outside_subset_is_refused(tmp_path: Path) -> None:
    """``pattern`` is outside the closed subset: refused at ingestion, never
    accepted and ignored at resolution."""
    schema = {"type": "object", "properties": {"code": {"type": "string", "pattern": "^a"}}}
    with pytest.raises(InterruptRequestError, match="pattern"):
        parse_request(_write(tmp_path, _request(request_schema=schema)))
    with pytest.raises(InterruptRequestError, match="pattern"):
        interrupts.check_request(
            InterruptRequest(
                envelope=REQUEST_ENVELOPE_V1,
                kind="approval",
                request_schema=schema,
                payload={},
            )
        )


def test_request_file_must_be_utf8_json_without_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(InterruptRequestError, match="UTF-8"):
        parse_request(path)
    with pytest.raises(InterruptRequestError, match="not JSON"):
        parse_request(_write(tmp_path, None, raw="{not json"))
    with pytest.raises(InterruptRequestError, match="repeats the key"):
        parse_request(
            _write(
                tmp_path,
                None,
                raw=json.dumps(_request())[:-1] + ', "kind": "other"}',
            )
        )
    with pytest.raises(InterruptRequestError, match="cannot be read"):
        parse_request(tmp_path / "absent.json")


# --------------------------------------------------------------------------
# Resolution input and the resolution document
# --------------------------------------------------------------------------


def test_validate_input_returns_canonical_text_and_verbatim_errors() -> None:
    assert validate_input(APPROVAL_SCHEMA, {"approved": True}) == '{"approved":true}'
    with pytest.raises(InterruptInputError) as excinfo:
        validate_input(APPROVAL_SCHEMA, {"approved": False})
    assert excinfo.value.errors == real_json_schema.validate(
        APPROVAL_SCHEMA, {"approved": False}
    )
    assert excinfo.value.errors == (("/approved", "const"),)


def test_validate_input_refuses_non_native_and_oversized_values() -> None:
    with pytest.raises(InterruptInputError, match="JSON-native") as excinfo:
        validate_input({}, {"x": float("nan")})
    assert excinfo.value.errors == ()
    with pytest.raises(InterruptInputError, match="JSON-native"):
        validate_input({}, {1: "non-string key"})
    fits = "x" * (INPUT_LIMIT - 2)
    assert len(validate_input({}, fits)) == INPUT_LIMIT
    with pytest.raises(InterruptInputError, match=str(INPUT_LIMIT)):
        validate_input({}, fits + "x")


def test_canonical_json_ignores_key_order() -> None:
    assert canonical_json({"b": 2, "a": [1, {"d": 4, "c": 3}]}) == canonical_json(
        {"a": [1, {"c": 3, "d": 4}], "b": 2}
    )
    assert canonical_json({"s": chr(0xE9)}) == '{"s":"\\u00e9"}'


def test_resolution_document_is_built_only_from_the_recorded_rows() -> None:
    record = InterruptRecord(
        id="7",
        run_id="run",
        job_id="job",
        attempt_no=1,
        continuation_no=0,
        envelope=REQUEST_ENVELOPE_V1,
        kind="approval",
        request_schema=APPROVAL_SCHEMA,
        payload={"target": "v1.2.0", "action": "push release tag"},
        created_at=100.0,
        expires_at=None,
        resolution=InterruptResolution(
            interrupt_id="7",
            outcome="answered",
            resolved_at=1790762400.0,
            input={"approved": True},
        ),
    )
    document = resolution_document(record)
    assert document == (
        '{"input":{"approved":true},"interrupt_id":"7","kind":"approval",'
        '"outcome":"answered","payload":{"action":"push release tag",'
        '"target":"v1.2.0"},"resolved_at":"2026-09-30T10:00:00Z",'
        '"schema":"job-kit.interrupt-resolution/v1"}'
    )
    assert resolution_document(record) == document
    with pytest.raises(ValueError):
        resolution_document(
            InterruptRecord(**{**record.__dict__, "resolution": None})
        )


# --------------------------------------------------------------------------
# The llm-scripting-kit json_schema probe
# --------------------------------------------------------------------------

_MISSING = object()


def _install_fake_json_schema(monkeypatch: Any, **overrides: Any) -> None:
    fake = types.ModuleType("llm_scripting_kit.completion.json_schema")
    fake.check_schema = real_json_schema.check_schema  # type: ignore[attr-defined]
    fake.validate = real_json_schema.validate  # type: ignore[attr-defined]
    for name, value in overrides.items():
        if value is _MISSING:
            delattr(fake, name)
        else:
            setattr(fake, name, value)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion.json_schema", fake)


def test_schema_validator_probe_accepts_the_real_module() -> None:
    assert interrupts._schema_validator() is importlib.import_module(
        "llm_scripting_kit.completion.json_schema"
    )


def test_schema_validator_probe_absent_names_install(monkeypatch: Any) -> None:
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    with pytest.raises(JsonSchemaSupportError) as excinfo:
        interrupts._schema_validator()
    message = str(excinfo.value)
    assert "claude plugin install llm-scripting-kit@plugins-kit" in message
    assert "update" not in message
    assert isinstance(excinfo.value, ImportError)


@pytest.mark.parametrize("part", ["module", "check_schema", "validate"])
def test_schema_validator_probe_too_old_names_0_56_0(monkeypatch: Any, part: str) -> None:
    if part == "module":
        monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion.json_schema", None)
    else:
        _install_fake_json_schema(monkeypatch, **{part: _MISSING})
    with pytest.raises(JsonSchemaSupportError) as excinfo:
        interrupts._schema_validator()
    message = str(excinfo.value)
    assert ">= 0.56.0" in message
    assert "claude plugin update llm-scripting-kit@plugins-kit" in message
    assert "install" not in message


def test_schema_validator_probe_message_names_jk_constant(monkeypatch: Any) -> None:
    monkeypatch.setattr(interrupts, "_JSON_SCHEMA_LSK_VERSION", "9.8.7")
    _install_fake_json_schema(monkeypatch, validate=_MISSING)
    with pytest.raises(JsonSchemaSupportError, match=r">= 9\.8\.7"):
        interrupts._schema_validator()


def test_schema_validator_probe_rejects_unbindable_check_schema(monkeypatch: Any) -> None:
    def check_schema(schema, strict):  # noqa: ANN001 - a later, incompatible shape
        raise AssertionError("never called")

    _install_fake_json_schema(monkeypatch, check_schema=check_schema)
    with pytest.raises(JsonSchemaSupportError, match="update"):
        interrupts._schema_validator()


def test_schema_validator_probe_rejects_unbindable_validate(monkeypatch: Any) -> None:
    def validate(schema):  # noqa: ANN001 - an older, incompatible shape
        raise AssertionError("never called")

    _install_fake_json_schema(monkeypatch, validate=validate)
    with pytest.raises(JsonSchemaSupportError, match="update"):
        interrupts._schema_validator()


def test_parse_request_refuses_when_the_validator_is_absent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    path = _write(tmp_path, _request())
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    with pytest.raises(JsonSchemaSupportError):
        parse_request(path)
