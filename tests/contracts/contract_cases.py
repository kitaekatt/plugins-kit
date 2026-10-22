"""The fixed case list the contract goldens are captured from and checked against.

Each case is a function of a `Ctx` (the three contract modules plus a scratch
directory). `outcome(fn, ctx)` runs one and reduces the result, or the
exception it raised, to plain data: that data is what
tests/contracts/contract_goldens.py freezes as literals.

The goldens were captured once from the bootstrap_lib modules at commit
a1257278 (the capture script is not kept: it ran this file against the three
modules extracted from that commit). They are never regenerated from the
canonical package: regenerating would make the check agree with whatever the
package does today, which is the property it exists to test.

A case input is a literal in this file, so the case list cannot drift with the
code under test. Adding a case means capturing its golden from the reference
modules at a1257278 and adding both in one commit.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class Ctx:
    mods: Any  # .execution_event, .interrupt_contract, .model_declaration
    tmp: Path


DIGEST = "0123456789abcdef" * 4


# --- reduction to plain data ----------------------------------------------

def plain(value: Any) -> Any:
    """Reduce a result to builtin data whose repr states its exact shape."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (type(value).__name__,
                {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)})
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, tuple):
        return tuple(plain(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return (type(value).__name__, sorted(plain(item) for item in value))
    return value


def outcome(fn: Callable[[Ctx], Any], ctx: Ctx, class_only: bool = False) -> Any:
    """('ok', result) or ('raised', class name, base class names, message, extras).

    `class_only` drops the message of a case whose text is the standard library's
    own and differs between Python releases.
    """
    try:
        result = fn(ctx)
    except Exception as exc:  # noqa: BLE001 - the case records every class
        extras = {}
        for name in ("pointer", "index", "errors"):
            if hasattr(exc, name):
                extras[name] = plain(getattr(exc, name))
        bases = [cls.__name__ for cls in type(exc).__mro__[1:-2]]
        return ("raised", type(exc).__name__, bases, None if class_only else str(exc), extras)
    return ("ok", plain(result))


# --- helpers ----------------------------------------------------------------

def _event(m, **over):
    args = dict(seq=0, run_id="run-1", event="terminal", plugin="demo",
                at="2026-01-02T03:04:05Z", payload={"state": "done"})
    args.update(over)
    return m.execution_event.make_event(**args)


def _attempt(m, event, payload, **over):
    return _event(m, event=event, payload=payload, unit_id="u1", attempt_id="a1", **over)


def _interrupt(phase, seq, **extra):
    return dict(seq=seq, event="interrupt", unit_id="u1", attempt_id="a1",
                schema="plugins-kit.execution-event/v2",
                payload={"interrupt_id": "i1", "kind": "ask-user", "phase": phase, **extra})


def _stream(m, specs):
    return m.execution_event.validate_stream(_event(m, **spec) for spec in specs)


class StubValidator:
    """A deterministic validator for the interrupt contract's validator seam."""

    SUPPORTED_SUBSETS = frozenset({"llm-scripting-kit.json-schema-subset/v1"})

    def check_schema(self, schema, *, subset):
        if "bad_keyword" in schema:
            raise ValueError("unsupported keyword bad_keyword")

    def validate(self, schema, value, *, subset):
        if schema.get("type") == "string" and not isinstance(value, str):
            return [("", "type")]
        return []


class NoMarkerValidator:
    def check_schema(self, schema, *, subset):
        return None

    def validate(self, schema, value, *, subset):
        return []


class WrongSubsetValidator(NoMarkerValidator):
    SUPPORTED_SUBSETS = ("other/v9", "another/v1")


class NotCallableValidator:
    SUPPORTED_SUBSETS = ["llm-scripting-kit.json-schema-subset/v1"]
    check_schema = None
    validate = None


V = StubValidator()
ENVELOPES = ("demo.interrupt-request/v1",)


def _req(m, **over):
    args = dict(envelope="demo.interrupt-request/v1", kind="ask-user",
                request_schema={"type": "string"}, payload={"q": "why"},
                expires_in_s=None, accepted_envelopes=ENVELOPES, owner="demo-plugin",
                validator=V)
    args.update(over)
    return m.interrupt_contract.check_request(**args)


def _doc(m, data, **over):
    args = dict(accepted_envelopes=ENVELOPES, owner="demo-plugin", validator=V)
    args.update(over)
    return m.interrupt_contract.parse_request_document(data, **args)


def _good_doc():
    return (b'{"schema":"demo.interrupt-request/v1","kind":"ask-user",'
            b'"request_schema":{"type":"string"},"payload":{"q":"why"},"expires_in_s":60}')


def _jsonl_bytes(ctx):
    m = ctx.mods.execution_event
    path = ctx.tmp / "events.jsonl"
    sink = m.JsonlSink(path)
    emitter = m.Emitter("demo", "run-1", unit_id="u1", sinks=[sink],
                        schema="plugins-kit.execution-event/v3")
    emitter.emit("call-started", attempt_id="a1", adapter="codex", model="sol",
                 payload={"n": 1, "ok": True, "f": 1.5, "t": ["x", None]},
                 at="2026-01-02T03:04:05Z")
    emitter.emit("usage", attempt_id="a1", at="2026-01-02T03:04:06.250000Z",
                 payload=m.usage_payload(input_tokens=3, output_tokens=0))
    emitter.emit("result", attempt_id="a1", at="2026-01-02T03:04:07Z",
                 payload={"status": "ok"})
    emitter.emit("contract", at="2026-01-02T03:04:08Z",
                 payload={"artifact": "out.json", "kind": "schema", "verdict": "violated",
                          "schema_digest": DIGEST, "error_count": 2})
    emitter.emit("terminal", at="2026-01-02T03:04:09Z", payload={"state": "done"})
    return path.read_bytes()


def _jsonl_roundtrip(ctx):
    _jsonl_bytes(ctx)
    return ctx.mods.execution_event.read_jsonl(ctx.tmp / "events.jsonl")


def _jsonl_bad(ctx, text):
    path = ctx.tmp / "bad.jsonl"
    path.write_bytes(text)
    return ctx.mods.execution_event.read_jsonl(path)


def _sink_modes(ctx):
    m = ctx.mods.execution_event
    path = ctx.tmp / "modes.jsonl"
    m.JsonlSink(path)
    try:
        m.JsonlSink(path)
    except FileExistsError as exc:
        existing = type(exc).__name__
    m.JsonlSink(path, mode="truncate")
    return existing


def _emitter_numbering(ctx):
    m = ctx.mods.execution_event
    sink = m.InMemorySink()
    emitter = m.Emitter("demo", "run-1", sinks=[sink], start_seq=5)
    emitter.emit("terminal", payload={"state": "a"}, at="2026-01-02T03:04:05Z")
    try:
        emitter.emit("nonsense", at="2026-01-02T03:04:05Z")
    except m.EventError:
        pass  # an invalid event consumes no seq
    emitter.emit("terminal", unit_id="u9", payload={"state": "b"}, at="2026-01-02T03:04:05Z")
    return [event["seq"] for event in sink.events], [event["identity"] for event in sink.events]


# --- the case list ----------------------------------------------------------

CASES: dict[str, Callable[[Ctx], Any]] = {}


def case(name: str):
    def register(fn):
        assert name not in CASES, name
        CASES[name] = fn
        return fn
    return register


def _add(name, fn):
    case(name)(fn)


# execution_event: surface and constants
_add("ee.all", lambda c: sorted(c.mods.execution_event.__all__))
_add("ee.constants", lambda c: {
    name: getattr(c.mods.execution_event, name)
    for name in ("OWNER", "SCHEMA_V1", "SCHEMA_V2", "SCHEMA_V3", "SUPPORTED_SCHEMAS",
                 "CORE_EVENTS", "CORE_EVENTS_V2", "CORE_EVENTS_V3", "ATTEMPT_SCOPED",
                 "ATTEMPT_SCOPED_V2", "ATTEMPT_SCOPED_V3", "LATER_REVISION_NAMES",
                 "INTERRUPT_PHASES", "INTERRUPT_PAYLOAD_KEYS", "CONTRACT_KINDS",
                 "CONTRACT_VERDICTS", "CONTRACT_PAYLOAD_KEYS", "MAX_PAYLOAD_BYTES")})
_add("ee.exception_hierarchy", lambda c: [cls.__name__ for cls in c.mods.execution_event.EventError.__mro__])

# execution_event: timestamps
for _label, _arg in (("zero", 0), ("whole", 1700000000), ("fraction", 1.5),
                     ("string", "1700000000"), ("string_fraction", "1700000000.123456"),
                     ("padded_string", " 42 "), ("large", 1e10)):
    _add(f"ee.utc_timestamp.{_label}", lambda c, a=_arg: c.mods.execution_event.utc_timestamp(a))
for _label, _arg in (("bool", True), ("word", "abc"), ("negative", -1), ("nan", float("nan")),
                     ("inf", float("inf")), ("list", []), ("negative_string", "-5"),
                     ("exp_string", "1e3"), ("empty", "")):
    _add(f"ee.utc_timestamp.bad_{_label}", lambda c, a=_arg: c.mods.execution_event.utc_timestamp(a))

# execution_event: usage payloads
for _label, _kw in (("none", {}), ("input_only", {"input_tokens": 7}),
                    ("zero_input", {"input_tokens": 0}),
                    ("zero_input_nonzero_output", {"input_tokens": 0, "output_tokens": 5}),
                    ("zeros_cache_total", {"cache_hit_tokens": 0, "total_tokens": 0}),
                    ("all", {"input_tokens": 1, "output_tokens": 2, "cache_hit_tokens": 3,
                             "total_tokens": 6}),
                    ("bad_negative", {"input_tokens": -1}),
                    ("bad_bool", {"output_tokens": True}),
                    ("bad_string", {"total_tokens": "3"})):
    _add(f"ee.usage_payload.{_label}", lambda c, k=_kw: c.mods.execution_event.usage_payload(**k))

# execution_event: valid events per revision
V1, V2, V3 = ("plugins-kit.execution-event/v1", "plugins-kit.execution-event/v2",
              "plugins-kit.execution-event/v3")
_add("ee.event.terminal_v1", lambda c: _event(c.mods))
_add("ee.event.dispatch_selected", lambda c: _attempt(
    c.mods, "dispatch-selected", {"route": "x"}, adapter="codex", model="sol"))
_add("ee.event.extension", lambda c: _event(c.mods, event="demo:custom-thing", payload={"k": [1, 2.5, None]}))
_add("ee.event.usage", lambda c: _attempt(c.mods, "usage", c.mods.execution_event.usage_payload(input_tokens=4, output_tokens=2)))
_add("ee.event.result", lambda c: _attempt(c.mods, "result", {"status": "ok"}))
_add("ee.event.interrupt_v2", lambda c: _event(c.mods, **_interrupt(
    "requested", 0, expires_at="2026-01-02T03:05:05.5Z", continuation_no=2)))
_add("ee.event.contract_v3_schema", lambda c: _event(
    c.mods, event="contract", unit_id="u1", schema=V3,
    payload={"artifact": "out.json", "kind": "schema", "verdict": "satisfied",
             "schema_digest": DIGEST}))
_add("ee.event.contract_v3_opaque_missing", lambda c: _event(
    c.mods, event="contract", unit_id="u1", schema=V3,
    payload={"artifact": "blob.bin", "kind": "opaque-file", "verdict": "missing"}))
_add("ee.event.now_default_shape", lambda c: sorted(c.mods.execution_event.make_event(
    seq=0, run_id="r", event="terminal", plugin="demo", payload={"state": "s"})))

# execution_event: refused events, each with its message and pointer
_BAD_EVENTS = {
    "bad_schema_selector": dict(schema="plugins-kit.execution-event/v9"),
    "seq_negative": dict(seq=-1),
    "seq_bool": dict(seq=True),
    "plugin_uppercase": dict(plugin="Demo"),
    "plugin_empty": dict(plugin=""),
    "run_id_empty": dict(run_id=""),
    "run_id_long": dict(run_id="x" * 201),
    "run_id_control": dict(run_id="a\x07b"),
    "run_id_type": dict(run_id=5),
    "attempt_without_unit": dict(attempt_id="a1"),
    "terminal_with_attempt": dict(unit_id="u1", attempt_id="a1"),
    "usage_without_attempt": dict(event="usage", payload={"input_tokens": 1}),
    "usage_unknown_key": dict(event="usage", unit_id="u1", attempt_id="a1",
                              payload={"input_tokens": 1, "output_tokens": None,
                                       "cache_hit_tokens": None, "total_tokens": None, "x": 1}),
    "usage_missing_key": dict(event="usage", unit_id="u1", attempt_id="a1",
                              payload={"input_tokens": 1}),
    "usage_all_null": dict(event="usage", unit_id="u1", attempt_id="a1",
                           payload={"input_tokens": None, "output_tokens": None,
                                    "cache_hit_tokens": None, "total_tokens": None}),
    "usage_negative": dict(event="usage", unit_id="u1", attempt_id="a1",
                           payload={"input_tokens": -1, "output_tokens": None,
                                    "cache_hit_tokens": None, "total_tokens": None}),
    "result_no_status": dict(event="result", unit_id="u1", attempt_id="a1", payload={}),
    "terminal_no_state": dict(payload={}),
    "unknown_event": dict(event="nonsense"),
    "foreign_prefix": dict(event="other:thing"),
    "extension_bad_part": dict(event="demo:Bad_Name"),
    "empty_event": dict(event=""),
    "contract_in_v1": dict(event="contract", unit_id="u1", payload={}),
    "interrupt_in_v1": dict(event="interrupt", unit_id="u1", attempt_id="a1", payload={}),
    "contract_in_v2": dict(event="contract", unit_id="u1", schema=V2, payload={}),
    "at_not_utc": dict(at="2026-01-02T03:04:05+00:00"),
    "at_not_calendar": dict(at="2026-13-45T03:04:05Z"),
    "at_type": dict(at=17),
    "payload_type": dict(payload=[1]),
    "payload_nan": dict(payload={"state": "x", "v": float("nan")}),
    "payload_object": dict(payload={"state": "x", "v": object()}),
    "payload_nonstring_key": dict(payload={"state": "x", 1: 2}),
    "payload_tuple": dict(payload={"state": "x", "v": (1, 2)}),
    "payload_too_big": dict(payload={"state": "x", "blob": "y" * 17000}),
    "source_adapter_empty": dict(adapter=""),
    "source_model_empty": dict(model=""),
    "interrupt_bad_phase": _interrupt("opened", 0),
    "interrupt_bad_kind": {**_interrupt("requested", 0),
                           "payload": {"interrupt_id": "i1", "kind": "Ask", "phase": "requested"}},
    "interrupt_unknown_key": _interrupt("requested", 0, reason="free text"),
    "interrupt_missing_id": {**_interrupt("requested", 0),
                             "payload": {"kind": "ask-user", "phase": "requested"}},
    "interrupt_bad_expiry": _interrupt("requested", 0, expires_at="tomorrow"),
    "interrupt_bad_continuation": _interrupt("requested", 0, continuation_no=-1),
    "interrupt_continuation_bool": _interrupt("requested", 0, continuation_no=True),
    "contract_unknown_key": dict(event="contract", unit_id="u1", schema=V3,
                                 payload={"artifact": "a", "kind": "opaque-file",
                                          "verdict": "missing", "content": "x"}),
    "contract_missing_artifact": dict(event="contract", unit_id="u1", schema=V3,
                                      payload={"kind": "opaque-file", "verdict": "missing"}),
    "contract_bad_kind": dict(event="contract", unit_id="u1", schema=V3,
                              payload={"artifact": "a", "kind": "blob", "verdict": "missing"}),
    "contract_bad_verdict": dict(event="contract", unit_id="u1", schema=V3,
                                 payload={"artifact": "a", "kind": "opaque-file",
                                          "verdict": "fine"}),
    "contract_violated_opaque": dict(event="contract", unit_id="u1", schema=V3,
                                     payload={"artifact": "a", "kind": "opaque-file",
                                              "verdict": "violated", "error_count": 1}),
    "contract_schema_no_digest": dict(event="contract", unit_id="u1", schema=V3,
                                      payload={"artifact": "a", "kind": "schema",
                                               "verdict": "satisfied"}),
    "contract_bad_digest": dict(event="contract", unit_id="u1", schema=V3,
                                payload={"artifact": "a", "kind": "schema",
                                         "verdict": "satisfied", "schema_digest": "ABC"}),
    "contract_opaque_with_digest": dict(event="contract", unit_id="u1", schema=V3,
                                        payload={"artifact": "a", "kind": "opaque-file",
                                                 "verdict": "missing", "schema_digest": DIGEST}),
    "contract_violated_no_count": dict(event="contract", unit_id="u1", schema=V3,
                                       payload={"artifact": "a", "kind": "schema",
                                                "verdict": "violated", "schema_digest": DIGEST}),
    "contract_violated_zero_count": dict(event="contract", unit_id="u1", schema=V3,
                                         payload={"artifact": "a", "kind": "schema",
                                                  "verdict": "violated", "schema_digest": DIGEST,
                                                  "error_count": 0}),
    "contract_count_on_satisfied": dict(event="contract", unit_id="u1", schema=V3,
                                        payload={"artifact": "a", "kind": "schema",
                                                 "verdict": "satisfied", "schema_digest": DIGEST,
                                                 "error_count": 1}),
    "contract_without_unit": dict(event="contract", schema=V3,
                                  payload={"artifact": "a", "kind": "opaque-file",
                                           "verdict": "missing"}),
    "contract_with_attempt": dict(event="contract", unit_id="u1", attempt_id="a1", schema=V3,
                                  payload={"artifact": "a", "kind": "opaque-file",
                                           "verdict": "missing"}),
}
for _label, _kw in _BAD_EVENTS.items():
    _add(f"ee.event.refused.{_label}", lambda c, k=_kw: _event(c.mods, **k))

# validate_event on raw mappings
_RAW_OK = {"schema": V1, "seq": 3, "identity": {"run_id": "r"}, "event": "terminal",
           "at": "2026-01-02T03:04:05Z", "source": {"plugin": "demo"}, "payload": {"state": "s"}}
_RAW_BAD = {
    "not_mapping": [],
    "unknown_top_key": {**_RAW_OK, "extra": 1},
    "missing_top_key": {k: v for k, v in _RAW_OK.items() if k != "at"},
    "unsupported_schema": {**_RAW_OK, "schema": "other/v1"},
    "identity_not_mapping": {**_RAW_OK, "identity": "r"},
    "identity_unknown_key": {**_RAW_OK, "identity": {"run_id": "r", "x": "y"}},
    "identity_missing_run": {**_RAW_OK, "identity": {"unit_id": "u"}},
    "identity_null_key": {**_RAW_OK, "identity": {"run_id": "r", "unit_id": None}},
    "source_not_mapping": {**_RAW_OK, "source": None},
    "source_unknown_key": {**_RAW_OK, "source": {"plugin": "demo", "x": "y"}},
}
_add("ee.validate_event.ok", lambda c: c.mods.execution_event.validate_event(_RAW_OK))
for _label, _raw in _RAW_BAD.items():
    _add(f"ee.validate_event.refused.{_label}",
         lambda c, r=_raw: c.mods.execution_event.validate_event(r))
_add("ee.validate_event.returns_copy", lambda c: (
    lambda out: (out == _RAW_OK, out is _RAW_OK, out["payload"] is _RAW_OK["payload"])
)(c.mods.execution_event.validate_event(_RAW_OK)))

# validate_stream
_T = dict(event="terminal", payload={"state": "s"})
_STREAMS_OK = {
    "empty": [],
    "two_groups": [dict(seq=0, **_T), dict(seq=0, run_id="run-2", **_T)],
    "interrupt_lifecycle": [_interrupt("requested", 0), _interrupt("resolved", 1)],
    "contract_then_terminal": [
        dict(seq=0, event="contract", unit_id="u1", schema=V3,
             payload={"artifact": "a", "kind": "opaque-file", "verdict": "satisfied"}),
        dict(seq=1, event="terminal", unit_id="u1", payload={"state": "s"})],
}
_STREAMS_BAD = {
    "duplicate_seq": [dict(seq=0, **_T), dict(seq=0, event="demo:y", payload={})],
    "seq_not_increasing": [dict(seq=2, event="demo:a", payload={}), dict(seq=1, event="demo:b", payload={})],
    "second_terminal": [dict(seq=0, **_T), dict(seq=1, **_T)],
    "second_result": [dict(seq=0, event="result", unit_id="u1", attempt_id="a1", payload={"status": "x"}),
                      dict(seq=1, event="result", unit_id="u1", attempt_id="a1", payload={"status": "y"})],
    "attempt_after_terminal": [
        dict(seq=0, event="terminal", unit_id="u1", payload={"state": "s"}),
        dict(seq=1, event="result", unit_id="u1", attempt_id="a1", payload={"status": "x"})],
    "bad_event_in_stream": [dict(seq=0, **_T), dict(seq=1, event="nonsense")],
    "interrupt_before_request": [_interrupt("resolved", 0)],
    "second_request": [_interrupt("requested", 0), _interrupt("requested", 1)],
    "second_close": [_interrupt("requested", 0), _interrupt("resolved", 1), _interrupt("rejected", 2)],
    "after_close": [_interrupt("requested", 0), _interrupt("resolved", 1),
                    {**_interrupt("requested", 2)}],
    "second_contract": [
        dict(seq=0, event="contract", unit_id="u1", schema=V3,
             payload={"artifact": "a", "kind": "opaque-file", "verdict": "satisfied"}),
        dict(seq=1, event="contract", unit_id="u1", schema=V3,
             payload={"artifact": "a", "kind": "opaque-file", "verdict": "missing"})],
    "contract_after_terminal": [
        dict(seq=0, event="terminal", unit_id="u1", payload={"state": "s"}),
        dict(seq=1, event="contract", unit_id="u1", schema=V3,
             payload={"artifact": "a", "kind": "opaque-file", "verdict": "missing"})],
}
_add("ee.stream.refused.raw_invalid_event", lambda c: c.mods.execution_event.validate_stream(
    [_RAW_OK, {**_RAW_OK, "seq": -1}]))
_add("ee.stream.refused.raw_not_event", lambda c: c.mods.execution_event.validate_stream([5]))
for _label, _specs in _STREAMS_OK.items():
    _add(f"ee.stream.ok.{_label}", lambda c, s=_specs: _stream(c.mods, s))
for _label, _specs in _STREAMS_BAD.items():
    _add(f"ee.stream.refused.{_label}", lambda c, s=_specs: _stream(c.mods, s))

# emitter, sinks, canonical bytes
_add("ee.emitter.numbering", _emitter_numbering)
_add("ee.emitter.bad_start_seq", lambda c: c.mods.execution_event.Emitter("demo", "r", start_seq=-1))
_add("ee.emitter.bad_plugin", lambda c: c.mods.execution_event.Emitter("Demo", "r"))
_add("ee.emitter.bad_schema", lambda c: c.mods.execution_event.Emitter("demo", "r", schema="x"))
_add("ee.emitter.bad_run_id", lambda c: c.mods.execution_event.Emitter("demo", "", unit_id="u"))
_add("ee.jsonl.canonical_bytes", _jsonl_bytes)
_add("ee.jsonl.roundtrip", _jsonl_roundtrip)
_add("ee.jsonl.sink_modes", _sink_modes)
_add("ee.jsonl.bad_sink_mode", lambda c: c.mods.execution_event.JsonlSink(c.tmp / "m.jsonl", mode="append"))
_add("ee.jsonl.refuses_not_json", lambda c: _jsonl_bad(c, b"not json\n"))
_add("ee.jsonl.refuses_nan", lambda c: _jsonl_bad(c, b"NaN\n"))
_add("ee.jsonl.refuses_invalid_event", lambda c: _jsonl_bad(c, b'{"schema":1}\n'))
_add("ee.jsonl.refuses_stream_fault", lambda c: _jsonl_bad(
    c, (b'{"schema":"plugins-kit.execution-event/v1","seq":0,"identity":{"run_id":"r"},'
        b'"event":"terminal","at":"2026-01-02T03:04:05Z","source":{"plugin":"demo"},'
        b'"payload":{"state":"s"}}\n') * 2))

# interrupt_contract: surface and constants
_add("ic.all", lambda c: sorted(c.mods.interrupt_contract.__all__))
_add("ic.constants", lambda c: {
    name: getattr(c.mods.interrupt_contract, name)
    for name in ("OWNER", "CONTRACT_V1", "SUPPORTED_CONTRACTS", "REQUEST_ENVELOPE_V1",
                 "RESOLUTION_ENVELOPE_V1", "VALIDATOR_MODULE", "VALIDATOR_SUBSET",
                 "REQUEST_KEYS", "OPTIONAL_REQUEST_KEYS", "REQUEST_DOCUMENT_LIMIT",
                 "INPUT_LIMIT", "KIND_LIMIT", "EXPIRES_IN_S_MAX", "REASON_LIMIT",
                 "OUTCOMES", "DECISIONS")})
_add("ic.exception_hierarchy", lambda c: {
    name: [cls.__name__ for cls in getattr(c.mods.interrupt_contract, name).__mro__]
    for name in ("ContractError", "RequestError", "InputError", "DecisionError", "ValidatorError")})

# validators
_add("ic.check_validator.ok", lambda c: c.mods.interrupt_contract.check_validator(V))
_add("ic.check_validator.no_marker", lambda c: c.mods.interrupt_contract.check_validator(NoMarkerValidator()))
_add("ic.check_validator.wrong_subset", lambda c: c.mods.interrupt_contract.check_validator(WrongSubsetValidator()))
_add("ic.check_validator.not_callable", lambda c: c.mods.interrupt_contract.check_validator(NotCallableValidator()))
_add("ic.check_validator.none", lambda c: c.mods.interrupt_contract.check_validator(None))

# canonical json
_add("ic.canonical_json.sorted", lambda c: c.mods.interrupt_contract.canonical_json({"b": [1, 2.5, None, True], "a": "\u00e9\u4e2d"}))
_add("ic.canonical_json.nan", lambda c: c.mods.interrupt_contract.canonical_json(float("nan")))
_add("ic.canonical_json.unserializable", lambda c: c.mods.interrupt_contract.canonical_json({"a": object()}))

# check_request
_add("ic.check_request.ok", lambda c: _req(c.mods, expires_in_s=3600))
_add("ic.check_request.canonicalises", lambda c: _req(
    c.mods, request_schema={"type": "string", "b": 1, "a": 2}, payload={"z": 1, "a": [3]}))
_BAD_REQ = {
    "envelope_not_accepted": dict(envelope="other.interrupt-request/v1"),
    "envelope_not_string": dict(envelope=5),
    "kind_uppercase": dict(kind="Ask"),
    "kind_long": dict(kind="a" * 65),
    "kind_not_string": dict(kind=None),
    "schema_not_object": dict(request_schema=[]),
    "payload_not_object": dict(payload="x"),
    "schema_not_native": dict(request_schema={"type": "string", "x": object()}),
    "payload_nan": dict(payload={"x": float("nan")}),
    "payload_nonstring_key": dict(payload={1: 2}),
    "expires_zero": dict(expires_in_s=0),
    "expires_bool": dict(expires_in_s=True),
    "expires_float": dict(expires_in_s=1.5),
    "expires_too_big": dict(expires_in_s=2**31),
    "outside_subset": dict(request_schema={"bad_keyword": 1}),
    "string_envelopes": dict(accepted_envelopes="demo.interrupt-request/v1"),
    "unusable_validator": dict(validator=NoMarkerValidator()),
}
for _label, _kw in _BAD_REQ.items():
    _add(f"ic.check_request.refused.{_label}", lambda c, k=_kw: _req(c.mods, **k))

# check_request_mapping
_MAP_OK = {"schema": "demo.interrupt-request/v1", "kind": "ask-user",
           "request_schema": {"type": "string"}, "payload": {}}
_add("ic.check_request_mapping.ok", lambda c: c.mods.interrupt_contract.check_request_mapping(
    {**_MAP_OK, "expires_in_s": 5}, accepted_envelopes=ENVELOPES, owner="demo-plugin", validator=V))
for _label, _raw in {"not_mapping": [], "unknown_keys": {**_MAP_OK, "zzz": 1, 3: 4},
                     "missing_keys": {"schema": "demo.interrupt-request/v1"}}.items():
    _add(f"ic.check_request_mapping.refused.{_label}", lambda c, r=_raw:
         c.mods.interrupt_contract.check_request_mapping(
             r, accepted_envelopes=ENVELOPES, owner="demo-plugin", validator=V))

# parse_request_document
_add("ic.parse_request_document.ok", lambda c: _doc(c.mods, _good_doc()))
for _label, _data in {
        "too_large": b" " * 131073,
        "not_utf8": b"\xff\xfe{}",
        "not_json": b"{nope",
        "nan": b'{"schema": NaN}',
        "infinity": b'{"schema": Infinity}',
        "duplicate_key": b'{"kind":"a","kind":"b"}',
        "not_object": b"[1]",
        "unknown_key": _good_doc().replace(b'"payload"', b'"extra":1,"payload"'),
}.items():
    _add(f"ic.parse_request_document.refused.{_label}", lambda c, d=_data: _doc(c.mods, d))

# validate_input
_add("ic.validate_input.ok", lambda c: c.mods.interrupt_contract.validate_input(
    {"type": "string"}, "h\u00e9llo", validator=V))
_add("ic.validate_input.schema_errors", lambda c: c.mods.interrupt_contract.validate_input(
    {"type": "string"}, 5, validator=V))
_add("ic.validate_input.not_native", lambda c: c.mods.interrupt_contract.validate_input(
    {"type": "object"}, {"a": {1, 2}}, validator=V))
_add("ic.validate_input.not_native_pointer", lambda c: c.mods.interrupt_contract.validate_input(
    {"type": "object"}, {"a/b": [1, float("inf")]}, validator=V))
_add("ic.validate_input.too_big", lambda c: c.mods.interrupt_contract.validate_input(
    {"type": "string"}, "x" * 65536, validator=V))
_add("ic.validate_input.unusable_validator", lambda c: c.mods.interrupt_contract.validate_input(
    {}, 1, validator=NoMarkerValidator()))

# small functions
_add("ic.expiry", lambda c: [c.mods.interrupt_contract.expiry(100.5, 10),
                             c.mods.interrupt_contract.expiry(100.5, None)])
_add("ic.lapsed", lambda c: [c.mods.interrupt_contract.lapsed(10.0, 9.9),
                             c.mods.interrupt_contract.lapsed(10.0, 10.0),
                             c.mods.interrupt_contract.lapsed(None, 1e18)])
_add("ic.bound_reason", lambda c: [c.mods.interrupt_contract.bound_reason(None),
                                   c.mods.interrupt_contract.bound_reason("x" * 2500) == "x" * 2000,
                                   c.mods.interrupt_contract.bound_reason(12)])
_add("ic.decision_outcome.ok", lambda c: [
    c.mods.interrupt_contract.decision_outcome("answer", input={"a": 1}),
    c.mods.interrupt_contract.decision_outcome("reject", reason="no")])
for _label, _args, _kw in (("unknown", ("approve",), {}), ("not_string", (3,), {}),
                           ("reject_with_input", ("reject",), {"input": 1}),
                           ("answer_with_reason", ("answer",), {"reason": "r"})):
    _add(f"ic.decision_outcome.refused.{_label}", lambda c, a=_args, k=_kw:
         c.mods.interrupt_contract.decision_outcome(*a, **k))
_add("ic.same_resolution", lambda c: [
    c.mods.interrupt_contract.same_resolution(
        stored_outcome="answered", stored_input_json='{"a":1}', stored_reason=None,
        outcome="answered", input={"a": 1}),
    c.mods.interrupt_contract.same_resolution(
        stored_outcome="answered", stored_input_json='{"a":1}', stored_reason=None,
        outcome="answered", input={"a": 2}),
    c.mods.interrupt_contract.same_resolution(
        stored_outcome="answered", stored_input_json='{"a":1}', stored_reason=None,
        outcome="rejected", reason="r"),
    c.mods.interrupt_contract.same_resolution(
        stored_outcome="answered", stored_input_json="x", stored_reason=None,
        outcome="answered", input=float("nan")),
    c.mods.interrupt_contract.same_resolution(
        stored_outcome="answered", stored_input_json="x", stored_reason=None,
        outcome="answered", input=object()),
    c.mods.interrupt_contract.same_resolution(
        stored_outcome="rejected", stored_input_json=None, stored_reason="r" * 2000,
        outcome="rejected", reason="r" * 2500),
])

# resolution_document
_RES = dict(resolution_envelope="demo.interrupt-resolution/v1", interrupt_id="i1",
            kind="ask-user", outcome="answered", input={"b": 1, "a": "x"},
            payload={"q": "why"}, resolved_at=1700000000.25)
_add("ic.resolution_document.answered", lambda c: c.mods.interrupt_contract.resolution_document(**_RES))
_add("ic.resolution_document.expired", lambda c: c.mods.interrupt_contract.resolution_document(
    **{**_RES, "outcome": "expired", "input": None, "resolved_at": 0}))
_add("ic.resolution_document.bad_envelope", lambda c: c.mods.interrupt_contract.resolution_document(
    **{**_RES, "resolution_envelope": "demo.interrupt-resolution/v2"}))
_add("ic.resolution_document.bad_envelope_type", lambda c: c.mods.interrupt_contract.resolution_document(
    **{**_RES, "resolution_envelope": None}))
_add("ic.resolution_document.bad_outcome", lambda c: c.mods.interrupt_contract.resolution_document(
    **{**_RES, "outcome": "approved"}))

# model_declaration
_add("md.all", lambda c: sorted(c.mods.model_declaration.__all__))
_add("md.constants", lambda c: {
    name: getattr(c.mods.model_declaration, name)
    for name in ("CORE_IDS", "EFFORT_LEVELS", "ENTRY_FIELDS")})
_add("md.exception_hierarchy", lambda c: [cls.__name__ for cls in c.mods.model_declaration.DeclarationError.__mro__])
_add("md.is_core_id", lambda c: [c.mods.model_declaration.is_core_id(v)
                                 for v in ("opus", " opus ", "Opus", "sonnett", "", 5)])
for _label, _val in (("string", " opus "), ("list", ["opus", " sonnet "]), ("tuple", ("a", "b")),
                     ("mixed_case_ids", ["Opus", "opus"])):
    _add(f"md.parse.ok.{_label}", lambda c, v=_val: c.mods.model_declaration.parse(v))
for _label, _val in (("empty_list", []), ("empty_string", ""), ("blank_string", "  "),
                     ("blank_entry", ["opus", " "]), ("non_string_entry", ["opus", 5]),
                     ("duplicate", ["opus", "sonnet", " opus"]), ("wrong_type", 5),
                     ("dict", {"id": "opus"}), ("none", None)):
    _add(f"md.parse.refused.{_label}", lambda c, v=_val: c.mods.model_declaration.parse(v))
for _label, _val in (("string", "opus"), ("mapping", {"id": "opus", "effort": "xhigh"}),
                     ("mixed", ["sonnet", {"id": "opus", "effort": "low"}, {"id": "fable"}]),
                     ("tuple", ("a",))):
    _add(f"md.parse_entries.ok.{_label}", lambda c, v=_val: c.mods.model_declaration.parse_entries(v))
for _label, _val in (("empty_list", []), ("unknown_field", [{"id": "opus", "tier": 1}]),
                     ("missing_id", [{"effort": "low"}]), ("unknown_effort", [{"id": "opus", "effort": "ultra"}]),
                     ("duplicate_across_forms", ["opus", {"id": "opus"}]),
                     ("blank_mapping_id", [{"id": " "}]), ("wrong_type", 3)):
    _add(f"md.parse_entries.refused.{_label}", lambda c, v=_val: c.mods.model_declaration.parse_entries(v))
_add("md.validate.ok", lambda c: (lambda d: (d, list(d), len(d), d.core_ids))(
    c.mods.model_declaration.validate(["sonnet", "gpt-x", "haiku"])))
_add("md.validate.refused", lambda c: c.mods.model_declaration.validate([]))
_add("md.dataclasses_frozen", lambda c: (
    c.mods.model_declaration.Entry("a").__class__.__dataclass_params__.frozen,
    c.mods.model_declaration.Declaration.__dataclass_params__.frozen))


# Cases whose refusal message is the standard library's own text.
CLASS_ONLY = frozenset({
    "ic.canonical_json.nan",
    "ic.canonical_json.unserializable",
    "ic.parse_request_document.refused.not_utf8",
    "ic.parse_request_document.refused.not_json",
    "ee.jsonl.refuses_not_json",
})
assert CLASS_ONLY <= set(CASES), CLASS_ONLY - set(CASES)


def run_all(ctx: Ctx) -> dict[str, Any]:
    """Every case's outcome, keyed by case name."""
    results = {}
    for number, (name, fn) in enumerate(CASES.items()):
        scratch = ctx.tmp / f"case-{number}"
        scratch.mkdir()
        results[name] = outcome(fn, Ctx(ctx.mods, scratch), name in CLASS_ONLY)
    return results
