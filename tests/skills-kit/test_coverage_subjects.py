"""Tests for scripts/coverage_subjects.py -- the producer and the verifier for
the analyze lane's `subjectsFile` mode.

`verify` is a GATE, so the tests that matter most are the ones where it FAILS. A
verifier that only has a happy-path test is indistinguishable from a verifier
that returns 0 unconditionally, and this whole engagement has been about
mandatory checks that were present, correct, and never reached. Every check the
verb makes has at least one test that trips it and asserts both the non-zero exit
AND that the offending subject is named in the output -- a failure count with no
subject in it is not actionable.

`build`'s invariants are equally mechanical: one record per line, LF, ASCII, no
blank lines, and a count that cannot disagree with the file it describes.

The last class pins the anchor rule ACROSS the JS/Python boundary. The rule is
implemented twice -- once in workflow/coverage-detect.js for the lane, once here
for the caller-side re-check -- because it cannot be shared. The case table below
is the same one tests/skills-kit/test_coverage_batching.py runs through the lane,
so a divergence between the two implementations fails here.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain" / "scripts"
SCRIPT = SCRIPTS / "coverage_subjects.py"
LANE = (
    REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
    / "references" / "lanes" / "coverage-lane.md"
)

_spec = importlib.util.spec_from_file_location("coverage_subjects", SCRIPT)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


def run(*argv, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *[str(a) for a in argv]],
        capture_output=True, text=True, encoding="utf-8", timeout=180, cwd=cwd,
    )


def make_tree(tmp_path, layout):
    """layout: {relative dir: [filenames]}. Creates dirs even when empty."""
    for rel, files in layout.items():
        directory = tmp_path / rel if rel else tmp_path
        directory.mkdir(parents=True, exist_ok=True)
        for name in files:
            (directory / name).write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def read_jsonl(path):
    raw = path.read_bytes().decode("ascii")
    lines = raw.split("\n")
    assert lines[-1] == "", "file must end with a single LF terminating the last record"
    return [json.loads(line) for line in lines[:-1]]


def record(key, root, code_files, candidates=(), status="ASSESSED"):
    return {
        "subjectKey": key,
        "root": root,
        "status": status,
        "provenance": "agent-attested",
        "verdict": "GAPS-FOUND" if candidates else "COVERAGE-ASSESSED",
        "candidates": list(candidates),
        "notes": [],
    }


def cand(destination, anchors):
    return {
        "fact": "a fact", "destination": destination, "why": "because",
        "tier": "CONTEXT-ONLY", "anchors": list(anchors),
    }


def write_report(tmp_path, records, name="report.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"perSubject": list(records), "totals": {}}),
                    encoding="utf-8")
    return path


@pytest.fixture
def built(tmp_path):
    """A real build over a real tree: two subjects, each with real code files."""
    src = make_tree(tmp_path / "src", {"alpha": ["a.py"], "beta": ["b.py", "c.py"]})
    out = tmp_path / "subjects.jsonl"
    proc = run("build", src / "alpha", src / "beta", "--out", out)
    assert proc.returncode == 0, proc.stderr
    return out, read_jsonl(out)


class TestBuildHappyPath:
    def test_one_named_directory_is_one_subject(self, tmp_path):
        src = make_tree(tmp_path, {"pkg": ["a.py"]})
        out = tmp_path / "s.jsonl"
        proc = run("build", src / "pkg", "--out", out)
        assert proc.returncode == 0, proc.stderr
        assert len(read_jsonl(out)) == 1

    def test_several_named_directories_keep_their_order(self, tmp_path):
        src = make_tree(tmp_path, {"a": ["x.py"], "b": ["y.py"], "c": ["z.py"]})
        out = tmp_path / "s.jsonl"
        assert run("build", src / "c", src / "a", src / "b",
                   "--out", out).returncode == 0
        roots = [Path(s["root"]).name for s in read_jsonl(out)]
        assert roots == ["c", "a", "b"]

    def test_the_printed_workflow_args_are_the_two_that_must_travel_together(
            self, tmp_path):
        src = make_tree(tmp_path, {"a": ["x.py"], "b": ["y.py"]})
        out = tmp_path / "s.jsonl"
        proc = run("build", src / "a", src / "b", "--out", out)
        printed = json.loads(proc.stdout)
        assert set(printed) == {"subjectsFile", "subjectCount"}
        assert printed["subjectCount"] == 2
        assert Path(printed["subjectsFile"]) == out.resolve()

    def test_a_windows_shaped_root_argument_works(self, tmp_path):
        src = make_tree(tmp_path, {"pkg": ["a.py"]})
        out = tmp_path / "s.jsonl"
        backslashed = str(src / "pkg").replace("/", "\\")
        proc = run("build", backslashed, "--out", out)
        assert proc.returncode == 0, proc.stderr
        assert len(read_jsonl(out)) == 1

    def test_a_directory_named_twice_becomes_one_subject(self, tmp_path):
        src = make_tree(tmp_path, {"pkg": ["a.py"]})
        out = tmp_path / "s.jsonl"
        assert run("build", src / "pkg", src / "pkg", "--out", out).returncode == 0
        assert len(read_jsonl(out)) == 1

    def test_records_carry_the_fields_the_lane_arg_contract_names(self, built):
        _, subjects = built
        for subject in subjects:
            assert set(subject) >= {
                "root", "codeFiles", "ambientClaudeMdPaths", "rootExclusion",
                "skipped", "unknownExtensions",
            }


class TestBuildFileInvariants:
    """A slice is a LINE RANGE. Everything here is load-bearing for that."""

    def test_exactly_one_record_per_line_and_no_blank_lines(self, built):
        out, subjects = built
        raw = out.read_bytes().decode("ascii")
        lines = raw.split("\n")[:-1]
        assert len(lines) == len(subjects)
        assert all(line.strip() for line in lines)

    def test_endings_are_lf_even_on_windows(self, built):
        out, _ = built
        assert b"\r" not in out.read_bytes()

    def test_the_file_is_ascii(self, tmp_path):
        src = make_tree(tmp_path, {"caf\u00e9": ["na\u00efve.py"]})
        out = tmp_path / "s.jsonl"
        assert run("build", src / "caf\u00e9", "--out", out).returncode == 0
        out.read_bytes().decode("ascii")   # raises if anything escaped

    def test_there_is_no_trailing_blank_line(self, built):
        out, _ = built
        assert not out.read_bytes().endswith(b"\n\n")

    def test_the_last_line_is_addressable_by_its_number(self, built):
        """sed -n 'N,Np' on the last record must yield that record."""
        out, subjects = built
        lines = out.read_bytes().decode("ascii").split("\n")
        assert json.loads(lines[len(subjects) - 1])["root"] == subjects[-1]["root"]


class TestBuildCountAtomicity:
    def test_the_count_equals_the_lines_in_the_file(self, tmp_path):
        src = make_tree(tmp_path, {f"d{i}": ["a.py"] for i in range(7)})
        out = tmp_path / "s.jsonl"
        proc = run("build", *[src / f"d{i}" for i in range(7)], "--out", out)
        assert json.loads(proc.stdout)["subjectCount"] == len(read_jsonl(out))

    def test_the_sidecar_records_the_same_count_and_a_digest(self, built):
        out, subjects = built
        meta = json.loads((out.parent / (out.name + ".meta.json")).read_text())
        assert meta["subjectCount"] == len(subjects)
        assert len(meta["sha256"]) == 64

    def test_a_bad_directory_writes_nothing_at_all(self, tmp_path):
        out = tmp_path / "s.jsonl"
        proc = run("build", tmp_path / "does-not-exist", "--out", out)
        assert proc.returncode == 2
        assert not out.exists()

    def test_a_directory_with_nothing_to_assess_is_refused_not_emitted_empty(
            self, tmp_path):
        src = make_tree(tmp_path, {"empty": []})
        out = tmp_path / "s.jsonl"
        proc = run("build", src / "empty", "--out", out, "--tree")
        assert proc.returncode == 2
        assert "no subjects" in proc.stderr
        assert not out.exists()

    def test_out_is_required(self, tmp_path):
        src = make_tree(tmp_path, {"pkg": ["a.py"]})
        assert run("build", src / "pkg").returncode != 0


class TestBuildTree:
    def test_every_directory_with_code_becomes_its_own_subject(self, tmp_path):
        src = make_tree(tmp_path, {
            "proj": ["top.py"],
            "proj/one": ["a.py"],
            "proj/two": ["b.py"],
            "proj/two/deep": ["c.py"],
        })
        out = tmp_path / "s.jsonl"
        assert run("build", src / "proj", "--out", out, "--tree").returncode == 0
        names = sorted(Path(s["root"]).name for s in read_jsonl(out))
        assert names == ["deep", "one", "proj", "two"]

    def test_a_directory_holding_only_subdirectories_is_not_a_subject(self, tmp_path):
        src = make_tree(tmp_path, {"proj": [], "proj/inner": ["a.py"]})
        out = tmp_path / "s.jsonl"
        proc = run("build", src / "proj", "--out", out, "--tree")
        assert proc.returncode == 0
        assert [Path(s["root"]).name for s in read_jsonl(out)] == ["inner"]
        assert "not made subjects" in proc.stderr

    def test_a_structurally_excluded_directory_is_pruned_with_its_children(
            self, tmp_path):
        src = make_tree(tmp_path, {
            "proj": ["a.py"],
            "proj/node_modules": ["vendor.js"],
            "proj/node_modules/pkg": ["more.js"],
        })
        out = tmp_path / "s.jsonl"
        assert run("build", src / "proj", "--out", out, "--tree").returncode == 0
        roots = [s["root"] for s in read_jsonl(out)]
        assert not any("node_modules" in r for r in roots)

    def test_a_dot_directory_is_pruned(self, tmp_path):
        src = make_tree(tmp_path, {"proj": ["a.py"], "proj/.cache": ["x.py"]})
        out = tmp_path / "s.jsonl"
        assert run("build", src / "proj", "--out", out, "--tree").returncode == 0
        assert not any(".cache" in s["root"] for s in read_jsonl(out))

    def test_a_never_read_directory_is_kept_not_dropped(self, tmp_path):
        """Zero code files plus unrecognized extensions is the discovery failure.

        Dropping it here would hide the exact case the lane refuses to call
        clean.
        """
        src = make_tree(tmp_path, {"proj": ["a.py"], "proj/odd": []})
        (src / "proj" / "odd" / "thing.zzzz").write_text("?", encoding="utf-8")
        out = tmp_path / "s.jsonl"
        assert run("build", src / "proj", "--out", out, "--tree").returncode == 0
        odd = [s for s in read_jsonl(out) if Path(s["root"]).name == "odd"]
        assert len(odd) == 1
        assert odd[0]["codeFiles"] == []
        assert odd[0]["unknownExtensions"]


class TestVerifyPasses:
    def test_a_faithful_report_verifies(self, built, tmp_path):
        out, subjects = built
        records = [
            record(f"L{i}", s["root"], s["codeFiles"],
                   [cand(s["root"], [s["codeFiles"][0] + ":3"])])
            for i, s in enumerate(subjects, start=1)
        ]
        report = write_report(tmp_path, records)
        proc = run("verify", report, out)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "OK:" in proc.stdout

    def test_a_not_assessed_record_with_the_lane_placeholder_verifies(
            self, built, tmp_path):
        out, subjects = built
        records = [
            record("L1", subjects[0]["root"], subjects[0]["codeFiles"]),
            record("L2", f"{out}#L2", [], status="NOT-ASSESSED"),
        ]
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 0, proc.stdout
        assert "1 NOT-ASSESSED" in proc.stdout

    def test_a_relative_anchor_that_names_one_real_file_verifies(
            self, built, tmp_path):
        out, subjects = built
        records = []
        for i, s in enumerate(subjects, start=1):
            bare = Path(s["codeFiles"][0]).name
            records.append(record(f"L{i}", s["root"], s["codeFiles"],
                                  [cand(s["root"], [bare + ":9"])]))
        assert run("verify", write_report(tmp_path, records), out).returncode == 0

    def test_a_bare_list_report_is_accepted(self, built, tmp_path):
        out, subjects = built
        records = [record(f"L{i}", s["root"], s["codeFiles"])
                   for i, s in enumerate(subjects, start=1)]
        path = tmp_path / "bare.json"
        path.write_text(json.dumps(records), encoding="utf-8")
        assert run("verify", path, out).returncode == 0


class TestVerifyFailsOnIdentity:
    """A verify that cannot fail is not a gate."""

    def _one_short(self, built, tmp_path, **kw):
        out, subjects = built
        records = [record("L1", subjects[0]["root"], subjects[0]["codeFiles"])]
        records.extend(kw.get("extra", []))
        return run("verify", write_report(tmp_path, records), out), subjects

    def test_a_missing_key_fails_and_names_the_subject(self, built, tmp_path):
        proc, subjects = self._one_short(built, tmp_path)
        assert proc.returncode == 1
        assert "[key] L2" in proc.stdout
        assert subjects[1]["root"] in proc.stdout

    def test_a_duplicate_key_fails(self, built, tmp_path):
        out, subjects = built
        records = [record("L1", subjects[0]["root"], subjects[0]["codeFiles"])] * 2
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "returned more than once" in proc.stdout

    def test_a_key_outside_the_file_fails(self, built, tmp_path):
        out, subjects = built
        records = [record(f"L{i}", s["root"], s["codeFiles"])
                   for i, s in enumerate(subjects, start=1)]
        records.append(record("L99", "/invented", []))
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "[key] L99" in proc.stdout

    def test_a_record_with_no_key_fails(self, built, tmp_path):
        out, subjects = built
        broken = record("L1", subjects[0]["root"], subjects[0]["codeFiles"])
        del broken["subjectKey"]
        proc = run("verify", write_report(tmp_path, [broken]), out)
        assert proc.returncode == 1
        assert "carries no subjectKey" in proc.stdout

    def test_an_inline_mode_report_is_refused_rather_than_silently_passed(
            self, built, tmp_path):
        out, subjects = built
        records = [record(f"S{i}", s["root"], s["codeFiles"])
                   for i, s in enumerate(subjects, start=1)]
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "not a subjectsFile key" in proc.stdout


class TestVerifyFailsOnProvenance:
    def test_an_invented_root_fails_and_names_both_spellings(self, built, tmp_path):
        """The check the lane structurally cannot make."""
        out, subjects = built
        records = [record("L1", "/totally/made/up", subjects[0]["codeFiles"]),
                   record("L2", subjects[1]["root"], subjects[1]["codeFiles"])]
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "[root] L1" in proc.stdout
        assert "/totally/made/up" in proc.stdout
        assert subjects[0]["root"] in proc.stdout

    def test_two_records_swapping_their_roots_fails(self, built, tmp_path):
        out, subjects = built
        records = [record("L1", subjects[1]["root"], subjects[1]["codeFiles"]),
                   record("L2", subjects[0]["root"], subjects[0]["codeFiles"])]
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert proc.stdout.count("[root]") == 2

    def test_an_anchor_naming_no_file_of_that_subject_fails(self, built, tmp_path):
        out, subjects = built
        records = [
            record("L1", subjects[0]["root"], subjects[0]["codeFiles"],
                   [cand(subjects[0]["root"], [subjects[1]["codeFiles"][0] + ":1"])]),
            record("L2", subjects[1]["root"], subjects[1]["codeFiles"]),
        ]
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "[anchor] L1" in proc.stdout
        assert "names no file" in proc.stdout

    def test_an_anchor_without_a_line_number_fails(self, built, tmp_path):
        out, subjects = built
        records = [
            record("L1", subjects[0]["root"], subjects[0]["codeFiles"],
                   [cand(subjects[0]["root"], [subjects[0]["codeFiles"][0]])]),
            record("L2", subjects[1]["root"], subjects[1]["codeFiles"]),
        ]
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "no line number" in proc.stdout

    def test_a_candidate_with_no_anchors_fails(self, built, tmp_path):
        out, subjects = built
        records = [
            record("L1", subjects[0]["root"], subjects[0]["codeFiles"],
                   [cand(subjects[0]["root"], [])]),
            record("L2", subjects[1]["root"], subjects[1]["codeFiles"]),
        ]
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "CV-7 evidence floor" in proc.stdout

    def test_a_destination_naming_another_directory_fails(self, built, tmp_path):
        out, subjects = built
        records = [
            record("L1", subjects[0]["root"], subjects[0]["codeFiles"],
                   [cand(subjects[1]["root"], [subjects[0]["codeFiles"][0] + ":1"])]),
            record("L2", subjects[1]["root"], subjects[1]["codeFiles"]),
        ]
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "[destination] L1" in proc.stdout


class TestVerifyFailsOnStatus:
    def test_a_missing_or_unknown_status_fails(self, built, tmp_path):
        out, subjects = built
        records = [record(f"L{i}", s["root"], s["codeFiles"])
                   for i, s in enumerate(subjects, start=1)]
        records[0]["status"] = "FINE-PROBABLY"
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "[status] L1" in proc.stdout

    def test_a_not_assessed_record_carrying_candidates_fails(self, built, tmp_path):
        out, subjects = built
        records = [
            record("L1", subjects[0]["root"], subjects[0]["codeFiles"],
                   [cand(subjects[0]["root"], [subjects[0]["codeFiles"][0] + ":1"])],
                   status="NOT-ASSESSED"),
            record("L2", subjects[1]["root"], subjects[1]["codeFiles"]),
        ]
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "NOT-ASSESSED but carries" in proc.stdout

    def test_a_not_assessed_record_with_a_fabricated_root_fails(self, built, tmp_path):
        out, subjects = built
        records = [
            record("L1", "/somewhere/else", [], status="NOT-ASSESSED"),
            record("L2", subjects[1]["root"], subjects[1]["codeFiles"]),
        ]
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "[root] L1" in proc.stdout


class TestVerifyFailsOnATamperedSubjectsFile:
    def test_an_edited_file_fails_the_digest(self, built, tmp_path):
        out, subjects = built
        lines = out.read_bytes().decode("ascii").split("\n")
        edited = json.loads(lines[0])
        edited["root"] = edited["root"] + "X"
        lines[0] = json.dumps(edited, ensure_ascii=True, sort_keys=True)
        out.write_bytes("\n".join(lines).encode("ascii"))
        records = [record(f"L{i}", s["root"], s["codeFiles"])
                   for i, s in enumerate(subjects, start=1)]
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "sha256" in proc.stdout

    def test_a_line_added_after_build_fails_the_count(self, built, tmp_path):
        out, subjects = built
        extra = json.dumps({"root": "/added", "codeFiles": []}, sort_keys=True)
        out.write_bytes(out.read_bytes() + (extra + "\n").encode("ascii"))
        records = [record(f"L{i}", s["root"], s["codeFiles"])
                   for i, s in enumerate(subjects, start=1)]
        proc = run("verify", write_report(tmp_path, records), out)
        assert proc.returncode == 1
        assert "subjectCount" in proc.stdout

    def test_a_blank_line_is_refused_with_the_reason(self, tmp_path):
        subjects_file = tmp_path / "s.jsonl"
        subjects_file.write_bytes(
            b'{"root": "/a", "codeFiles": []}\n\n{"root": "/b", "codeFiles": []}\n')
        report = write_report(tmp_path, [record("L1", "/a", [])])
        proc = run("verify", report, subjects_file)
        assert proc.returncode == 2
        assert "blank" in proc.stderr

    def test_an_unusable_report_is_a_usage_error_not_a_pass(self, tmp_path):
        subjects_file = tmp_path / "s.jsonl"
        subjects_file.write_bytes(b'{"root": "/a", "codeFiles": []}\n')
        report = tmp_path / "r.json"
        report.write_text(json.dumps({"totals": {}}), encoding="utf-8")
        proc = run("verify", report, subjects_file)
        assert proc.returncode == 2
        assert "perSubject" in proc.stderr


class TestBuildAndVerifyRoundTrip:
    def test_the_two_verbs_agree_end_to_end_over_a_tree(self, tmp_path):
        """The shape the real-dispatch gate will drive."""
        src = make_tree(tmp_path, {
            "proj": ["top.py"], "proj/one": ["a.py"], "proj/two": ["b.py"],
        })
        out = tmp_path / "subjects.jsonl"
        build = run("build", src / "proj", "--out", out, "--tree")
        assert build.returncode == 0, build.stderr
        count = json.loads(build.stdout)["subjectCount"]
        subjects = read_jsonl(out)
        assert count == len(subjects) == 3
        records = [
            record(f"L{i}", s["root"], s["codeFiles"],
                   [cand(s["root"], [s["codeFiles"][0] + ":1"])])
            for i, s in enumerate(subjects, start=1)
        ]
        verify = run("verify", write_report(tmp_path, records), out)
        assert verify.returncode == 0, verify.stdout
        assert "3 requested subject(s), 3 ASSESSED" in verify.stdout


GIT = shutil.which("git")


def make_git_repo(tmp_path, layout, ignore_lines):
    """A real git repo, because the ignore rule is answered by real git."""
    repo = make_tree(tmp_path / "repo", layout)
    (repo / ".gitignore").write_text("\n".join(ignore_lines) + "\n", encoding="utf-8")
    subprocess.run([GIT, "init", "-q", "."], cwd=repo, check=True,
                   capture_output=True)
    return repo


def wrap(inner):
    """The shape the Workflow tool actually hands back."""
    return {
        "summary": "Coverage done", "agentCount": 2, "logs": ["a log line"],
        "result": inner, "workflowProgress": "2/2", "totalTokens": 123456,
        "totalToolCalls": 42,
    }


def failure_lines(stdout):
    return sorted(l.strip() for l in stdout.splitlines() if l.startswith("  ["))


class TestVerifyAcceptsTheWorkflowWrapper:
    """The wrapper is the COMMON case, not an edge case.

    The Workflow tool does not hand back the lane's return object -- it wraps it,
    with the lane's object under `result`. So the file a caller actually holds is
    almost never the shape the lane returned. Refusing it forced the person
    holding the artifact to write an extraction step before the MANDATORY check
    would look at their report, and a gate that needs glue before it will run is
    a gate that gets skipped.
    """

    def _both_shapes(self, tmp_path, records, subjects_file):
        plain = write_report(tmp_path, records, "plain.json")
        wrapped = tmp_path / "wrapped.json"
        wrapped.write_text(json.dumps(wrap({"perSubject": records, "totals": {}})),
                           encoding="utf-8")
        return (run("verify", plain, subjects_file),
                run("verify", wrapped, subjects_file))

    def test_a_wrapped_passing_report_verifies_like_its_inner_object(
            self, built, tmp_path):
        out, subjects = built
        records = [
            record(f"L{i}", s["root"], s["codeFiles"],
                   [cand(s["root"], [s["codeFiles"][0] + ":3"])])
            for i, s in enumerate(subjects, start=1)
        ]
        plain, wrapped = self._both_shapes(tmp_path, records, out)
        assert plain.returncode == 0, plain.stdout
        assert wrapped.returncode == 0, wrapped.stdout
        assert "OK:" in wrapped.stdout

    def test_a_wrapped_failing_report_fails_identically(self, built, tmp_path):
        """Unwrapping must LOCATE the records, never become a path that skips
        checks. Same defects, same findings, same exit code."""
        out, subjects = built
        records = [
            record("L1", "/invented/directory", subjects[0]["codeFiles"],
                   [cand("/invented/directory", ["nowhere.py:1"])]),
        ]
        plain, wrapped = self._both_shapes(tmp_path, records, out)
        assert plain.returncode == 1
        assert wrapped.returncode == 1
        assert failure_lines(plain.stdout) == failure_lines(wrapped.stdout)
        assert failure_lines(wrapped.stdout), "no findings were reported at all"

    def test_a_single_nested_object_carrying_per_subject_is_found(
            self, built, tmp_path):
        out, subjects = built
        records = [record(f"L{i}", s["root"], s["codeFiles"])
                   for i, s in enumerate(subjects, start=1)]
        path = tmp_path / "odd.json"
        path.write_text(
            json.dumps({"meta": "x", "payload": {"perSubject": records}}),
            encoding="utf-8")
        assert run("verify", path, out).returncode == 0

    def test_two_nested_objects_carrying_per_subject_are_refused_not_guessed(
            self, built, tmp_path):
        out, subjects = built
        records = [record(f"L{i}", s["root"], s["codeFiles"])
                   for i, s in enumerate(subjects, start=1)]
        path = tmp_path / "ambiguous.json"
        path.write_text(
            json.dumps({"result": {"perSubject": records},
                        "previous": {"perSubject": []}}),
            encoding="utf-8")
        proc = run("verify", path, out)
        assert proc.returncode == 0, "an explicit `result` wins over a sibling"

        path.write_text(
            json.dumps({"runA": {"perSubject": records},
                        "runB": {"perSubject": records}}),
            encoding="utf-8")
        proc = run("verify", path, out)
        assert proc.returncode == 2
        assert "several nested objects" in proc.stderr

    def test_a_report_with_no_per_subject_anywhere_names_both_places_looked(
            self, built, tmp_path):
        out, _ = built
        path = tmp_path / "wrong.json"
        path.write_text(json.dumps({"totals": {}, "logs": []}), encoding="utf-8")
        proc = run("verify", path, out)
        assert proc.returncode == 2
        assert "no perSubject found (looked at the top level and under `result`)" \
            in proc.stderr
        # Someone who passed a genuinely wrong file still learns what a right one
        # looks like.
        assert "the Workflow tool" in proc.stderr
        assert "bare JSON list" in proc.stderr


@pytest.mark.skipif(GIT is None, reason="git is not installed")
class TestBuildTreeAndVcsIgnore:
    """A VCS-ignored directory is not a subject -- coverage-lane.md, Subject and
    unit. `build_subject` honours an explicitly named ignored root WHOLESALE,
    which is right for `build <dir>` and wrong for every descendant under
    `--tree`: the user named the tree, not the thousands of directories in it.
    """

    def test_an_ignored_descendant_is_not_a_subject(self, tmp_path):
        repo = make_git_repo(tmp_path, {
            "Module": ["Module.Build.cs"],
            "Binaries/Win64": ["thing.cpp"],
        }, ["Binaries/"])
        out = tmp_path / "s.jsonl"
        assert run("build", repo, "--out", out, "--tree").returncode == 0
        roots = [s["root"] for s in read_jsonl(out)]
        assert not any("Binaries" in r for r in roots)
        assert any(r.endswith("Module") for r in roots)

    def test_an_ignored_subtree_is_not_even_descended(self, tmp_path):
        repo = make_git_repo(tmp_path, {
            "Module": ["a.py"],
            "Saved/one/two/three": ["deep.py"],
        }, ["Saved/"])
        out = tmp_path / "s.jsonl"
        assert run("build", repo, "--out", out, "--tree").returncode == 0
        assert not any("Saved" in s["root"] for s in read_jsonl(out))

    def test_naming_an_ignored_directory_opts_its_whole_tree_in(self, tmp_path):
        """`build --tree ./Binaries` returning Binaries alone is not what anyone
        who typed that meant."""
        repo = make_git_repo(tmp_path, {
            "Module": ["a.py"],
            "Binaries/Win64": ["thing.cpp"],
        }, ["Binaries/"])
        out = tmp_path / "s.jsonl"
        assert run("build", repo / "Binaries", "--out", out, "--tree").returncode == 0
        roots = [s["root"] for s in read_jsonl(out)]
        assert any(r.endswith("Win64") for r in roots)

    def test_an_unignored_repo_is_unaffected(self, tmp_path):
        repo = make_git_repo(tmp_path, {"a": ["x.py"], "b": ["y.py"]}, ["nothing/"])
        out = tmp_path / "s.jsonl"
        assert run("build", repo, "--out", out, "--tree").returncode == 0
        assert len(read_jsonl(out)) == 2


class TestBuildTreeInheritsTheAcceptedFalsePositiveClass:
    """First-party build glue parked under a vendored parent is DROPPED.

    discover_coverage.py documents this as a known, accepted false-positive class
    and pins it with test_thirdparty_prune_also_drops_first_party_build_glue: a
    path-segment name rule cannot tell a vendored library from team-authored
    build glue sitting beside it. `--tree` prunes through that module's own
    `root_exclusion`, so it inherits the same behaviour rather than implementing
    a second, differently-wrong rule. This test exists so a future change to the
    enumeration cannot silently diverge from that decision.
    """

    def test_thirdparty_and_its_first_party_build_glue_are_both_pruned(
            self, tmp_path):
        src = make_tree(tmp_path, {
            "Module": ["Module.Build.cs"],
            "Module/ThirdParty/entt": ["EnTT.Build.cs"],
            "Module/ThirdParty/entt-3.15.0": ["entt.hpp"],
        })
        out = tmp_path / "s.jsonl"
        assert run("build", src / "Module", "--out", out, "--tree").returncode == 0
        roots = [s["root"] for s in read_jsonl(out)]
        assert [Path(r).name for r in roots] == ["Module"]
        assert not any("entt" in r for r in roots)

    def test_naming_the_glue_directory_explicitly_still_assesses_it(self, tmp_path):
        """The escape hatch discover_coverage.py names: reinstating such a
        directory is the consuming repo's call, by naming it."""
        src = make_tree(tmp_path, {"Module/ThirdParty/entt": ["EnTT.Build.cs"]})
        out = tmp_path / "s.jsonl"
        proc = run("build", src / "Module" / "ThirdParty" / "entt", "--out", out)
        assert proc.returncode == 0, proc.stderr
        subjects = read_jsonl(out)
        assert len(subjects) == 1
        assert [Path(f).name for f in subjects[0]["codeFiles"]] == ["EnTT.Build.cs"]
        # No rootExclusion: the rule matches a directory's OWN name, not its
        # ancestry, so `entt` named directly is an ordinary subject. That is what
        # makes the escape hatch usable -- reinstating one glue directory does
        # not drag the vendored-parent label along with it.
        assert subjects[0]["rootExclusion"] is None


class TestBuildTreePrunesNoise:
    """Build output is not a coverage subject.

    `discover_coverage.walk_directory` prunes NOISE_DIR_NAMES; `--tree` did not,
    which is exactly the disagreement that module's docstring exists to prevent
    ("one home for a shared constant ... so the verbs that use it cannot disagree
    about what it means"). `root_exclusion` was inherited; the noise half was
    left behind.

    Scale is what made it matter rather than merely untidy. An Unreal tree
    carries `Intermediate/`, `Saved/` and `Binaries/` under most module and
    plugin directories, and -- decisively -- a PERFORCE workspace's ignore rules
    do not cover them (`p4 ignores` in the consuming repo returns `.p4root` and
    `.p4config.txt` and nothing else), so the VCS-ignore prune is no defence
    there at all. NOISE_DIR_NAMES is the only thing standing between the walker
    and hundreds of full agent runs over compiler leavings.
    """

    NOISY = ["Intermediate", "Saved", "Binaries", "DerivedDataCache",
             "__pycache__", ".venv", ".pytest_cache", ".idea", ".vs"]

    def test_no_noise_directory_becomes_a_subject(self, tmp_path):
        layout = {"proj": ["real.py"]}
        for name in self.NOISY:
            layout[f"proj/{name}"] = ["artifact.cpp"]
            layout[f"proj/{name}/nested"] = ["deeper.cpp"]
        src = make_tree(tmp_path, layout)
        out = tmp_path / "s.jsonl"
        assert run("build", src / "proj", "--out", out, "--tree").returncode == 0
        roots = [s["root"] for s in read_jsonl(out)]
        assert [Path(r).name for r in roots] == ["proj"]
        for name in self.NOISY:
            assert not any(name in r for r in roots), name

    def test_a_noise_directory_is_not_even_descended(self, tmp_path):
        src = make_tree(tmp_path, {
            "proj": ["real.py"],
            "proj/Intermediate/Generated/Deep/Deeper": ["gen.cpp"],
        })
        out = tmp_path / "s.jsonl"
        assert run("build", src / "proj", "--out", out, "--tree").returncode == 0
        assert not any("Deeper" in s["root"] for s in read_jsonl(out))

    def test_dot_claude_is_the_one_dot_directory_that_survives(self, tmp_path):
        """The carve-out the blanket dot-prune was silently disagreeing with.

        `.claude` holds hand-authored team configuration -- exactly the content
        this verb exists to find -- and the discovery module exempts it by name.
        """
        src = make_tree(tmp_path, {
            "proj": ["real.py"],
            "proj/.claude": ["settings.json"],
            "proj/.idea": ["workspace.xml"],
        })
        out = tmp_path / "s.jsonl"
        assert run("build", src / "proj", "--out", out, "--tree").returncode == 0
        names = sorted(Path(s["root"]).name for s in read_jsonl(out))
        assert ".claude" in names
        assert ".idea" not in names

    def test_membership_is_case_sensitive_like_the_module(self, tmp_path):
        """Inherited deliberately, not endorsed.

        `_skip_reason` matches vendored/generated names case-INsensitively while
        NOISE_DIR_NAMES matches case-sensitively. That inconsistency belongs to
        the discovery module; matching it case-insensitively here would prune
        names that module keeps, which is the same disagreement in the opposite
        direction. Pinned so the inheritance is visible rather than accidental.
        """
        src = make_tree(tmp_path, {
            "proj": ["real.py"],
            "proj/intermediate": ["lower.py"],
            "proj/Intermediate": ["upper.py"],
        })
        out = tmp_path / "s.jsonl"
        assert run("build", src / "proj", "--out", out, "--tree").returncode == 0
        names = sorted(Path(s["root"]).name for s in read_jsonl(out))
        assert "Intermediate" not in names
        assert "intermediate" in names


class TestTheNoiseListIsSharedNotCopied:
    """The fix is only a fix if a later addition reaches this walk too."""

    def test_the_constant_is_the_same_object_the_discovery_module_owns(self):
        discovery = sys.modules["discover_coverage"]
        assert cs.NOISE_DIR_NAMES is discovery.NOISE_DIR_NAMES

    def test_the_script_declares_no_noise_list_of_its_own(self):
        src = SCRIPT.read_text(encoding="utf-8")
        assert "NOISE_DIR_NAMES = " not in src
        assert "NOISE_DIR_NAMES," in src, "expected an import, not a definition"

    def test_a_name_added_upstream_reaches_the_tree_walk_with_no_change_here(
            self, tmp_path):
        """The behavioural half of "imported, not copied".

        Exercised in-process against `_enumerate_tree` rather than through the
        CLI, because a subprocess would not see the mutation -- and it is the
        shared-object property, not the CLI, that this is pinning.
        """
        discovery = sys.modules["discover_coverage"]
        probe = "ZzNoiseProbeDir"
        src = make_tree(tmp_path, {"proj": ["real.py"], f"proj/{probe}": ["x.py"]})

        before = [d.name for d in cs._enumerate_tree(src / "proj")]
        assert probe in before, "probe directory is not noise yet"

        discovery.NOISE_DIR_NAMES.add(probe)
        try:
            after = [d.name for d in cs._enumerate_tree(src / "proj")]
        finally:
            discovery.NOISE_DIR_NAMES.discard(probe)
        assert probe not in after


class TestBuildOverrides:
    """A real corpus is "these trees, PLUS these specific directories, NOT
    recursively".

    Without `--overrides` the caller had two wrong invocations to choose from:
    leave the exceptions out, or name them alongside the roots and let `--tree`
    -- a whole-invocation flag -- walk them too. The second is the dangerous one.
    Reinstating 9 first-party directories parked under vendored parents dragged
    in 116 vendored descendants, and the corpus got BIGGER, which reads as more
    coverage. Nothing warned, because nothing was wrong by any rule the tool knew.

    An override entry is a CLAIM AGAINST A PRUNE. The plugin cannot know that
    `ThirdParty/SFDate` is first-party build glue and should not be taught to;
    that knowledge arrives as input.
    """

    def _vendored_tree(self, tmp_path):
        """Team-authored glue and upstream source under one vendored parent."""
        return make_tree(tmp_path, {
            "proj": ["top.py"],
            "proj/ThirdParty/glue": ["Glue.Build.cs"],
            "proj/ThirdParty/glue/nested": ["alsoglue.cs"],
            "proj/ThirdParty/upstream": ["lib.cpp"],
        })

    def _overrides(self, tmp_path, lines):
        path = tmp_path / "overrides.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_the_baseline_prunes_the_whole_vendored_parent(self, tmp_path):
        """Establishes what the override is overriding."""
        src = self._vendored_tree(tmp_path)
        out = tmp_path / "s.jsonl"
        assert run("build", src / "proj", "--out", out, "--tree").returncode == 0
        assert [Path(s["root"]).name for s in read_jsonl(out)] == ["proj"]

    def test_an_override_reinstates_a_directory_the_vendor_rule_prunes(
            self, tmp_path):
        src = self._vendored_tree(tmp_path)
        overrides = self._overrides(tmp_path, [str(src / "proj/ThirdParty/glue")])
        out = tmp_path / "s.jsonl"
        proc = run("build", src / "proj", "--out", out, "--tree",
                   "--overrides", overrides)
        assert proc.returncode == 0, proc.stderr
        names = sorted(Path(s["root"]).name for s in read_jsonl(out))
        assert names == ["glue", "proj"]

    def test_an_override_does_not_pull_in_that_directorys_children(self, tmp_path):
        """The whole point. `--tree` must not follow an override."""
        src = self._vendored_tree(tmp_path)
        overrides = self._overrides(tmp_path, [str(src / "proj/ThirdParty/glue")])
        out = tmp_path / "s.jsonl"
        assert run("build", src / "proj", "--out", out, "--tree",
                   "--overrides", overrides).returncode == 0
        roots = [s["root"] for s in read_jsonl(out)]
        assert not any("nested" in r for r in roots)
        assert not any("upstream" in r for r in roots)

    def test_an_override_outside_every_named_root_is_still_added(self, tmp_path):
        """Overrides are independent of what --tree is doing to the roots."""
        src = make_tree(tmp_path, {
            "proj": ["a.py"], "elsewhere/ThirdParty/glue": ["Glue.Build.cs"],
        })
        overrides = self._overrides(
            tmp_path, [str(src / "elsewhere/ThirdParty/glue")])
        out = tmp_path / "s.jsonl"
        assert run("build", src / "proj", "--out", out, "--tree",
                   "--overrides", overrides).returncode == 0
        assert sorted(Path(s["root"]).name
                      for s in read_jsonl(out)) == ["glue", "proj"]

    def test_overrides_work_without_tree(self, tmp_path):
        src = self._vendored_tree(tmp_path)
        overrides = self._overrides(tmp_path, [str(src / "proj/ThirdParty/glue")])
        out = tmp_path / "s.jsonl"
        assert run("build", src / "proj", "--out", out,
                   "--overrides", overrides).returncode == 0
        assert sorted(Path(s["root"]).name
                      for s in read_jsonl(out)) == ["glue", "proj"]

    def test_a_redundant_override_is_deduped_and_counted(self, tmp_path):
        """A stale override file must be VISIBLE, not silently inert."""
        src = make_tree(tmp_path, {"proj": ["a.py"], "proj/inner": ["b.py"]})
        overrides = self._overrides(tmp_path, [str(src / "proj/inner")])
        out = tmp_path / "s.jsonl"
        proc = run("build", src / "proj", "--out", out, "--tree",
                   "--overrides", overrides)
        assert proc.returncode == 0, proc.stderr
        assert len(read_jsonl(out)) == 2
        assert "0 added by override (1 redundant)" in proc.stderr

    def test_the_three_counts_are_reported_separately(self, tmp_path):
        src = self._vendored_tree(tmp_path)
        overrides = self._overrides(tmp_path, [str(src / "proj/ThirdParty/glue")])
        out = tmp_path / "s.jsonl"
        proc = run("build", src / "proj", "--out", out, "--tree",
                   "--overrides", overrides)
        assert "1 from the named roots, 1 added by override (0 redundant) = 2" \
            in proc.stderr

    def test_comments_and_blank_lines_are_ignored(self, tmp_path):
        """Where the evidence for a claim lives."""
        src = self._vendored_tree(tmp_path)
        overrides = self._overrides(tmp_path, [
            "# first-party build glue, per the p4-history audit",
            "",
            str(src / "proj/ThirdParty/glue") + "  # own file is Glue.Build.cs",
            "   ",
        ])
        out = tmp_path / "s.jsonl"
        proc = run("build", src / "proj", "--out", out, "--tree",
                   "--overrides", overrides)
        assert proc.returncode == 0, proc.stderr
        assert "1 added by override" in proc.stderr


class TestBuildOverridesFailLoudly:
    """A rotted override file must fail, not silently shrink the corpus."""

    def _src(self, tmp_path):
        return make_tree(tmp_path, {"proj": ["a.py"], "proj/empty": []})

    def _run(self, tmp_path, lines):
        src = self._src(tmp_path)
        overrides = tmp_path / "overrides.txt"
        overrides.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = tmp_path / "s.jsonl"
        return run("build", src / "proj", "--out", out, "--tree",
                   "--overrides", overrides), out

    def test_a_directory_that_no_longer_exists_is_an_error_naming_it(
            self, tmp_path):
        proc, out = self._run(tmp_path, [str(tmp_path / "proj" / "gone")])
        assert proc.returncode == 2
        assert "not a directory" in proc.stderr
        assert "gone" in proc.stderr
        assert not out.exists(), "a failed override run must write nothing"

    def test_a_directory_with_nothing_to_assess_is_an_error_naming_it(
            self, tmp_path):
        proc, out = self._run(tmp_path, [str(tmp_path / "proj" / "empty")])
        assert proc.returncode == 2
        assert "nothing to assess" in proc.stderr
        assert "empty" in proc.stderr
        assert not out.exists()

    def test_every_bad_entry_is_reported_not_just_the_first(self, tmp_path):
        """Fixing a stale file should not be an iterative guessing game."""
        proc, _ = self._run(tmp_path, [
            str(tmp_path / "proj" / "gone-one"),
            str(tmp_path / "proj" / "empty"),
            str(tmp_path / "proj" / "gone-two"),
        ])
        assert proc.returncode == 2
        assert "3 unusable override entry/ies" in proc.stderr
        for needle in ("gone-one", "empty", "gone-two"):
            assert needle in proc.stderr

    def test_the_failing_line_number_is_reported(self, tmp_path):
        proc, _ = self._run(tmp_path, [
            "# a comment", "", str(tmp_path / "proj" / "gone")])
        assert proc.returncode == 2
        assert "overrides.txt:3" in proc.stderr

    def test_a_missing_overrides_file_is_an_error(self, tmp_path):
        src = self._src(tmp_path)
        out = tmp_path / "s.jsonl"
        proc = run("build", src / "proj", "--out", out, "--tree",
                   "--overrides", tmp_path / "nope.txt")
        assert proc.returncode == 2
        assert "no such overrides file" in proc.stderr
        assert not out.exists()


class TestOverrideBuiltFileRoundTrips:
    def test_an_override_built_corpus_verifies(self, tmp_path):
        """The sidecar and verify path must keep working unchanged."""
        src = make_tree(tmp_path, {
            "proj": ["top.py"],
            "proj/ThirdParty/glue": ["Glue.Build.cs"],
            "proj/ThirdParty/upstream": ["lib.cpp"],
        })
        overrides = tmp_path / "overrides.txt"
        overrides.write_text(str(src / "proj/ThirdParty/glue") + "\n",
                             encoding="utf-8")
        out = tmp_path / "subjects.jsonl"
        build = run("build", src / "proj", "--out", out, "--tree",
                    "--overrides", overrides)
        assert build.returncode == 0, build.stderr
        count = json.loads(build.stdout)["subjectCount"]
        subjects = read_jsonl(out)
        assert count == len(subjects) == 2
        assert (out.parent / (out.name + ".meta.json")).is_file()

        records = [
            record(f"L{i}", s["root"], s["codeFiles"],
                   [cand(s["root"], [s["codeFiles"][0] + ":1"])])
            for i, s in enumerate(subjects, start=1)
        ]
        verify = run("verify", write_report(tmp_path, records), out)
        assert verify.returncode == 0, verify.stdout
        assert "2 requested subject(s), 2 ASSESSED" in verify.stdout


# The anchor rule, implemented twice because it cannot be shared. Each row is
# (anchor, code_files, accepted). tests/skills-kit/test_coverage_batching.py runs
# the same cases through workflow/coverage-detect.js.
ANCHOR_CASES = [
    ("/r/b/f0.py:12", ["/r/b/f0.py"], True),
    ("/r/b/f0.py:12:4", ["/r/b/f0.py"], True),
    ("f0.py:12", ["/r/b/f0.py"], True),
    ("/r/b/./sub/../f.py:1", ["/r/b/f.py"], True),
    ("C:/Repo/Src/f.py:22", ["C:\\Repo\\Src\\f.py"], True),
    ("c:\\repo\\source\\net\\f.py:1", ["C:\\Repo\\Source\\Net\\f.py"], True),
    ("/r/caf\u00e9/f.py:3", ["/r/cafe\u0301/f.py"], True),
    ("/r/a/f0.py:12", ["/r/b/f0.py"], False),
    ("/r/shared.py:3", ["/r/b/f0.py"], False),
    ("/r/b/sub/f.py:3", ["/r/b/f0.py"], False),
    ("/r/b/never_discovered.py:3", ["/r/b/f0.py"], False),
    ("foreign.py:1", ["/r/b/f0.py"], False),
    ("", ["/r/b/f0.py"], False),
    ("/r/b/f0.py", ["/r/b/f0.py"], False),
    ("/r/b/f0.py:0", ["/r/b/f0.py"], False),
    ("file.cpp:1", ["/r/b/Private/file.cpp", "/r/b/Public/file.cpp"], False),
    ("Private/file.cpp:1", ["/repo/A/Private/other.cpp"], False),
    ("f.py:1", ["/r/b/conf.py"], False),
    ("/r/b/file.py:1", ["/r/b/File.py"], False),
    ("/r/Source/NetCore/f.py:1", ["/r/Source/Net/f.py"], False),
]


class TestTheAnchorRuleMatchesTheLane:
    @pytest.mark.parametrize("anchor,code_files,accepted", ANCHOR_CASES)
    def test_case(self, anchor, code_files, accepted):
        reason = cs.anchor_rejection_reason(anchor, code_files)
        assert (reason is None) is accepted, f"{anchor!r} -> {reason!r}"

    def test_the_case_table_covers_both_outcomes(self):
        """A table of all-accept or all-reject would pass a broken rule."""
        outcomes = {row[2] for row in ANCHOR_CASES}
        assert outcomes == {True, False}


class TestLanePointsAtTheVerb:
    """If the doc says "verify before promoting" without naming the command,
    the gap is shipped one level up."""

    def _lane(self):
        return LANE.read_text(encoding="utf-8")

    def test_the_lane_names_the_script(self):
        assert "coverage_subjects.py" in self._lane()

    def test_the_lane_names_the_verify_verb_where_it_states_the_obligation(self):
        text = self._lane()
        section = text.split("Verifying an agent-attested run", 1)[1]
        assert "coverage_subjects.py verify" in section

    def test_the_lane_names_the_build_verb_for_the_input_mode(self):
        text = self._lane()
        assert "coverage_subjects.py build" in text
