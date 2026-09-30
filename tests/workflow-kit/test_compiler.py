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


def test_openrouter_node_passes_events_path(write_workflow):
    js = _compile_text(_OPENROUTER_WF, write_workflow)
    spec = js.split("const step_classify = await wkOpenRouter(")[1].split("\n")[0]
    assert "events: `./.workflow-kit/${inputs.runId}/classify.events.jsonl`" in spec
    assert "runId: inputs.runId" in spec
    assert 'unitId: "classify"' in spec
    # the inlined preamble turns the three spec fields into the runner's flags
    assert "' --events ' + shq(spec.events)" in js
    assert "' --run-id ' + shq(spec.runId)" in js
    assert "' --unit-id ' + shq(spec.unitId)" in js


def test_fanout_openrouter_node_indexes_events_and_unit(write_workflow):
    js = _compile_text(
        """
name: fan
description: fan-out openrouter
inputs:
  files: { type: list }
steps:
  - id: each
    for_each: "{{ inputs.files }}"
    openrouter:
      prompt_file: "{{ item }}"
""",
        write_workflow,
    )
    assert "await parallel(inputs.files.map((item, i) => () => wkOpenRouter(" in js
    assert "events: `./.workflow-kit/${inputs.runId}/each.${i}.events.jsonl`" in js
    assert "unitId: `each-${i}`" in js
    assert "`./.workflow-kit/${inputs.runId}/each.${i}.out`" in js


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


# --------------------------------------------------------------------------- #
# typed node contracts: emission (provides / requires / {{ artifacts.X }})
# --------------------------------------------------------------------------- #
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

from wk_testlib import PLUGIN_ROOT  # noqa: E402

_LSK_LIB = PLUGIN_ROOT.parent / "llm-scripting-kit" / "lib"


@pytest.fixture
def lsk_path(monkeypatch):
    # Stands in for the shared-libs .pth linking llm_scripting_kit onto the venv.
    monkeypatch.syspath_prepend(str(_LSK_LIB))


# sha256 of the compiled output bytes (UTF-8) of each artifact-free source
# document, captured at commit 2082c7fa, before typed contracts existed. A
# document that declares no provides/requires must still compile to exactly
# those bytes. The compiled scripts themselves are generated artifacts and are
# not checked in; only their digests are.
_GOLDEN_DIGESTS = {
    # plugins/workflow-kit/examples/review-changes.workflow.yaml @ 2082c7fa
    "review-changes": (
        EXAMPLES / "review-changes.workflow.yaml",
        "26b6bbd25244b9e1e75e027ccb2fea8c9abd49fa8672ee99d5e8ca276ea3a69a",
    ),
    # plugins/workflow-kit/examples/node-strategies.workflow.yaml @ 2082c7fa
    "node-strategies": (
        EXAMPLES / "node-strategies.workflow.yaml",
        "70b8f48d1ab4f4d1a1ff5e72521159218095c93e7b65846717f2a14c6fe8f56d",
    ),
    # tests/workflow-kit/fixtures/good/flat.workflow.yaml @ 2082c7fa
    "flat": (
        FIXTURES / "good" / "flat.workflow.yaml",
        "df283e11e9cc28f4ed9a4eb5c4c8feebb0b1a1fbaa31e284b31548a58c23906a",
    ),
}


def _compiled_digest(source):
    return hashlib.sha256(_compile(source).encode("utf-8")).hexdigest()


@pytest.mark.parametrize("golden", sorted(_GOLDEN_DIGESTS))
def test_examples_without_contracts_compile_unchanged(golden):
    source, digest = _GOLDEN_DIGESTS[golden]
    assert _compiled_digest(source) == digest


_TYPED = """
name: typed
description: x
inputs:
  xs: { type: list }
steps:
  - id: count
    script: { command: "wc -w in.txt" }
    provides:
      doc: { type: opaque-file }
  - id: many
    for_each: "{{ inputs.xs }}"
    script: { command: "wc -w {{ item }}" }
    provides:
      docs: { type: opaque-file }
  - id: classify
    openrouter: { prompt_file: p.txt }
    provides:
      label: { type: opaque-file }
"""


def _consumer(body, requires="      doc: { type: opaque-file }\n"):
    return _TYPED + "  - id: use\n    requires:\n" + requires + body


def test_artifact_expression_compiles_to_provider_path(write_workflow):
    js = _compile_text(_consumer('    agent: { prompt: "read {{ artifacts.doc }}" }\n'),
                       write_workflow)
    assert "const step_use = await agent(`read ${step_count.path}`);" in js


def test_each_artifact_expression_compiles_to_path_list(write_workflow):
    js = _compile_text(
        _consumer('    agent: { prompt: "read {{ artifacts.docs }}" }\n',
                  "      docs: { type: opaque-file, each: true }\n"),
        write_workflow,
    )
    assert "agent(`read ${step_many.map((r) => r.path)}`)" in js


def test_artifact_use_without_requires_is_a_compile_error(write_workflow):
    text = _TYPED + '  - id: use\n    agent: { prompt: "read {{ artifacts.doc }}" }\n'
    with pytest.raises(WorkflowError, match=r"step 'use': .* uses artifact 'doc', which this "
                                            r"step does not declare in `requires`"):
        _compile_text(text, write_workflow)


def test_artifact_expression_refuses_member_tail(write_workflow):
    with pytest.raises(WorkflowError, match=r"step 'use': .*has a member tail"):
        _compile_text(_consumer('    agent: { prompt: "{{ artifacts.doc.bytes }}" }\n'),
                      write_workflow)


def test_output_expression_has_no_artifacts(write_workflow):
    text = _consumer('    agent: { prompt: "hi" }\n') + 'output: "{{ artifacts.doc }}"\n'
    with pytest.raises(WorkflowError, match=r"output: .* uses artifact 'doc'"):
        _compile_text(text, write_workflow)


def _pipeline(over="[1]", first="hi", later="hi", fan=None):
    fan_line = f"          fan_out: {{ over: \"{fan[0]}\", as: f }}\n" if fan else ""
    second = fan[1] if fan else later
    return (
        f"    pipeline:\n      over: {over}\n      as: n\n      stages:\n"
        f"        - id: one\n          agent: {{ prompt: \"{first}\" }}\n"
        f"        - id: two\n{fan_line}          agent: {{ prompt: \"{second}\" }}\n"
    )


def test_artifact_expression_in_pipeline_over(write_workflow):
    js = _compile_text(
        _consumer(_pipeline(over='"{{ artifacts.docs }}"'),
                  "      docs: { type: opaque-file, each: true }\n"),
        write_workflow,
    )
    assert "await pipeline(\n  step_many.map((r) => r.path)," in js


@pytest.mark.parametrize("stage", ["first_stage", "later_stage"])
def test_artifact_expression_in_stage_prompt(write_workflow, stage):
    expr = "{{ artifacts.doc }}"
    body = _pipeline(first=expr) if stage == "first_stage" else _pipeline(later=expr)
    js = _compile_text(_consumer(body), write_workflow)
    assert "agent(`${step_count.path}`)" in js


def test_artifact_expression_in_fanout_over(write_workflow):
    body = _pipeline(fan=("{{ artifacts.docs }}", "hi"))
    js = _compile_text(_consumer(body, "      docs: { type: opaque-file, each: true }\n"),
                       write_workflow)
    assert "parallel(step_many.map((r) => r.path).map((f) => () => agent(`hi`)))" in js


def test_artifact_expression_in_fanout_body(write_workflow):
    body = _pipeline(fan=("{{ inputs.xs }}", "{{ f }} {{ artifacts.doc }}"))
    js = _compile_text(_consumer(body), write_workflow)
    assert "parallel(inputs.xs.map((f) => () => agent(`${f} ${step_count.path}`)))" in js


def test_validate_only_does_not_compile_artifact_expressions(write_workflow, capsys):
    # The undeclared-use check is compile_doc-only, like unknown steps.ID;
    # --validate-only never compiles expressions.
    spec = importlib.util.spec_from_file_location(
        "workflow_kit_compile_cli_expr", PLUGIN_ROOT / "scripts" / "compile_workflow.py"
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    path = write_workflow(_TYPED + '  - id: use\n    agent: { prompt: "{{ artifacts.doc }}" }\n')
    assert cli.main([str(path), "--validate-only"]) == 0
    capsys.readouterr()
    assert cli.main([str(path)]) == 1
    assert "uses artifact 'doc'" in capsys.readouterr().err


def test_contract_free_expressions_compile_as_at_the_anchor(write_workflow):
    # A contract-free document: `artifacts` is an ordinary `as` local, exactly
    # as before typed contracts (the golden digests pin the rest byte-for-byte).
    js = _compile_text(
        "name: t\ndescription: x\nsteps:\n  - id: p\n    pipeline:\n      over: [1]\n"
        "      as: artifacts\n      stages:\n        - id: s\n"
        "          agent: { prompt: \"{{ artifacts.x }}\" }\n",
        write_workflow,
    )
    assert "(prev, artifacts, i) => agent(`${artifacts.x}`)" in js
    source, digest = _GOLDEN_DIGESTS["node-strategies"]
    assert _compiled_digest(source) == digest


def test_contract_preamble_absent_without_provides():
    js = _compile(EXAMPLES / "node-strategies.workflow.yaml")
    assert "function wkScript(" in js
    assert "function wkProvided(" not in js
    assert "function wkScriptProvided(" not in js
    assert "function wkProviderFlags(" not in js


def test_contract_preamble_present_with_provides(write_workflow):
    js = _compile_text(_TYPED, write_workflow)
    preamble = js.index("function wkOpenRouter(")
    for name in ("wkProviderFlags", "wkScriptProvided", "wkProvided"):
        assert js.count(f"function {name}(") == 1
        assert js.index(f"function {name}(") > preamble  # inlined AFTER preamble.js


def test_script_provider_chains_checker_after_command(write_workflow):
    js = _compile_text(_TYPED, write_workflow)
    assert "const step_count = await wkScriptProvided(`wc -w in.txt`, " in js
    assert ('runner: `"${inputs.workflowKitVenvPython}" '
            '"${inputs.pluginRoot}/scripts/check_artifact.py"`') in js
    helper = js[js.index("function wkScriptProvided("):js.index("function wkProvided(")]
    order = [
        "'wk_e=; case $- in *e*) wk_e=1; set +e;; esac'",
        "'( if [ -n \"$wk_e\" ]; then set -e; fi'",
        "    command,",
        "') > ' + shq(out)",
        "'wk_rc=$?'",
        "'if [ -n \"$wk_e\" ]; then set -e; fi'",
        "' --command-exit \"$wk_rc\"'",
    ]
    positions = [helper.index(part) for part in order]
    assert positions == sorted(positions)


def test_openrouter_provider_passes_contract_flags(write_workflow):
    js = _compile_text(_TYPED, write_workflow)
    line = js.split("const step_classify = await ")[1].split("\n")[0]
    assert line.startswith(
        'wkOpenRouter(`"${inputs.workflowKitVenvPython}" '
        '"${inputs.pluginRoot}/scripts/openrouter_run.py"` + wkProviderFlags({ '
        'artifact: "label", kind: "opaque-file", '
        'verdict: `./.workflow-kit/${inputs.runId}/classify.contract.json` }), {'
    )
    helper = js[js.index("function wkProviderFlags("):js.index("function wkScriptProvided(")]
    for flag in ("--provides", "--kind", "--schema", "--schema-digest", "--verdict"):
        assert f"' {flag} ' + shq(" in helper


def test_verdict_path_is_indexed_under_fanout(write_workflow):
    js = _compile_text(_TYPED, write_workflow)
    assert "verdict: `./.workflow-kit/${inputs.runId}/many.${i}.contract.json`" in js
    assert "verdict: `./.workflow-kit/${inputs.runId}/count.contract.json`" in js
    assert ('wkProvided(step_many, "many", "docs", '
            '(i) => `./.workflow-kit/${inputs.runId}/many.${i}.contract.json`);') in js


def test_provider_step_is_followed_by_guard(write_workflow):
    js = _compile_text(_TYPED + '  - id: tail\n    agent: { prompt: "hi" }\n', write_workflow)
    lines = js.splitlines()
    for var, step, art in (("step_count", "count", "doc"), ("step_many", "many", "docs"),
                           ("step_classify", "classify", "label")):
        at = next(i for i, ln in enumerate(lines) if ln.startswith(f"const {var} = await "))
        assert lines[at + 1].startswith(f'wkProvided({var}, "{step}", "{art}", ')
    # non-provider steps get no guard
    assert js.count("\nwkProvided(") == 3


def test_shipped_typed_contracts_example_compiles(lsk_path):
    js = _compile(EXAMPLES / "typed-contracts.workflow.yaml")
    assert "const step_count = await wkScriptProvided(" in js
    assert 'artifact: "doc_stats", kind: "schema", schema: "{' in js
    assert re.search(r'digest: "[0-9a-f]{64}"', js)
    assert 'wkProvided(step_count, "count", "doc_stats", ' in js
    assert 'wkProviderFlags({ artifact: "doc_class", kind: "opaque-file"' in js
    assert 'wkProvided(step_classify, "classify", "doc_class", ' in js
    assert "Read ${step_count.path} (wordcount" in js
    assert "${step_classify.path} (external" in js


def test_schema_text_is_the_canonical_json(write_workflow, lsk_path):
    js = _compile_text(
        "name: t\ndescription: x\nschemas:\n  s:\n    type: object\n"
        "    properties: { b: { type: number, minimum: 1.0 }, a: { type: integer } }\n"
        "steps:\n  - id: n\n    script: { command: echo }\n    provides:\n"
        "      x: { schema: s }\n",
        write_workflow,
    )
    literal = re.search(r'schema: ("(?:[^"\\]|\\.)*")', js).group(1)
    assert json.loads(literal) == (
        '{"properties":{"a":{"type":"integer"},"b":{"minimum":1.0,"type":"number"}},'
        '"type":"object"}'
    )


# --------------------------------------------------------------------------- #
# TC4: a script provider's checker records the `contract` event in the node's
# events stream, at the path and unit id an openrouter node uses (E5).
# --------------------------------------------------------------------------- #
def test_script_provider_passes_events_path(write_workflow):
    js = _compile_text(_TYPED, write_workflow)
    line = js.split("const step_count = await ")[1].split("\n")[0]
    assert (
        "verdict: `./.workflow-kit/${inputs.runId}/count.contract.json`, "
        "events: `./.workflow-kit/${inputs.runId}/count.events.jsonl`, "
        'runId: inputs.runId, unitId: "count" }'
    ) in line
    helper = js[js.index("function wkScriptProvided("):js.index("function wkProvided(")]
    for flag, key in (("--events", "events"), ("--run-id", "runId"), ("--unit-id", "unitId")):
        assert f"' {flag} ' + shq(check.{key})" in helper
    # the event flags precede the command-exit argument on the checker line
    assert helper.index("eventFlags +") < helper.index("' --command-exit \"$wk_rc\"'")
    # an openrouter provider's events travel in wkOpenRouter's spec, not its check
    classify = js.split("const step_classify = await ")[1].split("\n")[0]
    assert classify.count("--events") == 0 and classify.count("events: `") == 1


def test_fanout_script_provider_indexes_events_and_unit(write_workflow):
    js = _compile_text(_TYPED, write_workflow)
    line = js.split("const step_many = await ")[1].split("\n")[0]
    assert "events: `./.workflow-kit/${inputs.runId}/many.${i}.events.jsonl`" in line
    assert "unitId: `many-${i}`" in line
    assert "runId: inputs.runId" in line
