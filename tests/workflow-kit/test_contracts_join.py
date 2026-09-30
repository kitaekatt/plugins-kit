"""The typed-contracts join: compile, execute under node + bash, read the evidence.

A compiled contract workflow runs under `node` exactly as the native Workflow
tool would run it, with `agent()` stubbed to do the executor's job: run the
node's COMMAND in `bash` and return the metadata object. Nothing between the
compiled script and the files on disk is faked except the two things that must
be: the OpenAI-compatible client BELOW ``OpenRouterBackend.complete`` (so
prepare_contract and finalize_contract really run) and the reachability probe.
Those are installed by shim scripts standing in for the plugin's ``scripts/``
directory, which run the real ``openrouter_run.py`` / ``check_artifact.py`` /
``wordcount.py`` through ``runpy``.

The evidence read back is what a consumer of the workflow would see: verdict
files (``workflow-kit.artifact-verdict/v1``), v3 execution-event streams
validated with ``bootstrap_lib.execution_event``, the compiled output files,
and which ``agent()`` calls were made at all.

Skipped when ``node`` or ``bash`` is absent (this host has both; the WSL
launcher is not accepted as ``bash``).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from wk_testlib import EXAMPLES, PLUGIN_ROOT

from bootstrap_lib import execution_event as ee
from workflow_kit_lib import compile_doc, load_workflow

_LSK_LIB = PLUGIN_ROOT.parent / "llm-scripting-kit" / "lib"
_BOOTSTRAP = PLUGIN_ROOT.parent / "bootstrap"
_TYPED = EXAMPLES / "typed-contracts.workflow.yaml"


def _find_bash():
    found = shutil.which("bash")
    # WSL's launcher (System32) cannot see this filesystem the same way.
    if found and "System32" not in found and "WindowsApps" not in found:
        return found
    return None


NODE = shutil.which("node")
BASH = _find_bash()
pytestmark = pytest.mark.skipif(
    NODE is None or BASH is None, reason="node and bash are required"
)

@pytest.fixture(autouse=True)
def lsk_path(monkeypatch):
    """llm_scripting_kit for the in-process compile (schema digests)."""
    monkeypatch.syspath_prepend(str(_LSK_LIB))


# --------------------------------------------------------------------------- #
# The node harness: compiled script + a stub agent() that is the executor
# --------------------------------------------------------------------------- #
_HARNESS = r"""
const fs = require('fs')
const path = require('path')
const cp = require('child_process')
const cfg = JSON.parse(process.env.WK_HARNESS)
const calls = []
globalThis.agent = async (prompt, opts) => {
  const command = /\nCOMMAND:\n([\s\S]*)\n$/.exec(prompt)
  if (!command) {
    calls.push({ kind: 'agent', prompt })
    return { exit_code: 0, path: 'agent-result', bytes: 0 }
  }
  const out = /\nOUT=([^\n]*)\n/.exec(prompt)[1]
  // the executor's step 1 (agents/workflow-kit-agent.md): the parent of OUT exists
  fs.mkdirSync(path.dirname(path.join(cfg.cwd, out)), { recursive: true })
  const r = cp.spawnSync(cfg.bash, ['-c', command[1]], {
    cwd: cfg.cwd, env: process.env, encoding: 'utf8',
  })
  calls.push({ kind: 'node', command: command[1], exit: r.status, stderr: r.stderr })
  let bytes = 0
  try { bytes = fs.statSync(path.join(cfg.cwd, out)).size } catch (e) {}
  return { exit_code: r.status, path: out, bytes }
}
globalThis.parallel = (thunks) => Promise.all(thunks.map((t) => t()))
const src = fs.readFileSync(cfg.script, 'utf8').replace('export const meta', 'const meta')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
AsyncFunction('args', src)(cfg.args).then(
  (r) => console.log(JSON.stringify({ calls, result: r })),
  (e) => console.log(JSON.stringify({ calls, error: String(e && e.message) })),
)
"""

# What the `scripts/` shims run. The openrouter shim fakes only what is BELOW
# OpenRouterBackend.complete (the client) and the reachability probe.
_SHIM_COMMON = """
import os, runpy, sys
for _p in ({paths!r}):
    sys.path.insert(0, _p)
"""

_SHIM_OPENROUTER = _SHIM_COMMON + """
from pathlib import Path
from types import SimpleNamespace
home = Path(os.environ["WK_FAKE_HOME"])
home.mkdir(exist_ok=True)
os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
os.environ.pop("MODEL_ENDPOINTS_REGISTRY", None)
from llm_scripting_kit import completion, declaration
from llm_scripting_kit.reachability import STATUS_REACHABLE, Reachability
declaration.check_many = lambda entries, **_kw: {{
    n: Reachability(STATUS_REACHABLE, "models-probe", "x") for n in entries
}}


class _Completions:
    def create(self, **kwargs):
        with open(os.environ["WK_FAKE_LOG"], "a", encoding="ascii") as fh:
            fh.write(str(kwargs["messages"][0]["content"][0]["text"]).replace("\\n", " ") + "\\n")
        message = SimpleNamespace(content=os.environ["WK_FAKE_REPLY"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None
        )


_client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
completion.OpenRouterBackend._ensure_client = lambda self: _client
runpy.run_path({real!r}, run_name="__main__")
"""

_SHIM_PLAIN = _SHIM_COMMON + """
runpy.run_path({real!r}, run_name="__main__")
"""


def _make_plugin_root(tmp_path):
    root = tmp_path / "plugin"
    paths = [str(_LSK_LIB), str(_BOOTSTRAP), str(PLUGIN_ROOT)]
    real = PLUGIN_ROOT
    shims = {
        "scripts/openrouter_run.py": (_SHIM_OPENROUTER, real / "scripts" / "openrouter_run.py"),
        "scripts/check_artifact.py": (_SHIM_PLAIN, real / "scripts" / "check_artifact.py"),
        "examples/scripts/wordcount.py": (
            _SHIM_PLAIN, real / "examples" / "scripts" / "wordcount.py",
        ),
    }
    for rel, (template, target) in shims.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            template.format(paths=paths, real=str(target)), encoding="utf-8"
        )
    return root


class Run:
    """One execution of a compiled workflow; read its evidence afterwards."""

    def __init__(self, tmp_path, report, calls):
        self.tmp = tmp_path
        self.report = report
        self.calls = calls
        self.error = report.get("error")
        self.agent_calls = [c for c in calls if c["kind"] == "agent"]
        self.node_calls = [c for c in calls if c["kind"] == "node"]
        self.fake_log = tmp_path / "transport.log"

    def path(self, name, step="count"):
        return self.tmp / ".workflow-kit" / "r1" / f"{step}.{name}"

    def verdict(self, step):
        p = self.path("contract.json", step)
        return json.loads(p.read_text(encoding="ascii")) if p.exists() else None

    def stream(self, step):
        p = self.path("events.jsonl", step)
        if not p.exists():
            return None
        events = ee.read_jsonl(p)
        assert ee.validate_stream(events) == events
        return events

    def transport_requests(self):
        return self.fake_log.read_text(encoding="ascii").splitlines() if self.fake_log.exists() else []


def _run(tmp_path, workflow_text, *, reply="", source_text="alpha beta\ngamma\n", preseed=None):
    wf = tmp_path / "wf.workflow.yaml"
    wf.write_text(workflow_text, encoding="utf-8")
    script = tmp_path / "compiled.js"
    script.write_text(compile_doc(load_workflow(wf)), encoding="utf-8")
    harness = tmp_path / "harness.cjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    source = tmp_path / "source.txt"
    source.write_text(source_text, encoding="utf-8")
    root = _make_plugin_root(tmp_path)
    if preseed:
        preseed(tmp_path)
    cfg = {
        "bash": BASH,
        "cwd": str(tmp_path),
        "script": str(script),
        "args": {
            "runId": "r1",
            "source": "source.txt",
            "pluginRoot": root.as_posix(),
            "workflowKitVenvPython": Path(sys.executable).as_posix(),
        },
    }
    env = {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV")}
    env.update(
        WK_HARNESS=json.dumps(cfg),
        WK_FAKE_REPLY=reply,
        WK_FAKE_LOG=str(tmp_path / "transport.log"),
        WK_FAKE_HOME=str(tmp_path / "home"),
    )
    proc = subprocess.run(
        [NODE, str(harness)], capture_output=True, text=True, env=env, timeout=240
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    return Run(tmp_path, report, report["calls"])


def _typed_text():
    return _TYPED.read_text(encoding="utf-8")


def _contract_events(stream):
    return [e for e in stream if e["event"] == "contract"]


# --------------------------------------------------------------------------- #
# the shipped typed example
# --------------------------------------------------------------------------- #
def test_typed_example_end_to_end(tmp_path):
    run = _run(tmp_path, _typed_text(), reply="essay")
    assert run.error is None, (run.error, run.node_calls)

    # both providers ran, then the consumer, in that order
    assert [c["kind"] for c in run.calls] == ["node", "node", "agent"]
    assert [c["exit"] for c in run.node_calls] == [0, 0]

    # the script provider: the artifact on disk, judged against the schema
    stats = json.loads(run.path("out").read_text(encoding="utf-8"))
    assert stats == {"lines": 2, "words": 3, "chars": 17}
    v = run.verdict("count")
    assert (v["schema"], v["artifact"], v["kind"], v["verdict"]) == (
        "workflow-kit.artifact-verdict/v1", "doc_stats", "schema", "satisfied",
    )
    assert v["bytes"] == run.path("out").stat().st_size and len(v["sha256"]) == 64
    assert v["errors"] == [] and v["errors_truncated"] is False

    # the openrouter provider: opaque file, reply text, through the real seam
    assert run.path("out", "classify").read_text(encoding="utf-8") == "essay"
    c = run.verdict("classify")
    assert (c["artifact"], c["kind"], c["verdict"]) == ("doc_class", "opaque-file", "satisfied")
    assert len(run.transport_requests()) == 1

    # a valid v3 stream per node: the checker records one contract event and
    # no terminal; the runner records contract BEFORE terminal
    count_stream = run.stream("count")
    assert [e["event"] for e in count_stream] == ["contract"]
    assert count_stream[0]["schema"] == ee.SCHEMA_V3
    assert count_stream[0]["payload"] == {
        "artifact": "doc_stats", "kind": "schema", "verdict": "satisfied",
        "schema_digest": v["schema_digest"],
    }
    classify_stream = run.stream("classify")
    names = [e["event"] for e in classify_stream]
    assert names.index("contract") < names.index("terminal") == len(names) - 1
    assert {e["schema"] for e in classify_stream} == {ee.SCHEMA_V3}
    assert _contract_events(classify_stream)[0]["payload"] == {
        "artifact": "doc_class", "kind": "opaque-file", "verdict": "satisfied",
    }
    assert {e["identity"]["unit_id"] for e in count_stream} == {"count"}
    assert {e["identity"]["unit_id"] for e in classify_stream} == {"classify"}

    # the consumer prompt carries the providers' reported paths
    prompt = run.agent_calls[0]["prompt"]
    assert "./.workflow-kit/r1/count.out" in prompt
    assert "./.workflow-kit/r1/classify.out" in prompt


def test_violating_provider_stops_before_consumer(tmp_path):
    # the wordcount output has integer `lines`; a schema demanding a string violates it
    text = _typed_text().replace(
        "lines: { type: integer, minimum: 0 }", "lines: { type: string }"
    )
    assert text != _typed_text()
    run = _run(tmp_path, text, reply="essay")

    assert run.error.startswith("workflow-kit: step count did not provide artifact doc_stats")
    assert "exit_code 1" in run.error and "count.contract.json" in run.error
    # the only agent() call is the failed provider: no consumer, no later provider
    assert [c["kind"] for c in run.calls] == ["node"]
    assert run.agent_calls == []
    assert not run.path("out", "classify").exists()
    assert run.transport_requests() == []

    v = run.verdict("count")
    assert (v["kind"], v["verdict"]) == ("schema", "violated")
    assert ["/lines", "type"] in v["errors"]
    # the violating file is still on disk and is exactly what was judged
    assert v["bytes"] == run.path("out").stat().st_size

    stream = run.stream("count")
    assert [e["event"] for e in stream] == ["contract"]
    assert stream[0]["payload"]["verdict"] == "violated"
    assert stream[0]["payload"]["error_count"] == len(v["errors"])


# --------------------------------------------------------------------------- #
# an openrouter schema provider, satisfied and violated
# --------------------------------------------------------------------------- #
_OPENROUTER_WF = """
name: or-join
description: an openrouter provider feeds a consumer
inputs:
  source: { type: string, description: "input path" }
schemas:
  verdict:
    type: object
    required: [label]
    additionalProperties: false
    properties:
      label: { type: string }
steps:
  - id: classify
    openrouter: { prompt_file: "{{ inputs.source }}", cheap: true }
    provides:
      doc_label: { schema: verdict }
  - id: reconcile
    requires:
      doc_label: { schema: verdict }
    agent:
      prompt: "Read {{ artifacts.doc_label }}"
output: "{{ steps.reconcile }}"
"""


def test_openrouter_provider_satisfied_feeds_the_consumer(tmp_path):
    run = _run(tmp_path, _OPENROUTER_WF, reply='  {"label":  "essay"} ')
    assert run.error is None, (run.error, run.node_calls)
    assert [c["kind"] for c in run.calls] == ["node", "agent"]
    # $OUT holds the VALIDATED value, not the raw reply text
    assert run.path("out", "classify").read_bytes() == b'{"label": "essay"}'
    v = run.verdict("classify")
    assert (v["kind"], v["verdict"]) == ("schema", "satisfied")
    assert len(v["schema_digest"]) == 64
    # the schema instruction really reached the transport
    assert len(run.transport_requests()) == 1
    stream = run.stream("classify")
    names = [e["event"] for e in stream]
    assert names.index("contract") < names.index("terminal") == len(names) - 1
    assert "./.workflow-kit/r1/classify.out" in run.agent_calls[0]["prompt"]


@pytest.mark.parametrize("reply", ['{"label": 3}', "not json at all", '{"label": "x", "n": 1}'])
def test_openrouter_provider_violation_stops_before_consumer(tmp_path, reply):
    run = _run(tmp_path, _OPENROUTER_WF, reply=reply)
    assert run.error.startswith("workflow-kit: step classify did not provide artifact doc_label")
    assert run.agent_calls == []  # the consumer never ran
    assert len(run.transport_requests()) == 1  # the transport really was called
    assert not run.path("out", "classify").exists()  # $OUT is not written on a violation
    v = run.verdict("classify")
    assert (v["kind"], v["verdict"]) == ("schema", "violated")
    stream = run.stream("classify")
    names = [e["event"] for e in stream]
    assert names.index("contract") < names.index("terminal") == len(names) - 1
    contract = _contract_events(stream)[0]["payload"]
    assert contract["verdict"] == "violated" and contract["error_count"] >= 1


# --------------------------------------------------------------------------- #
# a script provider that exits 7 (rev2 S2), through the real executor shell
# --------------------------------------------------------------------------- #
_EXIT7_WF = """
name: exit7
description: a failing provider
steps:
  - id: count
    script: { command: "echo partial; exit 7" }
    provides:
      doc: { type: opaque-file }
  - id: reconcile
    requires:
      doc: { type: opaque-file }
    agent:
      prompt: "Read {{ artifacts.doc }}"
output: "{{ steps.reconcile }}"
"""


def _seed_satisfied(tmp_path):
    """A previous execution's evidence: a `satisfied` verdict and events file."""
    directory = tmp_path / ".workflow-kit" / "r1"
    directory.mkdir(parents=True)
    (directory / "count.contract.json").write_text('{"verdict": "satisfied"}', encoding="ascii")
    (directory / "count.events.jsonl").write_text("stale\n", encoding="ascii")


def test_exit_7_provider_records_missing_and_stops_before_consumer(tmp_path):
    run = _run(tmp_path, _EXIT7_WF, preseed=_seed_satisfied)
    assert run.error.startswith("workflow-kit: step count did not provide artifact doc")
    assert "exit_code 7" in run.error
    assert [c["exit"] for c in run.node_calls] == [7]  # the checker chain exits 7
    assert run.agent_calls == []
    # the checker ran despite `exit 7`, and replaced the stale evidence
    v = run.verdict("count")
    assert (v["kind"], v["verdict"]) == ("opaque-file", "missing")
    stream = run.stream("count")  # the stale "stale\n" file is gone, the new one valid
    assert [e["event"] for e in stream] == ["contract"]
    assert stream[0]["payload"]["verdict"] == "missing"
    assert "stale" not in run.path("events.jsonl").read_text(encoding="ascii")
