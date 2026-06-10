"""Compiler tests: structural assertions on the emitted Workflow JS."""

import pytest

from wk_testlib import EXAMPLES, FIXTURES

from workflow_kit_lib import compile_doc, load_workflow
from workflow_kit_lib.errors import WorkflowError


def _compile(path):
    return compile_doc(load_workflow(path))


def _compile_text(text, write_workflow):
    return compile_doc(load_workflow(write_workflow(text)))


def test_example_compiles_to_pipeline_with_nested_parallel():
    js = _compile(EXAMPLES / "review-changes.workflow.yaml")

    # meta
    assert 'export const meta = {' in js
    assert 'name: "review-changes"' in js
    assert '{ title: "Review" }' in js and '{ title: "Verify" }' in js

    # schema consts emitted and referenced
    assert "const schema_findings = {" in js
    assert "const schema_verdict = {" in js
    assert "schema: schema_findings" in js

    # no-barrier pipeline with stage callbacks
    assert "await pipeline(" in js
    assert '["bugs", "perf", "style"]' in js
    assert "(prev, dim, i) =>" in js

    # stage 2 fans out over the previous stage's findings
    assert "parallel(prev.findings.map((finding) => () => agent(" in js

    # args normalization (args arrives as a JSON string at runtime)
    assert 'const inputs = typeof args === "string" ? JSON.parse(args)' in js

    # interpolation
    assert "${dim}" in js
    assert "${inputs.diff}" in js
    assert "${finding.title}" in js

    # per-step model override
    assert 'model: "sonnet"' in js

    # output
    assert "return step_dimensions;" in js


def test_flat_step_compiles_to_parallel_map():
    js = _compile(FIXTURES / "good" / "flat.workflow.yaml")

    # fan-out over a string expression
    assert "await parallel(inputs.paths.map((item) => () => agent(" in js
    assert "agentType: \"Explore\"" in js

    # single agent step (no fan-out)
    assert "const step_summarize = await agent(" in js

    # [*] flatten in the summarize prompt
    assert "step_scan.flatMap((r) => r.note)" in js

    # default output (no `output:` key) returns an object of all steps
    assert 'return { "scan": step_scan, "summarize": step_summarize };' in js


# --------------------------------------------------------------------------- #
# node strategies: script + openrouter emission
# --------------------------------------------------------------------------- #
_SCRIPT_WF = """
name: script-demo
description: a script node
inputs:
  source: { type: string }
steps:
  - id: stats
    phase: prepare
    script:
      command: '"{{ inputs.workflowKitVenvPython }}" wc.py "{{ inputs.source }}"'
      label: wordcount
"""

_OPENROUTER_WF = """
name: or-demo
description: an openrouter node
inputs:
  source: { type: string }
steps:
  - id: classify
    openrouter:
      prompt_file: "{{ inputs.source }}"
      cheap: true
      system: "Classify in one word."
"""


def test_script_node_compiles(write_workflow):
    js = _compile_text(_SCRIPT_WF, write_workflow)
    # args normalized + preamble inlined (sandbox cannot import)
    assert 'const inputs = typeof args === "string"' in js
    assert "function wkScript(" in js
    # the wkScript call with the command template and the default $OUT path
    assert "const step_stats = await wkScript(" in js
    assert "${inputs.source}" in js
    assert "`./.workflow-kit/${inputs.runId}/stats.out`" in js
    assert 'label: "wordcount"' in js
    assert 'phase: "prepare"' in js


def test_openrouter_node_compiles(write_workflow):
    js = _compile_text(_OPENROUTER_WF, write_workflow)
    assert "function wkOpenRouter(" in js
    assert "const step_classify = await wkOpenRouter(" in js
    # runner built from reserved args; never a bare interpreter
    assert "${inputs.workflowKitVenvPython}" in js
    assert "/scripts/openrouter_run.py" in js
    # spec: cheap (no model), prompt file, system, default out
    assert "cheap: true" in js
    assert "model:" not in js.split("wkOpenRouter(")[1].split(")")[0]
    assert "promptFile: `${inputs.source}`" in js
    assert "`./.workflow-kit/${inputs.runId}/classify.out`" in js


def test_script_node_for_each_indexes_out_path(write_workflow):
    js = _compile_text(
        """
name: fan
description: fan-out script
inputs:
  files: { type: string }
steps:
  - id: each
    for_each: "{{ inputs.files }}"
    script:
      command: "wc -w {{ item }}"
""",
        write_workflow,
    )
    assert "await parallel(inputs.files.map((item, i) => () => wkScript(" in js
    # index in the default out path so fan-out payloads do not collide
    assert "`./.workflow-kit/${inputs.runId}/each.${i}.out`" in js


def test_no_preamble_when_no_node_steps():
    js = _compile(EXAMPLES / "review-changes.workflow.yaml")
    assert "function wkScript(" not in js
    assert "function wkOpenRouter(" not in js
    # but the args-normalization const is always emitted
    assert "const inputs = typeof args" in js


def test_node_args_are_single_quote_shell_escaped(write_workflow):
    # The inlined preamble must shq()-quote every value it splices into the shell
    # command: double quotes do not stop $(), backticks, or $var expansion, and
    # JSON.stringify is a JS escaper, not a shell escaper. Guards the W4 fix in
    # preamble.js (shq + wkScript redirect target + every wkOpenRouter flag).
    js = _compile_text(_SCRIPT_WF, write_workflow)
    assert "function shq(" in js
    assert "; } > ' + shq(out)" in js                      # wkScript redirect target
    js2 = _compile_text(_OPENROUTER_WF, write_workflow)
    assert "' --model ' + shq(spec.model)" in js2
    assert "' --system ' + shq(spec.system)" in js2
    assert "' --status ' + shq(spec.status)" in js2
    assert "' --prompt-file ' + shq(spec.promptFile)" in js2
    assert "' --out ' + shq(spec.out)" in js2
    assert "JSON.stringify(spec.system)" not in js2        # the wrong escaper is gone


# --------------------------------------------------------------------------- #
# inputs cross-check (W3): unknown {{ inputs.* }} heads are compile errors
# --------------------------------------------------------------------------- #
def test_undeclared_input_is_a_compile_error():
    with pytest.raises(WorkflowError, match="unknown input 'dif'"):
        _compile(FIXTURES / "broken" / "undeclared_input.workflow.yaml")


def test_reserved_inputs_require_node_steps(write_workflow):
    # The reserved trio (runId/pluginRoot/workflowKitVenvPython) is injected by
    # the skill only when node steps exist; an agent-only workflow referencing
    # one is a typo, not a reserved arg.
    agent_only = """
name: a
description: x
steps:
  - id: s
    agent: { prompt: "run {{ inputs.runId }}" }
"""
    with pytest.raises(WorkflowError, match="unknown input 'runId'"):
        _compile_text(agent_only, write_workflow)

    with_node = """
name: a
description: x
steps:
  - id: n
    script: { command: "echo hi" }
  - id: s
    agent: { prompt: "run {{ inputs.runId }}" }
"""
    assert "${inputs.runId}" in _compile_text(with_node, write_workflow)


def test_shipped_node_strategies_example_compiles():
    # the declarative example must not silently rot
    js = _compile(EXAMPLES / "node-strategies.workflow.yaml")
    assert "const step_stats = await wkScript(" in js
    assert "const step_classify = await wkOpenRouter(" in js
    assert "const step_reconcile = await agent(" in js
    assert "step_stats.path" in js and "step_classify.path" in js
