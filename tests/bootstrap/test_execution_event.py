"""Tests for bootstrap_lib.execution_event -- the shared execution-event envelope.

The module owns one envelope with three frozen revisions (schema v1, v2 =
v1 plus the `interrupt` event, and v3 = v2 plus the unit-scoped `contract`
event), their vocabularies, the stream ordering and
lifecycle rules, the zero-versus-unknown usage rule, and two sinks. Every
plugin keeps its own store, so nothing here opens a database or imports
another plugin; the module is stdlib-only and imports nothing from
bootstrap_lib.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from bootstrap_lib import execution_event as ee


MODULE_PATH = Path(ee.__file__)
AT = "2026-09-29T20:00:00Z"


def _event(**overrides: Any) -> dict:
    """A valid attempt-scoped `result` event, with top-level overrides."""
    value = {
        "schema": ee.SCHEMA_V1,
        "seq": 0,
        "identity": {"run_id": "run-1", "unit_id": "unit-1", "attempt_id": "1"},
        "event": "result",
        "at": AT,
        "source": {"plugin": "job-kit", "adapter": "openrouter", "model": "m"},
        "payload": {"status": "completed"},
    }
    value.update(overrides)
    return value


def _make(**overrides: Any) -> dict:
    kwargs: dict[str, Any] = {
        "seq": 0,
        "run_id": "run-1",
        "event": "result",
        "plugin": "job-kit",
        "at": AT,
        "unit_id": "unit-1",
        "attempt_id": "1",
        "payload": {"status": "completed"},
    }
    kwargs.update(overrides)
    return ee.make_event(**kwargs)


def _refused(value: Any) -> ee.EventError:
    with pytest.raises(ee.EventError) as info:
        ee.validate_event(value)
    return info.value


# --------------------------------------------------------------------------
# Envelope shape
# --------------------------------------------------------------------------


def test_a_valid_event_round_trips_as_a_normalized_deep_copy() -> None:
    original = _event()
    result = ee.validate_event(original)
    assert result == original
    result["payload"]["status"] = "changed"
    result["identity"]["run_id"] = "changed"
    assert original["payload"]["status"] == "completed"
    assert original["identity"]["run_id"] == "run-1"


def test_unknown_top_level_key_is_refused() -> None:
    error = _refused(_event(extra=1))
    assert "unknown top-level key" in str(error)
    assert error.pointer == "/extra"


def test_missing_required_key_is_refused() -> None:
    for key in ("schema", "seq", "identity", "event", "at", "source", "payload"):
        value = _event()
        del value[key]
        error = _refused(value)
        assert "missing required key" in str(error), key
        assert error.pointer == "/" + key


def test_unknown_schema_revision_is_refused() -> None:
    error = _refused(_event(schema="plugins-kit.execution-event/v4"))
    assert "plugins-kit.execution-event/v4" in str(error)
    assert error.pointer == "/schema"


@pytest.mark.parametrize(
    "seq", [-1, True, 1.0], ids=["negative", "bool", "float"]
)
def test_seq_must_be_non_negative_int(seq: Any) -> None:
    error = _refused(_event(seq=seq))
    assert error.pointer == "/seq"


@pytest.mark.parametrize(
    "run_id",
    ["", "x" * 201, "a\nb"],
    ids=["empty", "too_long", "control_char"],
)
def test_identity_strings_are_bounded(run_id: str) -> None:
    error = _refused(_event(identity={"run_id": run_id}, event="terminal",
                            payload={"state": "done"}))
    assert error.pointer == "/identity/run_id"


def test_identity_string_at_the_bound_is_accepted() -> None:
    value = ee.validate_event(
        _event(identity={"run_id": "x" * 200}, event="terminal", payload={"state": "done"})
    )
    assert value["identity"] == {"run_id": "x" * 200}


def test_optional_keys_are_omitted_not_null() -> None:
    error = _refused(
        _event(identity={"run_id": "r", "unit_id": None}, event="terminal",
               payload={"state": "done"})
    )
    assert "omit" in str(error)
    assert error.pointer == "/identity/unit_id"
    error = _refused(_event(source={"plugin": "job-kit", "adapter": None}))
    assert "omit" in str(error)
    assert error.pointer == "/source/adapter"

    built = ee.make_event(seq=0, run_id="r", event="terminal", plugin="job-kit",
                          at=AT, payload={"state": "done"})
    assert built["identity"] == {"run_id": "r"}
    assert built["source"] == {"plugin": "job-kit"}


def test_attempt_id_requires_unit_id() -> None:
    error = _refused(
        _event(identity={"run_id": "r", "attempt_id": "1"}, event="job-kit:note",
               payload={})
    )
    assert "requires identity.unit_id" in str(error)
    assert error.pointer == "/identity/attempt_id"


def test_unknown_identity_or_source_sub_key_is_refused() -> None:
    assert _refused(_event(identity={"run_id": "r", "job": "j"})).pointer == "/identity/job"
    assert _refused(_event(source={"plugin": "job-kit", "host": "h"})).pointer == "/source/host"


@pytest.mark.parametrize("plugin", ["Job-kit", "1kit", "job_kit", "", "-kit"])
def test_source_plugin_pattern_is_enforced(plugin: str) -> None:
    error = _refused(
        _event(identity={"run_id": "r"}, event="terminal", payload={"state": "done"},
               source={"plugin": plugin})
    )
    assert error.pointer == "/source/plugin"


def test_source_plugin_pattern_accepts_lowercase_hyphenated_names() -> None:
    for plugin in ("job-kit", "a", "content-pipeline-kit", "k8s"):
        ee.validate_event(
            _event(identity={"run_id": "r"}, event="terminal",
                   payload={"state": "done"}, source={"plugin": plugin})
        )


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


def test_core_vocabulary_is_the_five_v1_names() -> None:
    assert ee.CORE_EVENTS == frozenset(
        {"dispatch-selected", "call-started", "usage", "result", "terminal"}
    )
    assert ee.ATTEMPT_SCOPED == frozenset(
        {"dispatch-selected", "call-started", "usage", "result"}
    )
    assert ee.LATER_REVISION_NAMES == frozenset({"contract", "interrupt"})
    assert ee.OWNER == "bootstrap@plugins-kit"
    assert ee.MAX_PAYLOAD_BYTES == 16384


def test_unknown_core_name_is_refused() -> None:
    error = _refused(_event(event="started"))
    assert "unknown event" in str(error)
    assert error.pointer == "/event"


@pytest.mark.parametrize("name", ["contract", "interrupt"])
def test_later_revision_names_are_refused_in_v1(name: str) -> None:
    error = _refused(_event(event=name))
    assert "defined by a later schema revision" in str(error)
    assert error.pointer == "/event"


@pytest.mark.parametrize(
    "name", ["job-kit:Bad", "job-kit:", "job-kit:a_b", "job-kit:a:b", "job-kit:1x"]
)
def test_extension_name_pattern_is_enforced(name: str) -> None:
    error = _refused(_event(event=name))
    assert "name part" in str(error)
    assert error.pointer == "/event"


def test_extension_in_the_own_namespace_is_accepted_at_any_scope() -> None:
    run_scope = ee.validate_event(
        _event(identity={"run_id": "r"}, event="job-kit:run-created",
               payload={"max_parallel": 2})
    )
    assert run_scope["event"] == "job-kit:run-created"
    attempt_scope = ee.validate_event(_event(event="job-kit:lease-renewed", payload={}))
    assert attempt_scope["identity"]["attempt_id"] == "1"


def test_extension_prefix_must_equal_source_plugin() -> None:
    error = _refused(_event(event="content-pipeline-kit:run-created", payload={}))
    assert "own prefix" in str(error)
    assert error.pointer == "/event"


@pytest.mark.parametrize("name", sorted(ee.ATTEMPT_SCOPED))
def test_attempt_scoped_event_requires_unit_and_attempt(name: str) -> None:
    payload = {"status": "completed"} if name == "result" else {}
    if name == "usage":
        payload = ee.usage_payload(input_tokens=3, output_tokens=4)
    for identity in ({"run_id": "r", "unit_id": "u"}, {"run_id": "r"}):
        error = _refused(_event(event=name, identity=identity, payload=payload))
        assert "attempt-scoped" in str(error), (name, identity)
        assert error.pointer == "/identity"


def test_terminal_forbids_attempt_id() -> None:
    error = _refused(_event(event="terminal", payload={"state": "done"}))
    assert "must not carry attempt_id" in str(error)
    assert error.pointer == "/identity/attempt_id"


def test_terminal_is_accepted_at_unit_and_run_scope() -> None:
    for identity in ({"run_id": "r", "unit_id": "u"}, {"run_id": "r"}):
        ee.validate_event(_event(event="terminal", identity=identity,
                                 payload={"state": "done"}))


@pytest.mark.parametrize("payload", [{}, {"status": ""}, {"status": 1}])
def test_result_requires_status(payload: dict) -> None:
    error = _refused(_event(payload=payload))
    assert error.pointer == "/payload/status"


@pytest.mark.parametrize("payload", [{}, {"state": ""}, {"state": None}])
def test_terminal_requires_state(payload: dict) -> None:
    error = _refused(
        _event(event="terminal", identity={"run_id": "r", "unit_id": "u"}, payload=payload)
    )
    assert error.pointer == "/payload/state"


# --------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------


def _usage(**fields: Any) -> dict:
    payload = {key: None for key in
               ("input_tokens", "output_tokens", "cache_hit_tokens", "total_tokens")}
    payload.update(fields)
    return _event(event="usage", payload=payload)


@pytest.mark.parametrize("bad", [-1, 1.5, True, "3"])
def test_usage_fields_are_non_negative_int_or_null(bad: Any) -> None:
    error = _refused(_usage(input_tokens=bad, output_tokens=2))
    assert error.pointer == "/payload/input_tokens"
    accepted = ee.validate_event(_usage(input_tokens=0, output_tokens=2))
    assert accepted["payload"]["input_tokens"] == 0


def test_usage_payload_keys_are_exactly_the_four_fields() -> None:
    value = _usage(total_tokens=5)
    del value["payload"]["cache_hit_tokens"]
    assert _refused(value).pointer == "/payload/cache_hit_tokens"
    value = _usage(total_tokens=5)
    value["payload"]["cost"] = 1
    assert _refused(value).pointer == "/payload/cost"


def test_usage_requires_a_known_token_field() -> None:
    error = _refused(_usage())
    assert "at least one known token field" in str(error)
    assert error.pointer == "/payload"


def test_usage_payload_zero_is_unknown_without_sibling() -> None:
    assert ee.usage_payload(input_tokens=0) is None
    assert ee.usage_payload(output_tokens=0, input_tokens=None) is None
    assert ee.usage_payload(input_tokens=0, output_tokens=0, total_tokens=9) == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_hit_tokens": None,
        "total_tokens": 9,
    }


def test_usage_payload_keeps_zero_beside_nonzero_sibling() -> None:
    assert ee.usage_payload(input_tokens=0, output_tokens=5) == {
        "input_tokens": 0,
        "output_tokens": 5,
        "cache_hit_tokens": None,
        "total_tokens": None,
    }
    assert ee.usage_payload(input_tokens=7, output_tokens=0)["output_tokens"] == 0


def test_usage_payload_total_only() -> None:
    # codex reports no split: every directional field is 0 and only the total is real.
    assert ee.usage_payload(
        input_tokens=0, output_tokens=0, cache_hit_tokens=0, total_tokens=1234
    ) == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_hit_tokens": None,
        "total_tokens": 1234,
    }


def test_usage_payload_all_zero_is_none() -> None:
    assert ee.usage_payload(
        input_tokens=0, output_tokens=0, cache_hit_tokens=0, total_tokens=0
    ) is None
    assert ee.usage_payload() is None


def test_usage_payload_keeps_a_nonzero_cache_hit_and_refuses_bad_input() -> None:
    assert ee.usage_payload(input_tokens=10, output_tokens=2, cache_hit_tokens=4) == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_hit_tokens": 4,
        "total_tokens": None,
    }
    for bad in (-1, True, 1.0):
        with pytest.raises(ee.EventError):
            ee.usage_payload(input_tokens=bad)


def test_usage_payload_output_is_a_valid_usage_event() -> None:
    payload = ee.usage_payload(input_tokens=0, output_tokens=0, total_tokens=3)
    event = _make(event="usage", payload=payload)
    assert event["payload"]["total_tokens"] == 3


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "at",
    [
        "2026-09-29T20:00:00+00:00",
        "2026-09-29 20:00:00Z",
        "2026-09-29T20:00:00.1Z",
        "2026-09-29T20:00:00.1234567Z",
        "2026-13-01T00:00:00Z",
        "2026-09-29T20:00:00z",
        1727640000,
    ],
)
def test_at_must_be_utc_z(at: Any) -> None:
    error = _refused(_event(at=at))
    assert error.pointer == "/at"


def test_at_accepts_millisecond_to_microsecond_fractions() -> None:
    for at in ("2026-09-29T20:00:00.123Z", "2026-09-29T20:00:00.123456Z", AT):
        assert ee.validate_event(_event(at=at))["at"] == at


def test_utc_timestamp_accepts_epoch_string() -> None:
    assert ee.utc_timestamp("1727640000.25") == "2024-09-29T20:00:00.250000Z"
    assert ee.utc_timestamp("0") == "1970-01-01T00:00:00Z"
    assert ee.utc_timestamp(" 1727640000 ") == "2024-09-29T20:00:00Z"


def test_utc_timestamp_accepts_epoch_numbers_and_defaults_to_now() -> None:
    assert ee.utc_timestamp(0) == "1970-01-01T00:00:00Z"
    assert ee.utc_timestamp(1.5) == "1970-01-01T00:00:01.500000Z"
    now = ee.utc_timestamp()
    assert ee.validate_event(_event(at=now))["at"] == now


@pytest.mark.parametrize("bad", ["nan", "abc", "1e9", "-1", True, -1.0, float("inf"), [1]])
def test_utc_timestamp_refuses_other_values(bad: Any) -> None:
    with pytest.raises(ee.EventError):
        ee.utc_timestamp(bad)


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok", "a": (1, 2)},
        {"status": "ok", "a": {1, 2}},
        {"status": "ok", "a": float("nan")},
        {"status": "ok", "nested": {1: "x"}},
    ],
    ids=["tuple", "set", "nan", "int_key"],
)
def test_payload_must_be_json_native(payload: dict) -> None:
    error = _refused(_event(payload=payload))
    assert error.pointer.startswith("/payload")


def test_payload_must_be_a_mapping() -> None:
    assert _refused(_event(payload=["status"])).pointer == "/payload"


def test_payload_over_cap_is_refused() -> None:
    # Compact sorted-key ASCII: {"status":"s","x":"<n>"} is 21 + n bytes.
    assert len('{"status":"s","x":""}') == 21
    at_cap = {"status": "s", "x": "a" * (ee.MAX_PAYLOAD_BYTES - 21)}
    ee.validate_event(_event(payload=at_cap))
    over = {"status": "s", "x": "a" * (ee.MAX_PAYLOAD_BYTES - 20)}
    error = _refused(_event(payload=over))
    assert "cap" in str(error)
    assert error.pointer == "/payload"


# --------------------------------------------------------------------------
# make_event and the schema selector
# --------------------------------------------------------------------------


def test_make_event_builds_a_valid_envelope() -> None:
    event = _make()
    assert event == {
        "schema": ee.SCHEMA_V1,
        "seq": 0,
        "identity": {"run_id": "run-1", "unit_id": "unit-1", "attempt_id": "1"},
        "event": "result",
        "at": AT,
        "source": {"plugin": "job-kit"},
        "payload": {"status": "completed"},
    }
    defaulted = ee.make_event(seq=1, run_id="r", event="job-kit:x", plugin="job-kit")
    assert defaulted["payload"] == {}
    assert ee.validate_event(defaulted)["at"] == defaulted["at"]


def test_schema_v1_literal_is_frozen() -> None:
    assert ee.SCHEMA_V1 == "plugins-kit.execution-event/v1"
    assert ee.SCHEMA_V1 in ee.SUPPORTED_SCHEMAS
    assert isinstance(ee.SUPPORTED_SCHEMAS, frozenset)


def test_make_event_default_schema_is_v1() -> None:
    assert inspect.signature(ee.make_event).parameters["schema"].default == ee.SCHEMA_V1
    assert inspect.signature(ee.Emitter).parameters["schema"].default == ee.SCHEMA_V1
    assert _make()["schema"] == "plugins-kit.execution-event/v1"


def test_make_event_refuses_unsupported_schema_selector() -> None:
    with pytest.raises(ee.EventError) as info:
        _make(schema="plugins-kit.execution-event/v4")
    assert "selector" in str(info.value)
    assert info.value.pointer == "/schema"


def test_emitter_refuses_unsupported_schema_selector() -> None:
    with pytest.raises(ee.EventError) as info:
        ee.Emitter("job-kit", "r", schema="plugins-kit.execution-event/v4")
    assert "selector" in str(info.value)


def test_consumer_probe_shape_binds_real_constructors() -> None:
    """The section-2.8 probe a consumer runs, against the real module."""
    module = ee
    assert ee.SCHEMA_V1 in module.SUPPORTED_SCHEMAS
    # A v2 emitter (job-kit) requires both literals and binds schema=SCHEMA_V2.
    assert ee.SCHEMA_V2 in module.SUPPORTED_SCHEMAS
    for name in ("make_event", "utc_timestamp", "usage_payload", "validate_stream",
                 "Emitter", "JsonlSink", "InMemorySink", "read_jsonl", "validate_event"):
        assert callable(getattr(module, name)), name
    make_keywords = {
        "seq": 0, "run_id": "r", "event": "result", "plugin": "p", "at": AT,
        "unit_id": "u", "attempt_id": "1", "adapter": "a", "model": "m",
        "payload": {},
    }
    inspect.signature(module.make_event).bind(**make_keywords)
    inspect.signature(module.make_event).bind(**make_keywords, schema=ee.SCHEMA_V1)
    inspect.signature(module.make_event).bind(**make_keywords, schema=ee.SCHEMA_V2)
    inspect.signature(module.Emitter).bind("workflow-kit", "r", unit_id="u", sinks=())
    inspect.signature(module.Emitter).bind(
        "workflow-kit", "r", unit_id="u", sinks=(), start_seq=0, schema=ee.SCHEMA_V1
    )
    inspect.signature(module.Emitter).bind(
        "job-kit", "r", unit_id="u", sinks=(), start_seq=0, schema=ee.SCHEMA_V2
    )
    # The bound shape is also a working one under v2.
    built = module.make_event(**{**make_keywords, "event": "interrupt",
                                 "payload": _interrupt_payload()}, schema=ee.SCHEMA_V2)
    assert built["schema"] == ee.SCHEMA_V2
    # A v3 emitter (workflow-kit provider nodes) binds schema=SCHEMA_V3, and the
    # bound shape is a working one: a unit-scoped contract event.
    assert ee.SCHEMA_V3 in module.SUPPORTED_SCHEMAS
    inspect.signature(module.make_event).bind(**make_keywords, schema=ee.SCHEMA_V3)
    inspect.signature(module.Emitter).bind(
        "workflow-kit", "r", unit_id="u", sinks=(), schema=ee.SCHEMA_V3
    )
    contract = module.make_event(**{**make_keywords, "event": "contract",
                                    "attempt_id": None, "payload": _contract_payload()},
                                 schema=ee.SCHEMA_V3)
    assert contract["schema"] == ee.SCHEMA_V3
    v3_sink = module.InMemorySink()
    module.Emitter("workflow-kit", "r", unit_id="u", sinks=[v3_sink],
                   schema=ee.SCHEMA_V3).emit("contract", payload=_contract_payload())
    assert v3_sink.events[0]["schema"] == ee.SCHEMA_V3
    inspect.signature(module.Emitter.emit).bind(
        None, "result", unit_id="u", attempt_id="1", adapter="a", model="m",
        payload={}, at=AT,
    )
    inspect.signature(module.JsonlSink).bind("events.jsonl", mode="truncate")
    inspect.signature(module.usage_payload).bind(
        input_tokens=1, output_tokens=2, cache_hit_tokens=0, total_tokens=3
    )


# --------------------------------------------------------------------------
# Streams
# --------------------------------------------------------------------------


def _attempt(seq: int, name: str, unit: str = "u", attempt: str = "1",
             plugin: str = "job-kit", payload: dict | None = None) -> dict:
    if payload is None:
        payload = {"status": "completed"} if name == "result" else {}
    return _make(seq=seq, event=name, unit_id=unit, attempt_id=attempt,
                 plugin=plugin, payload=payload)


def _terminal(seq: int, unit: str = "u", plugin: str = "job-kit") -> dict:
    return _make(seq=seq, event="terminal", unit_id=unit, attempt_id=None,
                 plugin=plugin, payload={"state": "accepted"})


def test_a_well_formed_stream_is_accepted() -> None:
    stream = [
        _make(seq=0, event="job-kit:run-created", unit_id=None, attempt_id=None,
              payload={}),
        _attempt(1, "dispatch-selected"),
        _attempt(2, "call-started"),
        _attempt(3, "usage", payload=ee.usage_payload(input_tokens=1, output_tokens=2)),
        _attempt(4, "result"),
        _terminal(5),
        _make(seq=6, event="job-kit:applied", unit_id="u", attempt_id=None, payload={}),
    ]
    assert ee.validate_stream(stream) == tuple(stream)


def test_stream_refuses_duplicate_seq_in_group() -> None:
    with pytest.raises(ee.EventError) as info:
        ee.validate_stream([_attempt(0, "call-started"), _attempt(1, "result"),
                            _attempt(0, "call-started", attempt="2")])
    assert "duplicate seq" in str(info.value)
    assert info.value.pointer == "/2/seq"


def test_stream_refuses_decreasing_seq_in_group() -> None:
    with pytest.raises(ee.EventError) as info:
        ee.validate_stream([_attempt(5, "call-started"), _attempt(3, "result")])
    assert "does not increase" in str(info.value)
    assert info.value.pointer == "/1/seq"


def test_stream_allows_equal_seq_in_different_units() -> None:
    stream = [_attempt(0, "call-started", unit="u1"), _attempt(0, "call-started", unit="u2")]
    assert len(ee.validate_stream(stream)) == 2


def test_stream_groups_are_per_plugin_and_run() -> None:
    stream = [
        _attempt(0, "call-started", plugin="job-kit"),
        _attempt(0, "call-started", plugin="content-pipeline-kit"),
        _make(seq=0, run_id="run-2", event="call-started", unit_id="u", attempt_id="1"),
    ]
    assert len(ee.validate_stream(stream)) == 3


def test_stream_refuses_second_result_for_attempt() -> None:
    with pytest.raises(ee.EventError) as info:
        ee.validate_stream([_attempt(0, "result"), _attempt(1, "result")])
    assert "second result" in str(info.value)
    assert info.value.pointer == "/1/event"
    # A different attempt of the same unit may end separately.
    ee.validate_stream([_attempt(0, "result"), _attempt(1, "result", attempt="2")])


def test_stream_refuses_second_terminal_for_unit() -> None:
    with pytest.raises(ee.EventError) as info:
        ee.validate_stream([_terminal(0), _terminal(1)])
    assert "second terminal" in str(info.value)
    assert info.value.pointer == "/1/event"


def test_stream_refuses_attempt_event_after_terminal() -> None:
    with pytest.raises(ee.EventError) as info:
        ee.validate_stream([_attempt(0, "result"), _terminal(1),
                            _attempt(2, "call-started", attempt="2")])
    assert "after the unit's terminal" in str(info.value)
    assert info.value.pointer == "/2/event"


def test_stream_names_the_index_of_an_invalid_event() -> None:
    with pytest.raises(ee.EventError) as info:
        ee.validate_stream([_attempt(0, "result"), _event(seq=-1)])
    assert info.value.pointer == "/1/seq"


# --------------------------------------------------------------------------
# Emitter
# --------------------------------------------------------------------------


def test_emitter_binds_run_and_unit() -> None:
    sink = ee.InMemorySink()
    emitter = ee.Emitter("job-kit", "run-9", unit_id="unit-9", sinks=[sink], start_seq=4)
    first = emitter.emit("call-started", attempt_id="1", adapter="codex", model="m", at=AT)
    second = emitter.emit("terminal", payload={"state": "done"}, at=AT)
    other = emitter.emit("terminal", unit_id="unit-10", payload={"state": "done"}, at=AT)
    assert sink.events == [first, second, other]
    assert [event["seq"] for event in sink.events] == [4, 5, 6]
    for event in sink.events:
        assert event["source"]["plugin"] == "job-kit"
        assert event["identity"]["run_id"] == "run-9"
    assert first["identity"] == {"run_id": "run-9", "unit_id": "unit-9", "attempt_id": "1"}
    assert first["source"] == {"plugin": "job-kit", "adapter": "codex", "model": "m"}
    assert second["identity"] == {"run_id": "run-9", "unit_id": "unit-9"}
    assert other["identity"] == {"run_id": "run-9", "unit_id": "unit-10"}
    assert ee.validate_stream(sink.events) == tuple(sink.events)


def test_emitter_invalid_event_consumes_no_seq_and_writes_nothing() -> None:
    sink = ee.InMemorySink()
    emitter = ee.Emitter("job-kit", "r", unit_id="u", sinks=[sink])
    with pytest.raises(ee.EventError):
        emitter.emit("started")
    assert sink.events == []
    assert emitter.emit("job-kit:ok")["seq"] == 0


def test_emitter_refuses_bad_construction() -> None:
    for args, kwargs in (
        (("Job-kit", "r"), {}),
        (("job-kit", ""), {}),
        (("job-kit", "r"), {"unit_id": "a\x00"}),
        (("job-kit", "r"), {"start_seq": -1}),
        (("job-kit", "r"), {"start_seq": True}),
    ):
        with pytest.raises(ee.EventError):
            ee.Emitter(*args, **kwargs)


def test_emitter_schema_selector_stamps_every_event() -> None:
    sink = ee.InMemorySink()
    emitter = ee.Emitter("job-kit", "r", unit_id="u", sinks=[sink], schema=ee.SCHEMA_V2)
    emitter.emit("call-started", attempt_id="1")
    emitter.emit("interrupt", attempt_id="1", payload=_interrupt_payload())
    emitter.emit("terminal", payload={"state": "done"})
    assert [event["schema"] for event in sink.events] == [ee.SCHEMA_V2] * 3
    assert ee.validate_stream(sink.events) == tuple(sink.events)


def test_emitter_holds_seq_and_write_under_one_lock() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingSink:
        def __init__(self) -> None:
            self.order: list[int] = []
            self._calls = 0
            self._guard = threading.Lock()

        def write(self, event: dict) -> None:
            with self._guard:
                self._calls += 1
                first = self._calls == 1
            if first:
                entered.set()
                release.wait(30)
            self.order.append(event["seq"])

    sink = BlockingSink()
    emitter = ee.Emitter("job-kit", "r", unit_id="u", sinks=[sink])
    first = threading.Thread(target=emitter.emit, args=("job-kit:first",))
    second = threading.Thread(target=emitter.emit, args=("job-kit:second",))
    try:
        first.start()
        assert entered.wait(30), "first emit never reached its sink"
        second.start()
        second.join(timeout=0.5)
        second_finished_while_first_blocked = not second.is_alive()
    finally:
        release.set()
        first.join(30)
        second.join(30)
    assert not second_finished_while_first_blocked, (
        "a second emit completed while the first held its sink write"
    )
    assert sink.order == [0, 1]


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------


def test_in_memory_sink_collects_in_write_order() -> None:
    sink = ee.InMemorySink()
    assert sink.events == []
    sink.write(_attempt(1, "call-started"))
    sink.write(_attempt(0, "call-started", unit="v"))
    assert [event["seq"] for event in sink.events] == [1, 0]


def test_jsonl_create_mode_refuses_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("keep\n", encoding="ascii")
    with pytest.raises(FileExistsError):
        ee.JsonlSink(path)
    with pytest.raises(FileExistsError):
        ee.JsonlSink(path, mode="create")
    assert path.read_text(encoding="ascii") == "keep\n"


def test_jsonl_create_mode_writes_a_fresh_file(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = ee.JsonlSink(str(path))
    assert path.read_bytes() == b""
    sink.write(_attempt(0, "call-started"))
    assert ee.read_jsonl(path) == (_attempt(0, "call-started"),)


def test_jsonl_sink_refuses_an_unknown_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ee.JsonlSink(tmp_path / "e.jsonl", mode="append")
    assert not (tmp_path / "e.jsonl").exists()


def test_jsonl_sink_refuses_an_invalid_event(tmp_path: Path) -> None:
    path = tmp_path / "e.jsonl"
    sink = ee.JsonlSink(path)
    with pytest.raises(ee.EventError):
        sink.write(_event(seq=-1))
    assert path.read_bytes() == b""


def test_jsonl_truncate_mode_replaces_previous_stream(tmp_path: Path) -> None:
    path = tmp_path / "node.events.jsonl"
    first_run = ee.Emitter("workflow-kit", "r", unit_id="step-0",
                           sinks=[ee.JsonlSink(path, mode="truncate")])
    first_run.emit("call-started", attempt_id="1", at=AT)
    first_run.emit("result", attempt_id="1", payload={"status": "failed"}, at=AT)
    first_run.emit("terminal", payload={"state": "failed"}, at=AT)

    rerun = ee.Emitter("workflow-kit", "r", unit_id="step-0",
                       sinks=[ee.JsonlSink(path, mode="truncate")])
    last = [
        rerun.emit("call-started", attempt_id="1", at=AT),
        rerun.emit("terminal", payload={"state": "completed"}, at=AT),
    ]
    assert ee.read_jsonl(path) == tuple(last)


def test_jsonl_round_trip_is_ascii_one_line_per_event(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = ee.InMemorySink()
    emitter = ee.Emitter("job-kit", "r", unit_id="u",
                         sinks=[sink, ee.JsonlSink(path)])
    emitter.emit("result", attempt_id="1",
                 payload={"status": "ok", "note": "caf\u00e9 \u2192 done", "n": [1, 2.5, None]})
    emitter.emit("terminal", payload={"state": "done"})
    data = path.read_bytes()
    assert all(byte < 128 for byte in data)
    lines = data.decode("ascii").split("\n")
    assert lines[-1] == ""
    assert len(lines[:-1]) == 2
    for line, event in zip(lines[:-1], sink.events):
        assert line == json.dumps(event, sort_keys=True, ensure_ascii=True,
                                  separators=(",", ":"))
    assert ee.read_jsonl(path) == tuple(sink.events)


def test_read_jsonl_names_bad_line(tmp_path: Path) -> None:
    good = json.dumps(_attempt(0, "call-started"))
    path = tmp_path / "bad.jsonl"
    path.write_text(good + "\n" + json.dumps(_event(seq=-1)) + "\n", encoding="ascii")
    with pytest.raises(ee.EventError) as info:
        ee.read_jsonl(path)
    assert str(info.value).startswith("line 2:")
    assert info.value.pointer == "/seq"

    path.write_text(good + "\n" + good + "\n{not json\n", encoding="ascii")
    with pytest.raises(ee.EventError) as info:
        ee.read_jsonl(path)
    assert str(info.value).startswith("line 3:")


def test_read_jsonl_validates_the_file_as_one_stream(tmp_path: Path) -> None:
    path = tmp_path / "twice.jsonl"
    line = json.dumps(_terminal(0))
    path.write_text(line + "\n" + json.dumps(_terminal(1)) + "\n", encoding="ascii")
    with pytest.raises(ee.EventError) as info:
        ee.read_jsonl(path)
    assert str(info.value).startswith("line 2:")
    assert "second terminal" in str(info.value)


def test_read_jsonl_refuses_non_json_constants(tmp_path: Path) -> None:
    path = tmp_path / "nan.jsonl"
    text = json.dumps(_attempt(0, "call-started")).replace("{}", '{"x": NaN}')
    path.write_text(text + "\n", encoding="ascii")
    with pytest.raises(ee.EventError) as info:
        ee.read_jsonl(path)
    assert str(info.value).startswith("line 1:")


# --------------------------------------------------------------------------
# Schema v2: v1 plus the `interrupt` event
# --------------------------------------------------------------------------


def _interrupt_payload(**overrides: Any) -> dict:
    payload: dict[str, Any] = {"interrupt_id": "int-1", "kind": "approval", "phase": "requested"}
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not _DROP}


_DROP = object()


def _interrupt(**payload_overrides: Any) -> dict:
    """A valid v2 `interrupt` event (raw, for validate_event)."""
    return _event(schema=ee.SCHEMA_V2, event="interrupt",
                  payload=_interrupt_payload(**payload_overrides))


def _v2(seq: int, phase: str, interrupt_id: str = "int-1", attempt: str = "1",
        unit: str = "u") -> dict:
    return _make(seq=seq, event="interrupt", unit_id=unit, attempt_id=attempt,
                 payload=_interrupt_payload(interrupt_id=interrupt_id, phase=phase),
                 schema=ee.SCHEMA_V2)


def _stream_refused(events: list) -> ee.EventError:
    with pytest.raises(ee.EventError) as info:
        ee.validate_stream(events)
    return info.value


def test_schema_v2_literal_is_frozen() -> None:
    assert ee.SCHEMA_V2 == "plugins-kit.execution-event/v2"


def test_supported_schemas_are_v1_v2_v3() -> None:
    assert ee.SUPPORTED_SCHEMAS == frozenset(
        {"plugins-kit.execution-event/v1", "plugins-kit.execution-event/v2",
         "plugins-kit.execution-event/v3"}
    )


def test_v1_core_events_are_frozen() -> None:
    v1_core = frozenset({"dispatch-selected", "call-started", "usage", "result", "terminal"})
    v1_scoped = frozenset({"dispatch-selected", "call-started", "usage", "result"})
    assert ee.CORE_EVENTS == v1_core
    assert ee.ATTEMPT_SCOPED == v1_scoped
    assert ee.LATER_REVISION_NAMES == frozenset({"contract", "interrupt"})
    assert ee.CORE_EVENTS_V2 == v1_core | {"interrupt"}
    assert ee.ATTEMPT_SCOPED_V2 == v1_scoped | {"interrupt"}
    assert ee.INTERRUPT_PHASES == frozenset({"requested", "resolved", "rejected", "expired"})
    assert ee.INTERRUPT_PAYLOAD_KEYS == frozenset(
        {"interrupt_id", "kind", "phase", "expires_at", "continuation_no"}
    )


def test_v1_event_serialization_is_unchanged(tmp_path: Path) -> None:
    """A fixed v1 event against a literal line: v2 must not move one v1 byte."""
    literal = (
        '{"at":"2026-09-29T20:00:00.123Z","event":"result",'
        '"identity":{"attempt_id":"2","run_id":"run-1","unit_id":"unit-1"},'
        '"payload":{"acceptance":"accepted","n":[1,2.5,null],"note":"caf\\u00e9",'
        '"status":"completed"},'
        '"schema":"plugins-kit.execution-event/v1","seq":7,'
        '"source":{"adapter":"openrouter","model":"m","plugin":"job-kit"}}'
    )
    kwargs = dict(
        seq=7, run_id="run-1", event="result", plugin="job-kit",
        at="2026-09-29T20:00:00.123Z", unit_id="unit-1", attempt_id="2",
        adapter="openrouter", model="m",
        payload={"status": "completed", "acceptance": "accepted",
                 "note": "caf" + chr(0xE9), "n": [1, 2.5, None]},
    )
    event = ee.make_event(**kwargs)
    assert event == ee.make_event(**kwargs, schema=ee.SCHEMA_V1)
    assert list(event) == ["schema", "seq", "identity", "event", "at", "source", "payload"]
    assert list(event["identity"]) == ["run_id", "unit_id", "attempt_id"]
    assert list(event["source"]) == ["plugin", "adapter", "model"]
    path = tmp_path / "v1.jsonl"
    ee.JsonlSink(path).write(event)
    assert path.read_bytes() == (literal + "\n").encode("ascii")
    assert ee.read_jsonl(path) == (event,)


def test_v2_accepts_interrupt_event() -> None:
    minimal = ee.validate_event(_interrupt())
    assert minimal["schema"] == ee.SCHEMA_V2
    assert minimal["event"] == "interrupt"
    assert minimal["payload"] == {"interrupt_id": "int-1", "kind": "approval",
                                  "phase": "requested"}
    full = ee.validate_event(_interrupt(expires_at="2026-10-01T00:00:00Z", continuation_no=0))
    assert full["payload"]["expires_at"] == "2026-10-01T00:00:00Z"
    assert full["payload"]["continuation_no"] == 0
    for phase in sorted(ee.INTERRUPT_PHASES):
        assert ee.validate_event(_interrupt(phase=phase))["payload"]["phase"] == phase
    built = _v2(0, "requested")
    assert built["schema"] == ee.SCHEMA_V2


@pytest.mark.parametrize("name", sorted(ee.CORE_EVENTS))
def test_v2_accepts_every_v1_core_name(name: str) -> None:
    payload: dict = {}
    identity = {"run_id": "r", "unit_id": "u", "attempt_id": "1"}
    if name == "usage":
        payload = ee.usage_payload(input_tokens=3, output_tokens=4)
    elif name == "result":
        payload = {"status": "completed"}
    elif name == "terminal":
        payload = {"state": "done"}
        identity = {"run_id": "r", "unit_id": "u"}
    value = ee.validate_event(_event(schema=ee.SCHEMA_V2, event=name, identity=identity,
                                     payload=payload))
    assert value["event"] == name
    # ... under the v1 rules: the same required payload is still required.
    if name in ("result", "terminal", "usage"):
        bad = _refused(_event(schema=ee.SCHEMA_V2, event=name, identity=identity,
                              payload={}))
        assert bad.pointer.startswith("/payload")


def test_v2_refuses_contract_as_later_revision() -> None:
    error = _refused(_event(schema=ee.SCHEMA_V2, event="contract", payload={}))
    assert str(error) == (
        "event 'contract' is defined by a later schema revision; "
        "plugins-kit.execution-event/v2 does not accept it"
    )
    assert error.pointer == "/event"


def test_v1_refusal_wording_is_unchanged() -> None:
    error = _refused(_event(event="interrupt", payload=_interrupt_payload()))
    assert str(error) == (
        "event 'interrupt' is defined by a later schema revision; "
        "plugins-kit.execution-event/v1 does not accept it"
    )
    error = _refused(_event(event="started"))
    assert str(error) == (
        "unknown event 'started': use a core name "
        "(call-started, dispatch-selected, result, terminal, usage) or '<plugin>:<name>'"
    )


def test_interrupt_requires_unit_and_attempt() -> None:
    for identity in ({"run_id": "r", "unit_id": "u"}, {"run_id": "r"}):
        error = _refused(_event(schema=ee.SCHEMA_V2, event="interrupt", identity=identity,
                                payload=_interrupt_payload()))
        assert "attempt-scoped" in str(error), identity
        assert error.pointer == "/identity"


@pytest.mark.parametrize(
    "value",
    [_DROP, "", "x" * 201, "a\nb", 7],
    ids=["missing", "empty", "too_long", "control_char", "non_string"],
)
def test_interrupt_id_is_bounded(value: Any) -> None:
    error = _refused(_interrupt(interrupt_id=value))
    assert error.pointer == "/payload/interrupt_id"
    at_bound = ee.validate_event(_interrupt(interrupt_id="x" * 200))
    assert at_bound["payload"]["interrupt_id"] == "x" * 200


def test_interrupt_kind_pattern_is_enforced() -> None:
    for kind in ("Approval", "1x", "a_b", "", "a:b", 3, _DROP):
        error = _refused(_interrupt(kind=kind))
        assert error.pointer == "/payload/kind", kind
    for kind in ("approval", "free-text-answer", "k8s"):
        assert ee.validate_event(_interrupt(kind=kind))["payload"]["kind"] == kind


def test_interrupt_phase_is_a_closed_set() -> None:
    for phase in ("answered", "Requested", "", None, 1, _DROP):
        error = _refused(_interrupt(phase=phase))
        assert error.pointer == "/payload/phase", phase


@pytest.mark.parametrize(
    "key", ["payload", "request_schema", "request_payload", "input", "resolution"]
)
def test_interrupt_payload_refuses_content_key(key: str) -> None:
    error = _refused(_interrupt(**{key: {"answer": "yes"}}))
    assert "closed" in str(error)
    assert error.pointer == "/payload/" + key
    # A scalar under the same key is refused too: the key, not the shape, is barred.
    assert _refused(_interrupt(**{key: "x"})).pointer == "/payload/" + key


def test_interrupt_payload_refuses_unknown_key() -> None:
    error = _refused(_interrupt(note="n"))
    assert "unknown key 'note'" in str(error)
    assert error.pointer == "/payload/note"


def test_interrupt_payload_refuses_reason() -> None:
    for reason in ("operator said no", ""):
        error = _refused(_interrupt(phase="rejected", reason=reason))
        assert "unknown key 'reason'" in str(error)
        assert error.pointer == "/payload/reason"


@pytest.mark.parametrize(
    "value",
    ["2026-10-01T00:00:00+00:00", "2026-10-01", "2026-13-01T00:00:00Z", 1727640000, None],
)
def test_interrupt_expires_at_must_be_utc_z(value: Any) -> None:
    error = _refused(_interrupt(expires_at=value))
    assert error.pointer == "/payload/expires_at"


@pytest.mark.parametrize("value", [-1, True, 1.0], ids=["negative", "bool", "float"])
def test_interrupt_continuation_no_is_non_negative_int(value: Any) -> None:
    error = _refused(_interrupt(continuation_no=value))
    assert error.pointer == "/payload/continuation_no"
    assert ee.validate_event(_interrupt(continuation_no=3))["payload"]["continuation_no"] == 3


def test_interrupt_lifecycle_stream_is_accepted() -> None:
    stream = [
        _attempt(0, "call-started"),
        _attempt(1, "result", payload={"status": "completed",
                                       "acceptance": "interrupt_requested"}),
        _v2(2, "requested"),
        _v2(3, "resolved"),
        _make(seq=4, event="job-kit:continuation-started", unit_id="u", attempt_id="1",
              payload={"interrupt_id": "int-1", "continuation_no": 1}),
        _v2(5, "requested", interrupt_id="int-2"),
        _v2(6, "rejected", interrupt_id="int-2"),
        _terminal(7),
    ]
    assert ee.validate_stream(stream) == tuple(stream)
    # The same interrupt id on another attempt is a different key.
    ee.validate_stream([_v2(0, "requested"), _v2(1, "expired"),
                        _v2(2, "requested", attempt="2")])


def test_stream_refuses_interrupt_close_before_request() -> None:
    for phase in ("resolved", "rejected", "expired"):
        error = _stream_refused([_attempt(0, "result"), _v2(1, phase)])
        assert "before its request" in str(error), phase
        assert error.pointer == "/1/payload/phase"


def test_stream_refuses_second_interrupt_request() -> None:
    error = _stream_refused([_v2(0, "requested"), _v2(1, "requested")])
    assert "second interrupt request" in str(error)
    assert error.pointer == "/1/payload/phase"


def test_stream_refuses_second_interrupt_close() -> None:
    error = _stream_refused([_v2(0, "requested"), _v2(1, "resolved"), _v2(2, "expired")])
    assert "second interrupt close" in str(error)
    assert error.pointer == "/2/payload/phase"


def test_stream_refuses_interrupt_event_after_close() -> None:
    error = _stream_refused([_v2(0, "requested"), _v2(1, "rejected"), _v2(2, "requested")])
    assert "after its close" in str(error)
    assert error.pointer == "/2/payload/phase"


def test_stream_refuses_interrupt_after_unit_terminal() -> None:
    error = _stream_refused([_v2(0, "requested"), _terminal(1), _v2(2, "expired")])
    assert "after the unit's terminal" in str(error)
    assert error.pointer == "/2/event"


def test_stream_seq_orders_across_v1_and_v2() -> None:
    mixed = [_attempt(0, "call-started"), _v2(1, "requested"), _v2(2, "resolved"),
             _terminal(3)]
    assert ee.validate_stream(mixed) == tuple(mixed)
    error = _stream_refused([_attempt(5, "call-started"), _v2(3, "requested")])
    assert "does not increase" in str(error)
    assert error.pointer == "/1/seq"
    error = _stream_refused([_attempt(4, "call-started"), _v2(4, "requested")])
    assert "duplicate seq" in str(error)


def test_emitter_default_schema_is_v1() -> None:
    assert inspect.signature(ee.Emitter).parameters["schema"].default == ee.SCHEMA_V1
    sink = ee.InMemorySink()
    emitter = ee.Emitter("job-kit", "r", unit_id="u", sinks=[sink])
    emitter.emit("call-started", attempt_id="1", at=AT)
    assert sink.events[0]["schema"] == "plugins-kit.execution-event/v1"
    with pytest.raises(ee.EventError) as info:
        emitter.emit("interrupt", attempt_id="1", payload=_interrupt_payload())
    assert "later schema revision" in str(info.value)


def test_read_jsonl_accepts_mixed_v1_v2_stream(tmp_path: Path) -> None:
    path = tmp_path / "mixed.jsonl"
    sink = ee.JsonlSink(path)
    events = [
        _attempt(0, "call-started"),
        _attempt(1, "result"),
        _v2(2, "requested"),
        _v2(3, "resolved"),
        _terminal(4),
    ]
    for event in events:
        sink.write(event)
    assert ee.read_jsonl(path) == tuple(events)
    assert [event["schema"] for event in ee.read_jsonl(path)] == [
        ee.SCHEMA_V1, ee.SCHEMA_V1, ee.SCHEMA_V2, ee.SCHEMA_V2, ee.SCHEMA_V1
    ]


# --------------------------------------------------------------------------
# Schema v3: v2 plus the unit-scoped `contract` event
# --------------------------------------------------------------------------


DIGEST = "0123456789abcdef" * 4


def _contract_payload(**overrides: Any) -> dict:
    payload: dict[str, Any] = {"artifact": "report", "kind": "schema",
                               "verdict": "satisfied", "schema_digest": DIGEST}
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not _DROP}


def _contract(identity: dict | None = None, **payload_overrides: Any) -> dict:
    """A valid v3 `contract` event (raw, for validate_event)."""
    if identity is None:
        identity = {"run_id": "run-1", "unit_id": "unit-1"}
    return _event(schema=ee.SCHEMA_V3, event="contract", identity=identity,
                  source={"plugin": "workflow-kit"},
                  payload=_contract_payload(**payload_overrides))


def _v3(seq: int, artifact: str = "report", unit: str = "u", **payload_overrides: Any) -> dict:
    return _make(seq=seq, event="contract", unit_id=unit, attempt_id=None,
                 payload=_contract_payload(artifact=artifact, **payload_overrides),
                 schema=ee.SCHEMA_V3)


def test_schema_v3_literal_is_frozen() -> None:
    assert ee.SCHEMA_V3 == "plugins-kit.execution-event/v3"


def test_v2_core_events_are_frozen() -> None:
    v1_core = frozenset({"dispatch-selected", "call-started", "usage", "result", "terminal"})
    v1_scoped = frozenset({"dispatch-selected", "call-started", "usage", "result"})
    assert ee.CORE_EVENTS_V2 == v1_core | {"interrupt"}
    assert ee.ATTEMPT_SCOPED_V2 == v1_scoped | {"interrupt"}
    assert "contract" not in ee.CORE_EVENTS_V2


def test_v3_vocabulary_and_contract_sets() -> None:
    assert ee.CORE_EVENTS_V3 == ee.CORE_EVENTS_V2 | {"contract"}
    assert ee.ATTEMPT_SCOPED_V3 == ee.ATTEMPT_SCOPED_V2
    assert "contract" not in ee.ATTEMPT_SCOPED_V3
    assert ee.CONTRACT_KINDS == frozenset({"schema", "opaque-file"})
    assert ee.CONTRACT_VERDICTS == frozenset({"satisfied", "violated", "missing"})
    assert ee.CONTRACT_PAYLOAD_KEYS == frozenset(
        {"artifact", "kind", "verdict", "schema_digest", "error_count"}
    )


def test_v3_accepts_contract_event() -> None:
    minimal = ee.validate_event(_contract())
    assert minimal["schema"] == ee.SCHEMA_V3
    assert minimal["event"] == "contract"
    assert minimal["identity"] == {"run_id": "run-1", "unit_id": "unit-1"}
    assert minimal["payload"] == {"artifact": "report", "kind": "schema",
                                  "verdict": "satisfied", "schema_digest": DIGEST}
    violated = ee.validate_event(_contract(verdict="violated", error_count=3))
    assert violated["payload"]["error_count"] == 3
    assert ee.validate_event(_contract(verdict="missing"))["payload"]["verdict"] == "missing"
    for verdict in ("satisfied", "missing"):
        opaque = ee.validate_event(_contract(kind="opaque-file", verdict=verdict,
                                             schema_digest=_DROP))
        assert opaque["payload"] == {"artifact": "report", "kind": "opaque-file",
                                     "verdict": verdict}
    built = _v3(0)
    assert built["schema"] == ee.SCHEMA_V3
    assert built["identity"] == {"run_id": "run-1", "unit_id": "u"}


@pytest.mark.parametrize("name", sorted(ee.CORE_EVENTS_V2))
def test_v3_accepts_every_v2_core_name(name: str) -> None:
    payload: dict = {}
    identity = {"run_id": "r", "unit_id": "u", "attempt_id": "1"}
    if name == "usage":
        payload = ee.usage_payload(input_tokens=3, output_tokens=4)
    elif name == "result":
        payload = {"status": "completed"}
    elif name == "terminal":
        payload = {"state": "done"}
        identity = {"run_id": "r", "unit_id": "u"}
    elif name == "interrupt":
        payload = _interrupt_payload()
    value = ee.validate_event(_event(schema=ee.SCHEMA_V3, event=name, identity=identity,
                                     payload=payload))
    assert value["event"] == name
    assert value["schema"] == ee.SCHEMA_V3
    # ... under the v2 rules: the same required payload is still required.
    if name in ("result", "terminal", "usage", "interrupt"):
        bad = _refused(_event(schema=ee.SCHEMA_V3, event=name, identity=identity,
                              payload={}))
        assert bad.pointer.startswith("/payload")


def test_v3_interrupt_keeps_v2_payload_and_stream_rules() -> None:
    def v3_interrupt(seq: int, phase: str, **extra: Any) -> dict:
        return _make(seq=seq, event="interrupt", unit_id="u", attempt_id="1",
                     payload=_interrupt_payload(phase=phase, **extra), schema=ee.SCHEMA_V3)

    # Payload: the closed key set and the attempt scope hold under v3.
    error = _refused(_event(schema=ee.SCHEMA_V3, event="interrupt",
                            payload=_interrupt_payload(request_payload={"q": 1})))
    assert "closed" in str(error)
    assert error.pointer == "/payload/request_payload"
    error = _refused(_event(schema=ee.SCHEMA_V3, event="interrupt",
                            identity={"run_id": "r", "unit_id": "u"},
                            payload=_interrupt_payload()))
    assert "attempt-scoped" in str(error)
    # Stream: the lifecycle rules hold under v3.
    assert len(ee.validate_stream([v3_interrupt(0, "requested"),
                                   v3_interrupt(1, "resolved")])) == 2
    error = _stream_refused([_attempt(0, "result"), v3_interrupt(1, "resolved")])
    assert "before its request" in str(error)
    error = _stream_refused([v3_interrupt(0, "requested"), v3_interrupt(1, "requested")])
    assert "second interrupt request" in str(error)
    error = _stream_refused([v3_interrupt(0, "requested"), _terminal(1),
                             v3_interrupt(2, "expired")])
    assert "after the unit's terminal" in str(error)


def test_contract_requires_unit_id() -> None:
    error = _refused(_contract(identity={"run_id": "r"}))
    assert "unit-scoped" in str(error)
    assert error.pointer == "/identity"
    with pytest.raises(ee.EventError):
        _make(event="contract", unit_id=None, attempt_id=None,
              payload=_contract_payload(), schema=ee.SCHEMA_V3)


def test_contract_forbids_attempt_id() -> None:
    error = _refused(_contract(identity={"run_id": "r", "unit_id": "u", "attempt_id": "1"}))
    assert "must not carry attempt_id" in str(error)
    assert error.pointer == "/identity/attempt_id"


@pytest.mark.parametrize(
    "value",
    [_DROP, "", "x" * 201, "a\nb", 7],
    ids=["missing", "empty", "too_long", "control_char", "non_string"],
)
def test_contract_artifact_is_bounded(value: Any) -> None:
    error = _refused(_contract(artifact=value))
    assert error.pointer == "/payload/artifact"
    at_bound = ee.validate_event(_contract(artifact="x" * 200))
    assert at_bound["payload"]["artifact"] == "x" * 200


def test_contract_kind_is_a_closed_set() -> None:
    for kind in ("json", "Schema", "opaque_file", "", None, 3, ["schema"], _DROP):
        error = _refused(_contract(kind=kind))
        assert error.pointer == "/payload/kind", kind


def test_contract_verdict_is_a_closed_set() -> None:
    for verdict in ("passed", "Satisfied", "invalid", "", None, 1, ["missing"], _DROP):
        error = _refused(_contract(verdict=verdict))
        assert error.pointer == "/payload/verdict", verdict


@pytest.mark.parametrize(
    "overrides",
    [{"schema_digest": _DROP}, {"kind": "opaque-file"}],
    ids=["missing_for_schema", "present_for_opaque"],
)
def test_contract_schema_digest_iff_schema_kind(overrides: dict) -> None:
    error = _refused(_contract(**overrides))
    assert error.pointer == "/payload/schema_digest"


@pytest.mark.parametrize(
    "digest",
    ["0123456789abcdef" * 3, "0123456789ABCDEF" * 4, "0123456789abcdeg" * 4],
    ids=["short", "upper", "non_hex"],
)
def test_contract_schema_digest_is_64_lowercase_hex(digest: str) -> None:
    error = _refused(_contract(schema_digest=digest))
    assert "64 lowercase hex" in str(error)
    assert error.pointer == "/payload/schema_digest"


@pytest.mark.parametrize(
    "overrides",
    [{"verdict": "violated"}, {"verdict": "satisfied", "error_count": 1}],
    ids=["missing", "present_when_satisfied"],
)
def test_contract_error_count_iff_violated(overrides: dict) -> None:
    error = _refused(_contract(**overrides))
    assert error.pointer == "/payload/error_count"
    # Neither `missing` nor an opaque-file artifact carries a count either.
    assert _refused(_contract(verdict="missing", error_count=2)).pointer == (
        "/payload/error_count"
    )


@pytest.mark.parametrize("value", [0, True, 1.0], ids=["zero", "bool", "float"])
def test_contract_error_count_is_positive_int(value: Any) -> None:
    error = _refused(_contract(verdict="violated", error_count=value))
    assert error.pointer == "/payload/error_count"
    assert ee.validate_event(_contract(verdict="violated", error_count=1))


def test_contract_opaque_cannot_be_violated() -> None:
    for extra in ({"error_count": 1}, {}):
        error = _refused(_contract(kind="opaque-file", verdict="violated",
                                   schema_digest=_DROP, **extra))
        assert "requires kind 'schema'" in str(error)
        assert error.pointer == "/payload/verdict"


@pytest.mark.parametrize("key", ["errors", "value", "payload", "reason", "message"])
def test_contract_payload_refuses_content_key(key: str) -> None:
    error = _refused(_contract(verdict="violated", error_count=1,
                               **{key: [{"pointer": "/a", "message": "bad"}]}))
    assert "closed" in str(error)
    assert error.pointer == "/payload/" + key
    # A scalar under the same key is refused too: the key, not the shape, is barred.
    assert _refused(_contract(**{key: "x"})).pointer == "/payload/" + key


def test_contract_payload_refuses_unknown_key() -> None:
    error = _refused(_contract(note="n"))
    assert "unknown key 'note'" in str(error)
    assert error.pointer == "/payload/note"


def test_stream_refuses_second_contract_for_artifact() -> None:
    for second in (_v3(1), _v3(1, verdict="missing")):
        error = _stream_refused([_v3(0), second])
        assert "second contract" in str(error)
        assert error.pointer == "/1/payload/artifact"


def test_stream_accepts_contracts_for_two_artifacts() -> None:
    stream = [_v3(0, artifact="report"), _v3(1, artifact="summary"),
              _v3(0, artifact="report", unit="v")]
    assert ee.validate_stream(stream) == tuple(stream)


def test_stream_refuses_contract_after_unit_terminal() -> None:
    ok = [_attempt(0, "result"), _v3(1), _terminal(2)]
    assert ee.validate_stream(ok) == tuple(ok)
    error = _stream_refused([_attempt(0, "result"), _terminal(1), _v3(2)])
    assert "contract after the unit's terminal" in str(error)
    assert error.pointer == "/2/event"
    # Another unit's terminal does not close this unit.
    ee.validate_stream([_terminal(0, unit="v"), _v3(1)])


def test_stream_seq_orders_across_v1_v2_v3() -> None:
    mixed = [_attempt(0, "call-started"), _v2(1, "requested"), _v2(2, "resolved"),
             _attempt(3, "result"), _v3(4), _terminal(5)]
    assert ee.validate_stream(mixed) == tuple(mixed)
    error = _stream_refused([_v2(5, "requested"), _v3(3)])
    assert "does not increase" in str(error)
    assert error.pointer == "/1/seq"
    error = _stream_refused([_attempt(4, "call-started"), _v3(4)])
    assert "duplicate seq" in str(error)


def test_v2_event_serialization_is_unchanged(tmp_path: Path) -> None:
    """A fixed v2 event against a literal line: v3 must not move one v2 byte."""
    literal = (
        '{"at":"2026-09-29T20:00:00.123Z","event":"interrupt",'
        '"identity":{"attempt_id":"2","run_id":"run-1","unit_id":"unit-1"},'
        '"payload":{"continuation_no":0,"expires_at":"2026-10-01T00:00:00Z",'
        '"interrupt_id":"int-1","kind":"approval","phase":"requested"},'
        '"schema":"plugins-kit.execution-event/v2","seq":7,'
        '"source":{"adapter":"openrouter","model":"m","plugin":"job-kit"}}'
    )
    event = ee.make_event(
        seq=7, run_id="run-1", event="interrupt", plugin="job-kit",
        at="2026-09-29T20:00:00.123Z", unit_id="unit-1", attempt_id="2",
        adapter="openrouter", model="m", schema=ee.SCHEMA_V2,
        payload={"phase": "requested", "kind": "approval", "interrupt_id": "int-1",
                 "expires_at": "2026-10-01T00:00:00Z", "continuation_no": 0},
    )
    assert list(event) == ["schema", "seq", "identity", "event", "at", "source", "payload"]
    assert list(event["identity"]) == ["run_id", "unit_id", "attempt_id"]
    assert list(event["source"]) == ["plugin", "adapter", "model"]
    path = tmp_path / "v2.jsonl"
    ee.JsonlSink(path).write(event)
    assert path.read_bytes() == (literal + "\n").encode("ascii")
    assert ee.read_jsonl(path) == (event,)


def test_emitter_v3_stamps_every_event() -> None:
    sink = ee.InMemorySink()
    emitter = ee.Emitter("workflow-kit", "r", unit_id="u", sinks=[sink], schema=ee.SCHEMA_V3)
    emitter.emit("call-started", attempt_id="1")
    emitter.emit("result", attempt_id="1", payload={"status": "completed"})
    emitter.emit("contract", payload=_contract_payload())
    emitter.emit("terminal", payload={"state": "done"})
    assert [event["schema"] for event in sink.events] == [ee.SCHEMA_V3] * 4
    assert [event["seq"] for event in sink.events] == [0, 1, 2, 3]
    assert sink.events[2]["identity"] == {"run_id": "r", "unit_id": "u"}
    assert ee.validate_stream(sink.events) == tuple(sink.events)
    # A contract with an attempt id is refused and consumes no seq.
    with pytest.raises(ee.EventError):
        emitter.emit("contract", attempt_id="1", payload=_contract_payload(artifact="b"))
    assert len(sink.events) == 4


def test_read_jsonl_accepts_mixed_v1_v2_v3_stream(tmp_path: Path) -> None:
    path = tmp_path / "mixed.jsonl"
    sink = ee.JsonlSink(path)
    events = [
        _attempt(0, "call-started"),
        _v2(1, "requested"),
        _v2(2, "resolved"),
        _attempt(3, "result"),
        _v3(4, verdict="violated", error_count=2),
        _v3(5, artifact="log", kind="opaque-file", schema_digest=_DROP),
        _terminal(6),
    ]
    for event in events:
        sink.write(event)
    assert ee.read_jsonl(path) == tuple(events)
    assert [event["schema"] for event in ee.read_jsonl(path)] == [
        ee.SCHEMA_V1, ee.SCHEMA_V2, ee.SCHEMA_V2, ee.SCHEMA_V1,
        ee.SCHEMA_V3, ee.SCHEMA_V3, ee.SCHEMA_V1,
    ]


# --------------------------------------------------------------------------
# Module boundary
# --------------------------------------------------------------------------


def test_module_imports_stdlib_only() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    relative: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.append(node.lineno)
            elif node.module:
                imported.add(node.module.split(".")[0])
    assert not relative, f"relative imports at lines {relative}"
    assert "bootstrap_lib" not in imported
    assert imported <= set(sys.stdlib_module_names), imported - set(
        sys.stdlib_module_names
    )


def test_public_surface_is_the_documented_one() -> None:
    assert sorted(ee.__all__) == sorted([
        "OWNER", "SCHEMA_V1", "SUPPORTED_SCHEMAS", "CORE_EVENTS", "ATTEMPT_SCOPED",
        "LATER_REVISION_NAMES", "MAX_PAYLOAD_BYTES", "EventError", "utc_timestamp",
        "usage_payload", "make_event", "validate_event", "validate_stream", "Emitter",
        "InMemorySink", "JsonlSink", "read_jsonl",
        "SCHEMA_V2", "CORE_EVENTS_V2", "ATTEMPT_SCOPED_V2", "INTERRUPT_PHASES",
        "INTERRUPT_PAYLOAD_KEYS",
        "SCHEMA_V3", "CORE_EVENTS_V3", "ATTEMPT_SCOPED_V3", "CONTRACT_KINDS",
        "CONTRACT_VERDICTS", "CONTRACT_PAYLOAD_KEYS",
    ])
    for name in ee.__all__:
        assert hasattr(ee, name), name
    assert issubclass(ee.EventError, ValueError)
    assert ee.EventError("x").pointer == ""
