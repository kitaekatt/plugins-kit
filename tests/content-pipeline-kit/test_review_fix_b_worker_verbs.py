"""A worker-enumerated envelope file can only carry ITS verb and ITS ids.

A worker authors the JSON body of the envelope file that an allowlisted
``protocol @<path>`` invocation names. The file name is fixed by the
dispatcher (``<run>__<unit>.<verb>.json``, claim files additionally carry the
worker id), so the mount checks the body against the name: the body's verb
must equal the verb in the name and its run/unit(/worker) ids must produce
the same file name. Files with any other name (orchestrator use, stdin,
literal argv) are not affected.
"""

from __future__ import annotations

import json

import pytest

from content_pipeline.cli.run import build_commands
from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.workerpack import (
    WorkerCommand,
    claim_envelope_path_for,
    envelope_path_for,
    worker_envelopes_for,
)

RUN = "r"


@pytest.fixture
def env(tmp_path):
    applied = []
    adapter = RunAdapter(
        user_for=lambda u: f"user:{u.id}",
        parse_fn=lambda t: t,
        apply=lambda uid, payload: applied.append(uid),
    )
    store = ExecutionStore(tmp_path / "r.db")
    store.create_run(RUN, driver="claude-bg", backend="b", model="m", adapter_version="")
    store.register_units(RUN, ["A", "B"])
    wc = WorkerCommand(argv=("python", "mount.py"), answer_dir=str(tmp_path))
    commands = build_commands(store, adapter=adapter)
    return store, commands, wc, applied


def _write(path, verb, payload):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"protocol_version": "2", "verb": verb, "payload": payload}))


def test_worker_submit_file_cannot_carry_finalize(env):
    store, commands, wc, applied = env
    tok = store.claim_unit(RUN, "A", "w0").fencing_token
    store.accept_unit(RUN, "A", tok, text="a")
    store.set_halt(RUN, "rate_limit", "429")
    path, _ = worker_envelopes_for(wc, RUN, "B", "wB")["submit"]
    for verb in ("resume", "finalize", "pause", "prepare"):
        _write(path, verb, {"run_id": RUN})
        result = commands["protocol"].handler(["@" + path])
        assert result["ok"] is False, verb
        assert result["error"]["type"] == "EnvelopeIdentityError"
    assert applied == []
    assert store.get_run(RUN).halted_kind == "rate_limit"


def test_worker_file_cannot_target_another_unit(env):
    store, commands, wc, _ = env
    tok_a = store.claim_unit(RUN, "A", "w0").fencing_token
    store.claim_unit(RUN, "B", "wB")
    path, _ = worker_envelopes_for(wc, RUN, "B", "wB")["fail"]
    _write(
        path,
        "fail",
        {"run_id": RUN, "unit_id": "A", "worker_id": "w0", "fencing_token": tok_a, "terminal": True, "error": "x"},
    )
    result = commands["protocol"].handler(["@" + path])
    assert result["ok"] is False
    assert result["error"]["type"] == "EnvelopeIdentityError"
    assert store.get_unit(RUN, "A").state.value == "claimed"


def test_claim_file_is_bound_to_its_worker_id(env):
    _, commands, wc, _ = env
    path = claim_envelope_path_for(wc, RUN, "A", "wA")
    _write(path, "claim", {"run_id": RUN, "unit_id": "A", "worker_id": "someone-else"})
    result = commands["protocol"].handler(["@" + path])
    assert result["ok"] is False
    assert result["error"]["type"] == "EnvelopeIdentityError"


# -- accept cases: everything the dispatcher enumerates still works -----------


def test_enumerated_claim_read_submit_fail_still_dispatch(env):
    store, commands, wc, _ = env
    claim_path = claim_envelope_path_for(wc, RUN, "A", "wA")
    _write(claim_path, "claim", {"run_id": RUN, "unit_id": "A", "worker_id": "wA"})
    claimed = commands["protocol"].handler(["@" + claim_path])
    assert claimed["ok"] is True
    tok = claimed["result"]["fencing_token"]

    read_path, _ = worker_envelopes_for(wc, RUN, "A", "wA")["read"]
    _write(read_path, "read", {"run_id": RUN, "unit_id": "A", "worker_id": "wA"})
    assert commands["protocol"].handler(["@" + read_path])["ok"] is True

    fail_path, _ = worker_envelopes_for(wc, RUN, "A", "wA")["fail"]
    _write(
        fail_path,
        "fail",
        {"run_id": RUN, "unit_id": "A", "worker_id": "wA", "fencing_token": tok, "terminal": False, "error": "e"},
    )
    assert commands["protocol"].handler(["@" + fail_path])["ok"] is True

    tok2 = store.claim_unit(RUN, "A", "wA").fencing_token
    sub_path, _ = worker_envelopes_for(wc, RUN, "A", "wA")["submit"]
    txt = sub_path + ".answer.txt"
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write(f"content-pipeline-fence: {tok2}\nanswer")
    _write(sub_path, "submit", {"run_id": RUN, "unit_id": "A", "worker_id": "wA", "fencing_token": tok2})
    assert commands["protocol"].handler(["@" + sub_path, f"--text-file={txt}"])["ok"] is True


def test_sanitized_ids_still_match(env, tmp_path):
    store, commands, wc, _ = env
    store.register_units(RUN, ["u/1 x"])
    path = envelope_path_for(wc, RUN, "u/1 x", "read")
    _write(path, "read", {"run_id": RUN, "unit_id": "u/1 x", "worker_id": "w"})
    assert commands["protocol"].handler(["@" + path])["ok"] is True


def test_other_file_names_and_stdin_are_unrestricted(env, tmp_path):
    _, commands, _, _ = env
    other = tmp_path / "orchestrator.json"
    _write(str(other), "status", {"run_id": RUN})
    assert commands["protocol"].handler([f"@{other}"])["ok"] is True
    literal = json.dumps({"protocol_version": "2", "verb": "resume", "payload": {"run_id": RUN}})
    assert commands["protocol"].handler([literal])["ok"] is True


@pytest.mark.parametrize(
    "name,verb,payload",
    [
        ("orchestrator.claim.json", "claim", {"run_id": RUN, "unit_id": "A", "worker_id": "w"}),
        ("orchestrator.claim.json", "prepare", {"run_id": RUN}),
        ("x.submit.json", "prepare", {"run_id": RUN}),
        ("x.submit.json", "claim", {"run_id": RUN, "unit_id": "A", "worker_id": "w"}),
    ],
)
def test_non_worker_shaped_names_with_verb_suffix_are_unchecked(env, tmp_path, name, verb, payload):
    _, commands, _, _ = env
    path = tmp_path / name
    _write(str(path), verb, payload)
    result = commands["protocol"].handler([f"@{path}"])
    assert result.get("error", {}).get("type") != "EnvelopeIdentityError"


def test_enumerated_paths_with_underscored_ids_are_still_checked(env):
    _, commands, wc, _ = env
    for unit in ("a_b", "a__b", "_a", "a_"):
        path = envelope_path_for(wc, "r__1", unit, "submit")
        _write(path, "finalize", {"run_id": "r__1", "unit_id": unit})
        result = commands["protocol"].handler(["@" + path])
        assert result["error"]["type"] == "EnvelopeIdentityError", unit
        cpath = claim_envelope_path_for(wc, "r_", unit, "w__")
        _write(cpath, "resume", {"run_id": "r_", "unit_id": unit, "worker_id": "w__"})
        assert commands["protocol"].handler(["@" + cpath])["error"]["type"] == "EnvelopeIdentityError"
