"""Behavioural tests for coverage-detect.js batching, identity, and containment.

These are the exception to the "text-level assertions only" note in
test_coverage_workflow_contract.py. That note is right about what a substring
check can pin -- a phrase in a prompt, a field in a schema -- and wrong as a
ceiling. Batch slicing is arithmetic, identity reconciliation is a join, and
anchor membership is string arithmetic; a substring assertion over any of them
restates the implementation instead of testing it. The two defects an adversarial
review found in the first version of this lane -- positional reconciliation
MISFILING one subject's findings under another subject's root, and a path-prefix
containment check that passed empty strings, foreign files and same-named
directories in other modules -- were both invisible to every text pin in this
suite and both are caught here by execution.

So this module EXECUTES the lane, under a harness that supplies the primitives
the Workflow tool injects (`args`, `agent`, `parallel`, `phase`, `log`), records
what the lane did with them, AND validates every canned agent response against
the schema the lane actually passed to `agent()`. That last part matters: a test
feeding a response the real harness would have rejected proves nothing, and the
earlier version of these tests had no way to notice.

What the harness still is not: it is not the Workflow tool. It cannot catch the
two unrunnable shapes described in skills-kit's CLAUDE.md (a bare `input`, a
stray backtick in prompt prose) -- only a real dispatch and the tagged-template
guard in test_workflow_js_drift.py catch those respectively.

Node is required; the module skips without it.
"""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MD_DOMAIN = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
DETECT = MD_DOMAIN / "workflow" / "coverage-detect.js"
LANE = MD_DOMAIN / "references" / "lanes" / "coverage-lane.md"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# The anchor-rule case table lives with the Python implementation of the same
# rule (scripts/coverage_subjects.py, exercised by test_coverage_subjects.py) and
# is imported here so BOTH implementations are pinned to one table. Loaded by
# path rather than by package import: these test modules are not a namespace this
# suite imports across, and the path load has no such assumption.
_CASES_SPEC = importlib.util.spec_from_file_location(
    "_coverage_subjects_tests", Path(__file__).with_name("test_coverage_subjects.py")
)
_CASES_MOD = importlib.util.module_from_spec(_CASES_SPEC)
_CASES_SPEC.loader.exec_module(_CASES_MOD)
ANCHOR_CASES = _CASES_MOD.ANCHOR_CASES

REFS = {
    "criteria": "/abs/standards/coverage-standards.md",
    "observationKinds": "/abs/standards/claude-md-standards.md",
    "placement": "/abs/cohesion-principles.md",
}

# The lane is a Workflow script: `export const meta` at the top and a top-level
# `return` at the bottom. Neither is legal in a plain script or a plain module,
# so the harness strips the export keyword and wraps the body in an async
# function whose parameters are the injected primitives.
#
# `validate` is a deliberately small subset of JSON Schema -- exactly the
# keywords this lane's schemas use. It exists so a test cannot smuggle in a
# response shape the real dispatch would have refused.
HARNESS = r"""
const fs = require('fs')

function validate(schema, value, path, errs) {
  if (!schema) return
  if (schema.type === 'object') {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      errs.push(path + ': not an object'); return
    }
    for (const k of (schema.required || [])) {
      if (!Object.prototype.hasOwnProperty.call(value, k)) errs.push(path + '.' + k + ': missing required')
    }
    const props = schema.properties || {}
    if (schema.additionalProperties === false) {
      for (const k of Object.keys(value)) if (!props[k]) errs.push(path + '.' + k + ': additional property')
    }
    for (const k of Object.keys(props)) {
      if (Object.prototype.hasOwnProperty.call(value, k)) validate(props[k], value[k], path + '.' + k, errs)
    }
    return
  }
  if (schema.type === 'array') {
    if (!Array.isArray(value)) { errs.push(path + ': not an array'); return }
    if (typeof schema.minItems === 'number' && value.length < schema.minItems) errs.push(path + ': below minItems')
    if (typeof schema.maxItems === 'number' && value.length > schema.maxItems) errs.push(path + ': above maxItems')
    value.forEach((v, i) => validate(schema.items, v, path + '[' + i + ']', errs))
    return
  }
  if (schema.type === 'string') {
    if (typeof value !== 'string') { errs.push(path + ': not a string'); return }
    if (typeof schema.minLength === 'number' && value.length < schema.minLength) errs.push(path + ': below minLength')
    if (schema.enum && schema.enum.indexOf(value) === -1) errs.push(path + ': not in enum')
    return
  }
  if (schema.type === 'integer') { if (!Number.isInteger(value)) errs.push(path + ': not an integer'); return }
  if (schema.type === 'boolean') { if (typeof value !== 'boolean') errs.push(path + ': not a boolean'); return }
}

// Answer a subjectsFile batch by actually READING the slice the prompt names.
// This is the only way to exercise the line arithmetic against a real file.
function answerFromFile(prompt) {
  const m = /sed -n '(\d+),(\d+)p' "([^"]+)"/.exec(prompt)
  if (!m) return { subjects: [] }
  const start = Number(m[1]), end = Number(m[2]), file = m[3]
  let lines = []
  try { lines = fs.readFileSync(file, 'utf8').split(/\r?\n/) } catch (e) { return { subjects: [] } }
  const out = []
  for (let n = start; n <= end; n++) {
    const raw = lines[n - 1]
    if (raw === undefined || raw.trim() === '') continue   // omit, never invent
    let rec
    try { rec = JSON.parse(raw) } catch (e) { continue }   // omit, never invent
    const cf = rec.codeFiles || []
    out.push({
      subjectKey: 'L' + n,
      root: rec.root,
      codeFiles: cf,
      candidates: cf.length ? [{
        fact: 'a fact about ' + rec.root,
        destination: rec.root,
        why: 'because',
        tier: 'CONTEXT-ONLY',
        anchors: [cf[0] + ':7'],
      }] : [],
      verdict: cf.length ? 'GAPS-FOUND' : 'COVERAGE-ASSESSED',
      ceilingReached: false,
      notes: [],
      assessedFileCount: cf.length,
      unknownExtensionCount: Object.keys(rec.unknownExtensions || {}).length,
      ambientChainCount: (rec.ambientClaudeMdPaths || []).length,
    })
  }
  return { subjects: out }
}

const src = fs.readFileSync(process.argv[2], 'utf8').replace('export const meta', 'const meta')
const spec = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'))
const calls = []
const logs = []
const schemaErrors = []
const agent = (prompt, opts) => {
  const i = calls.length
  calls.push({ prompt, opts })
  let resp
  if (spec.readSubjectsFile) resp = answerFromFile(prompt)
  else {
    const responses = spec.responses || []
    resp = i < responses.length ? responses[i] : (spec.defaultResponse || { subjects: [] })
  }
  const errs = []
  validate(opts.schema, resp, 'response[' + i + ']', errs)
  for (const e of errs) schemaErrors.push(e)
  return Promise.resolve(resp)
}
const parallel = (thunks) => Promise.all(thunks.map((t) => t()))
const phase = () => {}
const log = (m) => logs.push(m)
let fn
try {
  fn = new Function(
    'args', 'agent', 'parallel', 'phase', 'log',
    '"use strict"; return (async () => {\n' + src + '\n})()'
  )
} catch (e) {
  console.log(JSON.stringify({ ok: false, stage: 'parse', error: String((e && e.message) || e) }))
  process.exit(0)
}
Promise.resolve()
  .then(() => fn(spec.args, agent, parallel, phase, log))
  .then((result) => console.log(JSON.stringify({
    ok: true,
    result,
    logs,
    schemaErrors,
    calls: calls.map((c) => ({
      label: c.opts.label,
      model: c.opts.model,
      effort: c.opts.effort,
      phase: c.opts.phase,
      schema: c.opts.schema,
      prompt: c.prompt,
    })),
  })))
  .catch((e) => console.log(JSON.stringify({
    ok: false, stage: 'run', error: String((e && e.message) || e), logs,
  })))
"""


def run_lane(tmp_path, args, responses=None, default_response=None,
             read_subjects_file=False):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps({
            "args": args,
            "responses": responses or [],
            "defaultResponse": default_response or {"subjects": []},
            "readSubjectsFile": bool(read_subjects_file),
        }),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE, str(harness), str(DETECT), str(spec)],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def subject(root, files=None, chain=(), unknown=None):
    code = list(files) if files is not None else [f"{root}/f0.py"]
    return {
        "root": root,
        "codeFiles": code,
        "ambientClaudeMdPaths": list(chain),
        "rootExclusion": None,
        "skipped": [],
        "unknownExtensions": unknown or {},
    }


def result_for(key, root, candidates=(), verdict="COVERAGE-ASSESSED",
               code_files=None, unknown=0, chain=0, file_count=None):
    code = list(code_files) if code_files is not None else [f"{root}/f0.py"]
    return {
        "subjectKey": key,
        "root": root,
        "codeFiles": code,
        "candidates": list(candidates),
        "verdict": verdict,
        "ceilingReached": False,
        "notes": [],
        "assessedFileCount": len(code) if file_count is None else file_count,
        "unknownExtensionCount": unknown,
        "ambientChainCount": chain,
    }


def candidate(destination, anchors, tier="CONTEXT-ONLY"):
    return {
        "fact": "a fact",
        "destination": destination,
        "why": "because",
        "tier": tier,
        "anchors": list(anchors),
    }


def by_root(out):
    return {r["root"]: r for r in out["result"]["perSubject"]}


BASE = {"depth": "basic", "refs": REFS}


class TestTheHarnessItself:
    def test_the_lane_parses_and_runs(self, tmp_path):
        """A guard on the guard: every other test here is vacuous if it does not."""
        out = run_lane(tmp_path, {**BASE, "subjects": [subject("/r/a")]})
        assert out["ok"], out.get("error")

    def test_the_harness_enforces_the_lane_own_schema(self, tmp_path):
        """Without this, a test can feed a shape a real dispatch would refuse."""
        args = {**BASE, "subjects": [subject("/r/a")]}
        bad = {"subjects": [{"subjectKey": "S1", "root": "/r/a"}]}
        out = run_lane(tmp_path, args, [bad])
        assert any("missing required" in e for e in out["schemaErrors"])

    def test_the_pinned_tier_survives_batching(self, tmp_path):
        out = run_lane(tmp_path, {**BASE, "subjects": [subject("/r/a"), subject("/r/b")]})
        assert [c["model"] for c in out["calls"]] == ["opus"]
        assert [c["effort"] for c in out["calls"]] == ["high"]


class TestBatchSlicing:
    def test_inline_subjects_are_cut_into_batches_of_batch_size(self, tmp_path):
        args = {**BASE, "batchSize": 2,
                "subjects": [subject(f"/r/{c}") for c in "abcde"]}
        out = run_lane(tmp_path, args)
        assert out["result"]["batches"] == 3
        assert len(out["calls"]) == 3

    def test_the_last_batch_is_short_not_dropped(self, tmp_path):
        args = {**BASE, "batchSize": 2,
                "subjects": [subject(f"/r/{c}") for c in "abcde"]}
        responses = [
            {"subjects": [result_for("S1", "/r/a"), result_for("S2", "/r/b")]},
            {"subjects": [result_for("S3", "/r/c"), result_for("S4", "/r/d")]},
            {"subjects": [result_for("S5", "/r/e")]},
        ]
        out = run_lane(tmp_path, args, responses)
        assert out["schemaErrors"] == []
        assert [r["root"] for r in out["result"]["perSubject"]] == [
            "/r/a", "/r/b", "/r/c", "/r/d", "/r/e"]
        assert out["result"]["totals"]["completed"] == 5

    def test_batch_size_larger_than_the_subject_count_is_one_batch(self, tmp_path):
        args = {**BASE, "batchSize": 50,
                "subjects": [subject("/r/a"), subject("/r/b")]}
        out = run_lane(tmp_path, args)
        assert out["result"]["batches"] == 1

    def test_a_single_subject_still_goes_through_the_workflow(self, tmp_path):
        out = run_lane(tmp_path, {**BASE, "subjects": [subject("/r/a")]})
        assert len(out["calls"]) == 1
        assert out["calls"][0]["model"] == "opus"

    def test_batch_size_defaults_and_rejects_nonsense(self, tmp_path):
        for bad in (0, -3, "eight", 2.5):
            out = run_lane(tmp_path, {**BASE, "batchSize": bad,
                                      "subjects": [subject("/r/a")]})
            assert out["ok"], out.get("error")
            assert out["result"]["batchSize"] == 8

    def test_a_discovery_failure_does_not_consume_a_batch_slot(self, tmp_path):
        args = {**BASE, "batchSize": 2, "subjects": [
            subject("/r/a"),
            subject("/r/bad", files=[], unknown={".weird": 3}),
            subject("/r/b"),
        ]}
        out = run_lane(tmp_path, args)
        assert out["result"]["batches"] == 1
        assert by_root(out)["/r/bad"]["verdict"] == "DISCOVERY-FAILED"
        assert "/r/bad" not in out["calls"][0]["prompt"]


class TestIdentityReconciliation:
    """The misfiling defect, and its fix.

    Positional zipping does not merely lose a subject the agent skipped: it
    shifts every later result one slot, and the inline root OVERWRITE -- meant as
    a safeguard -- then stamps the wrong directory onto real findings. Matching
    by a harness-issued key is what makes an omission a loss rather than a
    fabrication.
    """

    def _abc(self):
        return {**BASE, "batchSize": 3,
                "subjects": [subject("/r/a"), subject("/r/b"), subject("/r/c")]}

    def test_a_middle_omission_loses_that_subject_and_only_that_subject(self, tmp_path):
        responses = [{"subjects": [
            result_for("S1", "/r/a"),
            result_for("S3", "/r/c",
                       [candidate("/r/c", ["/r/c/f0.py:9"])], verdict="GAPS-FOUND"),
        ]}]
        out = run_lane(tmp_path, self._abc(), responses)
        assert out["schemaErrors"] == []
        recs = by_root(out)
        assert recs["/r/b"]["verdict"] == "BATCH-INCOMPLETE"
        assert recs["/r/b"]["candidates"] == []
        # The decisive assertion: C's finding is filed under C, not under B.
        assert recs["/r/c"]["verdict"] == "GAPS-FOUND"
        assert len(recs["/r/c"]["candidates"]) == 1
        assert recs["/r/c"]["candidates"][0]["destination"] == "/r/c"

    def test_results_returned_out_of_order_are_still_filed_correctly(self, tmp_path):
        responses = [{"subjects": [
            result_for("S3", "/r/c", [candidate("/r/c", ["/r/c/f0.py:1"])],
                       verdict="GAPS-FOUND"),
            result_for("S1", "/r/a"),
            result_for("S2", "/r/b"),
        ]}]
        out = run_lane(tmp_path, self._abc(), responses)
        assert out["result"]["totals"]["completed"] == 3
        assert len(by_root(out)["/r/c"]["candidates"]) == 1

    def test_an_unrequested_key_is_discarded_and_counted(self, tmp_path):
        responses = [{"subjects": [
            result_for("S1", "/r/a"),
            result_for("S9", "/r/invented"),
        ]}]
        out = run_lane(tmp_path, self._abc(), responses)
        assert "/r/invented" not in by_root(out)
        assert out["result"]["totals"]["identityUnmatched"] == 1
        assert out["result"]["totals"]["extraReturned"] == 1

    def test_a_duplicated_key_keeps_the_first_and_discards_the_rest(self, tmp_path):
        responses = [{"subjects": [
            result_for("S1", "/r/a", [candidate("/r/a", ["/r/a/f0.py:1"])],
                       verdict="GAPS-FOUND"),
            result_for("S1", "/r/a"),
        ]}]
        out = run_lane(tmp_path, self._abc(), responses)
        assert len(by_root(out)["/r/a"]["candidates"]) == 1
        assert out["result"]["totals"]["extraReturned"] == 1

    def test_an_inline_root_is_taken_from_the_input_not_the_agent(self, tmp_path):
        responses = [{"subjects": [
            result_for("S1", "NONSENSE"),
            result_for("S2", "ALSO-WRONG"),
            result_for("S3", "/r/c"),
        ]}]
        out = run_lane(tmp_path, self._abc(), responses)
        assert sorted(by_root(out)) == ["/r/a", "/r/b", "/r/c"]

    def test_every_record_carries_its_key_and_provenance(self, tmp_path):
        out = run_lane(tmp_path, self._abc(),
                       [{"subjects": [result_for("S1", "/r/a")]}])
        for rec in out["result"]["perSubject"]:
            assert rec["provenance"] == "harness-verified"
            assert rec["subjectKey"] in ("S1", "S2", "S3")


class TestAnchorMembership:
    """The containment defect, and its fix.

    A path-prefix test ("is the anchor under the root") passed an empty string, a
    file that does not exist, a foreign file, and a same-named directory in
    another module. Membership in the subject's own concrete code-file list has
    none of those surfaces.
    """

    def _run(self, tmp_path, anchors, files=None, root="/r/b"):
        subj_b = subject(root, files=files)
        args = {**BASE, "batchSize": 2, "subjects": [subject("/r/a"), subj_b]}
        responses = [{"subjects": [
            result_for("S1", "/r/a"),
            result_for("S2", root, [candidate(root, anchors)],
                       verdict="GAPS-FOUND", code_files=subj_b["codeFiles"]),
        ]}]
        out = run_lane(tmp_path, args, responses)
        assert out["ok"], out.get("error")
        return by_root(out)[root], out

    def test_an_anchor_naming_one_of_the_subjects_own_files_survives(self, tmp_path):
        rec, out = self._run(tmp_path, ["/r/b/f0.py:12"])
        assert len(rec["candidates"]) == 1
        assert out["result"]["totals"]["isolationViolations"] == 0

    def test_a_sibling_subjects_file_is_rejected(self, tmp_path):
        rec, out = self._run(tmp_path, ["/r/a/f0.py:12"])
        assert rec["candidates"] == []
        assert out["result"]["totals"]["isolationViolations"] == 1

    def test_a_parent_file_is_rejected(self, tmp_path):
        rec, _ = self._run(tmp_path, ["/r/shared.py:3"])
        assert rec["candidates"] == []

    def test_a_subdirectory_file_is_rejected(self, tmp_path):
        rec, _ = self._run(tmp_path, ["/r/b/sub/f.py:3"])
        assert rec["candidates"] == []

    def test_a_file_not_in_the_list_is_rejected_even_under_the_root(self, tmp_path):
        """The hole a prefix test cannot see: right directory, wrong file."""
        rec, _ = self._run(tmp_path, ["/r/b/never_discovered.py:3"])
        assert rec["candidates"] == []

    def test_a_bare_foreign_filename_is_rejected(self, tmp_path):
        rec, _ = self._run(tmp_path, ["foreign.py:1"])
        assert rec["candidates"] == []

    def test_an_empty_anchor_string_is_rejected_by_schema_and_by_the_lane(self, tmp_path):
        rec, out = self._run(tmp_path, [""])
        assert any("minLength" in e for e in out["schemaErrors"])
        assert rec["candidates"] == []

    def test_an_empty_anchor_array_is_rejected_by_schema_and_by_the_lane(self, tmp_path):
        rec, out = self._run(tmp_path, [])
        assert any("minItems" in e for e in out["schemaErrors"])
        assert rec["candidates"] == []

    def test_an_anchor_without_a_line_number_is_rejected(self, tmp_path):
        """CV-7 asks for a file AND a line."""
        rec, _ = self._run(tmp_path, ["/r/b/f0.py"])
        assert rec["candidates"] == []

    def test_a_zero_line_number_is_rejected(self, tmp_path):
        rec, _ = self._run(tmp_path, ["/r/b/f0.py:0"])
        assert rec["candidates"] == []

    def test_a_line_and_column_anchor_is_accepted(self, tmp_path):
        rec, _ = self._run(tmp_path, ["/r/b/f0.py:12:4"])
        assert len(rec["candidates"]) == 1

    def test_a_unique_relative_spelling_is_accepted(self, tmp_path):
        rec, _ = self._run(tmp_path, ["f0.py:12"])
        assert len(rec["candidates"]) == 1

    def test_an_ambiguous_relative_spelling_is_rejected(self, tmp_path):
        """Two files of the same name in one list: the citation names neither."""
        rec, _ = self._run(
            tmp_path, ["file.cpp:1"],
            files=["/r/b/Private/file.cpp", "/r/b/Public/file.cpp"])
        assert rec["candidates"] == []

    def test_a_same_named_directory_in_another_module_is_rejected(self, tmp_path):
        """The suffix-match hole: root /repo/A/Private, anchor from module B."""
        rec, _ = self._run(
            tmp_path, ["Private/file.cpp:1"], root="/repo/A/Private",
            files=["/repo/A/Private/other.cpp"])
        assert rec["candidates"] == []

    def test_a_segment_boundary_is_required_for_a_suffix_match(self, tmp_path):
        rec, _ = self._run(tmp_path, ["f.py:1"], files=["/r/b/conf.py"])
        assert rec["candidates"] == []

    def test_one_bad_anchor_condemns_the_whole_candidate(self, tmp_path):
        rec, _ = self._run(tmp_path, ["/r/b/f0.py:1", "/r/a/f0.py:2"])
        assert rec["candidates"] == []

    def test_a_subject_stripped_of_every_candidate_reads_as_assessed(self, tmp_path):
        rec, _ = self._run(tmp_path, ["/r/a/f0.py:12"])
        assert rec["verdict"] == "COVERAGE-ASSESSED"
        assert any("isolation" in n for n in rec["notes"])

    def test_the_run_summary_names_the_violations(self, tmp_path):
        _, out = self._run(tmp_path, ["/r/a/f0.py:12"])
        assert "cross-subject contamination" in out["logs"][0]


class TestPathNormalization:
    def _run(self, tmp_path, root, files, anchors):
        subj = subject(root, files=files)
        args = {**BASE, "subjects": [subj]}
        responses = [{"subjects": [
            result_for("S1", root, [candidate(root, anchors)],
                       verdict="GAPS-FOUND", code_files=files),
        ]}]
        out = run_lane(tmp_path, args, responses)
        assert out["ok"], out.get("error")
        return by_root(out)[root]

    def test_windows_case_differences_do_not_reject_a_valid_anchor(self, tmp_path):
        rec = self._run(tmp_path, "C:\\Repo\\Source\\Net",
                        ["C:\\Repo\\Source\\Net\\f.py"],
                        ["c:\\repo\\source\\net\\f.py:1"])
        assert len(rec["candidates"]) == 1

    def test_windows_separators_mixed_with_posix_compare_equal(self, tmp_path):
        rec = self._run(tmp_path, "C:\\Repo\\Src", ["C:\\Repo\\Src\\f.py"],
                        ["C:/Repo/Src/f.py:22"])
        assert len(rec["candidates"]) == 1

    def test_a_windows_drive_anchor_splits_at_the_last_colon(self, tmp_path):
        rec = self._run(tmp_path, "C:\\Repo\\Src", ["C:\\Repo\\Src\\f.cpp"],
                        ["C:\\Repo\\Src\\f.cpp:12:4"])
        assert len(rec["candidates"]) == 1

    def test_posix_case_differences_still_separate_two_files(self, tmp_path):
        """Folding everywhere would merge files that legitimately differ."""
        rec = self._run(tmp_path, "/r/b", ["/r/b/File.py"], ["/r/b/file.py:1"])
        assert rec["candidates"] == []

    def test_dot_and_dotdot_spellings_resolve(self, tmp_path):
        rec = self._run(tmp_path, "/r/b", ["/r/b/f.py"], ["/r/b/./sub/../f.py:1"])
        assert len(rec["candidates"]) == 1

    def test_sibling_prefixes_are_still_separated(self, tmp_path):
        """Source/Net must not match Source/NetCore."""
        rec = self._run(tmp_path, "/r/Source/Net", ["/r/Source/Net/f.py"],
                        ["/r/Source/NetCore/f.py:1"])
        assert rec["candidates"] == []

    def test_unicode_spellings_are_normalized_before_comparison(self, tmp_path):
        composed = "/r/caf\u00e9/f.py"
        decomposed = "/r/cafe\u0301/f.py"
        rec = self._run(tmp_path, "/r/caf\u00e9", [composed], [decomposed + ":3"])
        assert len(rec["candidates"]) == 1


class TestDestinationIsDerived:
    def test_a_candidate_naming_another_directory_is_corrected_and_counted(self, tmp_path):
        args = {**BASE, "batchSize": 2, "subjects": [subject("/r/a"), subject("/r/b")]}
        responses = [{"subjects": [
            result_for("S1", "/r/a"),
            result_for("S2", "/r/b", [candidate("/r/a", ["/r/b/f0.py:1"])],
                       verdict="GAPS-FOUND"),
        ]}]
        out = run_lane(tmp_path, args, responses)
        rec = by_root(out)["/r/b"]
        assert rec["candidates"][0]["destination"] == "/r/b"
        assert out["result"]["totals"]["destinationCorrected"] == 1
        assert any("destination corrected" in n for n in rec["notes"])
        assert "destination(s) corrected" in out["logs"][0]

    def test_a_correct_destination_is_left_alone(self, tmp_path):
        args = {**BASE, "subjects": [subject("/r/a")]}
        responses = [{"subjects": [
            result_for("S1", "/r/a", [candidate("/r/a", ["/r/a/f0.py:1"])],
                       verdict="GAPS-FOUND"),
        ]}]
        out = run_lane(tmp_path, args, responses)
        assert out["result"]["totals"]["destinationCorrected"] == 0


class TestSubjectsFileMode:
    def test_slices_are_contiguous_one_based_line_ranges(self, tmp_path):
        args = {**BASE, "subjectsFile": "/abs/subjects.jsonl",
                "subjectCount": 5, "batchSize": 2}
        out = run_lane(tmp_path, args)
        prompts = [c["prompt"] for c in out["calls"]]
        assert "sed -n '1,2p'" in prompts[0]
        assert "sed -n '3,4p'" in prompts[1]
        assert "sed -n '5,5p'" in prompts[2]

    def test_every_line_is_covered_exactly_once(self, tmp_path):
        args = {**BASE, "subjectsFile": "/abs/subjects.jsonl",
                "subjectCount": 23, "batchSize": 4}
        out = run_lane(tmp_path, args)
        covered = []
        for call in out["calls"]:
            lo, hi = (int(x) for x in call["label"].split(":lines")[1].split("-"))
            covered.extend(range(lo, hi + 1))
        assert covered == list(range(1, 24))

    def test_keys_are_line_numbers_and_are_unique_across_the_run(self, tmp_path):
        args = {**BASE, "subjectsFile": "/abs/subjects.jsonl",
                "subjectCount": 7, "batchSize": 3}
        out = run_lane(tmp_path, args)
        keys = [r["subjectKey"] for r in out["result"]["perSubject"]]
        assert keys == [f"L{n}" for n in range(1, 8)]

    def test_batch_size_larger_than_the_line_count(self, tmp_path):
        args = {**BASE, "subjectsFile": "/abs/subjects.jsonl",
                "subjectCount": 3, "batchSize": 100}
        out = run_lane(tmp_path, args)
        assert out["result"]["batches"] == 1
        assert "sed -n '1,3p'" in out["calls"][0]["prompt"]

    def test_the_brief_carries_the_path_not_the_payload(self, tmp_path):
        args = {**BASE, "subjectsFile": "/abs/subjects.jsonl",
                "subjectCount": 4, "batchSize": 2}
        prompt = run_lane(tmp_path, args)["calls"][0]["prompt"]
        assert "/abs/subjects.jsonl" in prompt
        assert "DO NOT read the whole file" in prompt

    def test_a_relative_subjects_file_is_refused(self, tmp_path):
        out = run_lane(tmp_path, {**BASE, "subjectsFile": "rel/subjects.jsonl",
                                  "subjectCount": 2})
        assert not out["ok"]
        assert "ABSOLUTE path" in out["error"]

    def test_a_windows_absolute_subjects_file_is_accepted(self, tmp_path):
        out = run_lane(tmp_path, {**BASE, "subjectsFile": "C:\\runs\\subjects.jsonl",
                                  "subjectCount": 2})
        assert out["ok"], out.get("error")

    def test_subjects_file_without_a_count_is_refused(self, tmp_path):
        out = run_lane(tmp_path, {**BASE, "subjectsFile": "/abs/subjects.jsonl"})
        assert not out["ok"]
        assert "subjectCount" in out["error"]

    def test_a_zero_or_negative_count_is_refused(self, tmp_path):
        for bad in (0, -1):
            out = run_lane(tmp_path, {**BASE, "subjectsFile": "/abs/s.jsonl",
                                      "subjectCount": bad})
            assert not out["ok"]

    def test_neither_input_mode_is_refused(self, tmp_path):
        out = run_lane(tmp_path, dict(BASE))
        assert not out["ok"]
        assert "no subjects" in out["error"]

    def test_records_are_stamped_agent_attested(self, tmp_path):
        args = {**BASE, "subjectsFile": "/abs/s.jsonl", "subjectCount": 1}
        responses = [{"subjects": [result_for("L1", "/r/a")]}]
        out = run_lane(tmp_path, args, responses)
        assert out["result"]["provenance"] == "agent-attested"
        assert out["result"]["perSubject"][0]["provenance"] == "agent-attested"
        assert "AGENT-ATTESTED" in out["logs"][0]

    def test_an_invented_root_is_accepted_but_marked_attested(self, tmp_path):
        """Stated plainly rather than papered over: this lane cannot check it.

        The key still binds the result to a line the script requested, so the
        record is attributable; what it is not is verified.
        """
        args = {**BASE, "subjectsFile": "/abs/s.jsonl", "subjectCount": 1}
        responses = [{"subjects": [result_for("L1", "/totally/made/up")]}]
        out = run_lane(tmp_path, args, responses)
        rec = out["result"]["perSubject"][0]
        assert rec["root"] == "/totally/made/up"
        assert rec["provenance"] == "agent-attested"

    def test_the_echoed_file_list_never_returns_to_the_orchestrator(self, tmp_path):
        """It exists to check anchors, not to be carried back through context."""
        args = {**BASE, "subjectsFile": "/abs/s.jsonl", "subjectCount": 1}
        responses = [{"subjects": [
            result_for("L1", "/r/a", code_files=[f"/r/a/f{i}.py" for i in range(40)]),
        ]}]
        out = run_lane(tmp_path, args, responses)
        assert "codeFiles" not in out["result"]["perSubject"][0]

    def test_a_file_count_disagreeing_with_its_own_list_is_flagged(self, tmp_path):
        args = {**BASE, "subjectsFile": "/abs/s.jsonl", "subjectCount": 1}
        responses = [{"subjects": [result_for("L1", "/r/a", file_count=99)]}]
        out = run_lane(tmp_path, args, responses)
        assert out["result"]["totals"]["fileListDisagreements"] == 1
        assert any("transcription disagreement" in n
                   for n in out["result"]["perSubject"][0]["notes"])


class TestAgainstARealSubjectsFile:
    """Every subjectsFile test above names a file that does not exist.

    That is fine for the guards and useless as evidence that the line arithmetic
    addresses the right records. Here the harness agent actually opens the file
    and answers from the slice the prompt named.
    """

    def _write(self, tmp_path, roots, blanks=(), malformed=()):
        lines = []
        for i, root in enumerate(roots, start=1):
            if i in blanks:
                lines.append("")
            elif i in malformed:
                lines.append("{not json at all")
            else:
                lines.append(json.dumps(subject(root)))
        path = tmp_path / "subjects.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_each_agent_reads_only_its_own_slice_and_files_it_correctly(self, tmp_path):
        roots = [f"/repo/dir{i}" for i in range(1, 8)]
        path = self._write(tmp_path, roots)
        args = {**BASE, "subjectsFile": str(path.resolve()),
                "subjectCount": 7, "batchSize": 3}
        out = run_lane(tmp_path, args, read_subjects_file=True)
        assert out["ok"], out.get("error")
        assert out["schemaErrors"] == []
        assert [r["root"] for r in out["result"]["perSubject"]] == roots
        assert out["result"]["totals"]["completed"] == 7
        assert out["result"]["totals"]["isolationViolations"] == 0
        for rec in out["result"]["perSubject"]:
            for cand in rec["candidates"]:
                assert cand["destination"] == rec["root"]

    def test_a_blank_line_becomes_not_assessed_never_an_invented_subject(self, tmp_path):
        roots = [f"/repo/dir{i}" for i in range(1, 6)]
        path = self._write(tmp_path, roots, blanks=(3,))
        args = {**BASE, "subjectsFile": str(path.resolve()),
                "subjectCount": 5, "batchSize": 2}
        out = run_lane(tmp_path, args, read_subjects_file=True)
        recs = out["result"]["perSubject"]
        assert recs[2]["verdict"] == "BATCH-INCOMPLETE"
        assert recs[2]["status"] == "NOT-ASSESSED"
        assert out["result"]["totals"]["completed"] == 4
        assert out["result"]["totals"]["requested"] == 5

    def test_a_malformed_line_becomes_not_assessed(self, tmp_path):
        roots = [f"/repo/dir{i}" for i in range(1, 5)]
        path = self._write(tmp_path, roots, malformed=(2,))
        args = {**BASE, "subjectsFile": str(path.resolve()),
                "subjectCount": 4, "batchSize": 4}
        out = run_lane(tmp_path, args, read_subjects_file=True)
        assert out["result"]["perSubject"][1]["status"] == "NOT-ASSESSED"
        assert out["result"]["totals"]["notAssessed"] == 1

    def test_a_subject_count_higher_than_the_file_is_visible_not_silent(self, tmp_path):
        roots = [f"/repo/dir{i}" for i in range(1, 4)]
        path = self._write(tmp_path, roots)
        args = {**BASE, "subjectsFile": str(path.resolve()),
                "subjectCount": 6, "batchSize": 3}
        out = run_lane(tmp_path, args, read_subjects_file=True)
        totals = out["result"]["totals"]
        assert totals["requested"] == 6
        assert totals["completed"] == 3
        assert totals["notAssessed"] == 3
        assert "3 of 6 requested" in out["logs"][0]

    def test_a_subject_count_lower_than_the_file_never_requests_the_tail(self, tmp_path):
        roots = [f"/repo/dir{i}" for i in range(1, 7)]
        path = self._write(tmp_path, roots)
        args = {**BASE, "subjectsFile": str(path.resolve()),
                "subjectCount": 4, "batchSize": 2}
        out = run_lane(tmp_path, args, read_subjects_file=True)
        assert out["result"]["totals"]["requested"] == 4
        assert [r["root"] for r in out["result"]["perSubject"]] == roots[:4]


class TestInputModePrecedence:
    def test_inline_subjects_win_over_a_subjects_file(self, tmp_path):
        args = {**BASE, "subjects": [subject("/r/a")],
                "subjectsFile": "/abs/subjects.jsonl", "subjectCount": 900}
        out = run_lane(tmp_path, args)
        assert out["ok"], out.get("error")
        assert out["result"]["subjectsFile"] is None
        assert out["result"]["provenance"] == "harness-verified"

    def test_an_ignored_subjects_file_is_never_validated(self, tmp_path):
        """The mode is selected first; a file taking no part cannot fail the run."""
        args = {**BASE, "subjects": [subject("/r/a")],
                "subjectsFile": "rel/not/absolute.jsonl"}
        out = run_lane(tmp_path, args)
        assert out["ok"], out.get("error")
        assert out["result"]["batches"] == 1

    def test_the_ignored_file_is_announced_never_silent(self, tmp_path):
        args = {**BASE, "subjects": [subject("/r/a")],
                "subjectsFile": "/abs/subjects.jsonl", "subjectCount": 900}
        out = run_lane(tmp_path, args)
        assert any("inline subjects WIN" in n for n in out["result"]["notes"])
        assert "NOTE:" in out["logs"][0]


class TestNotAssessedIsHonestEndToEnd:
    def test_a_wholly_skipped_batch_is_expressible_under_the_schema(self, tmp_path):
        """minItems on the envelope made the not-assessed path unreachable."""
        args = {**BASE, "batchSize": 2, "subjects": [subject("/r/a"), subject("/r/b")]}
        out = run_lane(tmp_path, args, [{"subjects": []}])
        assert out["schemaErrors"] == []
        assert out["result"]["totals"]["batchIncomplete"] == 2
        assert out["result"]["totals"]["completed"] == 0

    def test_the_summary_reports_completed_against_requested(self, tmp_path):
        args = {**BASE, "batchSize": 3,
                "subjects": [subject("/r/a"), subject("/r/b"), subject("/r/c")]}
        out = run_lane(tmp_path, args, [{"subjects": [result_for("S1", "/r/a")]}])
        assert "1 of 3 requested" in out["logs"][0]
        assert "2 NOT assessed" in out["logs"][0]

    def test_an_unassessed_subject_carries_a_distinct_persisted_status(self, tmp_path):
        """A consumer reading only `candidates` cannot tell empty from unread.

        `status` is the field that makes the two distinguishable in a persisted
        report; the lane doc states the gate that must use it.
        """
        args = {**BASE, "batchSize": 2, "subjects": [subject("/r/a"), subject("/r/b")]}
        out = run_lane(tmp_path, args, [{"subjects": [result_for("S1", "/r/a")]}])
        recs = by_root(out)
        assert recs["/r/a"]["status"] == "ASSESSED"
        assert recs["/r/b"]["status"] == "NOT-ASSESSED"
        assert recs["/r/a"]["candidates"] == recs["/r/b"]["candidates"] == []

    def test_a_discovery_failure_also_carries_not_assessed(self, tmp_path):
        args = {**BASE, "subjects": [subject("/r/bad", files=[],
                                             unknown={".weird": 1})]}
        out = run_lane(tmp_path, args)
        rec = out["result"]["perSubject"][0]
        assert rec["verdict"] == "DISCOVERY-FAILED"
        assert rec["status"] == "NOT-ASSESSED"

    def test_an_unassessed_subject_is_not_counted_as_uncovered(self, tmp_path):
        """Nobody read it, so it is not evidence that nothing covers it."""
        args = {**BASE, "batchSize": 2,
                "subjects": [subject("/r/a", chain=["/r/CLAUDE.md"]), subject("/r/b")]}
        out = run_lane(tmp_path, args, [{"subjects": [result_for("S1", "/r/a", chain=1)]}])
        assert out["result"]["totals"]["uncovered"] == 0


class TestDiscoveryFailureFromTranscribedCounts:
    def test_zero_code_files_with_unknown_extensions_never_reads_as_assessed(self, tmp_path):
        args = {**BASE, "subjectsFile": "/abs/s.jsonl", "subjectCount": 1}
        responses = [{"subjects": [
            result_for("L1", "/r/a", code_files=[], unknown=2),
        ]}]
        out = run_lane(tmp_path, args, responses)
        rec = out["result"]["perSubject"][0]
        assert rec["verdict"] == "DISCOVERY-FAILED"
        assert rec["status"] == "NOT-ASSESSED"
        assert out["result"]["totals"]["assessed"] == 0

    def test_zero_code_files_without_unknown_extensions_is_a_normal_result(self, tmp_path):
        args = {**BASE, "subjectsFile": "/abs/s.jsonl", "subjectCount": 1}
        responses = [{"subjects": [
            result_for("L1", "/r/a", code_files=[], unknown=0),
        ]}]
        out = run_lane(tmp_path, args, responses)
        assert out["result"]["perSubject"][0]["verdict"] == "COVERAGE-ASSESSED"


class TestUncoveredTallySource:
    def test_inline_mode_reads_the_chain_from_the_input(self, tmp_path):
        args = {**BASE, "subjects": [subject("/r/a", chain=["/r/CLAUDE.md"])]}
        responses = [{"subjects": [result_for("S1", "/r/a", chain=0)]}]
        out = run_lane(tmp_path, args, responses)
        assert out["result"]["totals"]["uncovered"] == 0

    def test_subjects_file_mode_falls_back_to_the_echoed_count(self, tmp_path):
        args = {**BASE, "subjectsFile": "/abs/s.jsonl", "subjectCount": 2,
                "batchSize": 2}
        responses = [{"subjects": [result_for("L1", "/r/a", chain=0),
                                   result_for("L2", "/r/b", chain=2)]}]
        out = run_lane(tmp_path, args, responses)
        assert out["result"]["totals"]["uncovered"] == 1


class TestTheAnchorRuleMatchesTheVerifier:
    """One rule, two implementations, one table.

    The lane checks anchors in JavaScript because a Workflow script is
    JavaScript; `scripts/coverage_subjects.py verify` checks the same anchors in
    Python because the caller-side re-check needs a filesystem. The rule cannot
    be shared across that boundary, so the risk is silent divergence -- a
    verifier that passes what the lane dropped, or drops what the lane passed, is
    worse than no verifier, because it makes a mismatch look like a verdict.

    The table is imported from the Python side and run through the lane here, in
    ONE dispatch: one subject per case, each carrying that case's own code-file
    list and one candidate holding that case's anchor. A candidate that survives
    means the lane accepted the anchor.
    """

    def test_every_case_agrees_with_the_python_implementation(self, tmp_path):
        subjects = [
            subject(f"/case{i}", files=list(files))
            for i, (_anchor, files, _ok) in enumerate(ANCHOR_CASES)
        ]
        args = {**BASE, "batchSize": len(ANCHOR_CASES), "subjects": subjects}
        responses = [{"subjects": [
            result_for(f"S{i + 1}", f"/case{i}",
                       [candidate(f"/case{i}", [anchor])],
                       verdict="GAPS-FOUND", code_files=list(files))
            for i, (anchor, files, _ok) in enumerate(ANCHOR_CASES)
        ]}]
        out = run_lane(tmp_path, args, responses)
        assert out["ok"], out.get("error")
        got = {r["subjectKey"]: bool(r["candidates"])
               for r in out["result"]["perSubject"]}
        disagreements = []
        for i, (anchor, files, accepted) in enumerate(ANCHOR_CASES):
            lane_accepted = got[f"S{i + 1}"]
            if lane_accepted != accepted:
                disagreements.append(
                    f"{anchor!r} against {files}: lane "
                    f"{'accepted' if lane_accepted else 'rejected'}, table says "
                    f"{'accept' if accepted else 'reject'}"
                )
        assert disagreements == [], (
            "the lane and scripts/coverage_subjects.py disagree about the anchor "
            "rule:\n  " + "\n  ".join(disagreements)
        )

    def test_the_table_is_not_empty_and_covers_both_outcomes(self):
        """A vacuous or one-sided table would pass either implementation."""
        assert len(ANCHOR_CASES) >= 10
        assert {row[2] for row in ANCHOR_CASES} == {True, False}


class TestLaneDocumentsTheContract:
    """The lane doc is the caller-facing contract; the two must not drift."""

    def _lane(self) -> str:
        return LANE.read_text(encoding="utf-8")

    def test_lane_documents_the_subjects_file_argument(self):
        text = self._lane()
        assert "subjectsFile" in text
        assert "subjectCount" in text
        assert "JSONL" in text

    def test_lane_documents_the_precedence(self):
        assert "inline `subjects[]` WINS" in self._lane()

    def test_lane_documents_batching_and_the_identity_key(self):
        text = self._lane()
        assert "batchSize" in text
        assert "subjectKey" in text

    def test_lane_documents_the_anchor_membership_rule(self):
        assert "membership" in self._lane().lower()

    def test_lane_documents_the_new_non_assessment_outcome(self):
        text = self._lane()
        assert "BATCH-INCOMPLETE" in text
        assert "NOT-ASSESSED" in text

    def test_lane_documents_the_generation_gate(self):
        """An incomplete report must not be fed to generation."""
        text = self._lane()
        assert "status" in text
        assert "generation" in text.lower()

    def test_lane_states_the_residual_risk_rather_than_claiming_isolation(self):
        """The claim that got rejected was "isolation is preserved"."""
        text = self._lane()
        assert "does not eliminate" in text
        assert "bounds" in text.lower()

    def test_lane_documents_the_provenance_split(self):
        text = self._lane()
        assert "agent-attested" in text
        assert "harness-verified" in text
        assert "Verifying an agent-attested run" in text


def verdict(index, truth="STANDS", counterexample="", files_read=2,
            files_in_dir=2, narrowing=None, quote=""):
    row = {
        "index": index,
        "truth": truth,
        "counterexample": counterexample,
        "filesRead": files_read,
        "filesInDir": files_in_dir,
        "quote": quote,
    }
    if narrowing is not None:
        row["narrowing"] = narrowing
    return row


class TestRefutationStage:
    """The verification stage -- the lane's only semantic enforcement.

    Everything else the lane checks is FORM (subject identity, anchor
    membership, destination, the verdict rule). Until this stage existed the
    truth of a fact was enforced only by the agent that proposed it, judging its
    own output in its own context, while the depth table told the caller the
    result was "verified absent".

    Several of these pin a REFUSAL rather than an action. That is deliberate:
    the failure mode being guarded is a gate that deletes candidates it cannot
    justify, which is what an unshipped version of this filter was measured
    doing over a real corpus.
    """

    def _run(self, tmp_path, cands, verdicts, verify=None, default=None):
        args = {
            "subjects": [subject("d", ["d/a.py", "d/b.py"])],
            "depth": "advanced",
            "refs": {"criteria": "/abs/coverage-standards.md"},
        }
        if verify is not None:
            args["verify"] = verify
        batch = {"subjects": [result_for(
            "S1", "d", cands, verdict="GAPS-FOUND",
            code_files=["d/a.py", "d/b.py"])]}
        responses = [batch]
        if verdicts is not None:
            responses.append({"verdicts": verdicts})
        return run_lane(tmp_path, args, responses=responses,
                        default_response=default if default is not None else {})

    def _cands(self, n):
        return [candidate("d", ["d/a.py:1"]) for _ in range(n)]

    def _verify_calls(self, out):
        return [c for c in out["calls"] if str(c["label"]).startswith("verify ")]

    def test_falsified_candidate_is_deleted_and_named(self, tmp_path):
        out = self._run(tmp_path, self._cands(2),
                        [verdict(0, "FALSIFIED", "d/b.py:9"), verdict(1)])
        rec = by_root(out)["d"]
        assert len(rec["candidates"]) == 1
        assert out["result"]["totals"]["falsified"] == 1
        assert any("FALSIFIED" in n for n in rec["notes"])

    def test_verdict_is_rederived_after_a_deletion(self, tmp_path):
        """GAPS-FOUND iff candidates must hold AFTER verification, not just after the reducer."""
        out = self._run(tmp_path, self._cands(1),
                        [verdict(0, "FALSIFIED", "d/b.py:9")])
        rec = by_root(out)["d"]
        assert rec["candidates"] == []
        assert rec["verdict"] == "COVERAGE-ASSESSED"

    def test_falsified_without_a_counterexample_is_discarded_not_obeyed(self, tmp_path):
        """A verdict that cannot point at the contradiction is not evidence."""
        out = self._run(tmp_path, self._cands(1), [verdict(0, "FALSIFIED", "")])
        rec = by_root(out)["d"]
        assert len(rec["candidates"]) == 1
        assert out["result"]["totals"]["verifyUnsupported"] == 1
        assert out["result"]["totals"]["falsified"] == 0
        assert any("DISCARDED" in n for n in rec["notes"])

    def test_unreturned_verification_keeps_candidates_and_says_so(self, tmp_path):
        """Missing evidence must never read as a clean directory."""
        out = self._run(tmp_path, self._cands(2), None)
        rec = by_root(out)["d"]
        assert len(rec["candidates"]) == 2
        assert out["result"]["totals"]["verifySubjectsUnreturned"] == 2
        assert any("UNRETURNED" in n for n in rec["notes"])

    def test_partial_read_is_reported(self, tmp_path):
        out = self._run(tmp_path, self._cands(1),
                        [verdict(0, files_read=1, files_in_dir=2)])
        rec = by_root(out)["d"]
        assert out["result"]["totals"]["verifyPartialReads"] == 1
        assert any("fewer files" in n for n in rec["notes"])

    def test_narrowing_rides_on_the_candidate_but_is_not_applied(self, tmp_path):
        """A fact rewritten by its verifier has been proposed by nobody."""
        out = self._run(tmp_path, self._cands(1),
                        [verdict(0, narrowing="holds for a.py only")])
        c = by_root(out)["d"]["candidates"][0]
        assert c["narrowing"] == "holds for a.py only"
        assert c["fact"] == "a fact"

    def test_verdicts_match_by_issued_index_never_by_position(self, tmp_path):
        """Same argument as reconcileBatch matching subjects by key."""
        out = self._run(tmp_path, self._cands(2),
                        [verdict(1, "FALSIFIED", "d/b.py:3"), verdict(0)])
        rec = by_root(out)["d"]
        assert len(rec["candidates"]) == 1
        assert rec["candidates"][0]["verified"] is True

    def test_basic_depth_does_not_verify(self, tmp_path):
        args = {
            "subjects": [subject("d", ["d/a.py"])],
            "depth": "basic",
            "refs": {"criteria": "/abs/coverage-standards.md"},
        }
        out = run_lane(tmp_path, args, responses=[{"subjects": [result_for(
            "S1", "d", self._cands(1), verdict="GAPS-FOUND")]}],
            default_response={})
        assert out["result"]["totals"]["verifyRan"] is False
        assert len(by_root(out)["d"]["candidates"]) == 1
        assert not self._verify_calls(out)

    def test_verify_can_be_switched_off_and_the_run_says_so(self, tmp_path):
        """An advanced run without verification must not read as verified."""
        out = self._run(tmp_path, self._cands(1), None, verify=False)
        assert out["result"]["totals"]["verifyRan"] is False
        assert any("verification DISABLED" in line for line in out["logs"])
        assert len(by_root(out)["d"]["candidates"]) == 1

    def test_verification_dispatches_one_agent_per_subject_with_candidates(self, tmp_path):
        """A candidate-free subject is not worth a dispatch."""
        args = {
            "subjects": [subject("d", ["d/a.py"]), subject("e", ["e/a.py"])],
            "depth": "advanced",
            "batchSize": 8,
            "refs": {"criteria": "/abs/coverage-standards.md"},
        }
        batch = {"subjects": [
            result_for("S1", "d", self._cands(1), verdict="GAPS-FOUND"),
            result_for("S2", "e", [], verdict="COVERAGE-ASSESSED"),
        ]}
        out = run_lane(tmp_path, args,
                       responses=[batch, {"verdicts": [verdict(0, files_in_dir=1, files_read=1)]}],
                       default_response={})
        calls = self._verify_calls(out)
        assert len(calls) == 1
        assert calls[0]["label"] == "verify d"

    def test_verify_brief_carries_the_exhaustive_file_list(self, tmp_path):
        """A universal claim is falsified only against every direct file."""
        out = self._run(tmp_path, self._cands(1), [verdict(0)])
        brief = self._verify_calls(out)[0]["prompt"]
        assert "d/a.py" in brief and "d/b.py" in brief
        assert "exhaustive" in brief

    def test_verify_brief_scopes_itself_to_truth_not_value(self, tmp_path):
        """Measured: refutation pointed at value manufactures rejections."""
        out = self._run(tmp_path, self._cands(1), [verdict(0)])
        brief = self._verify_calls(out)[0]["prompt"]
        assert "TRUE AS WRITTEN" in brief
        assert "NOT deciding" in brief

    def test_verify_brief_forbids_inventing_criteria(self, tmp_path):
        """An invented rule caused more wrong rejections than any lane property."""
        out = self._run(tmp_path, self._cands(1), [verdict(0)])
        brief = self._verify_calls(out)[0]["prompt"]
        assert "invent" in brief.lower()
        assert "quote the rule verbatim" in brief

    def test_verify_is_pinned_to_opus_not_inherited(self, tmp_path):
        out = self._run(tmp_path, self._cands(1), [verdict(0)])
        call = self._verify_calls(out)[0]
        assert call["model"] == "opus"
        assert call["effort"] == "high"

    def test_verify_responses_satisfy_the_schema_the_lane_passed(self, tmp_path):
        out = self._run(tmp_path, self._cands(1), [verdict(0)])
        assert out["schemaErrors"] == []

    def test_verify_brief_names_the_criteria_document(self, tmp_path):
        """The quote requirement is unenforceable if the judge cannot read the doc.

        Shipped once without this: the brief demanded a verbatim quote from a
        document it never named, which is a rule the verifier can only satisfy
        by invention -- the exact failure the field exists to detect.
        """
        out = self._run(tmp_path, self._cands(1), [verdict(0)])
        brief = self._verify_calls(out)[0]["prompt"]
        assert "/abs/coverage-standards.md" in brief


class TestPartialReadDirectionality:
    """A partial read is SAFE falsifying and UNSAFE upholding, so it is not one fact.

    Unread files can only ADD counterexamples. They can never withdraw the one
    that killed a fact, so a partial FALSIFIED verdict is sound and is applied
    unchanged. They can easily hold the counterexample that would have killed a
    fact that was allowed to stand, so a partial STANDS has not been checked and
    must not claim it was.

    Measured on the 2026-08-24 second-root run: 21 of 287 verdict rows were
    judged on a partial read, 17 STANDS and 4 FALSIFIED, and all 17 survivors
    were stamped `verified: true` on the candidate record -- readable as a
    fully-checked fact by anything that consumes candidates rather than notes.
    """

    _run = TestRefutationStage._run
    _cands = TestRefutationStage._cands

    def test_a_partial_read_stands_verdict_does_not_claim_verified(self, tmp_path):
        out = self._run(tmp_path, self._cands(1),
                        [verdict(0, files_read=1, files_in_dir=2)])
        c = by_root(out)["d"]["candidates"][0]
        assert c["verified"] is False
        assert c["readComplete"] is False

    def test_the_candidate_carries_the_read_figures_not_just_a_flag(self, tmp_path):
        """A consumer must be able to see HOW partial without re-reading notes."""
        out = self._run(tmp_path, self._cands(1),
                        [verdict(0, files_read=1, files_in_dir=2)])
        c = by_root(out)["d"]["candidates"][0]
        assert c["filesRead"] == 1
        assert c["filesInDir"] == 2

    def test_a_complete_read_stands_verdict_is_still_verified(self, tmp_path):
        out = self._run(tmp_path, self._cands(1),
                        [verdict(0, files_read=2, files_in_dir=2)])
        c = by_root(out)["d"]["candidates"][0]
        assert c["verified"] is True
        assert c["readComplete"] is True
        assert "filesRead" not in c

    def test_a_partial_read_falsified_verdict_is_still_applied(self, tmp_path):
        """The directionality: an unread file cannot rescue a contradicted fact."""
        out = self._run(tmp_path, self._cands(1),
                        [verdict(0, "FALSIFIED", "d/b.py:9",
                                 files_read=1, files_in_dir=2)])
        rec = by_root(out)["d"]
        assert rec["candidates"] == []
        assert out["result"]["totals"]["falsified"] == 1

    def test_partial_stands_is_counted_apart_from_partial_reads(self, tmp_path):
        """verifyPartialReads counts both directions; only STANDS is the exposure."""
        out = self._run(tmp_path, self._cands(2),
                        [verdict(0, files_read=1, files_in_dir=2),
                         verdict(1, "FALSIFIED", "d/b.py:9",
                                 files_read=1, files_in_dir=2)])
        totals = out["result"]["totals"]
        assert totals["verifyPartialReads"] == 2
        assert totals["verifyPartialStands"] == 1

    def test_a_partial_stands_candidate_is_not_in_the_verified_tally(self, tmp_path):
        out = self._run(tmp_path, self._cands(2),
                        [verdict(0, files_read=1, files_in_dir=2), verdict(1)])
        totals = out["result"]["totals"]
        assert totals["verified"] == 1
        assert len(by_root(out)["d"]["candidates"]) == 2

    def test_the_subject_note_explains_the_asymmetry(self, tmp_path):
        out = self._run(tmp_path, self._cands(1),
                        [verdict(0, files_read=1, files_in_dir=2)])
        notes = " ".join(by_root(out)["d"]["notes"])
        assert "STANDS" in notes and "FALSIFIED" in notes
        assert "readComplete" in notes

    def test_the_run_summary_names_the_partial_stands(self, tmp_path):
        out = self._run(tmp_path, self._cands(1),
                        [verdict(0, files_read=1, files_in_dir=2)])
        assert any("NOT counted as verified" in line for line in out["logs"])


class TestTheKillRecordIsStructural:
    """A deletion the report cannot name did not stay accountable.

    Report-only means nothing is written to the CODEBASE. It was never a licence
    for the report to forget what the run decided -- and the prose note names
    only the first three kills at 60 characters each, so a subject with eight
    kills discarded the counterexample the other five died on. The lane doc's
    own promotion-gate section states the rule this violates: "a rejection is as
    accountable as a deletion".
    """

    _run = TestRefutationStage._run
    _cands = TestRefutationStage._cands

    def test_a_falsified_candidate_appears_in_the_structural_array(self, tmp_path):
        out = self._run(tmp_path, self._cands(1),
                        [verdict(0, "FALSIFIED", "d/b.py:9")])
        rec = by_root(out)["d"]
        assert len(rec["falsified"]) == 1
        entry = rec["falsified"][0]
        assert entry["fact"] == "a fact"
        assert entry["counterexample"] == "d/b.py:9"
        assert entry["anchors"] == ["d/a.py:1"]
        assert entry["tier"] == "CONTEXT-ONLY"

    def test_an_empty_quote_is_carried_not_invented(self, tmp_path):
        """Empty is CORRECT for a pure falsification; the field still exists."""
        out = self._run(tmp_path, self._cands(1),
                        [verdict(0, "FALSIFIED", "d/b.py:9")])
        assert by_root(out)["d"]["falsified"][0]["quote"] == ""

    def test_a_quote_backing_a_criterion_survives_onto_the_record(self, tmp_path):
        out = self._run(tmp_path, self._cands(1),
                        [verdict(0, "FALSIFIED", "d/b.py:9",
                                 quote="a verbatim phrase")])
        assert by_root(out)["d"]["falsified"][0]["quote"] == "a verbatim phrase"

    def test_more_than_three_kills_are_ALL_recorded(self, tmp_path):
        """The prose note truncates at three; the array must not."""
        out = self._run(tmp_path, self._cands(8),
                        [verdict(i, "FALSIFIED", f"d/b.py:{i + 1}") for i in range(8)])
        rec = by_root(out)["d"]
        assert rec["candidates"] == []
        assert len(rec["falsified"]) == 8
        assert [e["counterexample"] for e in rec["falsified"]] == [
            f"d/b.py:{i + 1}" for i in range(8)
        ]

    def test_a_subject_with_no_kills_carries_an_empty_array(self, tmp_path):
        out = self._run(tmp_path, self._cands(1), [verdict(0)])
        assert by_root(out)["d"]["falsified"] == []

    def test_an_unsupported_falsified_verdict_records_no_kill(self, tmp_path):
        """Nothing was deleted, so nothing belongs in the deletion record."""
        out = self._run(tmp_path, self._cands(1), [verdict(0, "FALSIFIED", "")])
        rec = by_root(out)["d"]
        assert rec["falsified"] == []
        assert len(rec["candidates"]) == 1


class TestUnansweredCandidateVersusUnreturnedSubject:
    """Two different failures that one counter used to hide.

    A whole subject the stage never answered for is an infrastructure failure
    over a directory. One candidate missing from an otherwise-complete verdict
    set is what OUTPUT TRUNCATION looks like -- on the measured run the single
    unanswered candidate was the LAST index of the LONGEST candidate list, a
    shape a judgment does not produce. Both are handled SAFELY (the candidate is
    kept, never deleted); only the accounting was wrong.
    """

    _run = TestRefutationStage._run
    _cands = TestRefutationStage._cands

    def test_a_per_candidate_non_answer_has_its_own_counter(self, tmp_path):
        out = self._run(tmp_path, self._cands(3), [verdict(0), verdict(1)])
        totals = out["result"]["totals"]
        assert totals["verifyCandidatesUnanswered"] == 1
        assert totals["verifySubjectsUnreturned"] == 0

    def test_a_whole_subject_non_return_has_the_other_counter(self, tmp_path):
        out = self._run(tmp_path, self._cands(3), None)
        totals = out["result"]["totals"]
        assert totals["verifySubjectsUnreturned"] == 3
        assert totals["verifyCandidatesUnanswered"] == 0

    def test_the_unanswered_candidate_is_kept(self, tmp_path):
        out = self._run(tmp_path, self._cands(3), [verdict(0), verdict(1)])
        cands = by_root(out)["d"]["candidates"]
        assert len(cands) == 3
        assert cands[2]["verified"] is False

    def test_the_unreturned_subject_keeps_every_candidate(self, tmp_path):
        """Kept, and BOTH the subject and every candidate carry verified false.

        The per-candidate stamp is the point. A consumer reads candidate
        records, not subject flags, and an ABSENT key is worse than a wrong
        value: `c["verified"] is False` raised KeyError while
        `not c.get("verified")` passed by accident. Assert the key EXISTS and
        is exactly False, so a regression to the absent-key shape fails here
        rather than passing under the laxer of two idiomatic checks.
        """
        rec = by_root(self._run(tmp_path, self._cands(3), None))["d"]
        assert len(rec["candidates"]) == 3
        assert rec["verified"] is False
        for cand in rec["candidates"]:
            assert "verified" in cand
            assert cand["verified"] is False

    def test_the_per_candidate_case_is_visible_in_the_subject_note(self, tmp_path):
        out = self._run(tmp_path, self._cands(3), [verdict(0), verdict(1)])
        notes = " ".join(by_root(out)["d"]["notes"])
        assert "no verdict row" in notes
        assert "truncation" in notes.lower()

    def test_both_counters_reach_the_run_summary(self, tmp_path):
        out = self._run(tmp_path, self._cands(3), [verdict(0), verdict(1)])
        line = " ".join(out["logs"])
        assert "missing a verdict row" in line
        out2 = self._run(tmp_path, self._cands(3), None)
        assert any("never answered for" in line for line in out2["logs"])
