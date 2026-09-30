"""job-kit's interrupt rules run in ``bootstrap_lib.interrupt_contract`` and its
observable behaviour is unchanged.

Two kinds of test live here. The characterization tests pin what job-kit does
today as full literals and sha256 digests, and were written and observed green
against the code that held the rules locally, before any rule moved. The
delegation and parity tests prove the rules now execute in the shared module
(a sentinel patched into the contract surfaces through job-kit) and that the
few values job-kit still holds itself (limits, ``canonical_json``, the lapse
rule, the two literals) equal the contract's.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

import job_kit.interrupts as interrupts
from job_kit.interrupts import (
    INPUT_LIMIT,
    KIND_LIMIT,
    REQUEST_ENVELOPE_V1,
    REQUEST_FILE_LIMIT,
    RESOLUTION_ENVELOPE_V1,
    InterruptInputError,
    InterruptRequestError,
    canonical_json,
    check_request,
    parse_request,
    resolution_document,
    validate_input,
)
from job_kit.model import (
    Acceptance,
    Attempt,
    Contract,
    InterruptRecord,
    InterruptRequest,
    InterruptResolution,
    Job,
    Prompt,
    Usage,
    interrupt_lapsed,
)
from job_kit.store import ERROR_LIMIT, JobStore

from bootstrap_lib import interrupt_contract as contract


SHARED_REQUEST_ENVELOPE = "plugins-kit.interrupt-request/v1"
APPROVAL = {
    "type": "object",
    "required": ["approved"],
    "properties": {"approved": {"const": True}},
    "additionalProperties": False,
}
ARMED_AT = "2026-09-01T00:00:01Z"
ENDED_AT = "2026-09-01T00:00:02Z"
CREATED = 1000.0


def _req(**overrides: Any) -> InterruptRequest:
    fields: dict = dict(
        envelope=REQUEST_ENVELOPE_V1,
        kind="approval",
        request_schema={"type": "object"},
        payload={},
        expires_in_s=None,
    )
    fields.update(overrides)
    return InterruptRequest(**fields)


# --------------------------------------------------------------------------
# Characterization: every refusal message, as a full literal
# --------------------------------------------------------------------------

_LONG_KIND = "a" * (KIND_LIMIT + 1)

_REQUEST_REFUSALS = [
    ("envelope_other", _req(envelope="x"),
     "interrupt request schema 'x' is not accepted; this job-kit accepts "
     "'job-kit.interrupt-request/v1'"),
    ("envelope_shared", _req(envelope=SHARED_REQUEST_ENVELOPE),
     "interrupt request schema 'plugins-kit.interrupt-request/v1' is not accepted; "
     "this job-kit accepts 'job-kit.interrupt-request/v1'"),
    ("envelope_non_string", _req(envelope=5),
     "interrupt request schema 5 is not accepted; this job-kit accepts "
     "'job-kit.interrupt-request/v1'"),
    ("kind_upper", _req(kind="Bad"),
     "interrupt kind must match [a-z][a-z0-9-]* and be at most 64 characters, got 'Bad'"),
    ("kind_too_long", _req(kind=_LONG_KIND),
     "interrupt kind must match [a-z][a-z0-9-]* and be at most 64 characters, "
     f"got {_LONG_KIND!r}"),
    ("kind_non_string", _req(kind=7),
     "interrupt kind must match [a-z][a-z0-9-]* and be at most 64 characters, got 7"),
    ("schema_not_object", _req(request_schema=[1]),
     "interrupt request_schema must be a JSON object"),
    ("payload_not_object", _req(payload="p"), "interrupt payload must be a JSON object"),
    ("schema_not_native", _req(request_schema={"a": object()}),
     "interrupt request_schema is not JSON-native: /a: object is not a JSON value"),
    ("payload_not_finite", _req(payload={"a": {"b": [1, float("nan")]}}),
     "interrupt payload is not JSON-native: /a/b/1: nan is not finite"),
    ("payload_key_not_string", _req(payload={1: 2}),
     "interrupt payload is not JSON-native: /: key 1 is not a string"),
    ("expires_zero", _req(expires_in_s=0),
     "interrupt expires_in_s must be an int from 1 to 2147483647, got 0"),
    ("expires_bool", _req(expires_in_s=True),
     "interrupt expires_in_s must be an int from 1 to 2147483647, got True"),
    ("expires_too_large", _req(expires_in_s=2**31),
     "interrupt expires_in_s must be an int from 1 to 2147483647, got 2147483648"),
    ("expires_float", _req(expires_in_s=1.5),
     "interrupt expires_in_s must be an int from 1 to 2147483647, got 1.5"),
    ("schema_outside_subset", _req(request_schema={"type": "object", "oneOf": [{}]}),
     "interrupt request_schema is outside the supported JSON Schema subset: "
     "unsupported schema keyword 'oneOf' at /oneOf; supported: $ref, "
     "additionalProperties, anyOf, const, enum, exclusiveMaximum, exclusiveMinimum, "
     "items, maxItems, maxLength, maximum, minItems, minLength, minimum, properties, "
     "required, type; annotations: $defs, $id, $schema, default, description, "
     "examples, title"),
    ("schema_bad_type", _req(request_schema={"type": "nope"}),
     "interrupt request_schema is outside the supported JSON Schema subset: schema "
     "keyword 'type' at / must be one of ['array', 'boolean', 'integer', 'null', "
     "'number', 'object', 'string'] or a non-empty list of them"),
]

_GOOD_DOCUMENT = {
    "schema": REQUEST_ENVELOPE_V1,
    "kind": "approval",
    "request_schema": {"type": "object"},
    "payload": {},
}

_FILE_REFUSALS = [
    ("file_too_large", b" " * (REQUEST_FILE_LIMIT + 1),
     "interrupt request file is larger than 131072 bytes"),
    ("file_not_utf8", b"\xff\xfe",
     "interrupt request file is not UTF-8: 'utf-8' codec can't decode byte 0xff in "
     "position 0: invalid start byte"),
    ("file_not_json", b"{oops",
     "interrupt request file is not JSON: Expecting property name enclosed in "
     "double quotes: line 1 column 2 (char 1)"),
    ("file_nan", b'{"schema": NaN}', "interrupt request holds NaN, which is not JSON"),
    ("file_infinity", b'{"payload": Infinity}',
     "interrupt request holds Infinity, which is not JSON"),
    ("file_repeated_key", b'{"kind": "a", "kind": "b"}',
     "interrupt request repeats the key 'kind'"),
    ("file_top_level_array", b"[1]", "interrupt request must be a JSON object"),
    ("file_unknown_keys",
     json.dumps({**_GOOD_DOCUMENT, "zeta": 1, "alpha": 2}).encode("ascii"),
     "interrupt request has unknown keys ['alpha', 'zeta']; allowed: schema, kind, "
     "request_schema, payload, expires_in_s"),
    ("file_missing_keys", json.dumps({"schema": REQUEST_ENVELOPE_V1}).encode("ascii"),
     "interrupt request is missing keys ['kind', 'request_schema', 'payload']"),
    ("file_wrong_envelope",
     json.dumps({**_GOOD_DOCUMENT, "schema": "x"}).encode("ascii"),
     "interrupt request schema 'x' is not accepted; this job-kit accepts "
     "'job-kit.interrupt-request/v1'"),
]


@pytest.mark.parametrize(
    "request_, message",
    [(item[1], item[2]) for item in _REQUEST_REFUSALS],
    ids=[item[0] for item in _REQUEST_REFUSALS],
)
def test_check_request_refusal_message_is_unchanged(
    request_: InterruptRequest, message: str
) -> None:
    with pytest.raises(InterruptRequestError) as excinfo:
        check_request(request_)
    assert str(excinfo.value) == message


@pytest.mark.parametrize(
    "data, message",
    [(item[1], item[2]) for item in _FILE_REFUSALS],
    ids=[item[0] for item in _FILE_REFUSALS],
)
def test_parse_request_refusal_message_is_unchanged(
    tmp_path: Path, data: bytes, message: str
) -> None:
    path = tmp_path / "request.json"
    path.write_bytes(data)
    with pytest.raises(InterruptRequestError) as excinfo:
        parse_request(path)
    assert str(excinfo.value) == message


def test_parse_request_unreadable_file_message_is_job_kits_own(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(InterruptRequestError) as excinfo:
        parse_request(missing)
    text = str(excinfo.value)
    assert text.startswith("interrupt request file cannot be read: ")
    assert "nope.json" in text


_INPUT_REFUSALS = [
    ("input_not_native", {1: 2}, "resolution input is not JSON-native: /: key 1 is not a string", ()),
    ("input_not_finite", float("nan"), "resolution input is not JSON-native: /: nan is not finite", ()),
    ("input_oversized", {"a": "x" * 70000},
     "resolution input is 70008 bytes as canonical JSON; the cap is 65536", ()),
    ("input_fails_schema", {"approved": False},
     "resolution input does not satisfy the request schema", (("/approved", "const"),)),
    ("input_missing_required", {},
     "resolution input does not satisfy the request schema", (("/approved", "required"),)),
]


@pytest.mark.parametrize(
    "value, message, errors",
    [(item[1], item[2], item[3]) for item in _INPUT_REFUSALS],
    ids=[item[0] for item in _INPUT_REFUSALS],
)
def test_validate_input_refusal_message_is_unchanged(
    value: object, message: str, errors: tuple
) -> None:
    with pytest.raises(InterruptInputError) as excinfo:
        validate_input(APPROVAL, value)
    assert str(excinfo.value) == message
    assert excinfo.value.errors == errors


def test_validate_input_returns_canonical_text() -> None:
    assert validate_input(APPROVAL, {"approved": True}) == '{"approved":true}'


def test_check_request_returns_canonical_deep_copies() -> None:
    schema = {"type": "object", "properties": {"b": {"type": "string"}, "a": {"type": "integer"}}}
    payload = {"z": [1, {"y": 2, "x": 3}], "a": None}
    request = _req(request_schema=schema, payload=payload, expires_in_s=30)
    checked = check_request(request)
    assert checked == request
    assert list(checked.payload) == ["a", "z"]
    assert list(checked.payload["z"][1]) == ["x", "y"]
    assert checked.payload is not payload and checked.request_schema is not schema
    assert checked.envelope == REQUEST_ENVELOPE_V1 and checked.expires_in_s == 30


# --------------------------------------------------------------------------
# Characterization: the store's decision, replay, expiry and reason rules
# --------------------------------------------------------------------------


def _job(directory: Path, job_id: str = "job") -> Job:
    return Job(
        id=job_id,
        prompt=Prompt(user=f"run {job_id}"),
        models=("fake",),
        directory=directory,
        max_attempts=2,
        contract=Contract(command=(sys.executable, "-c", "pass"), directory=directory),
    )


def _acceptance() -> Acceptance:
    return Acceptance(
        command=("contract",),
        directory=Path.cwd(),
        exit_code=0,
        stdout="",
        stderr="",
        wall_ms=1,
        accepted=False,
        outcome="interrupt_requested",
    )


def _attempt(job_id: str, attempt_no: int) -> Attempt:
    return Attempt(
        run_id="run",
        job_id=job_id,
        attempt_no=attempt_no,
        endpoint="fake-endpoint",
        backend="fake-backend",
        model="fake-model",
        status="completed",
        started_at=ARMED_AT,
        ended_at=ENDED_AT,
        usage=Usage(input_tokens=3, output_tokens=5),
        response_text="the model answer",
        acceptance=_acceptance(),
    )


def _wait(
    store: JobStore, job_id: str, *, at: float = CREATED, expires_in_s: Optional[int] = None
) -> InterruptRecord:
    reservation = store.reserve_attempt(
        "run",
        job_id,
        endpoint="fake-endpoint",
        backend="fake-backend",
        model="fake-model",
        reserved_at="2026-09-01T00:00:00Z",
    )
    store.arm_reservation("run", job_id, reservation.attempt_no, invoke_armed_at=ARMED_AT)
    store.append_attempt(
        _attempt(job_id, reservation.attempt_no),
        interrupt=InterruptRequest(
            envelope=REQUEST_ENVELOPE_V1,
            kind="approval",
            request_schema=dict(APPROVAL),
            payload={"action": "push tag"},
            expires_in_s=expires_in_s,
        ),
        at=at,
    )
    record = store.open_interrupt("run", job_id)
    assert record is not None
    return record


def _store(tmp_path: Path, *job_ids: str) -> JobStore:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path, job_id) for job_id in job_ids])
    return store


def _rows(store: JobStore, table: str) -> list[dict]:
    with sqlite3.connect(str(store.db_path)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("ascii")
    ).hexdigest()


def _events(store: JobStore) -> list[dict]:
    """The run's events, with the one wall-clock stamp (run creation) blanked."""
    return [
        {**item, "at": "<wall clock>"} if item["event"] == "job-kit:run-created" else item
        for item in store.list_events("run")
    ]


def _lifecycle(tmp_path: Path) -> dict[str, str]:
    """One answered, one rejected and one expired interrupt, on fixed clocks."""
    store = _store(tmp_path, "answered", "rejected", "expired")
    answered = _wait(store, "answered", at=CREATED)
    rejected = _wait(store, "rejected", at=CREATED + 10, expires_in_s=500)
    expiring = _wait(store, "expired", at=CREATED + 20, expires_in_s=30)
    store.resolve_interrupt(
        "run", answered.id, decision="answer", input={"approved": True}, now=CREATED + 100
    )
    store.resolve_interrupt(
        "run", answered.id, decision="answer", input={"approved": True}, now=CREATED + 101
    )
    store.begin_continuation("run", "answered", now=CREATED + 102)
    store.resolve_interrupt(
        "run", rejected.id, decision="reject", reason="r" * 2500, now=CREATED + 110
    )
    store.expire_interrupts("run", CREATED + 200)
    assert expiring.id
    return {
        "interrupts": _digest(_rows(store, "interrupts")),
        "interrupt_resolutions": _digest(_rows(store, "interrupt_resolutions")),
        "continuations": _digest(_rows(store, "continuations")),
        "events": _digest(_events(store)),
    }


# Recorded from the code that held these rules locally, before they moved.
_BASE_DIGESTS = {
    "interrupts": "b28262b9e0d101b867b0e5dddc467e185987e994c74e7681c3064800839ee483",
    "interrupt_resolutions": "a1410d374cd1ad35462c1e7ee9221dada66f2b92dd2f2c84e5942f6fc3640c4d",
    "continuations": "f3572d6f31d335637211f04778a79188f031a6ebee7d7fdd5c8a30a757e5c36c",
    "events": "d96e2e96756678a62d9a21b2581098e8d636102796d2252addc20618811256d2",
}


def test_lifecycle_rows_and_events_match_the_base_digests(tmp_path: Path) -> None:
    observed = _lifecycle(tmp_path)
    assert observed == _BASE_DIGESTS


def test_lifecycle_digest_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    assert _lifecycle(tmp_path / "one") == _lifecycle(tmp_path / "two")


_RECORD = InterruptRecord(
    id="7",
    run_id="run",
    job_id="job",
    attempt_no=1,
    continuation_no=0,
    envelope=REQUEST_ENVELOPE_V1,
    kind="approval",
    request_schema=dict(APPROVAL),
    payload={"target": "v1.2.0", "action": "push release tag"},
    created_at=CREATED,
    expires_at=None,
    resolution=InterruptResolution(
        interrupt_id="7", outcome="answered", resolved_at=1790762400.0, input={"approved": True}
    ),
)


def test_resolution_document_bytes_are_unchanged() -> None:
    assert resolution_document(_RECORD) == (
        '{"input":{"approved":true},"interrupt_id":"7","kind":"approval",'
        '"outcome":"answered","payload":{"action":"push release tag",'
        '"target":"v1.2.0"},"resolved_at":"2026-09-30T10:00:00Z",'
        '"schema":"job-kit.interrupt-resolution/v1"}'
    )


def test_resolution_document_bytes_for_a_rejection_with_microseconds() -> None:
    import dataclasses

    record = dataclasses.replace(
        _RECORD,
        resolution=InterruptResolution(
            interrupt_id="7", outcome="rejected", resolved_at=1790762400.25, reason="no"
        ),
    )
    assert resolution_document(record) == (
        '{"input":null,"interrupt_id":"7","kind":"approval","outcome":"rejected",'
        '"payload":{"action":"push release tag","target":"v1.2.0"},'
        '"resolved_at":"2026-09-30T10:00:00.250000Z",'
        '"schema":"job-kit.interrupt-resolution/v1"}'
    )


def _resolve_error(tmp_path: Path, **arguments: Any) -> BaseException:
    store = _store(tmp_path, "job")
    record = _wait(store, "job")
    with pytest.raises(BaseException) as excinfo:
        store.resolve_interrupt("run", record.id, **arguments)
    return excinfo.value


def test_resolve_refusal_messages_are_unchanged(tmp_path: Path) -> None:
    unknown = _resolve_error(tmp_path / "a", decision="approve")
    assert type(unknown) is ValueError
    assert str(unknown) == "decision must be one of answer, reject, got 'approve'"
    non_string = _resolve_error(tmp_path / "b", decision=5)
    assert type(non_string) is ValueError
    assert str(non_string) == "decision must be one of answer, reject, got 5"
    crossed = _resolve_error(tmp_path / "c", decision="reject", input={"a": 1})
    assert type(crossed) is ValueError
    assert str(crossed) == "a rejection carries a reason, not an input"
    reasoned = _resolve_error(tmp_path / "d", decision="answer", reason="why")
    assert type(reasoned) is ValueError
    assert str(reasoned) == "an answer carries an input, not a reason"


def test_resolve_refuses_an_unhashable_decision_with_the_type_error_it_always_raised(
    tmp_path: Path,
) -> None:
    assert type(_resolve_error(tmp_path, decision=["answer"])) is TypeError


def test_reject_reason_is_bounded_and_replays(tmp_path: Path) -> None:
    store = _store(tmp_path, "job")
    record = _wait(store, "job")
    long_reason = "r" * (ERROR_LIMIT + 500)
    first = store.resolve_interrupt("run", record.id, decision="reject", reason=long_reason)
    assert first.reason == "r" * 2000 and not first.replayed
    again = store.resolve_interrupt("run", record.id, decision="reject", reason=long_reason)
    assert again.replayed and again.reason == first.reason


# --------------------------------------------------------------------------
# Parity of what job-kit still holds itself
# --------------------------------------------------------------------------


def test_limits_equal_the_contract() -> None:
    assert INPUT_LIMIT == contract.INPUT_LIMIT
    assert KIND_LIMIT == contract.KIND_LIMIT
    assert REQUEST_FILE_LIMIT == contract.REQUEST_DOCUMENT_LIMIT
    assert interrupts.EXPIRES_IN_S_MAX == contract.EXPIRES_IN_S_MAX


def test_error_limit_equals_the_contract_reason_limit() -> None:
    assert ERROR_LIMIT == contract.REASON_LIMIT


_CANONICAL_VALUES = [
    ("nested_keys", {"b": {"z": 1, "a": [3, 2, {"y": 0, "x": 1}]}, "a": None}),
    ("non_ascii", {"k\xe9y": "v\xe5l \U0001f600"}),
    ("numbers", [1, 1.5, -0.0, 10**20, 1e-7, True, False, None]),
    ("empty", {}),
    ("string_escapes", {"q": 'a"b\\c\n\t\x85\xa0'}),
]


@pytest.mark.parametrize(
    "value", [item[1] for item in _CANONICAL_VALUES], ids=[item[0] for item in _CANONICAL_VALUES]
)
def test_canonical_json_matches_the_contract(value: object) -> None:
    assert canonical_json(value) == contract.canonical_json(value)


def test_canonical_json_refuses_what_the_contract_refuses() -> None:
    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            canonical_json(value)
        with pytest.raises(ValueError):
            contract.canonical_json(value)


@pytest.mark.parametrize(
    "when, expected",
    [("before", False), ("at", True), ("after", True), ("none", False)],
)
def test_lapse_rule_matches_the_contract(when: str, expected: bool) -> None:
    expires_at: Optional[float] = None if when == "none" else 100.0
    now = {"before": 99.999, "at": 100.0, "after": 100.001, "none": 10**12}[when]
    assert interrupt_lapsed(expires_at, now) is expected
    assert contract.lapsed(expires_at, now) is expected


def test_job_kit_refuses_the_shared_request_literal() -> None:
    assert REQUEST_ENVELOPE_V1 == "job-kit.interrupt-request/v1"
    assert interrupts.REQUEST_ENVELOPES == frozenset({"job-kit.interrupt-request/v1"})
    assert SHARED_REQUEST_ENVELOPE == contract.REQUEST_ENVELOPE_V1
    with pytest.raises(InterruptRequestError, match="is not accepted"):
        check_request(_req(envelope=contract.REQUEST_ENVELOPE_V1))


def test_resolution_document_names_the_job_kit_literal() -> None:
    import inspect

    assert RESOLUTION_ENVELOPE_V1 == "job-kit.interrupt-resolution/v1"
    assert list(inspect.signature(resolution_document).parameters) == ["record"]
    assert json.loads(resolution_document(_RECORD))["schema"] == RESOLUTION_ENVELOPE_V1


# --------------------------------------------------------------------------
# The rules run in the contract, and job-kit re-raises its own error classes
# --------------------------------------------------------------------------


class _Sentinel(Exception):
    """Raised by a patched contract function to show the call reached it."""


def _patch_contract(monkeypatch: pytest.MonkeyPatch, name: str, result: Any = None) -> list:
    """Replace one contract function with a recorder that returns ``result``.

    The replacement takes any arguments, so the probe's call-shape binding
    still passes and only the RULE is different: whatever job-kit returns or
    stores afterwards came from this function and not from a local copy.
    """
    calls: list = []

    def replacement(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(contract, name, replacement)
    return calls


def test_check_request_delegates_to_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_contract(
        monkeypatch,
        "check_request",
        {
            "envelope": REQUEST_ENVELOPE_V1,
            "kind": "from-the-contract",
            "request_schema": {"type": "object"},
            "payload": {"k": 1},
            "expires_in_s": 9,
        },
    )
    checked = check_request(_req(kind="local-kind"))
    assert checked.kind == "from-the-contract" and checked.expires_in_s == 9
    [(args, kwargs)] = calls
    assert args == ()
    assert kwargs["owner"] == "job-kit"
    assert kwargs["accepted_envelopes"] == interrupts.REQUEST_ENVELOPES
    assert kwargs["kind"] == "local-kind"


def test_parse_request_delegates_to_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_contract(
        monkeypatch,
        "parse_request_document",
        {
            "envelope": REQUEST_ENVELOPE_V1,
            "kind": "from-the-contract",
            "request_schema": {"type": "object"},
            "payload": {},
            "expires_in_s": None,
        },
    )
    path = tmp_path / "request.json"
    path.write_bytes(b"not even json")
    assert parse_request(path).kind == "from-the-contract"
    [(args, kwargs)] = calls
    assert args == (b"not even json",)
    assert kwargs["owner"] == "job-kit"


def test_validate_input_delegates_to_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_contract(monkeypatch, "validate_input", "from-the-contract")
    assert validate_input(APPROVAL, {"approved": False}) == "from-the-contract"
    [(args, kwargs)] = calls
    assert args == (APPROVAL, {"approved": False})
    assert "validator" in kwargs


def test_resolution_document_delegates_to_the_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_contract(monkeypatch, "resolution_document", "from-the-contract")
    assert resolution_document(_RECORD) == "from-the-contract"
    [(args, kwargs)] = calls
    assert args == ()
    assert kwargs["resolution_envelope"] == "job-kit.interrupt-resolution/v1"
    assert kwargs["interrupt_id"] == "7" and kwargs["resolved_at"] == 1790762400.0


@pytest.mark.parametrize("rule", ["decision", "replay", "expiry", "reason"])
def test_store_rules_delegate_to_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rule: str
) -> None:
    store = _store(tmp_path, "job")
    if rule == "decision":
        record = _wait(store, "job")
        calls = _patch_contract(monkeypatch, "decision_outcome", _Sentinel("decision"))
        with pytest.raises(_Sentinel):
            store.resolve_interrupt("run", record.id, decision="answer", input={"approved": True})
        assert calls and calls[0][0] == ("answer",)
    elif rule == "replay":
        record = _wait(store, "job")
        store.resolve_interrupt(
            "run", record.id, decision="answer", input={"approved": True}, now=CREATED + 1
        )
        calls = _patch_contract(monkeypatch, "same_resolution", False)
        # The identical call replays -- unless the contract says it does not.
        with pytest.raises(Exception, match="already resolved"):
            store.resolve_interrupt(
                "run", record.id, decision="answer", input={"approved": True}, now=CREATED + 2
            )
        assert calls and calls[0][1]["stored_outcome"] == "answered"
    elif rule == "expiry":
        calls = _patch_contract(monkeypatch, "expiry", 4242.0)
        record = _wait(store, "job", expires_in_s=30)
        assert record.expires_at == 4242.0
        assert calls == [((CREATED, 30), {})]
    else:
        record = _wait(store, "job")
        calls = _patch_contract(monkeypatch, "bound_reason", "BOUND-BY-THE-CONTRACT")
        resolved = store.resolve_interrupt(
            "run", record.id, decision="reject", reason="a reason", now=CREATED + 1
        )
        assert resolved.reason == "BOUND-BY-THE-CONTRACT"
        assert calls == [(("a reason",), {})]


def test_refusals_raise_job_kit_error_classes(tmp_path: Path) -> None:
    with pytest.raises(InterruptRequestError) as request_error:
        check_request(_req(kind="Bad"))
    assert type(request_error.value) is InterruptRequestError
    assert not isinstance(request_error.value, contract.ContractError)

    path = tmp_path / "request.json"
    path.write_bytes(b"[1]")
    with pytest.raises(InterruptRequestError) as file_error:
        parse_request(path)
    assert type(file_error.value) is InterruptRequestError
    assert not isinstance(file_error.value, contract.ContractError)

    with pytest.raises(InterruptInputError) as input_error:
        validate_input(APPROVAL, {"approved": False})
    assert type(input_error.value) is InterruptInputError
    assert not isinstance(input_error.value, contract.ContractError)
    assert input_error.value.errors == (("/approved", "const"),)

    (tmp_path / "s").mkdir()
    store = _store(tmp_path / "s", "job")
    record = _wait(store, "job")
    with pytest.raises(ValueError) as decision_error:
        store.resolve_interrupt("run", record.id, decision="nope")
    assert type(decision_error.value) is ValueError
    assert not isinstance(decision_error.value, contract.ContractError)


def test_canonical_json_works_without_bootstrap_lib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
    monkeypatch.setitem(sys.modules, "bootstrap_lib.interrupt_contract", None)
    assert canonical_json({"b": 1, "a": [2]}) == '{"a":[2],"b":1}'


def test_an_invalid_request_is_refused_on_its_merits_before_an_unusable_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order job-kit always had: request faults first, the validator last."""
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion.json_schema", None)
    with pytest.raises(InterruptRequestError, match="interrupt kind must match"):
        check_request(_req(kind="Bad"))
    with pytest.raises(interrupts.JsonSchemaSupportError):
        check_request(_req())
    with pytest.raises(InterruptInputError, match="not JSON-native"):
        validate_input(APPROVAL, {1: 2})
    with pytest.raises(interrupts.JsonSchemaSupportError):
        validate_input(APPROVAL, {"approved": True})
