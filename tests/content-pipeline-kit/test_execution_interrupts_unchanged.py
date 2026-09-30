"""A consumer that never asks sees no change from the interrupt verbs.

Characterization of the execution store, the status digest, the wave
functions and the event projection for a run that never calls an interrupt
verb. Every expected value below is a literal recorded at commit 0c906bc6,
before the waiting state existed: the sha256 digests of the rows a scripted
run writes, of its status digest, of its projected events and of its wave
answers, the signature of every function a consumer already calls, and the
SQL of every table and index that existed at that commit.

The one visible change for such a consumer is stated, not hidden: opening a
store adds the two interrupt tables (schema step 10). This file pins that no
table or index that existed before is altered by it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import inspect
import json
import sqlite3

import pytest

from content_pipeline.execution import events as events_mod
from content_pipeline.execution import status as status_mod
from content_pipeline.execution import wave as wave_mod
from content_pipeline.execution.controller import unfinished_units
from content_pipeline.execution.events import project_run
from content_pipeline.execution.model import StaleFenceError, UnitState, UsageRecord
from content_pipeline.execution.status import RunStatus, compute_status
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.wave import graph_block_reason, ready_wave
from content_pipeline.pipeline.workunit import FlatChunkStrategy, GraphWalkStrategy

RUN = "r1"
FLAT = FlatChunkStrategy(select=lambda store: [])
GRAPH = GraphWalkStrategy(order=lambda store: [])


def _digest(value) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _rows(store: ExecutionStore, query: str) -> list:
    with contextlib.closing(sqlite3.connect(str(store.db_path))) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query)]


def _scripted_run(tmp_path) -> ExecutionStore:
    """One run that touches every row kind and state the store had before
    the waiting state, with fixed clocks, and never calls an interrupt verb."""
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(
        RUN,
        driver="inline",
        backend="mock",
        model="m1",
        adapter_version="7",
        created_at=1000.0,
        environment={"PWD": "proj"},
    )
    store.register_units(RUN, ["u0", "u1", "u2", "u3", "u4", "u5", "u6"], at=1000.0)
    # A second run shares the attempts id sequence, so this run's ids have gaps.
    store.create_run(
        "r2", driver="inline", backend="mock", model="m1", adapter_version="7", created_at=1000.5
    )
    store.register_units("r2", ["x"], at=1000.5)

    t0 = store.claim_unit(RUN, "u0", "w0", at=1001.0).fencing_token
    store.claim_unit("r2", "x", "wx", at=1001.5)
    store.renew_lease(RUN, "u0", t0, at=1002.0)
    store.accept_unit(RUN, "u0", t0, text="T0", usage=UsageRecord(5, 6, None), at=1003.0)
    store.record_apply_started(RUN, "u0", at=1004.0)
    store.record_apply_succeeded(RUN, "u0", at=1005.0)

    old = store.claim_unit(RUN, "u1", "w1", lease_seconds=1, at=1006.0).fencing_token
    new = store.claim_unit(RUN, "u1", "w1b", at=1100.0).fencing_token
    with pytest.raises(StaleFenceError):
        store.accept_unit(RUN, "u1", old, at=1101.0)
    store.fail_unit(RUN, "u1", new, error="again", usage=UsageRecord(1, 2, 3), at=1102.0)
    last = store.claim_unit(RUN, "u1", "w1c", at=1103.0).fencing_token
    store.fail_unit(RUN, "u1", last, error="dead", terminal=True, at=1104.0)

    t2 = store.claim_unit(RUN, "u2", "w2", at=1105.0).fencing_token
    store.fail_unit(
        RUN,
        "u2",
        t2,
        error="skip:up_to_date",
        terminal=True,
        terminal_state=UnitState.SKIPPED,
        at=1106.0,
    )

    t3 = store.claim_unit(RUN, "u3", "w3", at=1107.0).fencing_token
    store.accept_unit(RUN, "u3", t3, at=1108.0)
    store.record_apply_started(RUN, "u3", at=1109.0)
    store.record_apply_rejected(RUN, "u3", "no", at=1110.0)

    store.claim_unit(RUN, "u4", "w4", at=1111.0)
    store.record_dispatch(RUN, "u4", "w4", session_id="s4", cli_version="1.0", at=1111.5)

    t6 = store.claim_unit(RUN, "u6", "w6", at=1112.0).fencing_token
    store.fail_unit(RUN, "u6", t6, error={"code": "env_mismatch", "detail": "x"}, at=1113.0)

    store.set_halt(RUN, "rate_limit", "first", at=1114.0)
    store.clear_halt(RUN)
    store.set_halt(RUN, "budget", "second", at=1115.0)
    return store


def _wave_answers(tmp_path) -> dict:
    """What the wave functions answer for the states a run could be in before
    the waiting state existed."""
    answers: dict = {}

    def fresh(name: str) -> ExecutionStore:
        store = ExecutionStore(tmp_path / f"{name}.db")
        store.create_run(
            RUN, driver="inline", backend="mock", model="m1", adapter_version="7", created_at=1.0
        )
        store.register_units(RUN, ["u0", "u1", "u2"], at=1.0)
        return store

    def record(name: str, store: ExecutionStore) -> None:
        answers[name] = {
            "flat": [u.unit_id for u in ready_wave(store, RUN, FLAT)],
            "flat_capped": [u.unit_id for u in ready_wave(store, RUN, FLAT, max_wave_size=1)],
            "graph": [u.unit_id for u in ready_wave(store, RUN, GRAPH)],
            "graph_reclaim": [u.unit_id for u in ready_wave(store, RUN, GRAPH, reclaim_at=500.0)],
            "reason": graph_block_reason(store, RUN, GRAPH),
            "reason_at": graph_block_reason(store, RUN, GRAPH, at=500.0),
            "reason_flat": graph_block_reason(store, RUN, FLAT),
            "unfinished": [u.unit_id for u in unfinished_units(store, RUN)],
        }

    record("fresh", fresh("fresh"))

    store = fresh("claimed")
    store.claim_unit(RUN, "u0", "w", lease_seconds=10, at=2.0)
    record("claimed", store)

    store = fresh("accepted")
    token = store.claim_unit(RUN, "u0", "w", at=2.0).fencing_token
    store.accept_unit(RUN, "u0", token, at=3.0)
    record("accepted", store)
    store.record_apply_started(RUN, "u0", at=4.0)
    record("apply_started", store)
    store.record_apply_succeeded(RUN, "u0", at=5.0)
    record("applied", store)

    store = fresh("apply_rejected")
    token = store.claim_unit(RUN, "u0", "w", at=2.0).fencing_token
    store.accept_unit(RUN, "u0", token, at=3.0)
    store.record_apply_rejected(RUN, "u0", "no", at=4.0)
    record("apply_rejected", store)

    store = fresh("failed")
    token = store.claim_unit(RUN, "u0", "w", at=2.0).fencing_token
    store.fail_unit(RUN, "u0", token, terminal=True, at=3.0)
    record("failed", store)

    store = fresh("skipped")
    token = store.claim_unit(RUN, "u0", "w", at=2.0).fencing_token
    store.fail_unit(
        RUN, "u0", token, terminal=True, terminal_state=UnitState.SKIPPED, error="skip:x", at=3.0
    )
    record("skipped", store)
    return answers


# sha256 of canonical JSON, recorded at commit 0c906bc6.
_BASE_DIGESTS = {
    "units": "4796ee055da542801dc713739beeb80993c74d6b797bff7825188488e38c27e6",
    "attempts": "1cd2b98bf85a3be859670154b597f873007e098b2ee55b04ec710e60a3bfdb01",
    "runs": "98fd8f0868243c5ca6fc50bb9a7aede8512bed3d3cc1b13a6733e71a495326f1",
    "dispatches": "67a7630edca7fecaa36b9723e6823b24d340d9f0bf9670af12f1111302642c35",
    "status": "7779e4a8e44fdbea2941ce72e1bdf4be01aeb7e26f4c77c0403f5818a6ed0f42",
    "events": "08c828d779e198c5cd491bd0bb83af53a0d2cf8e6858216a3dc31340ed7d99e4",
    "waves": "0bea4a0b4959e2793f3fd11c265f3d8db5a480e9c75ecdd519b0428b8c8d7a7f",
}


def _run_digests(tmp_path) -> dict:
    store = _scripted_run(tmp_path)
    return {
        "units": _digest(_rows(store, "SELECT * FROM units ORDER BY run_id, ordinal")),
        "attempts": _digest(_rows(store, "SELECT * FROM attempts ORDER BY id")),
        "runs": _digest(_rows(store, "SELECT * FROM runs ORDER BY id")),
        "dispatches": _digest(_rows(store, "SELECT * FROM dispatches ORDER BY id")),
        "status": _digest(compute_status(store, RUN, now=1200.0).to_dict()),
        "events": _digest(list(project_run(store, RUN))),
        "waves": _digest(_wave_answers(tmp_path)),
    }


def test_run_without_interrupts_matches_the_base_digests(tmp_path):
    assert _run_digests(tmp_path) == _BASE_DIGESTS


def test_status_of_a_run_without_interrupts_has_the_five_base_states(tmp_path):
    store = _scripted_run(tmp_path)
    digest = compute_status(store, RUN, now=1200.0)
    assert digest.counts_by_state == {
        "accepted": 2,
        "claimed": 1,
        "failed": 1,
        "pending": 2,
        "skipped": 1,
    }


def test_run_status_field_set_is_the_base_set():
    assert [field.name for field in dataclasses.fields(RunStatus)] == [
        "run_id",
        "driver",
        "backend",
        "model",
        "adapter_version",
        "total_units",
        "counts_by_state",
        "elapsed_s",
        "oldest_in_flight_age_s",
        "expired_lease_count",
        "throughput_window_s",
        "accepted_in_window",
        "failed_in_window",
        "recent_failures",
        "truncated_failure_groups",
        "halted_kind",
        "halted_detail_code",
        "halted_at",
        "apply_counts",
        "apply_rejected_unit_ids",
        "apply_started_unit_ids",
    ]


# ``str(inspect.signature(...))`` of every function a consumer could already
# call, recorded at commit 0c906bc6.
_BASE_STORE_SIGNATURES = {
    "accept_unit": (
        "(self, run_id: 'str', unit_id: 'str', fencing_token: 'int', *, text: '"
        "Optional[str]' = None, usage: 'Optional[UsageRecord]' = None, at: 'Opt"
        "ional[float]' = None) -> 'None'"
    ),
    "acquire_dispatcher_lease": (
        "(self, run_id: 'str', dispatcher_id: 'str', *, lease_seconds: 'float',"
        " at: 'Optional[float]' = None) -> 'Optional[int]'"
    ),
    "attach_dispatch_session": (
        "(self, run_id: 'str', unit_id: 'str', session_id: 'str') -> 'None'"
    ),
    "claim_unit": (
        "(self, run_id: 'str', unit_id: 'str', worker_id: 'str', *, lease_secon"
        "ds: 'float' = 300.0, at: 'Optional[float]' = None) -> 'ClaimResult'"
    ),
    "clear_halt": (
        "(self, run_id: 'str') -> 'None'"
    ),
    "create_run": (
        "(self, run_id: 'str', *, driver: 'str', backend: 'str', model: 'str', "
        "adapter_version: 'str', created_at: 'Optional[float]' = None, environm"
        "ent: 'Optional[Mapping[str, str]]' = None) -> 'RunRecord'"
    ),
    "fail_unit": (
        "(self, run_id: 'str', unit_id: 'str', fencing_token: 'int', *, error: "
        "'Union[str, Mapping, Sequence]' = '', terminal: 'bool' = False, termin"
        "al_state: 'UnitState' = <UnitState.FAILED: 'failed'>, usage: 'Optional"
        "[UsageRecord]' = None, at: 'Optional[float]' = None) -> 'None'"
    ),
    "get_run": (
        "(self, run_id: 'str') -> 'Optional[RunRecord]'"
    ),
    "get_unit": (
        "(self, run_id: 'str', unit_id: 'str') -> 'Optional[UnitRecord]'"
    ),
    "list_attempts": (
        "(self, run_id: 'str', unit_id: 'Optional[str]' = None) -> 'List[Attemp"
        "tRecord]'"
    ),
    "list_units": (
        "(self, run_id: 'str') -> 'List[UnitRecord]'"
    ),
    "open_dispatches": (
        "(self, run_id: 'str') -> 'List[DispatchRecord]'"
    ),
    "read_transaction": (
        "(self) -> 'Iterator[sqlite3.Connection]'"
    ),
    "record_apply_rejected": (
        "(self, run_id: 'str', unit_id: 'str', reason: 'str', *, at: 'Optional["
        "float]' = None) -> 'None'"
    ),
    "record_apply_started": (
        "(self, run_id: 'str', unit_id: 'str', *, at: 'Optional[float]' = None)"
        " -> 'None'"
    ),
    "record_apply_succeeded": (
        "(self, run_id: 'str', unit_id: 'str', *, at: 'Optional[float]' = None)"
        " -> 'None'"
    ),
    "record_dispatch": (
        "(self, run_id: 'str', unit_id: 'str', worker_id: 'str', *, session_id:"
        " 'Optional[str]' = None, cli_version: 'Optional[str]' = None, at: 'Opt"
        "ional[float]' = None) -> 'int'"
    ),
    "register_units": (
        "(self, run_id: 'str', unit_ids: 'Sequence[str]', *, at: 'Optional[floa"
        "t]' = None) -> 'None'"
    ),
    "release_dispatcher_lease": (
        "(self, run_id: 'str', dispatcher_id: 'str', fence: 'int', *, at: 'Opti"
        "onal[float]' = None) -> 'None'"
    ),
    "renew_dispatcher_lease": (
        "(self, run_id: 'str', dispatcher_id: 'str', fence: 'int', *, lease_sec"
        "onds: 'float', at: 'Optional[float]' = None) -> 'float'"
    ),
    "renew_lease": (
        "(self, run_id: 'str', unit_id: 'str', fencing_token: 'int', *, lease_s"
        "econds: 'float' = 300.0, at: 'Optional[float]' = None) -> 'float'"
    ),
    "set_halt": (
        "(self, run_id: 'str', kind: 'str', detail: 'str' = '', *, at: 'Optiona"
        "l[float]' = None) -> 'None'"
    ),
    "settle_dispatch": (
        "(self, run_id: 'str', unit_id: 'str', *, outcome: 'str', session_id: '"
        "Optional[str]' = None, at: 'Optional[float]' = None) -> 'None'"
    ),
    "snapshot": (
        "(self, run_id: 'str', *, attempt_kinds: 'Optional[Iterable[AttemptKind"
        "]]' = None, attempts_since: 'Optional[float]' = None) -> 'Tuple[Option"
        "al[RunRecord], List[UnitRecord], List[AttemptRecord]]'"
    ),
}

_BASE_FUNCTION_SIGNATURES = {
    "status.compute_status": (
        "(store: 'ExecutionStore', run_id: 'str', *, throughput_window_s: 'floa"
        "t' = 300.0, max_failure_groups: 'int' = 5, now: 'Optional[float]' = No"
        "ne) -> 'RunStatus'"
    ),
    "status.apply_states": (
        "(units: 'List[Any]', attempts: 'List[Any]') -> 'Dict[str, str]'"
    ),
    "events.project_run": (
        "(store: 'Any', run_id: 'str') -> 'Tuple[dict, ...]'"
    ),
    "events.write_run_events": (
        "(store: 'Any', run_id: 'str', sink: 'Any') -> 'int'"
    ),
    "wave.ready_wave": (
        "(store, run_id: 'str', strategy: 'WorkUnitStrategy', *, max_wave_size:"
        " 'Optional[int]' = None, reclaim_at: 'Optional[float]' = None) -> 'Lis"
        "t[UnitRecord]'"
    ),
    "wave.graph_block_reason": (
        "(store, run_id: 'str', strategy: 'WorkUnitStrategy', *, at: 'Optional["
        "float]' = None) -> 'Optional[str]'"
    ),
    "wave.is_graph_strategy": (
        "(strategy: 'WorkUnitStrategy') -> 'bool'"
    ),
}


def test_existing_store_signatures_are_unchanged():
    current = {
        name: str(inspect.signature(getattr(ExecutionStore, name)))
        for name in _BASE_STORE_SIGNATURES
    }
    assert current == _BASE_STORE_SIGNATURES
    assert len(_BASE_STORE_SIGNATURES) == 24


def test_existing_function_signatures_are_unchanged():
    functions = {
        "status.compute_status": status_mod.compute_status,
        "status.apply_states": status_mod.apply_states,
        "events.project_run": events_mod.project_run,
        "events.write_run_events": events_mod.write_run_events,
        "wave.ready_wave": wave_mod.ready_wave,
        "wave.graph_block_reason": wave_mod.graph_block_reason,
        "wave.is_graph_strategy": wave_mod.is_graph_strategy,
    }
    current = {name: str(inspect.signature(fn)) for name, fn in functions.items()}
    assert current == _BASE_FUNCTION_SIGNATURES


# The SQL of every table and index the store had at commit 0c906bc6, as
# SQLite records it, reduced to one sha256 per object.
_BASE_SCHEMA_OBJECTS = {
    "attempts": "05b9e4b1a4f0a906d22ae303ead1b3f145225d70ccbea57a7ade290be9460a20",
    "dispatches": "ee2cb814981e94881fe79d1897979dfe03956b15d0e32d09f2656733ec0a5013",
    "idx_attempts_run_unit": "885de6b1ba5b7c79cc2ffe72d58cb1909ee498969a01cb875b31efbee4563689",
    "idx_dispatches_open_unique": "b04c9b5be980bd2905ff64c47dbe077fd3da7a32d4f8d00410f1a4768a456a55",
    "idx_dispatches_run_unit": "3c152cc926f6463b2cd61dae89542480ec2224cdf382b9df853b1da534fe42aa",
    "idx_units_run_state": "e6d443dfceafb83f23ccefc92b373b2c8db9fbd471d090b7c9451cc66e499518",
    "runs": "be681562abb1202c9cc29b642cf7117484755e493279b1191e767ab2c985fbb0",
    "schema_version": "611c04fc7d2e5808e5ab6a15bf04c437d3902fb132c59cf45effb2492a9fe7ad",
    "sqlite_sequence": "4cb1eaf14467f226196148cb5688569660cb290d414bae4c1c450b149b62befd",
    "units": "6ab6cf4b06777e48c87fcb66f03198fad1a602207218e7f1077bc2efae56475f",
}


def test_schema_objects_that_existed_before_are_unaltered(tmp_path):
    store = ExecutionStore(tmp_path / "run.db")
    with contextlib.closing(sqlite3.connect(str(store.db_path))) as conn:
        found = {
            name: hashlib.sha256(sql.encode("utf-8")).hexdigest()
            for name, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            )
        }
    assert {name: found.get(name) for name in _BASE_SCHEMA_OBJECTS} == _BASE_SCHEMA_OBJECTS
    assert len(_BASE_SCHEMA_OBJECTS) == 10
