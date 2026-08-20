"""T1 -- the loop-closer for the B2 worker-invocation-shape fix.

Proves the FULL worker round trip actually works end to end through the real
seams a worker session uses: a real :class:`ExecutionStore`, a real
``cli.run.build_commands`` mount, and
``execution.drivers.claude_bg.enumerate_worker_invocations``'s own returned
command/Write-tool-target strings, fed to ``cli.scaffold.dispatch`` exactly
as a worker session would run them (module-level ``argv`` prefix stripped,
since this test never spawns a real subprocess).

Three shapes shipped in one prior commit and could not interoperate:
``enumerate_worker_invocations`` emitted a FLAG form
(``claim --run-id R --unit-id U --worker-id W``), the ``execute-work-unit``
skill documented that flag form, and ``cli.run.build_commands`` implemented
neither ``read`` nor ``submit`` as commands at all (only ``protocol`` is
registered) -- so a real worker's first verb succeeded BY ACCIDENT while
``read``/``submit`` failed as unknown commands, silently stranding the unit
CLAIMED forever. This test is what proves the fix actually closes that loop:
every verb succeeds, ``read`` returns real adapter content, and the unit
reaches ``ACCEPTED`` in the STORE -- not just that each call returns
``ok: true``.

The round trip now starts at ``read``, because the DISPATCHER claims. That
claim is made here through the same real mount, via its ``claim`` command,
before any worker invocation runs -- the worker's own set carries no claim
at all (``enumerate_worker_invocations`` returns six strings, none of them a
claim). The answer artifact carries its fencing token on its FIRST LINE
(``claude_bg.format_fenced_answer``), which ``--text-file=`` matches against
the submit envelope before splicing.

See the module docstring of ``execution/drivers/claude_bg.py`` (worker
verbs: ``<argv> protocol @<envelope path>``, optionally
``--text-file=<answer path>`` for ``submit``) and
``execution/drivers/claude_bg.py::worker_envelopes_for`` (which envelope
text a caller must pre-write vs. author itself at runtime).
"""

from __future__ import annotations

import io
import shlex
from pathlib import Path

import pytest
import yaml

from content_pipeline.cli.run import build_commands
from content_pipeline.cli.scaffold import dispatch
from content_pipeline.execution.adapter import PreparedRequest, RunAdapter
from content_pipeline.execution.drivers.claude_bg import (
    WorkerCommand,
    enumerate_worker_invocations,
    format_fenced_answer,
    worker_envelopes_for,
)
from content_pipeline.execution.model import UnitState
from content_pipeline.execution.store import ExecutionStore

RUN_ID = "run-1"
UNIT_ID = "unit-1"
WORKER_ID = "worker-1"
ARGV = ("python", "mount.py", "run")


def _adapter() -> RunAdapter:
    """A fake adapter: builds a deterministic prepared request naming the
    unit id (so `read`'s reply is checkable), accepts any submitted text
    (identity `parse_fn`, no validators)."""
    return RunAdapter(
        build_request=lambda unit: PreparedRequest(
            unit=unit, system="", user=f"prepared:{unit.id}"
        ),
        parse_fn=lambda t: t,
        apply=lambda uid, payload: None,
        adapter_version="v1",
    )


def _run_cli(tail, commands):
    """`cli.scaffold.dispatch` over one already-tail-stripped argv list,
    returning `(exit_code, parsed_yaml_or_None, stderr_text)`."""
    out = io.StringIO()
    err = io.StringIO()
    code = dispatch(tail, commands, out=out, err=err)
    parsed = yaml.safe_load(out.getvalue()) if out.getvalue().strip() else None
    return code, parsed, err.getvalue()


def _tail(command_str: str) -> list:
    """Strip the mount's own `ARGV` prefix off a shlex-split invocation
    string, leaving exactly what `cli.scaffold.dispatch` expects -- the
    tokens a worker's shell would pass to the mount after the fixed
    `python mount.py run` prefix."""
    tokens = shlex.split(command_str)
    assert tuple(tokens[: len(ARGV)]) == ARGV, (
        f"invocation {command_str!r} does not start with the WorkerCommand "
        f"argv prefix {ARGV!r}"
    )
    return tokens[len(ARGV) :]


def _dispatcher_claim(commands, *, skip: bool = False):
    """The DISPATCHER's claim, made through the same real mount the worker
    will use, before the worker's first invocation. Returns the fencing
    token, or ``None`` when ``skip`` (the mutation this test is built to
    catch: with no dispatcher claim, nothing downstream can produce an
    accepted unit)."""
    if skip:
        return None
    result = commands["claim"].handler([RUN_ID, UNIT_ID, WORKER_ID])
    return result["fencing_token"]


def _roundtrip(tmp_path, *, skip_dispatcher_claim: bool = False):
    """Drive the whole round trip once and return
    ``(store, read_user, invocations)``.

    Every step is deliberately BEST-EFFORT (no hard mid-flow assertion on
    shape or exit code) -- the load-bearing assertion belongs to the caller
    and is on the STORE's own final state. That is what the mutations this
    test exists to catch actually break; an early "this call returned ok"
    assertion would fire on an unrelated shape mismatch and mask the real
    defect.
    """
    store = ExecutionStore(tmp_path / "run.db")
    adapter = _adapter()
    commands = build_commands(store, adapter=adapter)

    create_result = commands["create-run"].handler([RUN_ID, "claude-bg", "mock", "m"])
    assert create_result["adapter_version"] == "v1"
    commands["register-units"].handler([RUN_ID, UNIT_ID])

    answer_dir = tmp_path / "answers"
    envelope_dir = tmp_path / "envelopes"
    answer_dir.mkdir()
    envelope_dir.mkdir()
    wc = WorkerCommand(argv=ARGV, answer_dir=str(answer_dir), envelope_dir=str(envelope_dir))

    # The dispatcher claims BEFORE the worker's first invocation.
    fencing_token = _dispatcher_claim(commands, skip=skip_dispatcher_claim)

    invocations = enumerate_worker_invocations(wc, RUN_ID, UNIT_ID, WORKER_ID)
    assert len(invocations) == 6
    (
        read_cmd,
        submit_cmd,
        fail_cmd,
        write_answer,
        write_submit,
        write_fail,
    ) = invocations

    # No invocation/Write-tool-target string carries unit content, and none
    # is a claim -- every one of them is computable before the worker ever
    # runs (P5). The no-fencing-token half of P5 is pinned in
    # `test_worker_invocation_contract.py`, whose ids and paths are
    # deliberately digit-free; this module's `run-1`/`unit-1` ids would
    # collide with a small token by coincidence.
    for s in invocations:
        assert "prepared:" not in s
        assert "my answer text" not in s
        assert "claim" not in s.lower()

    # The dispatcher pre-writes `read` (build_launch_prompt's job in
    # production); this test drives enumerate_worker_invocations directly,
    # so it does that pre-write itself using the library's own generated
    # text -- never hand-composed.
    envelopes = worker_envelopes_for(wc, RUN_ID, UNIT_ID, WORKER_ID)
    read_path, read_text = envelopes["read"]
    Path(read_path).write_text(read_text, encoding="utf-8")

    # 1. Read -- this is the ONLY step that returns unit content.
    code, result, err = _run_cli(_tail(read_cmd), commands)
    read_user = None
    if code == 0 and isinstance(result, dict) and result.get("ok") is True:
        read_user = result.get("result", {}).get("user")

    # 2. Write the answer, fence line first (Write-tool target, not a
    # subprocess call).
    assert write_answer.startswith("Write tool -> ")
    answer_path = write_answer[len("Write tool -> ") :]
    if fencing_token is not None:
        Path(answer_path).write_text(
            format_fenced_answer(fencing_token, "my answer text"), encoding="utf-8"
        )

        # 3. Write the submit envelope, substituting ONLY the fencing token
        # the launch prompt named -- everything else verbatim from the
        # library's own template.
        assert write_submit.startswith("Write tool -> ")
        submit_path = write_submit[len("Write tool -> ") :]
        submit_template = envelopes["submit"][1]
        Path(submit_path).write_text(
            submit_template.replace("<FENCING_TOKEN>", str(fencing_token)), encoding="utf-8"
        )

        # 4. Submit.
        _run_cli(_tail(submit_cmd), commands)

    return store, read_user, invocations


def test_worker_invocation_roundtrip(tmp_path):
    store, read_user, _invocations = _roundtrip(tmp_path)

    # The load-bearing assertions: the unit reached ACCEPTED in the STORE,
    # carrying exactly the answer BODY (the fence line stripped, nothing
    # else touched), and `read` actually returned this adapter's real
    # prepared content.
    unit = store.get_unit(RUN_ID, UNIT_ID)
    assert unit is not None
    assert unit.state is UnitState.ACCEPTED, (
        f"unit never reached ACCEPTED (ended {unit.state!r}) -- claimed_by="
        f"{unit.claimed_by!r}, fencing_token={unit.fencing_token!r}"
    )
    assert unit.accepted_text == "my answer text"
    assert read_user == f"prepared:{UNIT_ID}"


def test_roundtrip_without_the_dispatcher_claim_never_reaches_accepted(tmp_path):
    """MUTATION, run as a test: skip the dispatcher-side claim and the round
    trip cannot complete -- there is no fencing token for the worker to
    substitute, and the unit never leaves PENDING. This is what makes the
    happy-path test above test anything: `read` still succeeds without a
    claim, so only the final store state distinguishes the two runs."""
    store, read_user, _invocations = _roundtrip(tmp_path, skip_dispatcher_claim=True)

    unit = store.get_unit(RUN_ID, UNIT_ID)
    assert unit.state is UnitState.PENDING
    assert unit.accepted_text is None
    # `read` is unaffected by the missing claim -- it needs no fence.
    assert read_user == f"prepared:{UNIT_ID}"


def test_fail_invocation_and_envelope_also_work(tmp_path):
    """The `fail` verb goes through the identical shape (`protocol
    @<path>`) as `read`/`submit` -- exercised separately from the happy path
    above so a defect specific to `fail`'s own envelope template (e.g. a
    stray field) cannot hide behind the accept-path test."""
    store = ExecutionStore(tmp_path / "run.db")
    adapter = _adapter()
    commands = build_commands(store, adapter=adapter)
    commands["create-run"].handler([RUN_ID, "claude-bg", "mock", "m"])
    commands["register-units"].handler([RUN_ID, UNIT_ID])

    answer_dir = tmp_path / "answers"
    envelope_dir = tmp_path / "envelopes"
    answer_dir.mkdir()
    envelope_dir.mkdir()
    wc = WorkerCommand(argv=ARGV, answer_dir=str(answer_dir), envelope_dir=str(envelope_dir))

    fencing_token = _dispatcher_claim(commands)

    invocations = enumerate_worker_invocations(wc, RUN_ID, UNIT_ID, WORKER_ID)
    _read_cmd, _submit_cmd, fail_cmd, _wa, _ws, write_fail = invocations

    envelopes = worker_envelopes_for(wc, RUN_ID, UNIT_ID, WORKER_ID)
    read_path, read_text = envelopes["read"]
    Path(read_path).write_text(read_text, encoding="utf-8")

    fail_path = write_fail[len("Write tool -> ") :]
    fail_template = envelopes["fail"][1]
    Path(fail_path).write_text(
        fail_template.replace("<FENCING_TOKEN>", str(fencing_token)), encoding="utf-8"
    )

    code, result, err = _run_cli(_tail(fail_cmd), commands)
    assert code == 0, f"fail failed: {err}"
    assert result["ok"] is True
    assert result["result"]["state"] == "pending"
