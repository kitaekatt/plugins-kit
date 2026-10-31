"""Behaviour tests for the md-domain workflow `maxConcurrent` argument.

Every md-domain workflow script takes an optional args.maxConcurrent, a positive
integer bounding the agents in flight in each fan-out, and routes every fan-out
through laneParallel(), which lives in the shared LANE_ROUTE_CHUNK that
scripts/gen_workflow_js.py owns. These tests run the REAL chunk (and the real
shipped scripts) under node with stub agent()/parallel()/log() globals, so they
observe the concurrency the Workflow tool would see, not the source text.

Harness pattern: tests/skills-kit/test_lane_route_dispatch.py. The stub
parallel() follows the Workflow contract: a thunk that throws resolves to null
and the call itself never rejects.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain" / "workflow"
GEN_PATH = REPO_ROOT / "plugins" / "skills-kit" / "scripts" / "gen_workflow_js.py"
ROUTE_TEST_PATH = Path(__file__).resolve().parent / "test_lane_route_dispatch.py"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

_spec = importlib.util.spec_from_file_location("gen_workflow_js_maxc", GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

# The sibling runtime test owns the per-lane argument builders; reuse them.
_rspec = importlib.util.spec_from_file_location("lane_route_dispatch_helpers", ROUTE_TEST_PATH)
route = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(route)

SONNET_LOW = {"run": [{"id": "sonnet", "effort": "low"}], "dropped": []}

# --- unit harness: the shared chunk alone, with controlled thunks ------------

POOL_HARNESS = r"""
const fs = require('fs')
const chunk = fs.readFileSync(process.argv[2], 'utf8')
const flush = () => new Promise((r) => setImmediate(r))
const NO_PARALLEL_LIMIT = (thunks, calls) => {
  calls.push(thunks.length)
  return Promise.all(thunks.map((t) => Promise.resolve().then(t).catch(() => null)))
}

async function load(maxConcurrent, ctl, body) {
  const parallelCalls = []
  const agentCalls = []
  const args = { laneModels: { run: [{ id: 'sonnet', effort: 'low' }], dropped: [] } }
  if (maxConcurrent !== undefined) args.maxConcurrent = maxConcurrent
  const fn = new Function('args', 'agent', 'parallel', 'phase', 'log', 'ctl',
    '"use strict"; return (async () => {\nlet input = args\n' + chunk + '\n' + body + '\n})()')
  const run = () => fn(args,
    (...a) => { agentCalls.push(a); return Promise.resolve(null) },
    (thunks) => NO_PARALLEL_LIMIT(thunks, parallelCalls),
    () => {}, (m) => { ctl.logs.push(String(m)) }, ctl)
  return { run, parallelCalls, agentCalls }
}

// n thunks; each stays pending until the scenario settles it.
function controlled(n, syncThrow) {
  const ctl = { started: [], settle: [], inflight: 0, peak: 0, logs: [], thunks: [] }
  ctl.thunks = Array.from({ length: n }, (_, i) => () => {
    ctl.started.push(i)
    if ((syncThrow || []).includes(i)) throw new Error('sync ' + i)
    ctl.inflight++
    ctl.peak = Math.max(ctl.peak, ctl.inflight)
    return new Promise((res, rej) => { ctl.settle[i] = { res, rej } })
      .finally(() => { ctl.inflight-- })
  })
  return ctl
}

async function main() {
  const out = {}

  // 1. refill: N=3 over 8 items; a slot freed by item 1 starts item 3 while
  // items 0 and 2 are still running (a fixed batch of 3 would not).
  {
    const ctl = controlled(8)
    const { run, parallelCalls } = await load(3, ctl, 'return laneParallel(ctl.thunks)')
    const p = run()
    await flush()
    const s0 = ctl.started.slice()
    ctl.settle[1].res('r1'); await flush()
    const s1 = ctl.started.slice()
    ctl.settle[3].res('r3'); await flush()
    const s2 = ctl.started.slice()
    ctl.settle[2].res('r2'); await flush()
    const s3 = ctl.started.slice()
    ctl.settle[4].rej(new Error('boom')); await flush()
    const s4 = ctl.started.slice()
    ctl.settle[5].res('r5'); await flush()
    ctl.settle[7].res('r7'); await flush()
    ctl.settle[6].res('r6'); ctl.settle[0].res('r0')
    const results = await p
    out.refill = { s0, s1, s2, s3, s4, results, peak: ctl.peak, parallelCalls, logs: ctl.logs }
  }

  // 2. failures: a sync throw and a rejection each resolve to null in place,
  // and the pool keeps going.
  {
    const ctl = controlled(5, [1])
    const { run } = await load(2, ctl, 'return laneParallel(ctl.thunks)')
    const p = run()
    for (let k = 0; k < 20 && ctl.started.length < 5; k++) {
      await flush()
      ;[0, 2, 3, 4].forEach((i) => {
        const s = ctl.settle[i]
        if (s && !s.done) { s.done = true; i === 3 ? s.rej(new Error('boom')) : s.res('r' + i) }
      })
    }
    await flush()
    out.failures = { results: await p, started: ctl.started, peak: ctl.peak }
  }

  // 3. unset: parallel() receives every thunk at once, exactly as before.
  {
    const ctl = controlled(8)
    const { run, parallelCalls } = await load(undefined, ctl, 'return laneParallel(ctl.thunks)')
    const p = run()
    await flush()
    const startedAtOnce = ctl.started.length
    ctl.settle.forEach((s, i) => s.res('r' + i))
    out.unset = { startedAtOnce, parallelCalls, results: await p, logs: ctl.logs }
  }

  // 4. no more items than the bound: delegates to parallel() unchanged.
  {
    const ctl = controlled(3)
    const { run, parallelCalls } = await load(5, ctl, 'return laneParallel(ctl.thunks)')
    const p = run()
    await flush()
    ctl.settle.forEach((s, i) => s.res('r' + i))
    out.fewer = { parallelCalls, results: await p }
  }

  // 5. N=1 is strictly serial and keeps order.
  {
    const ctl = controlled(4)
    const { run } = await load(1, ctl, 'return laneParallel(ctl.thunks)')
    const p = run()
    const seen = []
    for (let i = 0; i < 4; i++) {
      await flush()
      seen.push(ctl.started.slice())
      ctl.settle[i].res('r' + i)
    }
    out.serial = { seen, results: await p, peak: ctl.peak }
  }

  // 6. an empty fan-out under a bound.
  {
    const ctl = controlled(0)
    const { run } = await load(2, ctl, 'return laneParallel(ctl.thunks)')
    out.empty = await run()
  }

  // 7. validation: rejected before any agent is dispatched.
  const bad = [0, -1, 1.5, '3', '', null, true, false, NaN, Infinity, -Infinity, [3], {}, 'abc']
  out.bad = []
  for (const v of bad) {
    const ctl = controlled(0)
    const { run, agentCalls } = await load(v, ctl, 'return "ran"')
    let error = null, value = null
    try { value = await run() } catch (e) { error = String(e && e.message) }
    out.bad.push({ shown: Number.isNaN(v) ? 'NaN' : JSON.stringify(v), error, value, agents: agentCalls.length })
  }
  out.good = []
  for (const v of [1, 2, 16, 1000000]) {
    const ctl = controlled(0)
    const { run } = await load(v, ctl, 'return "ran"')
    let error = null, value = null
    try { value = await run() } catch (e) { error = String(e && e.message) }
    out.good.push({ v, error, value })
  }
  return out
}

main().then((o) => console.log(JSON.stringify(o)))
  .catch((e) => { console.log(JSON.stringify({ harnessError: String(e && e.stack) })) })
"""


@pytest.fixture(scope="module")
def pool(tmp_path_factory) -> dict:
    d = tmp_path_factory.mktemp("pool")
    (d / "chunk.js").write_text(gen.LANE_ROUTE_CHUNK, encoding="utf-8")
    (d / "harness.js").write_text(POOL_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(d / "harness.js"), str(d / "chunk.js")],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "harnessError" not in out, out
    return out


class TestWorkerPool:
    def test_a_freed_slot_takes_the_next_item_at_once(self, pool):
        r = pool["refill"]
        assert r["s0"] == [0, 1, 2]
        # item 1 finished while 0 and 2 still ran: item 3 starts immediately.
        assert r["s1"] == [0, 1, 2, 3]
        assert r["s2"] == [0, 1, 2, 3, 4]
        assert r["s3"] == [0, 1, 2, 3, 4, 5]
        assert r["s4"] == [0, 1, 2, 3, 4, 5, 6]

    def test_peak_in_flight_never_exceeds_the_bound(self, pool):
        assert pool["refill"]["peak"] == 3
        assert pool["failures"]["peak"] <= 2
        assert pool["serial"]["peak"] == 1

    def test_results_keep_input_order_whatever_the_completion_order(self, pool):
        assert pool["refill"]["results"] == [
            "r0", "r1", "r2", "r3", None, "r5", "r6", "r7"]

    def test_the_pool_runs_on_n_workers_not_one_call_per_item(self, pool):
        assert pool["refill"]["parallelCalls"] == [3]

    def test_a_failed_item_is_null_in_place_and_the_rest_complete(self, pool):
        f = pool["failures"]
        assert f["results"] == ["r0", None, "r2", None, "r4"]
        assert sorted(f["started"]) == [0, 1, 2, 3, 4]

    def test_bound_of_one_is_serial_in_order(self, pool):
        s = pool["serial"]
        assert s["seen"] == [[0], [0, 1], [0, 1, 2], [0, 1, 2, 3]]
        assert s["results"] == ["r0", "r1", "r2", "r3"]

    def test_unset_is_the_unbounded_parallel_call(self, pool):
        u = pool["unset"]
        assert u["startedAtOnce"] == 8
        assert u["parallelCalls"] == [8]
        assert u["results"] == [f"r{i}" for i in range(8)]
        assert not any("in flight" in line for line in u["logs"])

    def test_no_more_items_than_the_bound_is_the_plain_parallel_call(self, pool):
        f = pool["fewer"]
        assert f["parallelCalls"] == [3]
        assert f["results"] == ["r0", "r1", "r2"]

    def test_an_empty_fan_out_returns_an_empty_list(self, pool):
        assert pool["empty"] == []

    def test_a_set_bound_is_disclosed_in_the_log(self, pool):
        assert any("at most 3 agent(s) in flight" in line for line in pool["refill"]["logs"])


class TestValidation:
    def test_every_invalid_value_throws_before_any_agent_launches(self, pool):
        assert len(pool["bad"]) == 14
        for row in pool["bad"]:
            assert row["error"] is not None, row
            assert "args.maxConcurrent must be a positive integer" in row["error"], row
            assert row["value"] is None and row["agents"] == 0, row

    def test_the_error_names_the_offending_value(self, pool):
        by_shown = {row["shown"]: row["error"] for row in pool["bad"]}
        assert "got 0." in by_shown["0"]
        assert 'got "3".' in by_shown['"3"']
        assert "got 1.5." in by_shown["1.5"]
        assert "got null." in by_shown["null"]

    def test_positive_integers_are_accepted(self, pool):
        assert [g["error"] for g in pool["good"]] == [None] * 4
        assert [g["value"] for g in pool["good"]] == ["ran"] * 4


# --- shipped scripts: the bound reaches every lane ---------------------------

SCRIPT_HARNESS = r"""
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace('export const meta', 'const meta')
const spec = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'))
let inflight = 0, peak = 0, calls = 0
const agent = async () => {
  calls++; inflight++; peak = Math.max(peak, inflight)
  await new Promise((r) => setTimeout(r, 15))
  inflight--
  return spec.reply
}
const parallel = (thunks) => Promise.all(thunks.map((t) => Promise.resolve().then(t).catch(() => null)))
const fn = new Function('args', 'agent', 'parallel', 'phase', 'log',
  '"use strict"; return (async () => {\n' + src + '\n})()')
Promise.resolve()
  .then(() => fn(spec.args, agent, parallel, () => {}, () => {}))
  .then((result) => console.log(JSON.stringify({ calls, peak, error: null })))
  .catch((e) => console.log(JSON.stringify({ calls, peak, error: String((e && e.message) || e) })))
"""

FANOUT = 6


def _scale(tmp_path: Path, lane: str, n: int) -> tuple[dict, object]:
    """The lane's one-item arguments widened to n independent items."""
    args, reply = route._args_for(tmp_path, lane)
    if lane in route.DETECT_LANES:
        base = args["files"][0]
        files = []
        for i in range(n):
            ext = "CLAUDE.md" if lane == "claude-md-detect.js" else "doc.md"
            subject = tmp_path / f"s{i}" / ext
            subject.parent.mkdir()
            subject.write_text("# Title\n\nBody.\n", encoding="utf-8")
            files.append({**base, "path": str(subject)})
        args["files"] = files
    elif lane in route.REMEDIATE_LANES:
        base = args["perFile"][0]
        key = "file" if lane == "references-remediate.js" else "path"
        args["perFile"] = [{**base, key: f"/x/doc{i}.md"} for i in range(n)]
    elif lane == "references-classify.js":
        base = args["files"][0]
        args["files"] = [{**base, "file": f"/x/doc{i}.md"} for i in range(n)]
    elif lane == "coverage-detect.js":
        base = args["subjects"][0]
        args["subjects"] = [
            {**base, "root": f"src{i}", "codeFiles": [f"src{i}/a.py"]} for i in range(n)]
        args["batchSize"] = 1
    elif lane == "claude-md-generate.js":
        args["subjects"] = [
            {"root": f"src{i}", "compositionOnly": True} for i in range(n)]
    else:
        raise AssertionError(lane)
    args["laneModels"] = SONNET_LOW
    return args, reply


def _run_script(tmp_path: Path, lane: str, max_concurrent) -> dict:
    args, reply = _scale(tmp_path, lane, FANOUT)
    if max_concurrent is not None:
        args["maxConcurrent"] = max_concurrent
    (tmp_path / "harness.js").write_text(SCRIPT_HARNESS, encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"args": args, "reply": reply}), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(tmp_path / "harness.js"), str(WORKFLOW_DIR / lane), str(spec)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("lane", route.ALL_LANES)
def test_unbounded_lane_runs_every_item_at_once(tmp_path: Path, lane: str) -> None:
    """The instrument sees real concurrency, so the bounded assertion below is
    not satisfied by a harness that never overlaps agents."""
    out = _run_script(tmp_path, lane, None)
    assert out["error"] is None, out
    assert out["calls"] == FANOUT and out["peak"] == FANOUT, out


@pytest.mark.parametrize("lane", route.ALL_LANES)
def test_bounded_lane_never_exceeds_max_concurrent(tmp_path: Path, lane: str) -> None:
    out = _run_script(tmp_path, lane, 2)
    assert out["error"] is None, out
    assert out["calls"] == FANOUT, out
    assert out["peak"] == 2, out


@pytest.mark.parametrize("lane", route.ALL_LANES)
@pytest.mark.parametrize("value", [0, -1, 1.5, "2"])
def test_invalid_bound_throws_before_any_agent_in_every_lane(
        tmp_path: Path, lane: str, value) -> None:
    out = _run_script(tmp_path, lane, value)
    assert out["calls"] == 0, out
    assert "args.maxConcurrent must be a positive integer" in (out["error"] or ""), out


# --- the guard: no script may fan out around the pool ------------------------

class TestEveryFanOutGoesThroughThePool:
    def test_no_workflow_script_has_a_bare_fan_out(self):
        assert gen.check_fanout_pool() == []

    def test_every_workflow_script_calls_lane_parallel(self):
        scripts = sorted(WORKFLOW_DIR.glob("*.js"))
        assert len(scripts) == 10
        for path in scripts:
            body = gen._blank_comments_and_chunk(path.read_text(encoding="utf-8"))
            assert gen._POOL_CALL_RE.search(body), path.name

    def test_the_guard_is_part_of_the_shared_chunk_check(self):
        assert gen.check_shared_chunks() == []

    @pytest.mark.parametrize("bare", [
        "const r = await parallel(xs.map((x) => () => f(x)))",
        "const r = await pipeline(xs, stage)",
        "const r = await Promise.all(xs.map(f))",
        "const r = await Promise . allSettled(xs.map(f))",
    ])
    def test_a_bare_fan_out_is_caught(self, tmp_path, bare):
        path = tmp_path / "x-detect.js"
        path.write_text(
            gen.LANE_ROUTE_CHUNK + "const a = await laneParallel(ts)\n" + bare + "\n",
            encoding="utf-8")
        problems = gen.bare_fanout_problems(path)
        assert len(problems) == 1 and "bare fan-out" in problems[0], problems

    def test_a_script_with_no_pool_call_is_caught(self, tmp_path):
        path = tmp_path / "x-detect.js"
        path.write_text(gen.LANE_ROUTE_CHUNK + "const a = 1\n", encoding="utf-8")
        problems = gen.bare_fanout_problems(path)
        assert problems == [f"{path}: no fan-out routed through laneParallel"]

    def test_prose_and_the_chunk_itself_are_not_flagged(self, tmp_path):
        path = tmp_path / "x-detect.js"
        path.write_text(
            "// a bare parallel() is discussed here\n"
            + gen.LANE_ROUTE_CHUNK
            + "const a = await laneParallel(ts)\n",
            encoding="utf-8")
        assert gen.bare_fanout_problems(path) == []

    def test_every_script_documents_the_argument(self):
        for path in sorted(WORKFLOW_DIR.glob("*.js")):
            assert "//   maxConcurrent: integer|undefined" in path.read_text(
                encoding="utf-8"), path.name
