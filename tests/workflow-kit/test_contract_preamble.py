"""preamble-contracts.js, executed: the provider helpers under `node`, their shell under `bash`.

`node` builds each command exactly as a compiled workflow does (a stub
`agent()` records the executor prompt), and `bash` runs the script-provider
chain with the real `scripts/check_artifact.py`. Both are skipped when the
binary is absent; this host has both. The static POSIX-construct test needs
neither.
"""

import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from wk_testlib import PLUGIN_ROOT

from workflow_kit_lib import compile_doc, load_workflow

_REFS = PLUGIN_ROOT / "skills" / "workflow-kit" / "references"
_PREAMBLE = _REFS / "preamble.js"
_CONTRACTS = _REFS / "preamble-contracts.js"
_CHECKER = PLUGIN_ROOT / "scripts" / "check_artifact.py"
_RUNNER = PLUGIN_ROOT / "scripts" / "openrouter_run.py"
_LSK_LIB = PLUGIN_ROOT.parent / "llm-scripting-kit" / "lib"


def _find_bash():
    found = shutil.which("bash")
    # WSL's launcher (System32) cannot see this filesystem the same way.
    if found and "System32" not in found and "WindowsApps" not in found:
        return found
    return None


NODE = shutil.which("node")
BASH = _find_bash()
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
needs_bash = pytest.mark.skipif(BASH is None or NODE is None, reason="bash and node required")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


checker = _load("workflow_kit_check_artifact_tc2", _CHECKER)
runner = _load("workflow_kit_openrouter_run_tc2", _RUNNER)


@pytest.fixture
def lsk_path(monkeypatch):
    monkeypatch.syspath_prepend(str(_LSK_LIB))


# --------------------------------------------------------------------------- #
# node harnesses
# --------------------------------------------------------------------------- #
_STUB = r"""
const __calls = []
function agent(prompt, opts) {
  __calls.push(prompt)
  return { exit_code: 0, path: 'out', bytes: 0 }
}
"""


def _node_eval(tmp_path, body):
    """Run `body` after preamble.js + preamble-contracts.js; return its printed JSON."""
    script = (
        _STUB + "\n" + _PREAMBLE.read_text(encoding="utf-8") + "\n"
        + _CONTRACTS.read_text(encoding="utf-8") + "\n" + body + "\n"
    )
    path = tmp_path / "harness.cjs"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_COMPILED_HARNESS = r"""
const fs = require('fs')
const calls = []
globalThis.agent = async (prompt, opts) => {
  calls.push(prompt)
  const m = /\nOUT=([^\n]*)\n/.exec(prompt)
  return { exit_code: 0, path: m ? m[1] : 'agent-result', bytes: 0 }
}
globalThis.parallel = (thunks) => Promise.all(thunks.map((t) => t()))
const src = fs.readFileSync(process.argv[2], 'utf8').replace('export const meta', 'const meta')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
AsyncFunction('args', src)(process.argv[3]).then(
  (r) => console.log(JSON.stringify({ calls, result: r })),
  (e) => console.log(JSON.stringify({ calls, error: String(e && e.message) })),
)
"""

_ARGS = {"runId": "r1", "pluginRoot": "/wk", "workflowKitVenvPython": "/venv/python"}


def _run_compiled(tmp_path, yaml_text):
    """Compile, run the script under node with a stub agent(); return the node COMMANDs."""
    wf = tmp_path / "wf.workflow.yaml"
    wf.write_text(yaml_text, encoding="utf-8")
    js = tmp_path / "compiled.js"
    js.write_text(compile_doc(load_workflow(wf)), encoding="utf-8")
    harness = tmp_path / "run.cjs"
    harness.write_text(_COMPILED_HARNESS, encoding="utf-8")
    proc = subprocess.run([NODE, str(harness), str(js), json.dumps(_ARGS)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert "error" not in report, report["error"]
    return [c.split("COMMAND:\n", 1)[1][:-1] for c in report["calls"] if "COMMAND:\n" in c]


def _argv_after_runner(line):
    tokens = shlex.split(line)
    assert tokens[0] == "/venv/python"
    return tokens[2:]


_SCHEMA_WF = """
name: t
description: x
schemas:
  s:
    type: object
    required: [n]
    additionalProperties: false
    properties:
      n: { type: number, minimum: 1.0 }
steps:
  - id: count
    script: { command: "echo '{\\"n\\": 2}'" }
    provides:
      doc: { schema: s }
  - id: classify
    openrouter: { prompt_file: p.txt, system: "It's one word." }
    provides:
      label: { schema: s }
"""


# --------------------------------------------------------------------------- #
# static
# --------------------------------------------------------------------------- #
def _defined_names(text):
    """Top-level function and const/let/var names a script defines."""
    pattern = r"(?m)^(?:function\s+(\w+)|(?:const|let|var)\s+(\w+))"
    return {a or b for a, b in re.findall(pattern, text)}


def test_contract_preamble_redefines_no_preamble_symbol():
    base = _defined_names(_PREAMBLE.read_text(encoding="utf-8"))
    extra = _defined_names(_CONTRACTS.read_text(encoding="utf-8"))
    assert {"shq", "wkNode", "wkScript", "wkOpenRouter"} <= base
    assert extra == {"wkProviderFlags", "wkScriptProvided", "wkProvided"}
    assert not base & extra


def test_contract_preamble_is_ascii():
    _CONTRACTS.read_bytes().decode("ascii")


def _template_strings():
    text = _CONTRACTS.read_text(encoding="utf-8")
    body = text[text.index("function wkScriptProvided("):text.index("function wkProvided(")]
    return re.findall(r"'((?:[^'\\]|\\.)*)'", body)


def test_provider_command_template_is_posix_only():
    shell = "\n".join(_template_strings())
    assert "case $- in" in shell and "wk_rc=$?" in shell  # the template was found
    for construct in ("[[", "function ", "local ", "$'", "&>", "declare ", "mapfile",
                      "readarray", "<<<", "set -o pipefail", "source "):
        assert construct not in shell, construct
    assert not re.search(r"\w=\(", shell), "array assignment"


# --------------------------------------------------------------------------- #
# the guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", ["one", "each", "non_object"])
@needs_node
def test_guard_throws_on_nonzero_exit_code(tmp_path, case):
    results = {
        "one": ("{ exit_code: 1, path: 'o' }", "'v.json'"),
        "each": ("[{ exit_code: 0 }, { exit_code: 3 }]", "(i) => 'v.' + i + '.json'"),
        "non_object": ("null", "'v.json'"),
    }
    value, verdict = results[case]
    body = f"""
let thrown = null
try {{ wkProvided({value}, 'count', 'doc_stats', {verdict}) }} catch (e) {{ thrown = e.message }}
let passed = wkProvided({{ exit_code: 0 }}, 'count', 'doc_stats', 'v.json') !== undefined
let passedEach = Array.isArray(wkProvided([{{ exit_code: 0 }}], 'count', 'doc_stats', 'v.json'))
console.log(JSON.stringify({{ thrown, passed, passedEach }}))
"""
    report = _node_eval(tmp_path, body)
    assert report["passed"] and report["passedEach"]
    thrown = report["thrown"]
    assert thrown is not None, "the guard returned instead of throwing"
    assert thrown.startswith("workflow-kit: step count did not provide artifact doc_stats (")
    expected = {
        "one": "(exit_code 1); see v.json",
        "each": "(item 1, exit_code 3); see v.1.json",
        "non_object": "(exit_code none); see v.json",
    }[case]
    assert thrown.endswith(expected)


@needs_node
def test_guard_stops_the_compiled_script_before_the_consumer(tmp_path):
    wf = tmp_path / "wf.workflow.yaml"
    wf.write_text(
        "name: t\ndescription: x\nsteps:\n  - id: p\n    script: { command: 'false' }\n"
        "    provides:\n      doc: { type: opaque-file }\n  - id: c\n    requires:\n"
        "      doc: { type: opaque-file }\n    agent: { prompt: 'read {{ artifacts.doc }}' }\n",
        encoding="utf-8",
    )
    js = tmp_path / "compiled.js"
    js.write_text(compile_doc(load_workflow(wf)), encoding="utf-8")
    harness = tmp_path / "run.cjs"
    harness.write_text(_COMPILED_HARNESS.replace("exit_code: 0", "exit_code: m ? 1 : 0"),
                       encoding="utf-8")
    proc = subprocess.run([NODE, str(harness), str(js), json.dumps(_ARGS)],
                          capture_output=True, text=True, timeout=60)
    report = json.loads(proc.stdout)
    assert report["error"].startswith("workflow-kit: step p did not provide artifact doc")
    assert len(report["calls"]) == 1  # the consumer agent() was never called


# --------------------------------------------------------------------------- #
# the compiled commands parse (rev3 R3)
# --------------------------------------------------------------------------- #
@needs_node
def test_checker_compiled_command_parses(tmp_path, lsk_path):
    commands = _run_compiled(tmp_path, _SCHEMA_WF)
    script = next(c for c in commands if "check_artifact.py" in c)
    last = script.splitlines()[-1]
    argv = [a if a != "$wk_rc" else "0" for a in _argv_after_runner(last)]
    assert shlex.split(last)[1] == "/wk/scripts/check_artifact.py"
    ns = checker._parser().parse_args(argv)
    assert ns.artifact == "doc" and ns.kind == "schema"
    assert ns.in_path == "./.workflow-kit/r1/count.out"
    assert ns.verdict == "./.workflow-kit/r1/count.contract.json"
    assert ns.command_exit == 0
    assert json.loads(ns.schema)["properties"]["n"]["minimum"] == 1.0
    assert re.fullmatch(r"[0-9a-f]{64}", ns.schema_digest)


@needs_node
def test_openrouter_provider_command_parses(tmp_path, lsk_path, monkeypatch, capsys):
    commands = _run_compiled(tmp_path, _SCHEMA_WF)
    line = next(c for c in commands if "openrouter_run.py" in c)
    argv = _argv_after_runner(line)
    seen = {}
    real = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        ns = real(self, args, namespace)
        seen["ns"] = ns
        return ns

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    monkeypatch.chdir(tmp_path)
    # Parsing and the flag-pairing checks pass (a usage error is SystemExit(2));
    # the run then stops at the blocked llm_scripting_kit probe.
    assert runner.main(argv) == 2
    assert "llm_scripting_kit not importable" in capsys.readouterr().err
    ns = seen["ns"]
    assert (ns.provides, ns.kind) == ("label", "schema")
    assert ns.verdict == "./.workflow-kit/r1/classify.contract.json"
    assert ns.out == "./.workflow-kit/r1/classify.out"
    assert ns.system == "It's one word."
    assert ns.events == "./.workflow-kit/r1/classify.events.jsonl"
    assert re.fullmatch(r"[0-9a-f]{64}", ns.schema_digest)


@needs_node
def test_schema_literal_digest_round_trips(tmp_path, lsk_path):
    from llm_scripting_kit.completion import POLICY_VALIDATED_RESULT, OutputContract

    commands = _run_compiled(tmp_path, _SCHEMA_WF)
    checked = 0
    for command in commands:
        tokens = shlex.split(command.splitlines()[-1])
        schema = tokens[tokens.index("--schema") + 1]
        digest = tokens[tokens.index("--schema-digest") + 1]
        rebuilt = OutputContract(id="x", policy=POLICY_VALIDATED_RESULT, schema=json.loads(schema))
        assert rebuilt.schema_digest == digest
        assert "1.0" in schema  # the float survived as Python wrote it
        checked += 1
    assert checked == 2


# --------------------------------------------------------------------------- #
# the script-provider shell chain under bash (rev2 S2, rev3 R1)
# --------------------------------------------------------------------------- #
def _chain(tmp_path, command):
    """The script-provider command wkScriptProvided builds for `command` (opaque kind)."""
    run = f'"{Path(sys.executable).as_posix()}" "{_CHECKER.as_posix()}"'
    body = (
        "const r = wkScriptProvided(" + json.dumps(command) + ", 'out.txt', "
        "{ runner: " + json.dumps(run) + ", artifact: 'doc', kind: 'opaque-file', "
        "verdict: 'v/verdict.json' }, {})\n"
        "console.log(JSON.stringify({ cmd: __calls[0].split('COMMAND:\\n')[1].slice(0, -1) }))"
    )
    return _node_eval(tmp_path, body)["cmd"]


def _bash(tmp_path, script, errexit=False):
    env = {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV")}
    args = [BASH] + (["-e"] if errexit else []) + ["-c", script]
    return subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, env=env,
                          timeout=120)


def _verdict(tmp_path):
    p = tmp_path / "v" / "verdict.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _out(tmp_path):
    p = tmp_path / "out.txt"
    return p.read_text(encoding="utf-8") if p.exists() else None


@needs_bash
def test_script_provider_command_exit_7_still_runs_checker(tmp_path):
    proc = _bash(tmp_path, _chain(tmp_path, "exit 7"))
    assert proc.returncode == 7, proc.stderr
    assert _verdict(tmp_path)["verdict"] == "missing"


@needs_bash
def test_script_provider_satisfied_chain(tmp_path):
    proc = _bash(tmp_path, _chain(tmp_path, "echo hello"))
    assert proc.returncode == 0, proc.stderr
    assert _out(tmp_path) == "hello\n"
    assert _verdict(tmp_path)["verdict"] == "satisfied"


@needs_bash
def test_script_provider_under_ambient_errexit_still_runs_checker(tmp_path):
    proc = _bash(tmp_path, _chain(tmp_path, "false"), errexit=True)
    assert proc.returncode == 1, proc.stderr
    assert _verdict(tmp_path)["verdict"] == "missing"


@pytest.mark.parametrize("shell", ["plain_shell", "errexit_shell"])
@needs_bash
def test_authored_set_e_inside_command_is_honored(tmp_path, shell):
    proc = _bash(tmp_path, _chain(tmp_path, "set -e; false; echo after"),
                 errexit=shell == "errexit_shell")
    assert "after" not in (_out(tmp_path) or "")
    assert proc.returncode == 1, proc.stderr
    assert _verdict(tmp_path)["verdict"] == "missing"


@needs_bash
def test_ambient_errexit_applies_inside_command(tmp_path):
    proc = _bash(tmp_path, _chain(tmp_path, "false; echo after"), errexit=True)
    assert "after" not in (_out(tmp_path) or "")
    assert _verdict(tmp_path)["verdict"] == "missing"
    assert proc.returncode == 1


@needs_bash
def test_ambient_errexit_restored_after_capture(tmp_path):
    script = _chain(tmp_path, "echo hi") + '\necho "flags=$-" > flags.txt'
    proc = _bash(tmp_path, script, errexit=True)
    assert proc.returncode == 0, proc.stderr
    flags = (tmp_path / "flags.txt").read_text(encoding="utf-8").strip()
    assert flags.startswith("flags=") and "e" in flags[len("flags="):]


@needs_bash
def test_plain_shell_stays_without_errexit_after_capture(tmp_path):
    script = _chain(tmp_path, "echo hi") + '\necho "flags=$-" > flags.txt'
    proc = _bash(tmp_path, script)
    assert proc.returncode == 0, proc.stderr
    flags = (tmp_path / "flags.txt").read_text(encoding="utf-8").strip()
    assert "e" not in flags[len("flags="):]


@needs_bash
def test_command_trailing_comment_is_contained(tmp_path):
    proc = _bash(tmp_path, _chain(tmp_path, "echo hi # trailing comment )"))
    assert proc.returncode == 0, proc.stderr
    assert _out(tmp_path) == "hi\n"
    assert _verdict(tmp_path)["verdict"] == "satisfied"
