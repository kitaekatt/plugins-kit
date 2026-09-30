"""Typed node contracts: the provides/requires surface and its compile-time checks.

Every check here runs in model validation (``contracts.analyze`` from
``WorkflowDoc._validate_cross_refs``), so each rejection is asserted under both
``compile_workflow.py --validate-only`` and a full compile.
"""

import importlib.util
import re
import sys
import types

import pytest

from wk_testlib import PLUGIN_ROOT

from workflow_kit_lib import compile_doc, load_workflow
from workflow_kit_lib.contracts import NODE_SCHEMA_MAX_BYTES
from workflow_kit_lib.errors import WorkflowError

_LSK_LIB = PLUGIN_ROOT.parent / "llm-scripting-kit" / "lib"
_COMPILE = PLUGIN_ROOT / "scripts" / "compile_workflow.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("workflow_kit_compile_cli", _COMPILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()


@pytest.fixture(autouse=True)
def _lsk_on_path(monkeypatch):
    # Stands in for the bootstrap shared-libs .pth that links llm_scripting_kit
    # onto workflow-kit's venv.
    monkeypatch.syspath_prepend(str(_LSK_LIB))


_STATS = """
schemas:
  stats:
    type: object
    required: [lines, words]
    additionalProperties: false
    properties:
      lines: { type: integer }
      words: { type: integer }
"""


def _wf(steps, schemas=_STATS):
    return f"name: t\ndescription: x\ninputs:\n  xs: {{ type: list }}\n{schemas}\nsteps:\n{steps}"


_PROVIDER = """
  - id: count
    script: { command: "echo {}" }
    provides:
      doc_stats: { schema: stats }
"""

_CONSUMER = """
  - id: use
    requires:
      doc_stats: { schema: stats }
    agent: { prompt: "read {{ artifacts.doc_stats }}" }
"""


def _rejected(write_workflow, capsys, text, match):
    """Assert the document is refused by --validate-only and by compile, located."""
    path = write_workflow(text)
    with pytest.raises(WorkflowError, match=match):
        load_workflow(path)
    capsys.readouterr()
    assert cli.main([str(path), "--validate-only"]) == 1
    err = capsys.readouterr().err
    assert re.search(match, err), err
    assert cli.main([str(path)]) == 1
    capsys.readouterr()
    return err


def _compiles(write_workflow, text):
    return compile_doc(load_workflow(write_workflow(text)))


# --------------------------------------------------------------------------- #
# 1. spec shape
# --------------------------------------------------------------------------- #
def test_artifact_spec_refuses_unknown_key(write_workflow, capsys):
    steps = _PROVIDER.replace("{ schema: stats }", "{ schema: stats, format: json }")
    _rejected(write_workflow, capsys, _wf(steps),
              r"step 'count'\.provides\.doc_stats: unknown field\(s\) \['format'\]")


@pytest.mark.parametrize("spec", ["{ schema: stats, type: opaque-file }", "{}"],
                         ids=["both", "neither"])
def test_artifact_spec_requires_exactly_one_of_schema_or_type(write_workflow, capsys, spec):
    steps = _PROVIDER.replace("{ schema: stats }", spec)
    _rejected(write_workflow, capsys, _wf(steps),
              r"step 'count'\.provides\.doc_stats: an artifact spec takes exactly one of")


def test_artifact_type_must_be_opaque_file(write_workflow, capsys):
    steps = _PROVIDER.replace("{ schema: stats }", "{ type: json }")
    _rejected(write_workflow, capsys, _wf(steps),
              r"step 'count'\.provides\.doc_stats: 'type' must be 'opaque-file'")


def test_artifact_name_must_be_identifier(write_workflow, capsys):
    steps = _PROVIDER.replace("doc_stats:", "doc-stats:")
    _rejected(write_workflow, capsys, _wf(steps),
              r"step 'count'\.provides: 'artifact name' must be an identifier")


@pytest.mark.parametrize("kind", ["agent", "pipeline"])
def test_provides_refused_on_agent_and_pipeline(write_workflow, capsys, kind):
    body = (
        '    agent: { prompt: "hi" }\n'
        if kind == "agent"
        else '    pipeline:\n      over: [1]\n      as: n\n      stages:\n'
        '        - id: s\n          agent: { prompt: "hi" }\n'
    )
    steps = f"  - id: a\n{body}    provides:\n      out: {{ type: opaque-file }}\n"
    _rejected(write_workflow, capsys, _wf(steps),
              r"step 'a'\.provides: an agent result is typed by its `schema:`")


def test_node_provides_at_most_one_artifact(write_workflow, capsys):
    steps = _PROVIDER.replace(
        "      doc_stats: { schema: stats }\n",
        "      doc_stats: { schema: stats }\n      other: { type: opaque-file }\n",
    )
    _rejected(write_workflow, capsys, _wf(steps),
              r"step 'count'\.provides: a node provides at most one artifact")


@pytest.mark.parametrize("role", ["provides", "requires"])
def test_unknown_artifact_schema_is_refused(write_workflow, capsys, role):
    if role == "provides":
        steps = _PROVIDER.replace("{ schema: stats }", "{ schema: nope }")
        where = r"step 'count'\.provides\.doc_stats"
    else:
        steps = _PROVIDER + _CONSUMER.replace("doc_stats: { schema: stats }",
                                              "doc_stats: { schema: nope }")
        where = r"step 'use'\.requires\.doc_stats"
    _rejected(write_workflow, capsys, _wf(steps), where + r": unknown schema 'nope'")


@pytest.mark.parametrize("case", ["in_provides", "non_bool"])
def test_each_is_a_requires_bool(write_workflow, capsys, case):
    if case == "in_provides":
        steps = _PROVIDER.replace("{ schema: stats }", "{ schema: stats, each: false }")
        match = r"step 'count'\.provides\.doc_stats: unknown field\(s\) \['each'\]"
    else:
        steps = _PROVIDER + _CONSUMER.replace("doc_stats: { schema: stats }",
                                              "doc_stats: { schema: stats, each: 'yes' }")
        match = r"step 'use'\.requires\.doc_stats: 'each' must be a boolean"
    _rejected(write_workflow, capsys, _wf(steps), match)


# --------------------------------------------------------------------------- #
# 3-5. duplicate, missing, order
# --------------------------------------------------------------------------- #
def test_missing_provider_is_a_compile_error(write_workflow, capsys):
    _rejected(write_workflow, capsys, _wf(_CONSUMER),
              r"step 'use'\.requires\.doc_stats: no step provides artifact 'doc_stats'")


def test_duplicate_provider_is_a_compile_error(write_workflow, capsys):
    steps = _PROVIDER + _PROVIDER.replace("id: count", "id: count2")
    _rejected(write_workflow, capsys, _wf(steps),
              r"artifact 'doc_stats' is provided by two steps, 'count' and 'count2'")


def test_provider_after_consumer_is_a_compile_error(write_workflow, capsys):
    _rejected(write_workflow, capsys, _wf(_CONSUMER + _PROVIDER),
              r"step 'use'\.requires\.doc_stats: provider step 'count' comes after "
              r"consumer step 'use'")


def test_step_cannot_require_its_own_artifact(write_workflow, capsys):
    steps = _PROVIDER + "    requires:\n      doc_stats: { schema: stats }\n"
    _rejected(write_workflow, capsys, _wf(steps),
              r"step 'count'\.requires\.doc_stats: a step cannot require the artifact it "
              r"provides itself")


def test_validate_only_rejects_missing_provider(write_workflow, capsys):
    # The checks live in model validation, which is all --validate-only runs.
    path = write_workflow(_wf(_CONSUMER.replace("{ schema: stats }", "{ type: opaque-file }")))
    assert cli.main([str(path), "--validate-only"]) == 1
    assert "no step provides artifact 'doc_stats'" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 6. compatibility
# --------------------------------------------------------------------------- #
def test_schema_requirement_refuses_opaque_provider(write_workflow, capsys):
    steps = _PROVIDER.replace("{ schema: stats }", "{ type: opaque-file }") + _CONSUMER
    _rejected(write_workflow, capsys, _wf(steps),
              r"step 'use'\.requires\.doc_stats: requires schema 'stats', but provider step "
              r"'count' provides an opaque file")


def test_opaque_requirement_accepts_schema_provider(write_workflow):
    steps = _PROVIDER + _CONSUMER.replace("doc_stats: { schema: stats }",
                                          "doc_stats: { type: opaque-file }")
    assert "step_count.path" in _compiles(write_workflow, _wf(steps))


_OTHER = _STATS + """
  other:
    type: object
    required: [lines]
    additionalProperties: false
    properties:
      lines: { type: integer }
"""


def test_different_schema_is_incompatible(write_workflow, capsys):
    steps = _PROVIDER + _CONSUMER.replace("{ schema: stats }", "{ schema: other }")
    _rejected(write_workflow, capsys, _wf(steps, _OTHER),
              r"step 'use'\.requires\.doc_stats: incompatible with provider step 'count': "
              r"schema 'other' differs from the provider's schema 'stats'")


_TWIN = _STATS + """
  twin:
    type: object
    required: [lines, words]
    additionalProperties: false
    properties:
      lines: { type: integer }
      words: { type: integer }
"""


def test_identical_schema_bodies_are_compatible(write_workflow):
    steps = _PROVIDER + _CONSUMER.replace("{ schema: stats }", "{ schema: twin }")
    assert "step_count.path" in _compiles(write_workflow, _wf(steps, _TWIN))


_REORDERED = _STATS + """
  reordered:
    properties:
      words: { type: integer }
      lines: { type: integer }
    additionalProperties: false
    required: [lines, words]
    type: object
"""


def test_schema_key_order_does_not_affect_compatibility(write_workflow):
    steps = _PROVIDER + _CONSUMER.replace("{ schema: stats }", "{ schema: reordered }")
    assert "step_count.path" in _compiles(write_workflow, _wf(steps, _REORDERED))


@pytest.mark.parametrize("case", ["one_vs_each", "each_vs_one"])
def test_cardinality_mismatch_is_incompatible(write_workflow, capsys, case):
    if case == "one_vs_each":
        # a one-file provider, an each: true requirement
        steps = _PROVIDER + _CONSUMER.replace("{ schema: stats }", "{ schema: stats, each: true }")
        match = r"needs `each: false`"
    else:
        # a fan-out provider, a one-file requirement
        steps = _PROVIDER.replace("    script:", '    for_each: "{{ inputs.xs }}"\n    script:') \
            + _CONSUMER
        match = r"needs `each: true`"
    _rejected(write_workflow, capsys, _wf(steps),
              r"step 'use'\.requires\.doc_stats: cardinality mismatch with provider step "
              r"'count'.*" + match)


# --------------------------------------------------------------------------- #
# 2. provider schema admissibility (llm-scripting-kit's OutputContract)
# --------------------------------------------------------------------------- #
def _provider_with_schema(schema_yaml):
    return _wf(_PROVIDER, "schemas:\n  stats:\n" + schema_yaml)


def test_node_artifact_schema_outside_subset_is_refused(write_workflow, capsys):
    text = _provider_with_schema("    type: string\n    pattern: '^a'\n")
    _rejected(write_workflow, capsys, text,
              r"step 'count'\.provides\.doc_stats: schema 'stats' cannot type a node artifact:"
              r".*pattern")


def test_node_artifact_schema_admitting_null_is_refused(write_workflow, capsys):
    text = _provider_with_schema("    type: [object, 'null']\n")
    _rejected(write_workflow, capsys, text,
              r"step 'count'\.provides\.doc_stats: schema 'stats' cannot type a node artifact:"
              r".*null")


def test_node_artifact_schema_over_cap_is_refused(write_workflow, capsys):
    props = "".join(f"      p{i:04d}: {{ type: integer }}\n" for i in range(400))
    text = _provider_with_schema(
        "    type: object\n    additionalProperties: false\n    properties:\n" + props
    )
    err = _rejected(write_workflow, capsys, text,
                    rf"step 'count'\.provides\.doc_stats: schema 'stats' is \d+ bytes of "
                    rf"canonical JSON, over the {NODE_SCHEMA_MAX_BYTES}-byte limit")
    assert "type: opaque-file" in err  # names the remedy


def test_node_artifact_schema_at_cap_is_accepted(write_workflow):
    # Boundary companion: a schema just under the cap compiles.
    props = "".join(f"      p{i:04d}: {{ type: integer }}\n" for i in range(200))
    text = _provider_with_schema(
        "    type: object\n    additionalProperties: false\n    properties:\n" + props
    )
    assert "wkScriptProvided(" in _compiles(write_workflow, text)


# --------------------------------------------------------------------------- #
# the lazy llm-scripting-kit probe
# --------------------------------------------------------------------------- #
def test_contract_probe_absent_message(write_workflow, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    err = _rejected(write_workflow, capsys, _wf(_PROVIDER), r"llm_scripting_kit is not importable")
    assert "shared-libs .pth" in err
    assert ">= 0.56.0" not in err


def test_contract_probe_too_old_message(write_workflow, capsys, monkeypatch):
    fake_pkg = types.ModuleType("llm_scripting_kit")
    fake_completion = types.ModuleType("llm_scripting_kit.completion")
    fake_completion.POLICY_VALIDATED_RESULT = "validated-result"  # OutputContract absent
    fake_pkg.completion = fake_completion
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", fake_pkg)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", fake_completion)
    err = _rejected(write_workflow, capsys, _wf(_PROVIDER),
                    r"llm-scripting-kit >= 0\.56\.0")
    assert "not importable" not in err
    assert "claude plugin update llm-scripting-kit@plugins-kit" in err


def test_documents_without_schema_artifacts_need_no_lsk(write_workflow, monkeypatch):
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", None)
    opaque = _wf(
        _PROVIDER.replace("{ schema: stats }", "{ type: opaque-file }")
        + _CONSUMER.replace("{ schema: stats }", "{ type: opaque-file }")
    )
    assert "wkProvided(step_count" in _compiles(write_workflow, opaque)
    # and a document with no artifacts at all
    plain = _wf('  - id: a\n    script: { command: "echo hi" }\n')
    assert "wkScript(" in _compiles(write_workflow, plain)


# --------------------------------------------------------------------------- #
# rev2 S4: `artifacts` is reserved only in contract documents
# --------------------------------------------------------------------------- #
_ARTIFACTS_LOCAL = """
  - id: p
    pipeline:
      over: "{{ inputs.xs }}"
      as: artifacts
      stages:
        - id: s
          agent: { prompt: "item {{ artifacts.x }}" }
"""


def test_artifacts_name_allowed_without_contracts(write_workflow):
    js = _compiles(write_workflow, _wf(_ARTIFACTS_LOCAL, ""))
    assert "(prev, artifacts, i) => agent(`item ${artifacts.x}`)" in js


@pytest.mark.parametrize("case", ["stage_id", "pipeline_as", "fanout_as"])
def test_artifacts_name_refused_in_contract_documents(write_workflow, capsys, case):
    if case == "stage_id":
        pipe = ('    pipeline:\n      over: [1]\n      as: n\n      stages:\n'
                '        - id: artifacts\n          agent: { prompt: "hi" }\n')
        match = r"step 'p'\.pipeline: stage id must not be 'artifacts'"
    elif case == "pipeline_as":
        pipe = ('    pipeline:\n      over: [1]\n      as: artifacts\n      stages:\n'
                '        - id: s\n          agent: { prompt: "hi" }\n')
        match = r"step 'p'\.pipeline: 'as' must not be 'artifacts'"
    else:
        pipe = ('    pipeline:\n      over: [1]\n      as: n\n      stages:\n'
                '        - id: s\n          fan_out: { over: [1], as: artifacts }\n'
                '          agent: { prompt: "hi" }\n')
        match = r"step 'p'\.pipeline\.stage 's'\.fan_out: 'as' must not be 'artifacts'"
    steps = _PROVIDER + "  - id: p\n" + pipe
    _rejected(write_workflow, capsys, _wf(steps), match)


def test_artifacts_step_id_allowed_in_contract_documents(write_workflow):
    steps = _PROVIDER + _CONSUMER.replace("id: use", "id: artifacts")
    js = _compiles(write_workflow, _wf(steps))
    assert "const step_artifacts = await agent(`read ${step_count.path}`)" in js
