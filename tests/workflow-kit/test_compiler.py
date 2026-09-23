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

    # fan-out over a string expression -- shares the node seam's (item, i) arity
    assert "await parallel(inputs.paths.map((item, i) => () => agent(" in js
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


# --------------------------------------------------------------------------- #
# input-doc header: a multi-line description must not escape the `//` comment
# --------------------------------------------------------------------------- #
def test_multiline_input_description_stays_inside_the_comment(write_workflow):
    js = _compile_text(
        "name: b\ndescription: x\n"
        "inputs:\n"
        "  diff:\n"
        "    type: string\n"
        "    description: |\n"
        "      first line\n"
        "      second line\n"
        "steps:\n  - id: s\n    agent: { prompt: \"{{ inputs.diff }}\" }\n",
        write_workflow,
    )
    header = js.split("// Inputs (provided via the Workflow `args` global):\n")[1].split("\n\n")[0]
    for line in header.splitlines():
        if line.strip():
            assert line.startswith("//")


# --------------------------------------------------------------------------- #
# agent fan-out shares the node seam's fan-out shape (one arity across all three
# emitters: script, openrouter, agent)
# --------------------------------------------------------------------------- #
def test_agent_for_each_emits_the_same_map_arity_as_a_node_step(write_workflow):
    js = _compile_text(
        "name: b\ndescription: x\ninputs:\n  xs: { type: list }\nsteps:\n"
        "  - id: s\n    for_each: \"{{ inputs.xs }}\"\n    agent: { prompt: \"{{ item }}\" }\n",
        write_workflow,
    )
    assert ".map((item, i) =>" in js


def test_shipped_node_strategies_example_compiles():
    # the declarative example must not silently rot
    js = _compile(EXAMPLES / "node-strategies.workflow.yaml")
    assert "const step_stats = await wkScript(" in js
    assert "const step_classify = await wkOpenRouter(" in js
    assert "const step_reconcile = await agent(" in js
    assert "step_stats.path" in js and "step_classify.path" in js


# --------------------------------------------------------------------------- #
# Migration step 8 (W1, W3, W4): an agent step routes Claude core ids through
# the harness. The first core id of the declaration compiles to agent(); any
# other id is skipped SILENTLY (no compile notice, nothing in the emitted
# script). A declaration with no core id is the floor: a compile error that
# itemises every declared id.
# --------------------------------------------------------------------------- #
def _agent_wf(model_yaml):
    return (
        "name: m\ndescription: x\nsteps:\n  - id: a\n    agent:\n"
        f"      prompt: hi\n      model: {model_yaml}\n"
    )


def test_fable_compiles_to_agent(write_workflow):
    js = _compile_text(_agent_wf("fable"), write_workflow)
    assert 'model: "fable"' in js


def test_an_unroutable_id_is_skipped_silently(write_workflow, capsys):
    js = _compile_text(_agent_wf("[sol, qwen3.8-5090, opus, sonnet]"), write_workflow)
    assert 'agent(`hi`, { model: "opus" })' in js
    for skipped in ("sol", "qwen3.8-5090"):
        assert skipped not in js
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_a_declaration_with_no_core_id_is_the_floor(write_workflow):
    with pytest.raises(WorkflowError) as caught:
        _compile_text(_agent_wf("[gpt-4, sol]"), write_workflow)
    message = str(caught.value)
    assert "no usable routing target" in message
    # itemised, in declaration order
    assert message.index("gpt-4") < message.index("sol")


def test_pipeline_stage_model_routes_the_same_way(write_workflow):
    js = _compile_text(
        "name: p\ndescription: x\nsteps:\n  - id: s\n    pipeline:\n      over: [1]\n"
        "      as: n\n      stages:\n        - id: one\n          agent:\n"
        "            prompt: hi\n            model: [luna, haiku]\n",
        write_workflow,
    )
    assert 'model: "haiku"' in js and "luna" not in js


def test_openrouter_list_declaration_compiles_to_the_comma_carrier(write_workflow):
    js = _compile_text(
        "name: o\ndescription: x\nsteps:\n  - id: c\n    openrouter:\n"
        "      prompt_file: p.txt\n      model: [or-qwen, or-gpt-mini]\n",
        write_workflow,
    )
    assert 'model: "or-qwen,or-gpt-mini"' in js


def test_executor_model_is_a_one_entry_declaration_carried_as_a_scalar():
    """W3/W4: the node executor's `haiku` is a one-entry declaration; the agent
    frontmatter and the preamble emit its scalar carrier, and must agree."""
    import re

    from bootstrap_lib.model_declaration import validate
    from wk_testlib import PLUGIN_ROOT

    from workflow_kit_lib.declarations import EXECUTOR_MODELS

    assert len(validate(list(EXECUTOR_MODELS))) == 1
    (only,) = EXECUTOR_MODELS
    agent_md = (PLUGIN_ROOT / "agents" / "workflow-kit-agent.md").read_text(encoding="utf-8")
    assert re.findall(r"(?m)^model:\s*(\S+)\s*$", agent_md) == [only]
    preamble = (
        PLUGIN_ROOT / "skills" / "workflow-kit" / "references" / "preamble.js"
    ).read_text(encoding="utf-8")
    assert re.findall(r"\bmodel:\s*'([^']+)'", preamble) == [only]
