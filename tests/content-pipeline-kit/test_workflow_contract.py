"""C1's test contract (``dev/tasks/cpk-session-recipients/c1-design.md``
section 6) for the workflow lane: ``workflows/run-ready-wave.js`` and the
Python pack builder in ``execution/workerpack.py``.

Every test here is written against a WRONG IMPLEMENTATION it is meant to
kill, named in its own docstring. That discipline is the point of the file:
the first draft of this contract had eight of eight tests that re-asserted
text the design itself had chosen, so all eight passed against a
deliberately wrong script. A test that only proves the builder transcribed
the design is worse than no test, because it reads as coverage.

Three lanes, in order of what they can actually prove:

* **Stub-runtime execution** (the payoff). The script is loaded under Node
  with a fake ``agent()``/``parallel()`` and ``args`` delivered as a JSON
  STRING, exactly as the Workflow runtime delivers it. Only an executing
  test can measure the concurrency bound, and only the concurrency bound
  catches a missing ``await`` -- no text check can see it.
* **Python fixture comparison and protocol round trip.** The pack builder's
  REAL output is compared byte-for-byte against
  ``enumerate_worker_invocations`` and then driven through a real
  ``ExecutionStore`` + ``build_handlers`` + ``cli.run.build_commands``
  mount. Consuming the builder's real output is what makes it fail when the
  builder drifts; a harness that re-tested the store would kill nothing.
* **Text pins**, demoted to what text can honestly prove.

Two mechanical gotchas this file exists downstream of, both verified rather
than assumed:

1. A native Workflow script uses a top-level ``return``, which only the
   runtime's wrapping makes legal. ``node --check`` on a ``.mjs`` copy
   therefore REJECTS a CORRECT file (verified on node v26.5.0: a two-line
   ``export const a = 1; return 5;`` file passes as ``.js`` and fails as
   ``.mjs``). The parse gate and the stub runtime both WRAP the body in an
   async function first -- see :func:`_wrap_for_node`.
2. The script's header comments name ``agent()``, ``parallel()``,
   ``Date.now``, ``Math.random`` and ``crypto.randomUUID``. Every
   "occurs exactly once" / "never occurs" text pin therefore runs over a
   COMMENT-STRIPPED copy (:func:`_strip_comments`); a naive match fails on
   the correct file, which is as bad as passing on a wrong one.

What only C2 can prove, stated rather than implied covered: the real
Workflow runtime's schema enforcement and concurrency cap, the permission
posture against a real allowlist, one-claim-per-agent as observed attempt
rows, lease expiry against real agent runtimes, and instruction fidelity.
The stub runtime proves the SCRIPT's logic, not the platform's execution of
it.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from content_pipeline.cli.run import build_commands
from content_pipeline.cli.scaffold import dispatch
from content_pipeline.execution.adapter import PreparedRequest, RunAdapter
from content_pipeline.execution.model import (
    AlreadyClaimedError,
    RunHaltedError,
    TerminalStateError,
    UnitState,
)
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.workerpack import (
    DEFAULT_MAX_RECLAIMS_PER_UNIT,
    WorkerCommand,
    answer_path_for,
    build_wave_args,
    claim_envelope_path_for,
    claim_envelope_text,
    enumerate_worker_invocations,
    enumerate_workflow_invocations,
    format_fenced_answer,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit"
SCRIPT_PATH = PLUGIN_ROOT / "workflows" / "run-ready-wave.js"

RUN_ID = "run-1"
ARGV = ("python", "mount.py", "run")

# The compiler's own normalization line, verbatim -- `_ARGS_NORMALIZE` in
# `plugins/workflow-kit/workflow_kit_lib/compiler.py`. Duplicated as a
# literal rather than imported: workflow-kit's lib is not on this test
# package's sys.path, and a cross-plugin import edge is worse than a pinned
# string that a grep in either direction finds.
ARGS_NORMALIZE = 'const inputs = typeof args === "string" ? JSON.parse(args) : (args || {});'


# ---------------------------------------------------------------------------
# Source handling: wrapping (gotcha 1) and comment stripping (gotcha 2)
# ---------------------------------------------------------------------------


def _source() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def _wrap_for_node(source: str) -> str:
    """The script body as a callable ES module function.

    A native Workflow script's top-level ``return`` and its ``agent`` /
    ``parallel` / ``args`` globals are supplied by the runtime's wrapping.
    This reproduces the minimum of that wrapping needed to PARSE and RUN the
    body: ``export const meta`` becomes a plain ``const`` (an ``export`` is
    illegal inside a function), and the whole body becomes the tail of an
    exported async function taking the three runtime names as parameters.
    """
    marker = "export const meta"
    assert source.count(marker) == 1, (
        f"expected exactly one {marker!r} declaration in {SCRIPT_PATH}; the "
        "wrapper transform is written against that shape"
    )
    body = source.replace(marker, "const meta", 1)
    return "export async function runWave(args, agent, parallel) {\n" + body + "\n}\n"


def _strip_comments(source: str) -> str:
    """``source`` with ``//`` and ``/* */`` comments removed, string and
    template literals left intact.

    Load-bearing for every "exactly once" / "never" text pin below: the
    script's header comments legitimately name ``agent()``, ``parallel()``,
    ``Date.now``, ``Math.random`` and ``crypto.randomUUID``, so a pin over
    the raw text fails on the CORRECT file.
    """
    out = []
    i = 0
    n = len(source)
    quote = None
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if quote is not None:
            out.append(ch)
            if ch == "\\":
                if i + 1 < n:
                    out.append(nxt)
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# The Node stub runtime
# ---------------------------------------------------------------------------

_NODE = shutil.which("node")

_NO_NODE_REASON = (
    "no `node` on PATH: run-ready-wave.js was NOT proven to parse, NOT proven "
    "to run against a JSON-string `args`, and its concurrency bound, lane "
    "partition, halt/agent_error handling, pack-sourced identity, "
    "arg-validation throws and output determinism are ALL unverified in this "
    "run. Only the Python-side fixture-comparison, protocol round-trip, reap "
    "and text-pin tests ran."
)

requires_node = pytest.mark.skipif(_NODE is None, reason=_NO_NODE_REASON)

_DRIVER_MJS = r"""
import { runWave } from "./wrapped.mjs";
import { readFileSync } from "node:fs";

const scenario = JSON.parse(readFileSync(process.argv[2], "utf8"));

let inFlight = 0;
let maxInFlight = 0;
let seq = 0;
let parallelCalls = 0;
const events = [];
const calls = [];

async function agent(prompt, opts) {
  const m = /^Unit id: (.*)$/m.exec(prompt);
  const unit = m ? m[1] : "<no-unit-id-line>";
  calls.push({
    unit: unit,
    schema: opts ? opts.schema : null,
    phase: opts ? opts.phase : null,
    label: opts ? opts.label : null,
    prompt: scenario.capturePrompts ? prompt : null,
  });
  inFlight += 1;
  if (inFlight > maxInFlight) maxInFlight = inFlight;
  events.push({ unit: unit, phase: "start", seq: seq++ });
  try {
    await new Promise((r) => setTimeout(r, 5));
    const reply = Object.prototype.hasOwnProperty.call(scenario.replies, unit)
      ? scenario.replies[unit]
      : scenario.defaultReply;
    if (reply && reply.__reject) throw new Error("stub agent rejection: " + unit);
    return JSON.parse(JSON.stringify(reply));
  } finally {
    inFlight -= 1;
    events.push({ unit: unit, phase: "end", seq: seq++ });
  }
}

function parallel(thunks) {
  parallelCalls += 1;
  return Promise.all(thunks.map((t) => t()));
}

let waveArgs = scenario.argsJson;
if (scenario.asObject) {
  waveArgs = JSON.parse(scenario.argsJson);
  if (scenario.nanMaxAgents) waveArgs.maxAgents = NaN;
}

function emit(extra) {
  process.stdout.write(
    JSON.stringify(
      Object.assign(
        {
          maxInFlight: maxInFlight,
          parallelCalls: parallelCalls,
          calls: calls,
          events: events,
        },
        extra
      )
    )
  );
}

runWave(waveArgs, agent, parallel).then(
  (result) => emit({ ok: true, result: result, resultJson: JSON.stringify(result) }),
  (err) => emit({ ok: false, error: String(err && err.message ? err.message : err) })
);
"""


def _run_stub(tmp_path: Path, scenario: dict, *, name: str = "stub") -> dict:
    """Run the real script under the stub runtime and return the driver's
    report. Never asserts the wave SUCCEEDED -- a validation-matrix case is
    expected to come back ``ok: false``."""
    workdir = tmp_path / f"node-{name}"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "wrapped.mjs").write_text(_wrap_for_node(_source()), encoding="utf-8")
    (workdir / "driver.mjs").write_text(_DRIVER_MJS, encoding="utf-8")
    scenario_path = workdir / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    proc = subprocess.run(
        [_NODE, str(workdir / "driver.mjs"), str(scenario_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"stub driver exited {proc.returncode}\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


ACCEPTED_REPLY = {"outcome": "accepted", "attempts": 1, "error_code": "none"}


def _pack(unit_id: str, ordinal: int) -> dict:
    """A fixture pack with every field the script's guard requires. Values
    are stand-ins -- the byte-exactness of the REAL ones is the Python
    fixture-comparison lane's job, not the stub runtime's."""
    return {
        "unitId": unit_id,
        "ordinal": ordinal,
        "workerId": f"wf-batch1-{unit_id}",
        "claimCmd": f"mount protocol @/env/{unit_id}.claim.json",
        "readCmd": f"mount protocol @/env/{unit_id}.read.json",
        "submitCmd": (
            f"mount protocol @/env/{unit_id}.submit.json "
            f"--text-file=/ans/{unit_id}.answer.txt"
        ),
        "failCmd": f"mount protocol @/env/{unit_id}.fail.json",
        "answerPath": f"/ans/{unit_id}.answer.txt",
        "writeSubmitPath": f"/env/{unit_id}.submit.json",
        "writeFailPath": f"/env/{unit_id}.fail.json",
        "submitTemplate": f"SUBMIT-TEMPLATE-{unit_id} <FENCING_TOKEN>",
        "failTemplate": f"FAIL-TEMPLATE-{unit_id} <FENCING_TOKEN>",
    }


def _wave_args(units, max_agents: int = 2, **over) -> dict:
    args = {
        "runId": RUN_ID,
        "batchId": "batch-1",
        "maxAgents": max_agents,
        "units": units,
    }
    args.update(over)
    return args


def _scenario(args: dict, *, replies=None, capture_prompts=False, **extra) -> dict:
    scenario = {
        "argsJson": json.dumps(args),
        "replies": replies or {},
        "defaultReply": ACCEPTED_REPLY,
        "capturePrompts": capture_prompts,
    }
    scenario.update(extra)
    return scenario


def _seq_of(report: dict, unit: str, phase: str) -> int:
    for ev in report["events"]:
        if ev["unit"] == unit and ev["phase"] == phase:
            return ev["seq"]
    raise AssertionError(f"no {phase!r} event for unit {unit!r} in {report['events']}")


# ===========================================================================
# 1. Parse gate
# ===========================================================================


@requires_node
def test_the_script_parses_once_wrapped_as_the_runtime_wraps_it(tmp_path):
    """KILLS: any syntax error, an unbalanced brace, an unterminated string
    -- the entire class the first version of this contract shipped green,
    because nothing established the file parses at all.

    Non-vacuity is asserted in the same test: a deliberately corrupted copy
    of the same source MUST fail the same gate. Without that, a gate that
    silently accepted everything would read as a passing test forever.
    """
    workdir = tmp_path / "parse"
    workdir.mkdir()
    good = workdir / "good.mjs"
    good.write_text(_wrap_for_node(_source()), encoding="utf-8")
    proc = subprocess.run(
        [_NODE, "--check", str(good)], capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, (
        f"run-ready-wave.js does not parse under the Workflow wrapper:\n{proc.stderr}"
    )

    bad = workdir / "bad.mjs"
    bad.write_text(_wrap_for_node(_source()) + "\nconst broken = ;\n", encoding="utf-8")
    bad_proc = subprocess.run(
        [_NODE, "--check", str(bad)], capture_output=True, text=True, encoding="utf-8"
    )
    assert bad_proc.returncode != 0, (
        "the parse gate accepted deliberately broken syntax -- it proves nothing"
    )


# ===========================================================================
# 2. Stub-runtime execution
# ===========================================================================


@requires_node
def test_runs_against_a_json_string_args_and_returns_the_aggregate(tmp_path):
    """KILLS: the v1 defect -- a script that dereferences the ``args``
    global directly. The Workflow runtime delivers ``args`` as a JSON
    STRING, so an unnormalized ``args.units`` is ``undefined`` and the whole
    wave crashes on its first dereference. The driver passes a string here,
    never an object.

    Also kills an aggregate that reports the wrong wave size or lane count.
    """
    units = [_pack(f"u{i}", i) for i in range(4)]
    report = _run_stub(tmp_path, _scenario(_wave_args(units, max_agents=2)))

    assert report["ok"] is True, report.get("error")
    result = report["result"]
    assert result["run_id"] == RUN_ID
    assert result["batch_id"] == "batch-1"
    assert result["wave_size"] == 4
    assert result["lanes"] == 2
    assert result["advisory"] is True
    assert result["counts"]["accepted"] == 4
    assert len(result["units"]) == 4


@requires_node
@pytest.mark.parametrize(
    "unit_count,max_agents,expected_n",
    [
        (6, 3, 3),  # N == maxAgents
        (4, 10, 4),  # N clamped by wave size
        (30, 20, 16),  # N clamped by the platform's 16-agent ceiling
    ],
)
def test_max_in_flight_agents_never_exceeds_N(tmp_path, unit_count, max_agents, expected_n):
    """KILLS the missing ``await`` -- the single defect no text check can
    see. Without ``await agent(...)`` inside the lane body, every lane fires
    all of its agents at once, the concurrency bound N is a fiction, and the
    script still returns a plausible-looking aggregate (with pending
    Promises where results should be). Measuring the stub's peak in-flight
    count is the only way to observe it.

    The three parametrizations also kill each clamp independently: dropping
    ``inputs.units.length`` from the ``Math.min`` over-fans a short wave, and
    dropping the literal ``16`` breaches the platform ceiling.
    """
    units = [_pack(f"u{i}", i) for i in range(unit_count)]
    report = _run_stub(
        tmp_path,
        _scenario(_wave_args(units, max_agents=max_agents)),
        name=f"n{unit_count}x{max_agents}",
    )
    assert report["ok"] is True, report.get("error")
    assert report["result"]["lanes"] == expected_n
    assert report["maxInFlight"] == expected_n, (
        f"peak concurrency was {report['maxInFlight']}, expected exactly "
        f"{expected_n} -- a value above N means a lane is not awaiting its "
        "agent; a value below N means lanes are not running in parallel"
    )
    assert report["parallelCalls"] == 1


@requires_node
def test_lanes_partition_wave_positions_not_stored_ordinal_residues(tmp_path):
    """KILLS the stored-ordinal-residue reading of the lane sentence, which
    design section 3 rules out.

    Fixture: stored ordinals [2, 5, 9] with N = 2 -- a holey wave, which
    ``_flat_ready_wave`` really produces (it preserves stored ordinals and
    renumbers nothing). Wave-position partitioning puts positions 0 and 2
    (ordinals 2 and 9) in lane 0 and position 1 (ordinal 5) in lane 1.
    Residue partitioning by stored ordinal would put ordinal 2 alone in one
    lane and 5 and 9 together in the other -- observably different, because
    two units in the SAME lane can never be in flight at the same time.

    Observed through the stub's start/end sequence, which is what makes this
    a behavioural test rather than a re-reading of the design.
    """
    units = [_pack("ord2", 2), _pack("ord5", 5), _pack("ord9", 9)]
    report = _run_stub(tmp_path, _scenario(_wave_args(units, max_agents=2)))
    assert report["ok"] is True, report.get("error")

    # Same lane: ordinal 9 must not start until ordinal 2 has finished.
    assert _seq_of(report, "ord2", "end") < _seq_of(report, "ord9", "start"), (
        "ordinals 2 and 9 (wave positions 0 and 2) overlapped -- they are "
        "not in the same lane, so the partition is not by wave position"
    )
    # Different lanes: ordinal 5 must be in flight while ordinal 2 still is.
    assert _seq_of(report, "ord5", "start") < _seq_of(report, "ord2", "end"), (
        "ordinal 5 did not overlap ordinal 2 -- the second lane is not "
        "running concurrently"
    )
    assert report["maxInFlight"] == 2


@requires_node
def test_results_come_back_in_wave_position_order(tmp_path):
    """KILLS a botched lane-to-wave reassembly -- concatenating the lane
    arrays (which for N=2 over 5 units yields positions 0,2,4,1,3), or
    reusing the wrong stride. Each unit gets a distinguishable ``attempts``
    value, so a permuted result array cannot pass by coincidence.
    """
    units = [_pack(f"u{i}", i) for i in range(5)]
    replies = {
        f"u{i}": {"outcome": "accepted", "attempts": i, "error_code": "none"}
        for i in range(5)
    }
    report = _run_stub(tmp_path, _scenario(_wave_args(units, max_agents=2), replies=replies))
    assert report["ok"] is True, report.get("error")
    got = [(u["unit_id"], u["ordinal"], u["attempts"]) for u in report["result"]["units"]]
    assert got == [(f"u{i}", i, i) for i in range(5)]


@requires_node
def test_a_rejecting_agent_becomes_agent_error_and_the_lane_continues(tmp_path):
    """KILLS two shapes. Without the ``try``/``catch`` around ``agent()`` a
    single rejection rejects the whole ``parallel()`` and the wave returns
    nothing at all -- every other unit's real work is lost from the report.
    And a ``catch`` that swallowed the rejection into an ``accepted`` (or
    dropped the unit from the array) would misreport a unit nobody ran.

    The rejecting unit is at wave position 0 with N = 2, so its own lane
    still owes position 2 -- a lane that stopped on the exception would
    leave that unit unreported.
    """
    units = [_pack(f"u{i}", i) for i in range(4)]
    report = _run_stub(
        tmp_path,
        _scenario(_wave_args(units, max_agents=2), replies={"u0": {"__reject": True}}),
    )
    assert report["ok"] is True, report.get("error")
    outcomes = [u["outcome"] for u in report["result"]["units"]]
    assert outcomes == ["agent_error", "accepted", "accepted", "accepted"]
    assert report["result"]["counts"]["agent_error"] == 1
    assert report["result"]["counts"]["accepted"] == 3
    # The lane really did continue: four agents were actually invoked.
    assert len(report["calls"]) == 4


@requires_node
def test_halted_stops_only_its_own_lane(tmp_path):
    """KILLS a module-scoped halt flag (one unit's ``halted`` would strand
    every other lane's remaining units as ``not_attempted``, throwing away
    work the run is entitled to) and equally kills a script that ignores
    ``halted`` altogether and keeps spending agents on a halted run.

    Fixture: 4 units, N = 2. Lane 0 holds positions 0 and 2, lane 1 holds
    positions 1 and 3. Position 0 halts, so position 2 must be
    ``not_attempted`` while positions 1 and 3 both complete normally.
    """
    units = [_pack(f"u{i}", i) for i in range(4)]
    halted = {"outcome": "halted", "attempts": 1, "error_code": "run_halted"}
    report = _run_stub(
        tmp_path, _scenario(_wave_args(units, max_agents=2), replies={"u0": halted})
    )
    assert report["ok"] is True, report.get("error")
    outcomes = [u["outcome"] for u in report["result"]["units"]]
    assert outcomes == ["halted", "accepted", "not_attempted", "accepted"]
    # The skipped unit cost no agent at all.
    assert sorted(c["unit"] for c in report["calls"]) == ["u0", "u1", "u3"]
    assert report["result"]["counts"]["not_attempted"] == 1


@requires_node
def test_identity_comes_from_the_pack_even_when_the_agent_supplies_its_own(tmp_path):
    """KILLS an identity merge that lets the AGENT win -- e.g.
    ``Object.assign({}, identityFields(u), r)``, where a returned
    ``unit_id`` silently overwrites the pack's. Identity from the agent is
    identity from an untrusted self-report: an agent that mislabels its unit
    makes the whole advisory aggregate point at the wrong rows, and a lying
    or confused agent could attribute its outcome to a unit it never
    touched.

    The stub returns deliberate garbage identity, which the real runtime's
    ``additionalProperties: false`` may or may not strip first -- the script
    must not depend on that, because the aggregate is the script's own
    artifact and the pack is the only identity source it has.
    """
    units = [_pack("real-a", 0), _pack("real-b", 1)]
    garbage = {
        "outcome": "accepted",
        "attempts": 1,
        "error_code": "none",
        "unit_id": "EVIL",
        "ordinal": 999,
        "worker_id": "EVIL-WORKER",
    }
    report = _run_stub(
        tmp_path,
        _scenario(_wave_args(units, max_agents=2), replies={"real-a": garbage, "real-b": garbage}),
    )
    assert report["ok"] is True, report.get("error")
    got = [(u["unit_id"], u["ordinal"], u["worker_id"]) for u in report["result"]["units"]]
    assert got == [
        ("real-a", 0, "wf-batch1-real-a"),
        ("real-b", 1, "wf-batch1-real-b"),
    ]


@requires_node
def test_two_runs_with_identical_stubs_produce_byte_identical_output(tmp_path):
    """KILLS any entropy source, including one nobody thought to blacklist.
    A ``Date.now()`` in the aggregate, a ``crypto.randomUUID()`` batch
    suffix, a ``Math.random`` tiebreak, or an iteration over a Set/Map keyed
    on something unstable all break Workflow resume and make the aggregate
    unreproducible -- and a keyword blacklist only catches the sources it
    already knows. Comparing two whole runs catches the class.
    """
    units = [_pack(f"u{i}", i) for i in range(5)]
    scenario = _scenario(_wave_args(units, max_agents=3))
    first = _run_stub(tmp_path, scenario, name="det1")
    second = _run_stub(tmp_path, scenario, name="det2")
    assert first["ok"] is True and second["ok"] is True
    assert first["resultJson"] == second["resultJson"]


@requires_node
def test_the_schema_is_actually_passed_to_every_agent_call(tmp_path):
    """KILLS the schema-defined-but-unused hole: a script can declare
    ``UNIT_RESULT_SCHEMA`` -- satisfying every text pin about its contents
    -- and never hand it to ``agent()``, in which case the runtime enforces
    nothing, the agent returns free-form text, and the value-bounding the
    design leans on for leak control does not exist. Only an executing test
    can see what was passed.
    """
    units = [_pack(f"u{i}", i) for i in range(3)]
    report = _run_stub(tmp_path, _scenario(_wave_args(units, max_agents=2)))
    assert report["ok"] is True, report.get("error")
    assert len(report["calls"]) == 3
    for call in report["calls"]:
        schema = call["schema"]
        assert isinstance(schema, dict), f"agent() got no schema for {call['unit']!r}"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {"outcome", "attempts", "error_code"}
        assert schema["properties"]["outcome"]["enum"] == [
            "accepted",
            "failed",
            "halted",
            "claim_unavailable",
        ]
        assert call["phase"] == "Execute"


@requires_node
def test_the_brief_carries_every_invocation_string_the_agent_needs(tmp_path):
    """KILLS a brief that drops a pack field. The agent's only route to its
    unit is the exact strings the pack carries: no claim command means the
    self-claim ruling (design section 2) is unimplemented and the agent
    stalls on a unit it can never take; no submit template means it must
    invent envelope JSON; no answer path means it writes where nothing
    reads. Each omission still produces a well-formed script and a
    well-formed aggregate.

    Also asserts the brief carries no fencing token -- the token is claimed
    at runtime and must never be computable before the wave (P5).
    """
    units = [_pack("solo", 0)]
    report = _run_stub(
        tmp_path, _scenario(_wave_args(units, max_agents=1), capture_prompts=True)
    )
    assert report["ok"] is True, report.get("error")
    prompt = report["calls"][0]["prompt"]
    pack = units[0]
    for field in (
        "claimCmd",
        "readCmd",
        "submitCmd",
        "failCmd",
        "answerPath",
        "writeSubmitPath",
        "writeFailPath",
        "submitTemplate",
        "failTemplate",
        "unitId",
        "workerId",
    ):
        assert pack[field] in prompt, f"the agent brief never mentions pack.{field}"
    assert "fencing_token" not in prompt.lower().replace("<fencing_token>", "")


# ===========================================================================
# 3. Arg-validation matrix
# ===========================================================================


def _matrix_cases():
    ok_units = [_pack("a", 0), _pack("b", 1)]
    dup = [_pack("same", 0), dict(_pack("same", 1))]
    non_increasing = [_pack("a", 5), _pack("b", 5)]
    missing_field = [_pack("a", 0), {k: v for k, v in _pack("b", 1).items() if k != "claimCmd"}]
    return [
        ("maxAgents-zero", _wave_args(ok_units, max_agents=0), {}),
        ("maxAgents-negative", _wave_args(ok_units, max_agents=-1), {}),
        ("maxAgents-fractional", _wave_args(ok_units, max_agents=2.5), {}),
        ("maxAgents-missing", {k: v for k, v in _wave_args(ok_units).items() if k != "maxAgents"}, {}),
        ("maxAgents-nan", _wave_args(ok_units), {"asObject": True, "nanMaxAgents": True}),
        ("units-empty", _wave_args([]), {}),
        ("units-1001", _wave_args([_pack(f"u{i}", i) for i in range(1001)], max_agents=1), {}),
        ("units-duplicate-id", _wave_args(dup), {}),
        ("ordinals-non-increasing", _wave_args(non_increasing), {}),
        ("pack-missing-field", _wave_args(missing_field), {}),
        ("runId-missing", {k: v for k, v in _wave_args(ok_units).items() if k != "runId"}, {}),
        ("batchId-missing", {k: v for k, v in _wave_args(ok_units).items() if k != "batchId"}, {}),
    ]


@requires_node
@pytest.mark.parametrize("case,args,extra", _matrix_cases(), ids=[c[0] for c in _matrix_cases()])
def test_invalid_args_throw_rather_than_returning_a_silent_aggregate(tmp_path, case, args, extra):
    """KILLS the silent-successful-no-op family, which no text check can
    express. Each of these inputs, ungarded, produces a script that RETURNS
    NORMALLY with a zero-count or truncated aggregate: ``maxAgents`` of 0 or
    NaN makes ``N`` zero or NaN so the lane loop never starts; a fractional
    ``maxAgents`` makes the stride fractional and silently skips or
    double-runs positions; an empty ``units`` array means the caller should
    be finalizing, not waving; 1001 units breaches the platform's 1000-agent
    cap mid-wave; a duplicate ``unitId`` runs two agents against one unit
    (two claims, a superseded submit, duplicated spend); non-increasing
    ordinals mean the wave was not the ordinal-sorted array the partition
    assumes; a missing pack field hands an agent ``undefined`` inside a shell
    command string.

    A wave that quietly reports "0 accepted" is indistinguishable from a run
    with nothing to do, which is exactly how the failure survives.
    """
    report = _run_stub(tmp_path, _scenario(args, **extra), name=f"m-{case}")
    assert report["ok"] is False, (
        f"{case}: the script returned normally instead of throwing; "
        f"result={report.get('result')!r}"
    )
    assert "run-ready-wave:" in report["error"], (
        f"{case}: threw, but not through the script's own guard: {report['error']!r}"
    )
    assert report["calls"] == [], f"{case}: an agent was spent before the guard fired"


@requires_node
def test_the_thousand_unit_boundary_is_accepted(tmp_path):
    """KILLS an off-by-one in the P7 cap -- a ``>= 1000`` guard refuses a
    legal 1000-unit wave, which is a silent capacity regression nobody would
    attribute to the guard. The positive control for the ``units-1001``
    matrix case above; without it that case passes against a guard that
    rejects everything.
    """
    units = [_pack(f"u{i}", i) for i in range(1000)]
    report = _run_stub(tmp_path, _scenario(_wave_args(units, max_agents=16)), name="cap1000")
    assert report["ok"] is True, report.get("error")
    assert report["result"]["wave_size"] == 1000


# ===========================================================================
# 4. Fixture comparison (anti-drift, pure Python)
# ===========================================================================


def _envelope_arg(command_str: str) -> str:
    """The filesystem path behind a ``protocol @<path>`` invocation's final
    token. Goes through ``shlex.split`` rather than a naive ``@`` split
    because the library quotes a Windows path, so the raw string ends in a
    stray quote."""
    token = shlex.split(command_str)[-1]
    assert token.startswith("@"), token
    return token[1:]


def _worker_command(tmp_path: Path) -> WorkerCommand:
    answers = tmp_path / "answers"
    envelopes = tmp_path / "envelopes"
    answers.mkdir(exist_ok=True)
    envelopes.mkdir(exist_ok=True)
    return WorkerCommand(argv=ARGV, answer_dir=str(answers), envelope_dir=str(envelopes))


def test_workflow_invocations_reuse_the_worker_invocations_byte_for_byte(tmp_path):
    """KILLS a second implementation of the read/submit/fail strings in the
    C lane. If ``enumerate_workflow_invocations`` re-derived them instead of
    calling ``enumerate_worker_invocations``, the two lanes could drift on
    envelope naming, argv substitution, or quoting -- and the drift would
    only show up as a worker running a command against a file nobody wrote.
    Byte equality is the only assertion that catches a difference in
    ``shlex`` quoting or path separators.
    """
    wc = _worker_command(tmp_path)
    six = enumerate_worker_invocations(wc, RUN_ID, "unit-a", "worker-a")
    seven = enumerate_workflow_invocations(wc, RUN_ID, "unit-a", "worker-a")
    assert len(seven) == 7
    assert seven[1:] == six
    assert seven[0] != six[0]
    claim_tokens = shlex.split(seven[0])
    assert claim_tokens[-2] == "protocol"
    assert claim_tokens[-1].startswith("@") and claim_tokens[-1].endswith(".claim.json")


def test_the_submit_command_keeps_the_equals_joined_text_file_flag(tmp_path):
    """KILLS a dropped or space-separated ``--text-file``. ``cli.run``'s
    ``_split_flags`` only recognizes an ``=``-joined flag as carrying a
    value, so ``--text-file <path>`` parses as a bare boolean and the submit
    envelope's payload never receives the answer text -- the submit then
    fails validation on empty text, or worse, is accepted empty. The flag's
    value must also be exactly ``answer_path_for``'s path, or the submit
    reads a file the agent never wrote.
    """
    wc = _worker_command(tmp_path)
    _claim, _read, submit_cmd, _fail, _wa, _ws, _wf = enumerate_workflow_invocations(
        wc, RUN_ID, "unit-a", "worker-a"
    )
    expected = answer_path_for(wc, RUN_ID, "unit-a")
    tail = shlex.split(submit_cmd)[-1]
    assert tail.startswith("--text-file=")
    assert tail[len("--text-file=") :] == expected


def test_the_claim_envelope_path_is_worker_scoped(tmp_path):
    """KILLS a claim envelope keyed on ``(run_id, unit_id)`` alone, like
    every other verb's. A later batch would overwrite a stalled agent's
    claim envelope in place and hand that still-live agent a DIFFERENT
    worker identity -- the identity-confusion hazard design section 2's
    remedy exists to close. Two worker ids for the same unit must produce
    two distinct paths, and the worker id must be visible in the name.
    """
    wc = _worker_command(tmp_path)
    a = claim_envelope_path_for(wc, RUN_ID, "unit-a", "worker-a")
    b = claim_envelope_path_for(wc, RUN_ID, "unit-a", "worker-b")
    assert a != b
    assert "worker-a" in os.path.basename(a)
    assert "worker-b" in os.path.basename(b)
    assert os.path.basename(a).endswith(".claim.json")


def test_the_claim_envelope_carries_exactly_the_claim_handlers_payload():
    """KILLS a claim envelope that does not parse, or that carries the wrong
    keys. ``build_handlers``' ``_claim`` requires exactly ``run_id``,
    ``unit_id`` and ``worker_id`` (read from ``execution/protocol.py``, not
    from the design). A missing key fails every claim in the lane; an extra
    ``fencing_token`` would be meaningless (a claim RETURNS one), and an
    extra ``lease_seconds`` may only SHORTEN the derived lease -- a value
    this lane must never send, because it has no renewer to correct a short
    one.

    Unlike the submit/fail TEMPLATE text, this envelope must be valid JSON
    as written: no agent substitutes anything into it.
    """
    envelope = json.loads(claim_envelope_text(RUN_ID, "unit-a", "worker-a"))
    assert envelope["verb"] == "claim"
    assert envelope["protocol_version"] == "1"
    assert set(envelope["payload"]) == {"run_id", "unit_id", "worker_id"}
    assert envelope["payload"] == {
        "run_id": RUN_ID,
        "unit_id": "unit-a",
        "worker_id": "worker-a",
    }


def test_build_wave_args_packs_match_the_library_strings_byte_for_byte(tmp_path):
    """KILLS pack-builder drift. ``build_wave_args`` could assemble its own
    command strings, its own answer path, or its own envelope templates and
    still look correct in isolation; every one of those would diverge from
    what the protocol mount actually reads. Comparing the builder's real
    output against the library's own generators is what makes this an
    anti-drift test rather than a restatement.
    """
    store, adapter, _commands = _make_run(tmp_path, ["unit-a", "unit-b"])
    wc = _worker_command(tmp_path)
    wave = build_wave_args(store, RUN_ID, adapter, wc, 2)

    assert [u["unitId"] for u in wave["units"]] == ["unit-a", "unit-b"]
    assert wave["runId"] == RUN_ID
    assert wave["maxAgents"] == 2
    assert wave["batchId"]

    for pack in wave["units"]:
        expected = enumerate_workflow_invocations(
            wc, RUN_ID, pack["unitId"], pack["workerId"]
        )
        claim_cmd, read_cmd, submit_cmd, fail_cmd, _wa, _ws, _wf = expected
        assert pack["claimCmd"] == claim_cmd
        assert pack["readCmd"] == read_cmd
        assert pack["submitCmd"] == submit_cmd
        assert pack["failCmd"] == fail_cmd
        assert pack["answerPath"] == answer_path_for(wc, RUN_ID, pack["unitId"])
        assert "<FENCING_TOKEN>" in pack["submitTemplate"]
        assert "<FENCING_TOKEN>" in pack["failTemplate"]
        # The envelopes the agent does NOT author must already be on disk.
        assert Path(_envelope_arg(pack["claimCmd"])).exists()
        assert Path(_envelope_arg(pack["readCmd"])).exists()
        # The ones it DOES author must not be pre-written.
        assert not Path(pack["writeSubmitPath"]).exists()
        assert not Path(pack["writeFailPath"]).exists()


# ===========================================================================
# 5. Protocol round-trip harness
# ===========================================================================


def _adapter(expected_unit_seconds=60.0) -> RunAdapter:
    return RunAdapter(
        build_request=lambda unit: PreparedRequest(
            unit=unit, system="", user=f"prepared:{unit.id}"
        ),
        parse_fn=lambda t: t,
        apply=lambda uid, payload: None,
        adapter_version="v1",
        expected_unit_seconds=expected_unit_seconds,
    )


def _make_run(tmp_path: Path, unit_ids, *, expected_unit_seconds=60.0):
    store = ExecutionStore(tmp_path / "run.db")
    adapter = _adapter(expected_unit_seconds)
    commands = build_commands(store, adapter=adapter)
    commands["create-run"].handler([RUN_ID, "workflow", "mock", "m"])
    commands["register-units"].handler([RUN_ID, *unit_ids])
    return store, adapter, commands


def _tail(command_str: str) -> list:
    tokens = shlex.split(command_str)
    assert tuple(tokens[: len(ARGV)]) == ARGV
    return tokens[len(ARGV) :]


def _run_cli(tail, commands):
    out = io.StringIO()
    err = io.StringIO()
    code = dispatch(tail, commands, out=out, err=err)
    parsed = yaml.safe_load(out.getvalue()) if out.getvalue().strip() else None
    return code, parsed, err.getvalue()


def test_the_pack_drives_the_real_protocol_all_the_way_to_accepted(tmp_path):
    """KILLS a pack that is internally consistent but does not work: an
    unrunnable claim command, a claim envelope the mount rejects, a read
    that returns nothing, a submit template whose substituted form is not
    valid JSON, or a ``--text-file`` whose fence line the mount refuses.
    Each of those passes every string-shape assertion above and still leaves
    a real unit stranded PENDING.

    This consumes ``build_wave_args``' REAL output -- the envelope files it
    wrote, the command strings it emitted -- so it fails if that builder
    drifts. A harness that constructed its own envelopes would only re-test
    the store, which ``test_execution_store.py`` already covers.
    """
    store, adapter, commands = _make_run(tmp_path, ["unit-a"])
    wc = _worker_command(tmp_path)
    pack = build_wave_args(store, RUN_ID, adapter, wc, 1)["units"][0]

    code, claim_reply, _err = _run_cli(_tail(pack["claimCmd"]), commands)
    assert code == 0 and claim_reply["ok"] is True, claim_reply
    token = claim_reply["result"]["fencing_token"]

    code, read_reply, _err = _run_cli(_tail(pack["readCmd"]), commands)
    assert code == 0 and read_reply["ok"] is True, read_reply
    assert read_reply["result"]["user"] == "prepared:unit-a"

    Path(pack["answerPath"]).write_text(
        format_fenced_answer(token, "the answer"), encoding="utf-8"
    )
    Path(pack["writeSubmitPath"]).write_text(
        pack["submitTemplate"].replace("<FENCING_TOKEN>", str(token)), encoding="utf-8"
    )
    code, submit_reply, _err = _run_cli(_tail(pack["submitCmd"]), commands)
    assert code == 0 and submit_reply["ok"] is True, submit_reply
    assert submit_reply["result"]["accepted"] is True

    unit = store.get_unit(RUN_ID, "unit-a")
    assert unit.state is UnitState.ACCEPTED
    assert unit.accepted_text == "the answer"


def test_an_answer_fenced_under_the_wrong_token_is_refused(tmp_path):
    """KILLS a submit path that splices the answer file without checking its
    fence. The fence line is the only thing that ties an answer artifact to
    the generation that produced it: a reclaimed unit's new worker writes to
    the SAME answer path (``answer_path_for`` deliberately carries no worker
    id), so without the check a stale agent's leftover text can be submitted
    under a live token and accepted as the unit's result.
    """
    store, adapter, commands = _make_run(tmp_path, ["unit-a"])
    wc = _worker_command(tmp_path)
    pack = build_wave_args(store, RUN_ID, adapter, wc, 1)["units"][0]

    _code, claim_reply, _err = _run_cli(_tail(pack["claimCmd"]), commands)
    token = claim_reply["result"]["fencing_token"]

    Path(pack["answerPath"]).write_text(
        format_fenced_answer(token + 77, "wrong-generation text"), encoding="utf-8"
    )
    Path(pack["writeSubmitPath"]).write_text(
        pack["submitTemplate"].replace("<FENCING_TOKEN>", str(token)), encoding="utf-8"
    )
    _code, submit_reply, _err = _run_cli(_tail(pack["submitCmd"]), commands)
    assert submit_reply["ok"] is False, submit_reply
    assert store.get_unit(RUN_ID, "unit-a").state is not UnitState.ACCEPTED


def test_a_second_claim_after_expiry_supersedes_the_first_agents_token(tmp_path):
    """KILLS the residual design section 2 names, if it were ever unbounded:
    a stalled C-lane agent keeps a runnable claim command for the life of
    its session. This proves the fence -- not politeness -- is what stops it
    doing damage. After a reclaim, the first agent's submit must be REFUSED
    and the unit must not be accepted under its stale token.
    """
    store, adapter, commands = _make_run(tmp_path, ["unit-a"])
    wc = _worker_command(tmp_path)
    pack = build_wave_args(store, RUN_ID, adapter, wc, 1)["units"][0]

    _code, claim_reply, _err = _run_cli(_tail(pack["claimCmd"]), commands)
    stale_token = claim_reply["result"]["fencing_token"]

    later = time.time() + 100_000
    fresh = store.claim_unit(RUN_ID, "unit-a", "worker-generation-2", at=later)
    assert fresh.fencing_token != stale_token

    Path(pack["answerPath"]).write_text(
        format_fenced_answer(stale_token, "stale text"), encoding="utf-8"
    )
    Path(pack["writeSubmitPath"]).write_text(
        pack["submitTemplate"].replace("<FENCING_TOKEN>", str(stale_token)), encoding="utf-8"
    )
    _code, submit_reply, _err = _run_cli(_tail(pack["submitCmd"]), commands)
    assert submit_reply["ok"] is False, submit_reply
    assert submit_reply["error"]["type"] == "StaleFenceError"
    assert store.get_unit(RUN_ID, "unit-a").state is not UnitState.ACCEPTED


# ===========================================================================
# 6. Reap at the front of wave assembly
# ===========================================================================


def test_an_abandoned_unit_re_enters_the_wave_once_its_lease_expires(tmp_path):
    """KILLS a builder that selects only PENDING units -- the shape
    ``wave._flat_ready_wave`` has, and the reason design section 4 moved the
    reap to the FRONT of wave assembly. This lane has no renewer and no
    mid-flight reclaim, so a unit whose agent died sits CLAIMED forever and
    is never seen again by any later wave. The run would simply never
    finish, with no error anywhere.

    The live-lease control in the same test kills the opposite defect: a
    builder that hands out a unit somebody is still working on, producing
    two agents on one unit.
    """
    store, adapter, _commands = _make_run(tmp_path, ["unit-a", "unit-b"])
    wc = _worker_command(tmp_path)
    t0 = time.time()
    store.claim_unit(RUN_ID, "unit-a", "dead-worker", lease_seconds=10, at=t0)

    live = build_wave_args(store, RUN_ID, adapter, wc, 2, at=t0 + 1)
    assert [u["unitId"] for u in live["units"]] == ["unit-b"]

    expired = build_wave_args(store, RUN_ID, adapter, wc, 2, at=t0 + 500)
    assert [u["unitId"] for u in expired["units"]] == ["unit-a", "unit-b"]


def test_a_unit_at_the_reclaim_bound_is_terminally_failed_and_excluded(tmp_path):
    """KILLS an unbounded reap. Without the bound a unit whose every agent
    dies is re-dispatched forever: each wave spends an agent, the unit
    expires again, and the run never converges while looking busy. The
    exclusion must also be durable in the STORE (a terminal state), not just
    a filter in this call -- otherwise the next wave picks it straight back
    up.
    """
    store, adapter, _commands = _make_run(tmp_path, ["unit-a", "unit-b"])
    wc = _worker_command(tmp_path)
    t0 = time.time()
    store.claim_unit(RUN_ID, "unit-a", "w0", lease_seconds=10, at=t0)
    # Each claim on an already-expired lease records one EXPIRE attempt.
    for i in range(DEFAULT_MAX_RECLAIMS_PER_UNIT):
        store.claim_unit(RUN_ID, "unit-a", f"w{i + 1}", lease_seconds=10, at=t0 + 100 * (i + 1))

    wave = build_wave_args(store, RUN_ID, adapter, wc, 2, at=t0 + 10_000)
    assert [u["unitId"] for u in wave["units"]] == ["unit-b"]
    assert store.get_unit(RUN_ID, "unit-a").state is UnitState.FAILED


def test_the_builder_refuses_a_mount_with_no_lease_information(tmp_path):
    """KILLS a silent fall-through to the store's bare 300s default. In a
    lane with no renewer, an under-sized lease expires while the agent is
    healthy: the unit is reclaimed, a second agent is spent on it, EXPIRE
    rows accrue, and the bound above terminally fails a unit that nothing
    was ever wrong with. The refusal must RAISE, not warn -- a warning in a
    wave-assembly path is read by nobody.

    The explicit-lease control kills the opposite defect: refusing a mount
    that did supply a lease, which would make the guard unusable.
    """
    store, adapter, _commands = _make_run(tmp_path, ["unit-a"], expected_unit_seconds=None)
    wc = _worker_command(tmp_path)
    with pytest.raises(ValueError, match="lease_seconds"):
        build_wave_args(store, RUN_ID, adapter, wc, 1)

    wave = build_wave_args(store, RUN_ID, adapter, wc, 1, lease_seconds=900)
    assert [u["unitId"] for u in wave["units"]] == ["unit-a"]


# ===========================================================================
# 7. Text pins -- only what text can honestly prove
# ===========================================================================


def test_args_are_normalized_with_the_compilers_own_line_and_nowhere_dereferenced():
    """KILLS a hand-rolled args normalization that diverges from
    ``_ARGS_NORMALIZE`` (e.g. assuming an object, or ``JSON.parse``-ing
    unconditionally so a future object-delivering runtime crashes), and
    kills a stray ``args.something`` dereference that bypasses the
    normalized ``inputs``. The stub-runtime tests prove the string path
    works; this pins that there is no second, unnormalized path.
    """
    stripped = _strip_comments(_source())
    assert ARGS_NORMALIZE in stripped
    remainder = stripped.replace(ARGS_NORMALIZE, "", 1)
    assert "args." not in remainder
    assert remainder.count("args") == 0 or "typeof args" not in remainder


def test_there_is_exactly_one_agent_call_site_and_one_parallel_call():
    """KILLS a second ``agent()`` call site or a second ``parallel()``. Two
    call sites mean two briefs, two schemas, and two places for the
    concurrency bound to be wrong -- and the design's whole loop shape is
    'exactly this lane loop, no other'. Run over comment-stripped source
    because the file's header legitimately names both functions in prose.
    """
    stripped = _strip_comments(_source())
    assert stripped.count("agent(") == 1
    assert stripped.count("parallel(") == 1


def test_the_platform_ceiling_literal_appears_only_in_the_N_formula():
    """KILLS a second, divergent copy of the 16-agent ceiling -- a duplicated
    magic number is how one of two clamps gets changed and the other does
    not, and the stub-runtime bound test would still pass against whichever
    one it happened to exercise.
    """
    stripped = _strip_comments(_source())
    sixteens = [line for line in stripped.splitlines() if "16" in line]
    assert len(sixteens) == 1, sixteens
    assert "Math.min(inputs.maxAgents, 16, inputs.units.length)" in sixteens[0]


def test_no_entropy_source_appears_in_executable_code():
    """KILLS a nondeterministic script. Workflow resume caches node results,
    so a value derived from the clock or a random source makes a resumed run
    disagree with the one it is resuming. The determinism test above catches
    the class behaviourally; this names the known sources so a violation is
    reported at its source line rather than as a mysterious diff. Comment
    stripping is load-bearing -- the header names all three deliberately.
    """
    stripped = _strip_comments(_source())
    for banned in ("Date.now", "Math.random", "new Date", "crypto.randomUUID", "performance.now"):
        assert banned not in stripped, f"entropy source {banned!r} in executable code"


def test_the_result_schema_bounds_values_and_carries_no_identity_or_content():
    """KILLS a schema that bounds KEYS but not VALUES -- the v1 defect, where
    ``additionalProperties: false`` was mistaken for 'no unit content can
    escape' while ``error_type`` and an agent-supplied ``unit_id`` were
    unbounded strings, a free channel for the unit content the agent is not
    allowed to return. Every property must be an enum or a bounded integer,
    and no identity or text property may exist at all.
    """
    stripped = _strip_comments(_source())
    start = stripped.index("const UNIT_RESULT_SCHEMA")
    block = stripped[start : stripped.index("};", start)]
    assert "additionalProperties: false" in block
    assert '"accepted", "failed", "halted", "claim_unavailable"' in block
    assert "minimum: 0" in block and "maximum: 8" in block
    for banned in ("unit_id", "worker_id", "ordinal", "text", "answer", "feedback", "error_type"):
        assert banned not in block, f"schema exposes a {banned!r} channel"


# ===========================================================================
# 8. Exactly-one-result-per-unit, the picked content bound, and the reap's
#    per-unit claim refusals
# ===========================================================================


@requires_node
def test_a_nullish_agent_reply_yields_one_result_and_no_position_shift(tmp_path):
    """KILLS the double-push. ``Object.assign({}, null)`` is LEGAL, so an
    ``agent()`` that resolves to ``null`` (or ``undefined``) used to be
    pushed as a result, after which reading ``r.outcome`` threw and the
    ``catch`` pushed a SECOND entry for the same unit. With the lane's
    length no longer what the stride assumed, a reassembly that recomputes
    ``k + j*N`` shifts every later entry off its true wave position, extends
    the array past ``units.length``, and leaves unwritten holes that crash
    the counting loop -- losing the whole wave, including the units that
    really did complete.

    Fixture: 3 units, N = 2. Lane 0 owns positions 0 and 2, lane 1 owns
    position 1. Position 0's agent resolves to ``null``, so pre-fix lane 0
    is three entries long and the stride writes positions 0, 2 and 4 --
    leaving position 3 a hole in a 5-long array. Post-fix the unit
    contributes exactly one ``agent_error`` and nothing else moves.
    """
    units = [_pack(f"u{i}", i) for i in range(3)]
    report = _run_stub(
        tmp_path,
        _scenario(_wave_args(units, max_agents=2), replies={"u0": None}),
    )
    assert report["ok"] is True, report.get("error")
    result = report["result"]
    assert len(result["units"]) == len(units) == result["wave_size"]
    assert [u["unit_id"] for u in result["units"]] == ["u0", "u1", "u2"]
    assert [u["outcome"] for u in result["units"]] == [
        "agent_error",
        "accepted",
        "accepted",
    ]
    assert result["counts"]["agent_error"] == 1
    assert result["counts"]["accepted"] == 2
    # One agent per unit, and no unit reported twice.
    assert sorted(c["unit"] for c in report["calls"]) == ["u0", "u1", "u2"]


@requires_node
def test_the_aggregate_carries_only_the_picked_schema_fields(tmp_path):
    """KILLS the spread. ``Object.assign({}, r, identity)`` copies EVERY key
    the agent returned into the aggregate's ``units``, so the stated bound
    -- no unit content reaches a workflow variable or the return value --
    held only because the Workflow runtime strips properties outside the
    schema. The aggregate is this script's own artifact, and this file
    already refuses that same trust for the identity fields; the content
    bound must be enforced by the script too.

    Second case, same fix: an ``outcome``/``error_code`` outside the
    schema's enum is an arbitrary agent-supplied string and must not be
    passed through into the aggregate at all -- it is an ``agent_error``.
    """
    units = [_pack("u0", 0)]
    leaky = {
        "outcome": "accepted",
        "attempts": 1,
        "error_code": "none",
        "answer_text": "SMUGGLED-UNIT-CONTENT",
        "feedback": "SMUGGLED-FEEDBACK",
        "error_type": "SMUGGLED-TYPE",
    }
    report = _run_stub(
        tmp_path,
        _scenario(_wave_args(units, max_agents=1), replies={"u0": leaky}),
        name="picked",
    )
    assert report["ok"] is True, report.get("error")
    entry = report["result"]["units"][0]
    assert sorted(entry) == [
        "attempts",
        "error_code",
        "ordinal",
        "outcome",
        "unit_id",
        "worker_id",
    ], entry
    assert "SMUGGLED" not in report["resultJson"]

    out_of_enum = {
        "outcome": "SMUGGLED-OUTCOME",
        "attempts": 1,
        "error_code": "SMUGGLED-CODE",
    }
    second = _run_stub(
        tmp_path,
        _scenario(_wave_args(units, max_agents=1), replies={"u0": out_of_enum}),
        name="enum",
    )
    assert second["ok"] is True, second.get("error")
    assert second["result"]["units"][0]["outcome"] == "agent_error"
    assert second["result"]["units"][0]["error_code"] == "other"
    assert "SMUGGLED" not in second["resultJson"]


@pytest.mark.parametrize(
    "exc_factory,expected_units",
    [
        (lambda: AlreadyClaimedError("unit-b is claimed"), ["unit-c"]),
        (lambda: TerminalStateError("unit-b is terminal"), ["unit-c"]),
        (lambda: RunHaltedError(RUN_ID, "manual"), []),
    ],
    ids=["already_claimed", "terminal_state", "run_halted"],
)
def test_a_refused_terminal_fail_does_not_abort_the_whole_wave_build(
    tmp_path, exc_factory, expected_units
):
    """KILLS an unguarded ``_terminally_fail_exhausted_unit`` call. That
    function's own docstring assigns three claim refusals to its caller, and
    ``store.claim_unit`` really raises all three on this path (a halted run;
    a still-fenced prior worker settling the unit terminally after its lease
    expired but before the reap; a re-claim landing between
    ``reclaimable_units`` and the claim). ``dispatch_wave`` handles each PER
    UNIT. Unhandled here, one refusal aborts the entire build AFTER earlier
    units in the same loop have already been driven terminal -- a durable
    partial reap with no wave at all, so every still-PENDING unit of the run
    is stranded for that call.

    Fixture: ``unit-a`` and ``unit-b`` are both at the reclaim bound;
    ``unit-b``'s reap claim refuses; ``unit-c`` is PENDING. ``unit-a`` must
    still be terminally failed, ``unit-b`` must be excluded from the wave
    (already terminal, or somebody else's live claim -- either way not ours
    to hand to a fresh agent), and the build must return. A halt
    additionally stops further selection, so ``unit-c`` is not emitted.
    """
    store, adapter, _commands = _make_run(tmp_path, ["unit-a", "unit-b", "unit-c"])
    wc = _worker_command(tmp_path)
    t0 = time.time()
    for unit_id in ("unit-a", "unit-b"):
        store.claim_unit(RUN_ID, unit_id, "w0", lease_seconds=10, at=t0)
        for i in range(DEFAULT_MAX_RECLAIMS_PER_UNIT):
            store.claim_unit(
                RUN_ID, unit_id, f"w{i + 1}", lease_seconds=10, at=t0 + 100 * (i + 1)
            )

    real_claim = store.claim_unit

    def refusing_claim(run_id, unit_id, worker_id, *a, **kw):
        if unit_id == "unit-b":
            raise exc_factory()
        return real_claim(run_id, unit_id, worker_id, *a, **kw)

    store.claim_unit = refusing_claim

    wave = build_wave_args(store, RUN_ID, adapter, wc, 3, at=t0 + 10_000)

    assert [u["unitId"] for u in wave["units"]] == expected_units
    assert store.get_unit(RUN_ID, "unit-a").state is UnitState.FAILED
    # The refused unit is excluded from the wave and left as it was found.
    assert store.get_unit(RUN_ID, "unit-b").state is UnitState.CLAIMED
