"""Join test: job-kit and content-pipeline-kit implement ONE interrupt contract.

Both plugins keep their own store, their own continuation and their own event
phases, and both execute every request, answer, replay, expiry and document
rule in ``bootstrap_lib.interrupt_contract``. This file drives the two REAL
stores with the same logical request and answer, under the REAL
llm-scripting-kit validator, and compares what each one recorded, refused and
emitted. Neither plugin imports the other; this file is the only place the two
meet.

The libraries are put on ``sys.path`` for one test by the ``env`` fixture and
unloaded afterwards: other tests rely on them being absent.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGINS = REPO / "plugins"

_LIBS = (
    PLUGINS / "llm-scripting-kit" / "lib",
    PLUGINS / "job-kit" / "lib",
    PLUGINS / "content-pipeline-kit" / "lib",
)
_ROOTS = ("llm_scripting_kit", "job_kit", "content_pipeline")

from bootstrap_lib import execution_event as ee  # noqa: E402
from bootstrap_lib import interrupt_contract as contract  # noqa: E402

RUN = "run"
UNIT = "job"
CREATED = 1000.0
ANSWERED_AT = 1001.0
EXPIRES_IN_S = 60

APPROVAL = {
    "type": "object",
    "required": ["approved"],
    "properties": {"approved": {"const": True}, "note": {"type": "string"}},
    "additionalProperties": False,
}


def _loaded() -> set[str]:
    return {
        name
        for name in sys.modules
        if name.split(".")[0] in _ROOTS
    }


_PREVIOUS: dict[str, set[str]] = {}


@pytest.fixture(autouse=True)
def _nothing_survives_a_test():
    """Fail a test that leaves a library module loaded that it found absent.

    Two checks. At teardown, for what is still loaded once the other fixtures
    have cleaned up. And at the next test's setup, against the set the previous
    test started from: a monkeypatch undo runs AFTER every fixture declared in
    this file is torn down (the suite's autouse fixtures hold the monkeypatch
    from before this one), so a module an undo re-inserts is only visible then.
    """
    leftover = _loaded() - _PREVIOUS["before"] if "before" in _PREVIOUS else set()
    for name in leftover:
        del sys.modules[name]
    assert not leftover, f"modules left by the previous test: {sorted(leftover)}"
    before = _loaded()
    _PREVIOUS["before"] = before
    yield
    leaked = sorted(_loaded() - before)
    for name in leaked:  # keep the failure from spreading to later tests
        del sys.modules[name]
    assert not leaked, f"modules survived the test: {leaked}"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> types.SimpleNamespace:
    """Link the three libraries for one test; unload what it loaded.

    A module a test swaps in goes through ``swap_module``: teardown puts the
    entry back as it was (or removes it) BEFORE the unload, so nothing the
    test installed survives and no monkeypatch undo can re-insert a module
    after this fixture has cleaned up.
    """
    before = _loaded()
    swapped: dict[str, Any] = {}

    def swap_module(name: str, module: Any) -> None:
        swapped.setdefault(name, sys.modules.get(name))
        sys.modules[name] = module

    for lib in reversed(_LIBS):
        monkeypatch.syspath_prepend(str(lib))
    import importlib

    ns = types.SimpleNamespace(tmp=tmp_path, swap_module=swap_module)
    ns.lsk = importlib.import_module("llm_scripting_kit.completion.json_schema")
    ns.jk_interrupts = importlib.import_module("job_kit.interrupts")
    ns.jk_model = importlib.import_module("job_kit.model")
    ns.jk_store = importlib.import_module("job_kit.store")
    ns.cp_interrupts = importlib.import_module("content_pipeline.execution.interrupts")
    ns.cp_model = importlib.import_module("content_pipeline.execution.model")
    ns.cp_store = importlib.import_module("content_pipeline.execution.store")
    ns.cp_events = importlib.import_module("content_pipeline.execution.events")
    yield ns
    for name, original in swapped.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    for name in _loaded() - before:
        del sys.modules[name]


# ---------------------------------------------------------------------------
# One driver per store, the same verbs
# ---------------------------------------------------------------------------


class _Side:
    """The operations the conformance tests use, over one store."""

    label: str
    resolution_literal: str
    request_literal: str

    def __init__(self, env: types.SimpleNamespace, directory: Path) -> None:
        self.env = env
        self.directory = directory

    # Overridden per store.
    def request(self, schema: dict, payload: dict, *, kind: str, expires_in_s: Optional[int]) -> str:
        raise NotImplementedError

    def resolve(self, interrupt_id: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    def expire(self, now: float) -> Any:
        raise NotImplementedError

    def record(self, interrupt_id: str) -> Any:
        raise NotImplementedError

    def document(self, interrupt_id: str) -> dict:
        raise NotImplementedError

    def events(self) -> tuple:
        raise NotImplementedError

    def build_request(self, **fields: Any) -> Any:
        raise NotImplementedError

    def check_request(self, request: Any) -> Any:
        raise NotImplementedError

    # Shared.
    @property
    def db_path(self) -> str:
        return str(self.store.db_path)

    def rows(self, table: str) -> list[dict]:
        connection = sqlite3.connect(self.db_path)
        try:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        finally:
            connection.close()

    def count_rows(self) -> tuple[int, int]:
        return len(self.rows("interrupts")), len(self.rows("interrupt_resolutions"))

    def interrupt_phases(self) -> list[str]:
        return [
            event["payload"]["phase"]
            for event in self.events()
            if event["event"] == "interrupt"
        ]


class _JobKitSide(_Side):
    label = "job-kit"

    def __init__(self, env: types.SimpleNamespace, directory: Path) -> None:
        super().__init__(env, directory)
        m = env.jk_model
        self.store = env.jk_store.JobStore(directory / "ledger.sqlite3")
        self.request_literal = env.jk_interrupts.REQUEST_ENVELOPE_V1
        self.resolution_literal = env.jk_interrupts.RESOLUTION_ENVELOPE_V1
        job = m.Job(
            id=UNIT,
            prompt=m.Prompt(user="run job"),
            models=("fake",),
            directory=directory,
            max_attempts=2,
            contract=m.Contract(command=(sys.executable, "-c", "pass"), directory=directory),
        )
        self.store.create_run(RUN, [job])
        self._attempts = 0

    def build_request(self, **fields: Any) -> Any:
        return self.env.jk_model.InterruptRequest(envelope=self.request_literal, **fields)

    def check_request(self, request: Any) -> Any:
        return self.env.jk_interrupts.check_request(request)

    def request(self, schema: dict, payload: dict, *, kind: str, expires_in_s: Optional[int]) -> str:
        m = self.env.jk_model
        reservation = self.store.reserve_attempt(
            RUN,
            UNIT,
            endpoint="fake-endpoint",
            backend="fake-backend",
            model="fake-model",
            reserved_at="2026-09-01T00:00:00Z",
        )
        self.store.arm_reservation(
            RUN, UNIT, reservation.attempt_no, invoke_armed_at="2026-09-01T00:00:01Z"
        )
        attempt = m.Attempt(
            run_id=RUN,
            job_id=UNIT,
            attempt_no=reservation.attempt_no,
            endpoint="fake-endpoint",
            backend="fake-backend",
            model="fake-model",
            status="completed",
            started_at="2026-09-01T00:00:01Z",
            ended_at="2026-09-01T00:00:02Z",
            usage=m.Usage(input_tokens=3, output_tokens=5),
            response_text="the model answer",
            acceptance=m.Acceptance(
                command=("contract",),
                directory=Path.cwd(),
                exit_code=0,
                stdout="",
                stderr="",
                wall_ms=1,
                accepted=False,
                outcome="interrupt_requested",
            ),
        )
        self.store.append_attempt(
            attempt,
            interrupt=self.build_request(
                kind=kind, request_schema=schema, payload=payload, expires_in_s=expires_in_s
            ),
            at=CREATED,
        )
        record = self.store.open_interrupt(RUN, UNIT)
        assert record is not None
        return record.id

    def resolve(self, interrupt_id: str, **kwargs: Any) -> Any:
        return self.store.resolve_interrupt(RUN, interrupt_id, **kwargs)

    def expire(self, now: float) -> Any:
        return self.store.expire_interrupts(RUN, now=now)

    def record(self, interrupt_id: str) -> Any:
        [record] = [r for r in self.store.list_interrupts(RUN) if r.id == interrupt_id]
        return record

    def document(self, interrupt_id: str) -> dict:
        text = self.env.jk_interrupts.resolution_document(self.record(interrupt_id))
        return json.loads(text)

    def events(self) -> tuple:
        return self.store.list_events(RUN)

    @property
    def errors(self) -> types.SimpleNamespace:
        s = self.env.jk_store
        return types.SimpleNamespace(
            input=s.ResolutionInputError,
            conflict=s.ResolutionConflictError,
            expired=s.InterruptExpiredError,
            request=self.env.jk_interrupts.InterruptRequestError,
            support=self.env.jk_interrupts.JsonSchemaSupportError,
        )

    def limits(self) -> dict:
        i = self.env.jk_interrupts
        return {
            "INPUT_LIMIT": i.INPUT_LIMIT,
            "KIND_LIMIT": i.KIND_LIMIT,
            "EXPIRES_IN_S_MAX": i.EXPIRES_IN_S_MAX,
        }


class _CpkSide(_Side):
    label = "content-pipeline-kit"

    def __init__(self, env: types.SimpleNamespace, directory: Path) -> None:
        super().__init__(env, directory)
        self.store = env.cp_store.ExecutionStore(directory / "run.db")
        self.store.create_run(
            RUN, driver="inline", backend="mock", model="m1", adapter_version="7", created_at=CREATED
        )
        self.store.register_units(RUN, [UNIT], at=CREATED)
        self.request_literal = env.cp_interrupts.REQUEST_ENVELOPE
        self.resolution_literal = env.cp_interrupts.RESOLUTION_ENVELOPE

    def build_request(self, **fields: Any) -> Any:
        return self.env.cp_model.InterruptRequest(envelope=self.request_literal, **fields)

    def check_request(self, request: Any) -> Any:
        return self.env.cp_interrupts.check_request(request)

    def request(self, schema: dict, payload: dict, *, kind: str, expires_in_s: Optional[int]) -> str:
        token = self.store.claim_unit(RUN, UNIT, "worker", at=CREATED - 1).fencing_token
        record = self.store.request_interrupt(
            RUN,
            UNIT,
            token,
            self.build_request(
                kind=kind, request_schema=schema, payload=payload, expires_in_s=expires_in_s
            ),
            at=CREATED,
        )
        return record.id

    def resolve(self, interrupt_id: str, **kwargs: Any) -> Any:
        return self.store.resolve_interrupt(RUN, interrupt_id, **kwargs)

    def expire(self, now: float) -> Any:
        return self.store.expire_interrupts(RUN, now=now)

    def record(self, interrupt_id: str) -> Any:
        record = self.store.get_interrupt(RUN, interrupt_id)
        assert record is not None
        return record

    def document(self, interrupt_id: str) -> dict:
        text = self.env.cp_interrupts.resolution_document(self.record(interrupt_id))
        return json.loads(text)

    def events(self) -> tuple:
        return self.env.cp_events.project_run(self.store, RUN)

    @property
    def errors(self) -> types.SimpleNamespace:
        m = self.env.cp_model
        return types.SimpleNamespace(
            input=m.ResolutionInputError,
            conflict=m.ResolutionConflictError,
            expired=m.InterruptExpiredError,
            request=m.InterruptRequestError,
            support=self.env.cp_interrupts.InterruptSupportError,
        )

    def limits(self) -> dict:
        i = self.env.cp_interrupts
        return {
            "INPUT_LIMIT": i.INPUT_LIMIT,
            "KIND_LIMIT": i.KIND_LIMIT,
            "EXPIRES_IN_S_MAX": i.EXPIRES_IN_S_MAX,
        }


@pytest.fixture
def sides(env: types.SimpleNamespace) -> tuple[_Side, _Side]:
    (env.tmp / "jk").mkdir()
    (env.tmp / "cp").mkdir()
    return _JobKitSide(env, env.tmp / "jk"), _CpkSide(env, env.tmp / "cp")


def _both(sides: tuple[_Side, _Side], fn: Callable[[_Side], Any]) -> tuple[Any, Any]:
    return fn(sides[0]), fn(sides[1])


# The same logical request: keys out of order and a non-ASCII character, so the
# canonical text is observable.
_SCHEMA_UNORDERED = {
    "additionalProperties": False,
    "properties": {"note": {"type": "string"}, "approved": {"const": True}},
    "required": ["approved"],
    "type": "object",
}
_PAYLOAD_UNORDERED = {"zeta": 1, "action": "push tag \u00e9", "alpha": {"b": 2, "a": 1}}
_ANSWER_UNORDERED = {"note": "ok \u00e9", "approved": True}


def _open(side: _Side, *, expires_in_s: Optional[int] = EXPIRES_IN_S) -> str:
    return side.request(
        _SCHEMA_UNORDERED, _PAYLOAD_UNORDERED, kind="approval", expires_in_s=expires_in_s
    )


# ---------------------------------------------------------------------------
# What each store records for the same request and answer
# ---------------------------------------------------------------------------


def test_request_is_stored_as_the_same_canonical_text(sides: tuple[_Side, _Side]) -> None:
    jk, cp = sides
    _open(jk)
    _open(cp)
    [jk_row], [cp_row] = jk.rows("interrupts"), cp.rows("interrupts")
    assert jk_row["request_schema_json"] == cp_row["request_schema_json"]
    assert jk_row["payload_json"] == cp_row["payload_json"]
    assert jk_row["request_schema_json"] == contract.canonical_json(_SCHEMA_UNORDERED)
    assert jk_row["payload_json"] == contract.canonical_json(_PAYLOAD_UNORDERED)
    assert jk_row["payload_json"].isascii()
    assert jk_row["kind"] == cp_row["kind"] == "approval"
    assert jk_row["expires_at"] == cp_row["expires_at"] == CREATED + EXPIRES_IN_S
    assert contract.expiry(CREATED, EXPIRES_IN_S) == CREATED + EXPIRES_IN_S
    # The envelope literals differ by design; each names its own store.
    assert jk_row["envelope"] == "job-kit.interrupt-request/v1"
    assert cp_row["envelope"] == "plugins-kit.interrupt-request/v1"


def test_answer_is_stored_as_the_same_input_json(sides: tuple[_Side, _Side]) -> None:
    jk, cp = sides
    results = []
    for side in sides:
        interrupt_id = _open(side)
        results.append(
            side.resolve(interrupt_id, decision="answer", input=_ANSWER_UNORDERED, now=ANSWERED_AT)
        )
    [jk_row], [cp_row] = jk.rows("interrupt_resolutions"), cp.rows("interrupt_resolutions")
    assert jk_row["input_json"] == cp_row["input_json"] == contract.canonical_json(_ANSWER_UNORDERED)
    assert jk_row["outcome"] == cp_row["outcome"] == "answered"
    assert jk_row["reason"] is None and cp_row["reason"] is None
    assert jk_row["resolved_at"] == cp_row["resolved_at"] == ANSWERED_AT
    assert [r.outcome for r in results] == ["answered", "answered"]
    assert [r.replayed for r in results] == [False, False]
    assert results[0].input == results[1].input == {"approved": True, "note": "ok \u00e9"}


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"approved": False}, id="const-violation"),
        pytest.param({"approved": True, "extra": 1}, id="additional-property"),
        pytest.param({}, id="missing-required"),
        pytest.param({"approved": True, "note": 5}, id="wrong-type"),
        pytest.param({"approved": True, "a": {1, 2}}, id="not-json-native"),
        pytest.param({"approved": True, "pad": "x" * 70000}, id="oversized"),
        pytest.param({"approved": float("nan")}, id="non-finite"),
    ],
)
def test_failing_answer_is_refused_with_the_same_tuples(
    sides: tuple[_Side, _Side], answer: object
) -> None:
    caught = []
    for side in sides:
        interrupt_id = _open(side)
        before = side.count_rows()
        with pytest.raises(side.errors.input) as excinfo:
            side.resolve(interrupt_id, decision="answer", input=answer, now=ANSWERED_AT)
        assert side.count_rows() == before == (1, 0), side.label
        caught.append((str(excinfo.value), excinfo.value.errors))
    assert caught[0] == caught[1]
    if answer.get("approved") is False or answer == {}:  # a schema failure, not a size failure
        assert caught[0][1], "a schema failure carries the validator's tuples"
    if isinstance(answer.get("pad"), str) or any(isinstance(v, (set, float)) for v in answer.values()):
        assert caught[0][1] == (), "a value refused before validation carries no tuples"


def test_failing_answer_tuples_are_the_validators_own(sides: tuple[_Side, _Side], env) -> None:
    expected = tuple(env.lsk.validate(_SCHEMA_UNORDERED, {"approved": False}, subset=env.lsk.SUBSET_V1))
    assert expected
    for side in sides:
        interrupt_id = _open(side)
        with pytest.raises(side.errors.input) as excinfo:
            side.resolve(interrupt_id, decision="answer", input={"approved": False}, now=ANSWERED_AT)
        assert tuple(excinfo.value.errors) == expected, side.label


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"kind": "Bad Kind", "request_schema": APPROVAL, "payload": {}}, id="kind"),
        pytest.param({"kind": "a", "request_schema": {"type": "nope"}, "payload": {}}, id="schema"),
        pytest.param({"kind": "a", "request_schema": APPROVAL, "payload": [1]}, id="payload-shape"),
        pytest.param(
            {"kind": "a", "request_schema": APPROVAL, "payload": {"x": float("nan")}},
            id="payload-non-finite",
        ),
        pytest.param(
            {"kind": "a", "request_schema": APPROVAL, "payload": {}, "expires_in_s": 0},
            id="expiry-low",
        ),
        pytest.param(
            {"kind": "a", "request_schema": APPROVAL, "payload": {}, "expires_in_s": 2**31},
            id="expiry-high",
        ),
    ],
)
def test_refused_request_reads_the_same_in_both_stores(
    sides: tuple[_Side, _Side], fields: dict
) -> None:
    messages = []
    for side in sides:
        with pytest.raises(side.errors.request) as excinfo:
            side.check_request(side.build_request(**fields))
        messages.append(str(excinfo.value))
    assert messages[0] == messages[1]


def test_a_request_envelope_is_accepted_only_by_its_own_store(sides: tuple[_Side, _Side]) -> None:
    jk, cp = sides
    fields = {"kind": "a", "request_schema": APPROVAL, "payload": {}}
    jk.check_request(jk.build_request(**fields))
    cp.check_request(cp.build_request(**fields))
    messages = []
    for side, foreign in ((jk, cp.request_literal), (cp, jk.request_literal)):
        request = side.env.jk_model.InterruptRequest if side is jk else side.env.cp_model.InterruptRequest
        with pytest.raises(side.errors.request) as excinfo:
            side.check_request(request(envelope=foreign, **fields))
        messages.append(str(excinfo.value))
    assert messages[0] == (
        "interrupt request schema 'plugins-kit.interrupt-request/v1' is not accepted; "
        "this job-kit accepts 'job-kit.interrupt-request/v1'"
    )
    assert messages[1] == (
        "interrupt request schema 'job-kit.interrupt-request/v1' is not accepted; "
        "this content-pipeline-kit accepts 'plugins-kit.interrupt-request/v1'"
    )


def test_shared_limits_are_one_set_of_numbers(sides: tuple[_Side, _Side], env) -> None:
    jk, cp = sides
    expected = {
        "INPUT_LIMIT": contract.INPUT_LIMIT,
        "KIND_LIMIT": contract.KIND_LIMIT,
        "EXPIRES_IN_S_MAX": contract.EXPIRES_IN_S_MAX,
    }
    assert expected == {"INPUT_LIMIT": 65536, "KIND_LIMIT": 64, "EXPIRES_IN_S_MAX": 2**31 - 1}
    assert jk.limits() == cp.limits() == expected
    assert env.cp_interrupts.REASON_LIMIT == contract.REASON_LIMIT == 2000
    assert env.jk_interrupts.REQUEST_FILE_LIMIT == contract.REQUEST_DOCUMENT_LIMIT == 131072


def test_the_answer_size_limit_is_the_same_boundary(sides: tuple[_Side, _Side]) -> None:
    """The largest answer both accept, and the first one both refuse."""
    schema = {"type": "object"}

    def answer_of(size: int) -> dict:
        base = len(contract.canonical_json({"p": ""}))
        return {"p": "x" * (size - base)}

    assert len(contract.canonical_json(answer_of(contract.INPUT_LIMIT))) == contract.INPUT_LIMIT
    for side in sides:
        interrupt_id = side.request(schema, {}, kind="big", expires_in_s=None)
        with pytest.raises(side.errors.input):
            side.resolve(
                interrupt_id,
                decision="answer",
                input=answer_of(contract.INPUT_LIMIT + 1),
                now=ANSWERED_AT,
            )
        resolved = side.resolve(
            interrupt_id, decision="answer", input=answer_of(contract.INPUT_LIMIT), now=ANSWERED_AT
        )
        assert resolved.outcome == "answered", side.label


# ---------------------------------------------------------------------------
# Replay, conflict, rejection, lapse
# ---------------------------------------------------------------------------


def test_identical_answer_replays_and_a_different_one_conflicts(sides: tuple[_Side, _Side]) -> None:
    verdicts = []
    for side in sides:
        interrupt_id = _open(side)
        first = side.resolve(interrupt_id, decision="answer", input=_ANSWER_UNORDERED, now=ANSWERED_AT)
        rows = (side.rows("interrupts"), side.rows("interrupt_resolutions"))
        reordered = {"approved": True, "note": "ok \u00e9"}
        replay = side.resolve(interrupt_id, decision="answer", input=reordered, now=ANSWERED_AT + 5)
        assert (side.rows("interrupts"), side.rows("interrupt_resolutions")) == rows, side.label
        with pytest.raises(side.errors.conflict) as different_answer:
            side.resolve(
                interrupt_id, decision="answer", input={"approved": True}, now=ANSWERED_AT + 6
            )
        with pytest.raises(side.errors.conflict) as rejection:
            side.resolve(interrupt_id, decision="reject", reason="no", now=ANSWERED_AT + 7)
        assert (side.rows("interrupts"), side.rows("interrupt_resolutions")) == rows, side.label
        verdicts.append(
            (
                first.replayed,
                replay.replayed,
                replay.resolved_at,
                str(different_answer.value),
                different_answer.value.stored_outcome,
                str(rejection.value),
            )
        )
    assert verdicts[0] == verdicts[1]
    assert verdicts[0][:3] == (False, True, ANSWERED_AT)
    assert verdicts[0][4] == "answered"


def test_identical_rejection_replays_and_the_reason_is_bounded(sides: tuple[_Side, _Side]) -> None:
    long_reason = "r" * (contract.REASON_LIMIT + 500)
    verdicts = []
    for side in sides:
        interrupt_id = _open(side)
        first = side.resolve(interrupt_id, decision="reject", reason=long_reason, now=ANSWERED_AT)
        replay = side.resolve(interrupt_id, decision="reject", reason=long_reason, now=ANSWERED_AT + 1)
        with pytest.raises(side.errors.conflict) as conflict:
            side.resolve(interrupt_id, decision="reject", reason="different", now=ANSWERED_AT + 2)
        [row] = side.rows("interrupt_resolutions")
        verdicts.append(
            (
                first.outcome,
                first.replayed,
                replay.replayed,
                len(first.reason),
                row["reason"],
                row["input_json"],
                conflict.value.stored_outcome,
            )
        )
    assert verdicts[0] == verdicts[1]
    assert verdicts[0][:4] == ("rejected", False, True, contract.REASON_LIMIT)
    assert verdicts[0][5] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"decision": "reject", "input": {"approved": True}}, id="reject-with-input"),
        pytest.param(
            {"decision": "answer", "input": {"approved": True}, "reason": "why"},
            id="answer-with-reason",
        ),
        pytest.param({"decision": "approve", "input": {"approved": True}}, id="unknown-decision"),
    ],
)
def test_malformed_decisions_are_refused_alike(sides: tuple[_Side, _Side], kwargs: dict) -> None:
    outcomes = []
    for side in sides:
        interrupt_id = _open(side)
        try:
            side.resolve(interrupt_id, now=ANSWERED_AT, **kwargs)
        except Exception as error:  # noqa: BLE001 - the exact class is compared
            # Both stores let the contract's DecisionError surface as the
            # builtin ValueError; compare the exact class and the message.
            outcomes.append((type(error), str(error)))
        else:  # pragma: no cover - the assertion below reports it
            outcomes.append(None)
        assert side.count_rows() == (1, 0), side.label
    assert outcomes[0] is not None and outcomes[0] == outcomes[1]
    assert outcomes[0][0] is ValueError


def test_expiry_is_inclusive_and_recorded_the_same(sides: tuple[_Side, _Side]) -> None:
    expires_at = CREATED + EXPIRES_IN_S
    seen = []
    for side in sides:
        interrupt_id = _open(side)
        assert [r.id for r in side.expire(expires_at - 0.001)] == []
        assert side.count_rows() == (1, 0)
        record = side.record(interrupt_id)
        assert record.lapsed(expires_at - 0.001) is False
        assert record.lapsed(expires_at) is True
        assert contract.lapsed(record.expires_at, expires_at) is True
        expired = side.expire(expires_at)
        assert [r.id for r in expired] == [interrupt_id]
        assert side.expire(expires_at + 10) == []
        [row] = side.rows("interrupt_resolutions")
        seen.append((row["outcome"], row["input_json"], row["reason"], row["resolved_at"]))
    assert seen[0] == seen[1] == ("expired", None, None, expires_at)


def test_resolving_after_a_lapse_records_the_expiry_then_refuses(sides: tuple[_Side, _Side]) -> None:
    seen = []
    for side in sides:
        interrupt_id = _open(side)
        with pytest.raises(side.errors.expired) as excinfo:
            side.resolve(
                interrupt_id,
                decision="answer",
                input=_ANSWER_UNORDERED,
                now=CREATED + EXPIRES_IN_S,
            )
        [row] = side.rows("interrupt_resolutions")
        seen.append((row["outcome"], row["input_json"], excinfo.value.expires_at))
    assert seen[0] == seen[1] == ("expired", None, CREATED + EXPIRES_IN_S)


# ---------------------------------------------------------------------------
# The resolution document
# ---------------------------------------------------------------------------


def _resolve_as(side: _Side, outcome: str) -> str:
    interrupt_id = _open(side)
    if outcome == "answered":
        side.resolve(interrupt_id, decision="answer", input=_ANSWER_UNORDERED, now=ANSWERED_AT)
    elif outcome == "rejected":
        side.resolve(interrupt_id, decision="reject", reason="not today", now=ANSWERED_AT)
    else:
        side.expire(CREATED + EXPIRES_IN_S)
    return interrupt_id


@pytest.mark.parametrize("outcome", ["answered", "rejected", "expired"])
def test_resolution_documents_agree_once_schema_and_id_are_removed(
    sides: tuple[_Side, _Side], outcome: str
) -> None:
    documents = []
    for side in sides:
        document = side.document(_resolve_as(side, outcome))
        assert sorted(document) == [
            "input",
            "interrupt_id",
            "kind",
            "outcome",
            "payload",
            "resolved_at",
            "schema",
        ]
        assert document["schema"] == side.resolution_literal
        documents.append(document)
    assert documents[0]["schema"] == "job-kit.interrupt-resolution/v1"
    assert documents[1]["schema"] == "plugins-kit.interrupt-resolution/v1"
    stripped = [
        {k: v for k, v in document.items() if k not in ("schema", "interrupt_id")}
        for document in documents
    ]
    assert stripped[0] == stripped[1]
    assert stripped[0]["outcome"] == outcome
    assert stripped[0]["resolved_at"].endswith("Z")


@pytest.mark.parametrize("outcome", ["answered", "rejected", "expired"])
def test_resolution_document_bytes_equal_the_contracts_rendering(
    sides: tuple[_Side, _Side], env, outcome: str
) -> None:
    for side in sides:
        interrupt_id = _resolve_as(side, outcome)
        record = side.record(interrupt_id)
        adapter = (
            env.jk_interrupts if side is sides[0] else env.cp_interrupts
        ).resolution_document(record)
        direct = contract.resolution_document(
            resolution_envelope=side.resolution_literal,
            interrupt_id=record.id,
            kind=record.kind,
            outcome=record.resolution.outcome,
            input=record.resolution.input,
            payload=record.payload,
            resolved_at=record.resolution.resolved_at,
        )
        assert adapter == direct, side.label


# ---------------------------------------------------------------------------
# Events: the phase sequences and the streams
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome,phases",
    [
        ("answered", ["requested", "resolved"]),
        ("rejected", ["requested", "rejected"]),
        ("expired", ["requested", "expired"]),
    ],
)
def test_interrupt_phase_sequences_are_equal(
    sides: tuple[_Side, _Side], outcome: str, phases: list[str]
) -> None:
    sequences = []
    for side in sides:
        _resolve_as(side, outcome)
        sequences.append(side.interrupt_phases())
    assert sequences[0] == sequences[1] == phases


def test_a_waiting_request_emits_only_the_requested_phase(sides: tuple[_Side, _Side]) -> None:
    for side in sides:
        _open(side)
        assert side.interrupt_phases() == ["requested"], side.label
        assert [e["event"] for e in side.events()].count("terminal") == 0, side.label


@pytest.mark.parametrize("outcome", ["answered", "rejected", "expired"])
def test_both_streams_validate_alone_and_interleaved(
    sides: tuple[_Side, _Side], outcome: str
) -> None:
    streams = []
    for side in sides:
        _resolve_as(side, outcome)
        stream = side.events()
        assert ee.validate_stream(stream) == stream, side.label
        streams.append(stream)
    jk_stream, cp_stream = streams
    assert ee.validate_stream(tuple(jk_stream) + tuple(cp_stream)) == tuple(jk_stream) + tuple(
        cp_stream
    )
    alternating = []
    for index in range(max(len(jk_stream), len(cp_stream))):
        alternating.extend(s[index] for s in (jk_stream, cp_stream) if index < len(s))
    assert ee.validate_stream(tuple(alternating)) == tuple(alternating)
    assert {event["schema"] for event in alternating if event["event"] == "interrupt"} == {
        "plugins-kit.execution-event/v2"
    }


# ---------------------------------------------------------------------------
# Both stores run the one module
# ---------------------------------------------------------------------------


class _Sentinel(Exception):
    """Raised by the patched contract; neither store may translate it."""


def test_both_stores_route_the_request_check_through_the_one_module(
    sides: tuple[_Side, _Side], env, monkeypatch: pytest.MonkeyPatch
) -> None:
    jk, cp = sides
    calls: list[str] = []

    def sentinel(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("owner", "?"))
        raise _Sentinel("contract.check_request was reached")

    monkeypatch.setattr(contract, "check_request", sentinel)
    fields = {"kind": "approval", "request_schema": APPROVAL, "payload": {}}
    with pytest.raises(_Sentinel):
        env.jk_interrupts.check_request(jk.build_request(**fields))
    with pytest.raises(_Sentinel):
        jk.request(APPROVAL, {}, kind="approval", expires_in_s=None)
    assert jk.rows("interrupts") == []
    cp_token = cp.store.claim_unit(RUN, UNIT, "worker", at=CREATED - 1).fencing_token
    with pytest.raises(_Sentinel):
        cp.store.request_interrupt(RUN, UNIT, cp_token, cp.build_request(**fields), at=CREATED)
    with pytest.raises(_Sentinel):
        env.cp_interrupts.check_request(cp.build_request(**fields))
    assert cp.rows("interrupts") == []
    assert calls == ["job-kit", "job-kit", "content-pipeline-kit", "content-pipeline-kit"]


def test_both_stores_route_the_answer_rules_through_the_one_module(
    sides: tuple[_Side, _Side], monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [_open(side) for side in sides]

    def sentinel(*args: Any, **kwargs: Any) -> Any:
        raise _Sentinel("contract.validate_input was reached")

    monkeypatch.setattr(contract, "validate_input", sentinel)
    for side, interrupt_id in zip(sides, ids):
        with pytest.raises(_Sentinel):
            side.resolve(interrupt_id, decision="answer", input={"approved": True}, now=ANSWERED_AT)
        assert side.count_rows() == (1, 0), side.label


def test_both_stores_route_the_replay_and_document_rules_through_the_one_module(
    sides: tuple[_Side, _Side], monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = []
    for side in sides:
        interrupt_id = _open(side)
        side.resolve(interrupt_id, decision="answer", input=_ANSWER_UNORDERED, now=ANSWERED_AT)
        ids.append(interrupt_id)
    monkeypatch.setattr(contract, "same_resolution", lambda **kwargs: (_ for _ in ()).throw(_Sentinel()))
    for side, interrupt_id in zip(sides, ids):
        with pytest.raises(_Sentinel):
            side.resolve(interrupt_id, decision="answer", input=_ANSWER_UNORDERED, now=ANSWERED_AT)
    monkeypatch.setattr(
        contract, "resolution_document", lambda **kwargs: (_ for _ in ()).throw(_Sentinel())
    )
    for side, interrupt_id in zip(sides, ids):
        with pytest.raises(_Sentinel):
            side.document(interrupt_id)


# ---------------------------------------------------------------------------
# Both stores validate under the one subset
# ---------------------------------------------------------------------------


def test_the_validation_subset_is_one_literal(env) -> None:
    assert contract.VALIDATOR_SUBSET == "llm-scripting-kit.json-schema-subset/v1"
    assert contract.VALIDATOR_SUBSET == env.lsk.SUBSET_V1
    assert env.cp_interrupts.VALIDATOR_SUBSET == env.lsk.SUBSET_V1
    assert contract.VALIDATOR_SUBSET in env.lsk.SUPPORTED_SUBSETS
    assert env.jk_interrupts._interrupt_contract().VALIDATOR_SUBSET == env.lsk.SUBSET_V1


def _fake_validator(env, **overrides: Any) -> None:
    fake = types.ModuleType("llm_scripting_kit.completion.json_schema")
    fake.check_schema = env.lsk.check_schema  # type: ignore[attr-defined]
    fake.validate = env.lsk.validate  # type: ignore[attr-defined]
    for name, value in overrides.items():
        setattr(fake, name, value)
    env.swap_module("llm_scripting_kit.completion.json_schema", fake)


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(None, id="no-marker"),
        pytest.param(frozenset(), id="empty-marker"),
        pytest.param(frozenset({"llm-scripting-kit.json-schema-subset/v2"}), id="other-subset"),
    ],
)
def test_a_validator_without_the_subset_marker_is_refused_by_both(
    sides: tuple[_Side, _Side], env, monkeypatch: pytest.MonkeyPatch, marker: Optional[frozenset]
) -> None:
    jk, cp = sides
    overrides = {} if marker is None else {"SUPPORTED_SUBSETS": marker}
    _fake_validator(env, **overrides)
    fields = {"kind": "approval", "request_schema": APPROVAL, "payload": {}}
    with pytest.raises(env.jk_interrupts.JsonSchemaSupportError):
        env.jk_interrupts._schema_validator()
    with pytest.raises(env.jk_interrupts.JsonSchemaSupportError):
        jk.check_request(jk.build_request(**fields))
    with pytest.raises(env.cp_interrupts.InterruptSupportError):
        env.cp_interrupts._schema_validator()
    with pytest.raises(env.cp_interrupts.InterruptSupportError):
        cp.check_request(cp.build_request(**fields))
    token = cp.store.claim_unit(RUN, UNIT, "worker", at=CREATED - 1).fencing_token
    with pytest.raises(env.cp_interrupts.InterruptSupportError):
        cp.store.request_interrupt(RUN, UNIT, token, cp.build_request(**fields), at=CREATED)
    assert cp.rows("interrupts") == []
    with pytest.raises(ImportError):  # both refusals are ImportErrors
        jk.check_request(jk.build_request(**fields))


def test_a_validator_with_the_subset_marker_is_accepted_by_both(
    sides: tuple[_Side, _Side], env, monkeypatch: pytest.MonkeyPatch
) -> None:
    jk, cp = sides
    _fake_validator(env, SUPPORTED_SUBSETS=frozenset({contract.VALIDATOR_SUBSET}))
    fields = {"kind": "approval", "request_schema": APPROVAL, "payload": {}}
    jk.check_request(jk.build_request(**fields))
    cp.check_request(cp.build_request(**fields))


def test_every_validator_call_selects_the_subset(
    sides: tuple[_Side, _Side], env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, Optional[str]]] = []
    real_check, real_validate = env.lsk.check_schema, env.lsk.validate

    def check_schema(schema: object, *, subset: Optional[str] = None) -> None:
        seen.append(("check_schema", subset))
        return real_check(schema, subset=subset)

    def validate(schema: object, value: object, *, subset: Optional[str] = None) -> tuple:
        seen.append(("validate", subset))
        return real_validate(schema, value, subset=subset)

    _fake_validator(
        env,
        SUPPORTED_SUBSETS=env.lsk.SUPPORTED_SUBSETS,
        check_schema=check_schema,
        validate=validate,
    )
    for side in sides:
        seen.clear()
        interrupt_id = _open(side)
        side.resolve(interrupt_id, decision="answer", input=_ANSWER_UNORDERED, now=ANSWERED_AT)
        assert {name for name, _ in seen} == {"check_schema", "validate"}, side.label
        assert {subset for _, subset in seen} == {env.lsk.SUBSET_V1}, side.label


# ---------------------------------------------------------------------------
# Composition with the library round trip, through the inline lane
# ---------------------------------------------------------------------------

_QUESTION_ANSWER_SCHEMA = {
    "type": "object",
    "required": ["id", "answer"],
    "properties": {"id": {"type": "string"}, "answer": {"type": "string"}},
    "additionalProperties": False,
}


def test_library_round_trip_composes_with_a_durable_wait(env) -> None:
    from content_pipeline.execution.drivers.inline import run_wave
    from content_pipeline.execution.interrupts import unit_resolutions, waiting_units
    from content_pipeline.execution.model import InterruptRequest, InterruptRequested, UnitState
    from content_pipeline.roundtrip import questions

    store = env.cp_store.ExecutionStore(env.tmp / "round.db")
    store.create_run(
        RUN, driver="inline", backend="mock", model="m1", adapter_version="7", created_at=CREATED
    )
    store.register_units(RUN, ["u0"], at=CREATED)
    asked = [
        {"id": "q1", "prompt": "Which colour?", "answer": ""},
        {"id": "q2", "prompt": "Which size?", "answer": "large"},
    ]
    contexts: list[list[dict]] = []

    def generate(work_unit: Any) -> str:
        previous = unit_resolutions(store, RUN, work_unit.id)
        if not previous:
            still_open = questions.unanswered(asked)
            assert [q["id"] for q in still_open] == ["q1"]
            raise InterruptRequested(
                InterruptRequest(
                    kind="clarify",
                    request_schema=_QUESTION_ANSWER_SCHEMA,
                    payload={"questions": still_open},
                )
            )
        [outcome] = previous
        assert outcome["outcome"] == "answered"
        merged = questions.answer(
            outcome["payload"]["questions"] + [asked[1]],
            outcome["input"]["id"],
            outcome["input"]["answer"],
        )
        contexts.append(questions.answered_context(merged))
        return "generated with the answers"

    wave = [store.get_unit(RUN, "u0")]
    assert run_wave(store, RUN, wave, generate=generate, at=CREATED) == []
    assert [u.unit_id for u in waiting_units(store, RUN)] == ["u0"]
    record = store.open_interrupt(RUN, "u0")
    assert record.payload == {"questions": [{"id": "q1", "prompt": "Which colour?", "answer": ""}]}

    with pytest.raises(env.cp_model.ResolutionInputError):
        store.resolve_interrupt(
            RUN, record.id, decision="answer", input={"id": "q1"}, now=CREATED + 1
        )
    store.resolve_interrupt(
        RUN, record.id, decision="answer", input={"id": "q1", "answer": "blue"}, now=CREATED + 2
    )
    assert store.get_unit(RUN, "u0").state is UnitState.PENDING

    wave = [store.get_unit(RUN, "u0")]
    assert run_wave(store, RUN, wave, generate=generate, at=CREATED + 3) == ["u0"]
    assert contexts == [
        [
            {"id": "q1", "prompt": "Which colour?", "answer": "blue"},
            {"id": "q2", "prompt": "Which size?", "answer": "large"},
        ]
    ]
    assert store.get_unit(RUN, "u0").state is UnitState.ACCEPTED
    stream = env.cp_events.project_run(store, RUN)
    assert ee.validate_stream(stream) == stream


# ---------------------------------------------------------------------------
# Neither plugin gains an edge to the other
# ---------------------------------------------------------------------------


def _manifest_text(plugin: str) -> str:
    return "\n".join(
        (PLUGINS / plugin / relative).read_text(encoding="utf-8")
        for relative in (".claude-plugin/plugin.json", "bootstrap.json")
    )


def test_content_pipeline_manifests_name_no_job_kit() -> None:
    text = _manifest_text("content-pipeline-kit").lower()
    assert "job-kit" not in text
    assert "job_kit" not in text
    manifest = json.loads((PLUGINS / "content-pipeline-kit" / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    assert manifest["dependencies"] == ["bootstrap"]


def test_job_kit_manifests_name_no_content_pipeline() -> None:
    text = _manifest_text("job-kit").lower()
    assert "content-pipeline" not in text
    assert "content_pipeline" not in text
    manifest = json.loads((PLUGINS / "job-kit" / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    assert manifest["dependencies"] == ["bootstrap"]


def test_the_last_test_leaves_nothing_behind() -> None:
    """Runs the autouse check once more, for the test before this one."""
