"""Two collision regressions between successive dispatches of ONE unit.

Both come from the same root: a unit's worker-facing artifacts are keyed on
``(run_id, unit_id)`` and nothing else, deliberately -- pre-run
enumerability is what makes a permission allowlist possible (P5, see
``execution/drivers/claude_bg.py``'s ``answer_path_for``). So when a
dispatch is settled without its session actually dying (the ``blocked``
branch), and the unit is later reclaimed and re-dispatched, the OLD session
is still alive and still pointed at the SAME paths as the new worker.

1. **Claim collision.** The zombie re-claims the unit under the new
   ``worker_id`` written into the envelope files, the dispatcher renews a
   lease the zombie holds, and the real worker gets ``AlreadyClaimedError``.
   Closed by the dispatcher claiming and the worker having no claim
   invocation at all -- ``test_a_live_prior_session_cannot_reclaim_a_redispatched_unit``.

2. **Answer collision -- silent wrong content, the more serious of the two.**
   The zombie overwrites the shared answer file; the new worker's
   ``--text-file=`` splice puts that text into ITS valid envelope, and
   ``_submit`` fences the envelope's TOKEN, not the PROVENANCE of the text.
   Old text under a current token was accepted. Closed by fencing the
   ARTIFACT's content --
   ``test_a_stale_answer_artifact_cannot_be_submitted_under_a_current_fence``.

Every refusal here is paired with its ACCEPT case: a check that refuses too
much passes every test aimed at what it should refuse.
"""

from __future__ import annotations

import io
import json
import shlex
from pathlib import Path

import pytest
import yaml

from content_pipeline.cli.run import build_commands
from content_pipeline.cli.scaffold import dispatch
from content_pipeline.execution.adapter import PreparedRequest, RunAdapter
from content_pipeline.execution.drivers.claude_bg import (
    ANSWER_FENCE_PREFIX,
    AnswerFenceMismatchError,
    MissingAnswerFenceError,
    WorkerCommand,
    answer_path_for,
    dispatch_unit,
    enumerate_worker_invocations,
    envelope_path_for,
    format_fenced_answer,
    parse_fenced_answer,
    reclaimable_units,
    supervise_tick,
    worker_envelopes_for,
)
from content_pipeline.execution.model import UnitState
from content_pipeline.execution.store import ExecutionStore

from test_execution_driver_claude_bg import FakeRunner, _bg_record, _cli

RUN_ID = "run-1"
UNIT_ID = "u0"
ARGV = ("python", "mount.py", "run")

T_DISPATCH_A = 1000.0
LEASE_A_EXPIRES_AFTER = 1000.0 + 400.0  # > DEFAULT_LEASE_SECONDS (300)
T_DISPATCH_B = 2000.0


def _adapter() -> RunAdapter:
    return RunAdapter(
        build_request=lambda unit: PreparedRequest(
            unit=unit, system="", user=f"prepared:{unit.id}"
        ),
        parse_fn=lambda t: t,
        apply=lambda uid, payload: None,
        adapter_version="v1",
    )


def _mount(tmp_path):
    """A real store + a real ``cli.run.build_commands`` mount over it."""
    store = ExecutionStore(tmp_path / "run.db")
    commands = build_commands(store, adapter=_adapter())
    commands["create-run"].handler([RUN_ID, "claude-bg", "mock", "m"])
    commands["register-units"].handler([RUN_ID, UNIT_ID])
    return store, commands


def _worker_command(tmp_path) -> WorkerCommand:
    answer_dir = tmp_path / "answers"
    envelope_dir = tmp_path / "envelopes"
    answer_dir.mkdir(exist_ok=True)
    envelope_dir.mkdir(exist_ok=True)
    return WorkerCommand(
        argv=ARGV, answer_dir=str(answer_dir), envelope_dir=str(envelope_dir)
    )


def _run_cli(command_str, commands):
    """Run one of the library's OWN enumerated invocation strings through the
    real mount, with the ``argv`` prefix stripped (this test never spawns a
    subprocess)."""
    tokens = shlex.split(command_str)
    assert tuple(tokens[: len(ARGV)]) == ARGV
    out = io.StringIO()
    err = io.StringIO()
    code = dispatch(tokens[len(ARGV) :], commands, out=out, err=err)
    parsed = yaml.safe_load(out.getvalue()) if out.getvalue().strip() else None
    return code, parsed


def _dispatch_cli(short_id, session_id, *, state="working"):
    runner = FakeRunner()
    runner.script(("claude", "--bg"), (f"backgrounded * {short_id}", "", 0))
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id=short_id, session_id=session_id, state=state)]), "", 0),
    )
    runner.script(("claude", "stop"), ("", "", 0))
    runner.script(("claude", "rm"), ("", "", 0))
    return _cli(runner)


def _unit(store):
    return next(u for u in store.list_units(RUN_ID) if u.unit_id == UNIT_ID)


def _write_worker_submit_envelope(wc, worker_id, token):
    """Author the submit envelope exactly as a worker does: the library's own
    template text, with ONLY ``<FENCING_TOKEN>`` substituted."""
    envelopes = worker_envelopes_for(wc, RUN_ID, UNIT_ID, worker_id)
    path, template = envelopes["submit"]
    Path(path).write_text(template.replace("<FENCING_TOKEN>", str(token)), encoding="utf-8")
    return path


# ===========================================================================
# Defect 1 -- the claim collision
# ===========================================================================


def test_a_live_prior_session_cannot_reclaim_a_redispatched_unit(tmp_path):
    """The full trace: dispatch, settle the dispatch on ``blocked`` (which
    does NOT reliably kill the session), let the lease expire, re-dispatch,
    then have the still-live prior session attempt the OLD worker's
    ``submit`` through a REAL ``build_commands`` mount.

    It must fail closed with ``StaleFenceError``, and ``claimed_by`` must be
    the NEW worker throughout -- the zombie must never be able to take the
    claim back.

    MUTATION: restore ``claim`` to ``_ENVELOPE_VERBS`` and pre-write its
    envelope for the zombie's old worker_id. The zombie can then re-claim
    (the reclaim path accepts an expired lease), ``claimed_by`` flips away
    from the new worker, and the ``claimed_by`` assertions below go red.
    """
    store, commands = _mount(tmp_path)
    wc = _worker_command(tmp_path)

    # -- dispatch A ---------------------------------------------------------
    cli_a = _dispatch_cli("aaaaaaaa", "sess-a")
    open_a = dispatch_unit(
        store, RUN_ID, _unit(store), cli_a, wc,
        worker_id="worker-A", sleep_fn=lambda s: None,
        clock_fn=lambda: T_DISPATCH_A, at=T_DISPATCH_A,
    )
    assert store.get_unit(RUN_ID, UNIT_ID).claimed_by == "worker-A"

    # -- A's session goes `blocked`: the dispatch is settled, but nothing
    # here establishes that the session actually died (see the Part C
    # comment in supervise_tick's blocked branch).
    cli_blocked = _dispatch_cli("aaaaaaaa", "sess-a", state="blocked")
    tick = supervise_tick(
        store, RUN_ID, cli_blocked, _adapter(), {UNIT_ID: open_a},
        at=T_DISPATCH_A + 10.0,
    )
    assert tick.settled == {UNIT_ID: "blocked"}

    # -- the lease expires; the unit is reclaimable -------------------------
    assert [u.unit_id for u in reclaimable_units(store, RUN_ID, at=LEASE_A_EXPIRES_AFTER)] == [
        UNIT_ID
    ]

    # -- dispatch B (the reclaim) ------------------------------------------
    cli_b = _dispatch_cli("bbbbbbbb", "sess-b")
    open_b = dispatch_unit(
        store, RUN_ID, _unit(store), cli_b, wc,
        worker_id="worker-B", sleep_fn=lambda s: None,
        clock_fn=lambda: T_DISPATCH_B, at=T_DISPATCH_B,
    )
    assert open_b.fencing_token > open_a.fencing_token
    assert store.get_unit(RUN_ID, UNIT_ID).claimed_by == "worker-B"

    # There is no claim envelope on disk, for EITHER worker id, and no
    # enumerated invocation is a claim -- so the zombie has nothing to run.
    claim_path = envelope_path_for(wc, RUN_ID, UNIT_ID, "claim")
    assert not Path(claim_path).exists()
    for inv in enumerate_worker_invocations(wc, RUN_ID, UNIT_ID, "worker-A"):
        assert "claim" not in inv.lower()

    # ... and the zombie trying it anyway, at the path it WOULD have used,
    # reaches nothing. This is the step that kills the mutation on both
    # halves of this test: with a claim envelope present, the reclaim path
    # accepts the expired lease, the fence bumps, and worker B's own
    # submission below stops being valid.
    _code, result = _run_cli(
        shlex.join(ARGV + ("protocol", f"@{claim_path}")), commands
    )
    assert result["ok"] is False
    assert result["error"]["type"] == "MissingEnvelopeFileError", result

    # -- the zombie submits, under its own (now stale) token ---------------
    zombie_submit = _write_worker_submit_envelope(wc, "worker-A", open_a.fencing_token)
    answer_path = answer_path_for(wc, RUN_ID, UNIT_ID)
    Path(answer_path).write_text(
        format_fenced_answer(open_a.fencing_token, "ZOMBIE ANSWER"), encoding="utf-8"
    )
    zombie_cmd = shlex.join(
        ARGV + ("protocol", f"@{zombie_submit}", f"--text-file={answer_path}")
    )
    _code, result = _run_cli(zombie_cmd, commands)

    assert result["ok"] is False
    assert result["error"]["type"] == "StaleFenceError", result

    # The claim never moved, and no zombie text landed anywhere.
    unit = store.get_unit(RUN_ID, UNIT_ID)
    assert unit.claimed_by == "worker-B"
    assert unit.fencing_token == open_b.fencing_token
    assert unit.state is UnitState.CLAIMED
    assert unit.accepted_text is None

    # -- ACCEPT DIRECTION: worker B, doing exactly the same thing under its
    # own token, still succeeds. Without this, "refuse everything" would pass.
    b_submit = _write_worker_submit_envelope(wc, "worker-B", open_b.fencing_token)
    Path(answer_path).write_text(
        format_fenced_answer(open_b.fencing_token, "REAL ANSWER"), encoding="utf-8"
    )
    b_cmd = shlex.join(ARGV + ("protocol", f"@{b_submit}", f"--text-file={answer_path}"))
    _code, result = _run_cli(b_cmd, commands)
    assert result["ok"] is True, result
    assert result["result"]["accepted"] is True
    assert store.get_unit(RUN_ID, UNIT_ID).accepted_text == "REAL ANSWER"


# ===========================================================================
# Defect 2 -- the answer collision (silent wrong content)
# ===========================================================================


def _two_generations(tmp_path):
    """A unit claimed twice: returns ``(store, commands, wc, token_a,
    token_b)`` with the second claim current."""
    store, commands = _mount(tmp_path)
    wc = _worker_command(tmp_path)
    token_a = store.claim_unit(
        RUN_ID, UNIT_ID, "worker-A", lease_seconds=10.0, at=T_DISPATCH_A
    ).fencing_token
    token_b = store.claim_unit(
        RUN_ID, UNIT_ID, "worker-B", at=T_DISPATCH_B
    ).fencing_token
    assert token_b != token_a
    return store, commands, wc, token_a, token_b


def _submit_result(commands, wc, worker_id, token, answer_path):
    envelope = _write_worker_submit_envelope(wc, worker_id, token)
    cmd = shlex.join(ARGV + ("protocol", f"@{envelope}", f"--text-file={answer_path}"))
    return _run_cli(cmd, commands)[1]


def test_a_stale_answer_artifact_cannot_be_submitted_under_a_current_fence(tmp_path):
    """The defect-2 regression. The answer path is generation-neutral, so a
    still-live prior session can overwrite the file the CURRENT worker is
    about to submit. The current worker's envelope is entirely valid, so
    fencing the envelope alone accepts the zombie's text under the live
    worker's claim -- silent wrong content.

    MUTATION: splice ``--text-file=``'s contents without comparing the
    artifact's declared token to the envelope's (i.e. the pre-fix
    ``cli/run.py``) -- the submission is ACCEPTED and carries the zombie's
    text -> red on both assertions below.
    """
    store, commands, wc, token_a, token_b = _two_generations(tmp_path)
    answer_path = answer_path_for(wc, RUN_ID, UNIT_ID)

    # The zombie overwrites the shared artifact with ITS OWN fenced text.
    Path(answer_path).write_text(
        format_fenced_answer(token_a, "STALE ZOMBIE TEXT"), encoding="utf-8"
    )

    # The CURRENT worker submits, with a perfectly valid current envelope.
    result = _submit_result(commands, wc, "worker-B", token_b, answer_path)

    assert result["ok"] is False, result
    assert result["error"]["type"] == "AnswerFenceMismatchError", result

    unit = store.get_unit(RUN_ID, UNIT_ID)
    assert unit.state is not UnitState.ACCEPTED
    assert unit.accepted_text is None
    for u in store.list_units(RUN_ID):
        assert u.accepted_text != "STALE ZOMBIE TEXT"


def test_the_current_workers_own_artifact_still_submits_fine(tmp_path):
    """The ACCEPT case the refusal above is worthless without: the same
    worker, the same path, the same invocation -- with the artifact it
    actually wrote itself."""
    store, commands, wc, _token_a, token_b = _two_generations(tmp_path)
    answer_path = answer_path_for(wc, RUN_ID, UNIT_ID)

    Path(answer_path).write_text(
        format_fenced_answer(token_b, "the real answer"), encoding="utf-8"
    )
    result = _submit_result(commands, wc, "worker-B", token_b, answer_path)

    assert result["ok"] is True, result
    assert result["result"]["accepted"] is True
    assert store.get_unit(RUN_ID, UNIT_ID).accepted_text == "the real answer"


def test_a_current_artifact_under_a_stale_envelope_is_refused_on_the_token(tmp_path):
    """The mirror direction. The artifact is current, the ENVELOPE is stale.
    ``StaleFenceError`` would catch this eventually, but the mismatch is
    detected first and named for what it is -- the text and the standing to
    submit it came from different generations."""
    store, commands, wc, token_a, token_b = _two_generations(tmp_path)
    answer_path = answer_path_for(wc, RUN_ID, UNIT_ID)

    Path(answer_path).write_text(
        format_fenced_answer(token_b, "current text"), encoding="utf-8"
    )
    result = _submit_result(commands, wc, "worker-A", token_a, answer_path)

    assert result["ok"] is False, result
    assert result["error"]["type"] == "AnswerFenceMismatchError", result
    assert store.get_unit(RUN_ID, UNIT_ID).accepted_text is None


def test_an_answer_artifact_with_no_fence_line_at_all_is_refused(tmp_path):
    """A MISSING declaration is refused, never read as unfenced-and-fine: an
    artifact with no declared generation is exactly what a prior session
    leaves behind."""
    store, commands, wc, _token_a, token_b = _two_generations(tmp_path)
    answer_path = answer_path_for(wc, RUN_ID, UNIT_ID)

    Path(answer_path).write_text("just some answer text\nsecond line\n", encoding="utf-8")
    result = _submit_result(commands, wc, "worker-B", token_b, answer_path)

    assert result["ok"] is False, result
    assert result["error"]["type"] == "MissingAnswerFenceError", result
    assert store.get_unit(RUN_ID, UNIT_ID).accepted_text is None


def test_answer_text_containing_the_fence_prefix_is_submitted_untouched(tmp_path):
    """ACCEPT case, and the encoding's own hard requirement: only the FIRST
    line is a declaration, so a body that itself contains the prefix -- even
    with a different token -- is ordinary text and reaches the store
    byte-for-byte."""
    store, commands, wc, token_a, token_b = _two_generations(tmp_path)
    answer_path = answer_path_for(wc, RUN_ID, UNIT_ID)

    body = (
        "Here is my answer.\n"
        f"{ANSWER_FENCE_PREFIX} {token_a}\n"
        "...and that line above was part of the answer, deliberately.\n"
    )
    Path(answer_path).write_text(format_fenced_answer(token_b, body), encoding="utf-8")
    result = _submit_result(commands, wc, "worker-B", token_b, answer_path)

    assert result["ok"] is True, result
    assert store.get_unit(RUN_ID, UNIT_ID).accepted_text == body


def test_an_envelope_with_no_fencing_token_cannot_splice_a_text_file(tmp_path):
    """``--text-file=`` has nothing to match the artifact against without a
    token in the envelope, so it refuses rather than splicing blind."""
    store, commands, wc, _token_a, token_b = _two_generations(tmp_path)
    answer_path = answer_path_for(wc, RUN_ID, UNIT_ID)
    Path(answer_path).write_text(format_fenced_answer(token_b, "text"), encoding="utf-8")

    envelope_path = Path(envelope_path_for(wc, RUN_ID, UNIT_ID, "submit"))
    envelope_path.write_text(
        json.dumps(
            {
                "protocol_version": "1",
                "verb": "submit",
                "payload": {"run_id": RUN_ID, "unit_id": UNIT_ID, "worker_id": "worker-B"},
            }
        ),
        encoding="utf-8",
    )
    cmd = shlex.join(
        ARGV + ("protocol", f"@{envelope_path}", f"--text-file={answer_path}")
    )
    result = _run_cli(cmd, commands)[1]

    assert result["ok"] is False, result
    assert result["error"]["type"] == "MissingAnswerFenceError", result


# ===========================================================================
# The fence encoding itself
# ===========================================================================


def test_format_and_parse_round_trip_preserves_the_body_exactly():
    for body in (
        "",
        "one line",
        "trailing newline\n",
        "windows\r\nline\r\nendings\r\n",
        f"{ANSWER_FENCE_PREFIX} 99\nlooks like a fence but is not\n",
        "unicode: zh-Hans text, em-dash --, tab\there\n",
    ):
        raw = format_fenced_answer(7, body)
        assert parse_fenced_answer(raw, 7) == body


def test_parse_refuses_a_missing_or_unparsable_declaration():
    for raw in ("", "no fence here", "content-pipeline-fence: not-a-number\nbody"):
        with pytest.raises(MissingAnswerFenceError):
            parse_fenced_answer(raw, 7)


def test_parse_refuses_a_different_token():
    with pytest.raises(AnswerFenceMismatchError) as excinfo:
        parse_fenced_answer(format_fenced_answer(6, "body"), 7)
    assert excinfo.value.declared == 6
    assert excinfo.value.expected == 7


def test_parse_tolerates_spacing_and_crlf_in_the_declaration():
    """ACCEPT cases for the declaration itself -- a worker that writes no
    space, extra spaces, or CRLF line endings is not refused for cosmetics."""
    for declaration in (
        f"{ANSWER_FENCE_PREFIX}7",
        f"{ANSWER_FENCE_PREFIX}   7   ",
        f"  {ANSWER_FENCE_PREFIX} 7\r",
    ):
        assert parse_fenced_answer(f"{declaration}\nbody", 7) == "body"
