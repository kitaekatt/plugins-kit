"""T2/T3/T4 -- contract tests for the B2 worker-invocation-shape fix.

Sibling of ``test_worker_invocation_roundtrip.py`` (T1, the full round
trip). This module pins three narrower, independently-mutable properties:

- T2: ``cli.run``'s ``protocol`` command splices a ``--text-file=<path>``
  file's content into ``payload["text"]`` before dispatch, and ONLY the
  ``=``-joined form works (``_split_flags`` never treats a bare
  ``--text-file <path>`` pair as carrying a value).
- T3: no invocation ``enumerate_worker_invocations`` returns ever carries a
  fencing token, and the same ``(worker_command, run_id, unit_id,
  worker_id)`` inputs produce the IDENTICAL six strings regardless of what
  the store has done to the unit in between two calls. This is the P5
  anchor, and the risk it guards is HIGHER than it used to be: the
  dispatcher now claims before the launch, so a real token EXISTS at
  enumeration time and could be interpolated into these strings by an
  otherwise-reasonable edit. It must not be -- an allowlist entry has to be
  computable before any claim happens.
- T4: ``renew`` is never enumerated for a worker -- D5 makes the DISPATCHER
  the renewer in the background lane (``supervise_tick`` calls
  ``store.renew_lease`` itself); a worker session never runs ``renew``.
"""

from __future__ import annotations

import io
import tempfile

import pytest

from content_pipeline.cli.run import build_commands
from content_pipeline.execution.adapter import PreparedRequest, RunAdapter
from content_pipeline.execution.drivers.claude_bg import (
    WorkerCommand,
    enumerate_worker_invocations,
    format_fenced_answer,
)
from content_pipeline.execution.store import ExecutionStore

RUN_ID = "myrun"
UNIT_ID = "myunit"
WORKER_ID = "myworker"
ARGV = ("python", "mount.py", "run")

# Fixed, digit-free, non-filesystem-derived paths for the T3/T4 tests below.
# `enumerate_worker_invocations` is a PURE function -- it never touches the
# filesystem -- so these need not exist. Using literal strings here (rather
# than a pytest `tmp_path`, whose basename embeds the test's own node id,
# e.g. "test_renew_is_never_enumerated0", or a random OS temp-dir suffix
# that can coincidentally contain the digit under test) keeps the
# substring assertions below free of incidental collisions with the test's
# own name or the machine's ambient temp-path numbering.
FAKE_ANSWER_DIR = "/fake/answers"
FAKE_ENVELOPE_DIR = "/fake/envelopes"


def _adapter() -> RunAdapter:
    return RunAdapter(
        build_request=lambda unit: PreparedRequest(unit=unit, system="", user="prepared"),
        parse_fn=lambda t: t,
        apply=lambda uid, payload: None,
        adapter_version="v1",
    )


# -- T2: --text-file= splice -------------------------------------------------


@pytest.fixture
def store(tmp_path) -> ExecutionStore:
    return ExecutionStore(tmp_path / "run.db")


def _seed_claimed_unit(store: ExecutionStore) -> None:
    store.create_run(RUN_ID, driver="claude-bg", backend="mock", model="m", adapter_version="v1")
    store.register_units(RUN_ID, [UNIT_ID])
    store.claim_unit(RUN_ID, UNIT_ID, WORKER_ID)


def _submit_envelope_text(fencing_token: int) -> str:
    import json

    return json.dumps(
        {
            "protocol_version": "1",
            "verb": "submit",
            "payload": {
                "run_id": RUN_ID,
                "unit_id": UNIT_ID,
                "worker_id": WORKER_ID,
                "fencing_token": fencing_token,
            },
        }
    )


def test_text_file_flag_splices_text_before_dispatch(store, tmp_path):
    adapter = _adapter()
    commands = build_commands(store, adapter=adapter)
    _seed_claimed_unit(store)

    envelope_path = tmp_path / "submit.json"
    envelope_path.write_text(_submit_envelope_text(1), encoding="utf-8")
    text_path = tmp_path / "answer.txt"
    # The artifact carries its own fence line (see `format_fenced_answer`);
    # the splice strips it and hands only the answer body to the protocol.
    text_path.write_text(format_fenced_answer(1, "the real answer text"), encoding="utf-8")

    result = commands["protocol"].handler(
        [f"@{envelope_path}", f"--text-file={text_path}"]
    )
    assert result["ok"] is True, result
    assert result["result"]["accepted"] is True

    run = store.get_run(RUN_ID)
    unit = store.get_unit(RUN_ID, UNIT_ID)
    assert unit.accepted_text == "the real answer text"


def test_text_file_flag_without_equals_form_does_not_carry_text(store, tmp_path):
    """`_split_flags` only recognizes the `--key=value` form; a bare
    `--text-file <path>` pair parses as a boolean flag (`text-file: "1"`)
    plus a stray positional envelope-shaped path token -- so the intended
    text never reaches the submission and the call fails (missing/invalid
    envelope), never silently succeeding with empty text."""
    adapter = _adapter()
    commands = build_commands(store, adapter=adapter)
    _seed_claimed_unit(store)

    envelope_path = tmp_path / "submit.json"
    envelope_path.write_text(_submit_envelope_text(1), encoding="utf-8")
    text_path = tmp_path / "answer.txt"
    text_path.write_text("the real answer text", encoding="utf-8")

    # Space-separated, NOT '=' -- the discouraged/broken shape.
    result = commands["protocol"].handler([f"@{envelope_path}", "--text-file", str(text_path)])
    # The stray positional token (str(text_path)) is silently ignored by
    # cli.run's protocol handler (only positional[0] is read), so the
    # envelope itself still dispatches -- but payload["text"] was NEVER
    # populated by the file, because `flags` never got a "text-file" key.
    unit = store.get_unit(RUN_ID, UNIT_ID)
    if result["ok"] is True and result["result"].get("accepted") is True:
        assert unit.accepted_text != "the real answer text"
    else:
        assert unit.state.value != "accepted"


# -- T3: no fencing token in any enumerated invocation; determinism ---------


def test_no_enumerated_invocation_carries_a_fencing_token(store):
    adapter = _adapter()
    build_commands(store, adapter=adapter)  # not used directly; store drives claim below
    store.create_run(RUN_ID, driver="claude-bg", backend="mock", model="m", adapter_version="v1")
    store.register_units(RUN_ID, [UNIT_ID])
    claim_result = store.claim_unit(RUN_ID, UNIT_ID, WORKER_ID)
    fencing_token = claim_result.fencing_token

    wc = WorkerCommand(argv=ARGV, answer_dir=FAKE_ANSWER_DIR, envelope_dir=FAKE_ENVELOPE_DIR)
    invocations = enumerate_worker_invocations(wc, RUN_ID, UNIT_ID, WORKER_ID)

    token_str = str(fencing_token)
    for s in invocations:
        assert token_str not in s, (
            f"invocation {s!r} appears to carry the fencing token {fencing_token!r}, "
            "which cannot be known before `claim` runs (P5 determinism)"
        )
        assert "fencing" not in s.lower()


def test_enumerated_invocations_are_identical_across_different_store_state(tmp_path):
    """Same (worker_command, run_id, unit_id, worker_id) inputs -> the
    IDENTICAL six strings, whether called before or after the store's
    unit has been claimed -- enumerate_worker_invocations reads no store at
    all, by construction (P5: deterministic in those four inputs alone)."""
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(RUN_ID, driver="claude-bg", backend="mock", model="m", adapter_version="v1")
    store.register_units(RUN_ID, [UNIT_ID])

    wc = WorkerCommand(argv=ARGV, answer_dir=FAKE_ANSWER_DIR, envelope_dir=FAKE_ENVELOPE_DIR)

    before = enumerate_worker_invocations(wc, RUN_ID, UNIT_ID, WORKER_ID)
    store.claim_unit(RUN_ID, UNIT_ID, WORKER_ID)
    after = enumerate_worker_invocations(wc, RUN_ID, UNIT_ID, WORKER_ID)

    assert before == after


# -- T4: renew is never enumerated for a worker ------------------------------


def test_renew_is_never_enumerated():
    """D5: the DISPATCHER renews leases (`supervise_tick` calls
    `store.renew_lease` itself); a worker session never runs `renew`."""
    wc = WorkerCommand(argv=ARGV, answer_dir=FAKE_ANSWER_DIR, envelope_dir=FAKE_ENVELOPE_DIR)
    invocations = enumerate_worker_invocations(wc, RUN_ID, UNIT_ID, WORKER_ID)
    assert len(invocations) == 6
    for s in invocations:
        assert "renew" not in s.lower()


def test_claim_is_never_enumerated_for_a_worker():
    """The dispatcher claims each unit before launching its session (see
    ``dispatch_unit``), so a worker has no claim invocation and no claim
    envelope -- which is what stops a session left alive by an earlier
    dispatch from re-claiming a unit that has since been reclaimed."""
    wc = WorkerCommand(argv=ARGV, answer_dir=FAKE_ANSWER_DIR, envelope_dir=FAKE_ENVELOPE_DIR)
    invocations = enumerate_worker_invocations(wc, RUN_ID, UNIT_ID, WORKER_ID)
    for s in invocations:
        assert "claim" not in s.lower()
