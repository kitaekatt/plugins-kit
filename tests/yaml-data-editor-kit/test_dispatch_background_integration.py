"""Real-process verification for the claude_bg dispatch lane's worker mount.

Every other test in this suite (``test_dispatch_background.py``,
``test_dispatch_worker_mount.py``) drives ``worker_mount``'s protocol
handlers IN-PROCESS, and content-pipeline-kit's own driver tests
(``tests/content-pipeline-kit/test_execution_driver_claude_bg.py``) patch
``ClaudeCli.runner`` to a scripted fake and explicitly assert that no test
in that suite reaches a real subprocess. So nothing anywhere exercises the
one thing that actually matters for the ``claude_bg`` lane: that
``build_worker_command``'s argv, when executed as a REAL child process
(``sys.executable -m yaml_data_editor_kit.dispatch.worker_mount <plan>
protocol @<envelope>``), round-trips a real answer through the real
protocol mount to a real sqlite execution store on disk.

REALISM TIER (be precise about this, it is the point of the file):

  * REAL: a genuine subprocess is spawned for each of the worker's three
    verbs (``read``, ``submit``, ``fail``); no ``ClaudeCli.runner`` is
    faked because none of this file goes anywhere near ``ClaudeCli`` --
    ``worker_mount`` has no dependency on the background-session driver at
    all. Real files (corpus, comment store, dispatch plan, envelope files,
    answer file, sqlite execution store) live under ``tmp_path`` and are
    read back after each subprocess exits to confirm the write actually
    happened in that process, not merely that it exited 0.
  * NOT REAL, and this is the load-bearing gap this file cannot close: no
    ``claude --bg`` session is ever launched. A real ``claude`` binary is
    on PATH in this environment, but spawning one from an automated test
    would start a real, billed, interactive-adjacent background agent
    session as a side effect of a pytest run -- which is also nested
    inside the very Claude Code session running this task, a shape
    ``claude_bg.py``'s own module docstring and the CLAUDECODE nested-
    session guard treat as unsupported. That is reported as a FINDING
    below, not quietly worked around. What IS real here is everything
    ``claude --bg`` would hand off to: the worker's own process boundary,
    argv, and protocol round-trip.

The claim step is done by calling ``ExecutionStore.claim_unit`` directly
(real code, no fake) rather than through a subprocess, because that mirrors
production: ``claude_bg.dispatch_unit`` claims a unit itself, in the
dispatcher's own process, BEFORE ever launching a worker session (see that
module's docstring, "the DISPATCHER claims"). A worker session -- real or,
here, simulated -- has no claim envelope and never calls ``claim``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

from content_pipeline.execution.drivers.claude_bg import build_launch_prompt
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.workerpack import (
    answer_path_for,
    enumerate_worker_invocations,
    format_fenced_answer,
    worker_envelopes_for,
)
from content_pipeline.freshness.hashing import content_hash

from yaml_data_editor_kit.comments.store import CommentStore
from yaml_data_editor_kit.dispatch.state import write_plan
from yaml_data_editor_kit.dispatch.worker_mount import build_worker_command

PLUGIN_LIB = Path(__file__).resolve().parents[2] / "plugins" / "yaml-data-editor-kit" / "lib"
CPK_LIB = Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit" / "lib"

_SUBPROCESS_TIMEOUT_S = 30.0


def _subprocess_env() -> dict[str, str]:
    """The real ``worker_mount`` subprocess needs both plugin ``lib/`` dirs
    on its import path -- pytest's own ``sys.path`` insertion (conftest.py)
    is in-process only and a child process starts with none of it."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    parts = [str(PLUGIN_LIB), str(CPK_LIB)]
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _run_invocation(command: str, *, cwd: Path) -> dict:
    """Run one of ``enumerate_worker_invocations``'s command strings as a
    genuine child process and return the parsed protocol-envelope result.

    Uses ``shlex.split`` (POSIX), matching how ``enumerate_worker_invocations``
    itself builds the string with ``shlex.join`` -- this is not a shell
    invocation, it is argv reconstruction, so paths with spaces still round-
    trip correctly on the platforms this suite runs on.
    """
    argv = shlex.split(command)
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )
    assert proc.returncode == 0, (
        f"subprocess exited {proc.returncode}\nargv: {argv}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert proc.stdout.strip(), f"subprocess produced no output\nargv: {argv}\nstderr: {proc.stderr}"
    return yaml.safe_load(proc.stdout)


def _build_corpus(tmp_path: Path, write) -> tuple[Path, dict]:
    write(
        "profile/catalogue.yaml",
        """
dialect: type/1
id: product
identified_by: id
fields:
  id: {type: id}
  summary: {type: text}
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
""",
    )
    write("content/products.yaml", "- {id: bolt, summary: fastener}\n")
    comments_path = tmp_path / "comments"
    CommentStore.init(comments_path)
    target = {
        "id": "record:product/bolt",
        "anchor": "product/bolt",
        "anchored_slice": {"id": "bolt", "summary": "fastener"},
        "content_hash": content_hash({"id": "bolt", "summary": "fastener"}),
        "comment_anchors": [],
        "comment_guards": [],
        "comment_ids": ["summary-note"],
        "comments": ["Make it precise."],
    }
    return comments_path, target


def test_worker_mount_round_trips_a_real_answer_through_a_real_subprocess(
    tmp_path: Path, write
) -> None:
    """TIER: real subprocess for read + submit; real sqlite store on disk.

    Not exercised: a real ``claude --bg`` launch (see module docstring).
    """
    comments_path, target = _build_corpus(tmp_path, write)
    run_id = "run-1"
    unit_id = "unit-1"
    worker_id = "worker-1"

    plan_path = tmp_path / "dispatch-plan.yaml"
    plan = write_plan(
        plan_path,
        run_id=run_id,
        corpus_path=tmp_path,
        comment_store_path=comments_path,
        units=[{"id": unit_id, "payload": target}],
    )

    execution_path = tmp_path / "execution.sqlite3"
    execution = ExecutionStore(execution_path)
    execution.create_run(
        run_id, driver="claude_bg", backend="mock", model="",
        adapter_version=plan.adapter_version,
    )
    execution.register_units(run_id, [unit_id])

    # The DISPATCHER's own claim, in-process -- real code
    # (ExecutionStore.claim_unit), never a subprocess: this mirrors
    # claude_bg.dispatch_unit, which claims before ever launching a worker.
    claim = execution.claim_unit(run_id, unit_id, worker_id)
    token = claim.fencing_token

    worker_dir = tmp_path / "workers"
    command = build_worker_command(plan_path, worker_dir)

    # DEFECT CHECK (report item 2): build_launch_prompt is claude_bg.py's
    # OWN function for turning a WorkerCommand into the six invocation
    # strings a real background session would run. It is built on the same
    # enumerate_worker_invocations/worker_envelopes_for this test uses
    # directly below -- so if those two ever diverge, this assertion is
    # what catches it, rather than trusting "same function, must agree".
    prompt = build_launch_prompt(command, run_id, unit_id, worker_id, token)

    envelopes = worker_envelopes_for(command, run_id, unit_id, worker_id)
    envelope_dir = Path(command.resolved_envelope_dir)
    envelope_dir.mkdir(parents=True, exist_ok=True)

    # `read`'s envelope is pre-written by the dispatcher before launch, per
    # build_launch_prompt's own docstring -- do the same here rather than
    # letting build_launch_prompt's side effect do it, so this test does not
    # depend on an implementation detail of a function it is also using as
    # an oracle above.
    read_path, read_text = envelopes["read"]
    Path(read_path).write_text(read_text, encoding="utf-8")

    read_cmd, submit_cmd, fail_cmd, write_answer_cmd, write_submit_cmd, _write_fail_cmd = (
        enumerate_worker_invocations(command, run_id, unit_id, worker_id)
    )
    assert read_cmd in prompt
    assert submit_cmd in prompt
    assert fail_cmd in prompt

    # 1. READ, as a real child process.
    read_result = _run_invocation(read_cmd, cwd=tmp_path)
    assert read_result["ok"] is True, read_result
    assert read_result["result"]["unit_id"] == unit_id
    assert "Make it precise." in read_result["result"]["user"]

    # 2. Write the answer artifact (the worker's Write-tool step), fenced
    # with the REAL token this claim returned.
    answer_path = Path(answer_path_for(command, run_id, unit_id))
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    answer_path.write_text(format_fenced_answer(token, "updated fastener"), encoding="utf-8")
    assert write_answer_cmd == f"Write tool -> {answer_path}"

    # 3. Author the submit envelope, substituting only <FENCING_TOKEN>
    # (the one edit a worker is ever permitted to make to this template).
    submit_template_path, submit_template_text = envelopes["submit"]
    assert write_submit_cmd == f"Write tool -> {submit_template_path}"
    Path(submit_template_path).write_text(
        submit_template_text.replace("<FENCING_TOKEN>", str(token)), encoding="utf-8"
    )

    # 4. SUBMIT, as a real child process -- real fence check, real sqlite
    # write, all inside the subprocess.
    submit_result = _run_invocation(submit_cmd, cwd=tmp_path)
    assert submit_result["ok"] is True, submit_result
    assert submit_result["result"]["accepted"] is True

    # Reopen the store fresh (a NEW ExecutionStore instance over the same
    # file) so this assertion reads what the subprocess actually persisted
    # to disk, not anything cached in this process's `execution` handle.
    reopened = ExecutionStore(execution_path)
    unit = reopened.get_unit(run_id, unit_id)
    assert unit.state.value == "accepted"
    assert unit.accepted_text == "updated fastener"


def test_worker_mount_fail_path_through_a_real_subprocess(tmp_path: Path, write) -> None:
    """TIER: same as above, for the `fail` verb -- a worker that cannot
    complete its unit. Real subprocess, real sqlite write."""
    comments_path, target = _build_corpus(tmp_path, write)
    run_id = "run-1"
    unit_id = "unit-1"
    worker_id = "worker-1"

    plan_path = tmp_path / "dispatch-plan.yaml"
    plan = write_plan(
        plan_path,
        run_id=run_id,
        corpus_path=tmp_path,
        comment_store_path=comments_path,
        units=[{"id": unit_id, "payload": target}],
    )
    execution_path = tmp_path / "execution.sqlite3"
    execution = ExecutionStore(execution_path)
    execution.create_run(
        run_id, driver="claude_bg", backend="mock", model="",
        adapter_version=plan.adapter_version,
    )
    execution.register_units(run_id, [unit_id])
    claim = execution.claim_unit(run_id, unit_id, worker_id)
    token = claim.fencing_token

    command = build_worker_command(plan_path, tmp_path / "workers")
    envelopes = worker_envelopes_for(command, run_id, unit_id, worker_id)
    envelope_dir = Path(command.resolved_envelope_dir)
    envelope_dir.mkdir(parents=True, exist_ok=True)

    fail_template_path, fail_template_text = envelopes["fail"]
    Path(fail_template_path).write_text(
        fail_template_text
        .replace("<FENCING_TOKEN>", str(token))
        .replace("<FAILURE_DETAIL_JSON>", '"could not complete unit"'),
        encoding="utf-8",
    )

    _read_cmd, _submit_cmd, fail_cmd, _wa, _ws, write_fail_cmd = enumerate_worker_invocations(
        command, run_id, unit_id, worker_id
    )
    assert write_fail_cmd == f"Write tool -> {fail_template_path}"

    fail_result = _run_invocation(fail_cmd, cwd=tmp_path)
    assert fail_result["ok"] is True, fail_result

    reopened = ExecutionStore(execution_path)
    unit = reopened.get_unit(run_id, unit_id)
    assert unit.state.value == "failed"
