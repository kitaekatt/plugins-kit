"""Tests for emit_audit_jobs.py -- the job-kit job emitter for the project-doc
audit lane.

Loaded via importlib under a unique module name (same pattern as the sibling
discover_project_doc / check_project_doc_audit tests), so this file's module
cache entry never collides with a sibling test's.

The load-bearing assertion is (d): the JSON worked example embedded in each
emitted prompt is fed through the REAL check_report() from
check_project_doc_audit.py -- the same function job-kit's acceptance contract
runs. That is what stops the prompt describing a report shape the checker
would actually reject, and it costs zero endpoint calls.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain" / "scripts"
EMIT_PATH = SCRIPTS_DIR / "emit_audit_jobs.py"
CHECKER_PATH = SCRIPTS_DIR / "check_project_doc_audit.py"
SUBJECT_DIR = REPO_ROOT / "docs" / "reference"

_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


emit = _load(EMIT_PATH, "test_emit_audit_jobs_mod")
checker = _load(CHECKER_PATH, "test_emit_audit_jobs_checker")


@pytest.fixture(scope="module")
def document() -> dict:
    return emit.build_job_file(
        subject_dir=SUBJECT_DIR.resolve(),
        repo_root=REPO_ROOT,
        standards=checker.DEFAULT_STANDARDS,
        endpoints=["sonnet", "opus", "luna"],
        max_parallel=4,
        limit=None,
    )


@pytest.fixture(scope="module")
def contract() -> tuple[set[str], dict[str, str]]:
    return checker.load_contract(checker.DEFAULT_STANDARDS)


class TestSlugify:
    def test_repo_relative_path_becomes_underscore_slug(self):
        assert emit.slugify("docs/reference/testing.md") == "docs_reference_testing_md"

    def test_non_alnum_runs_collapse_to_one_underscore(self):
        assert emit.slugify("a--b__c.md") == "a_b_c_md"


class TestUniqueId:
    def test_first_use_is_unchanged(self):
        used: set[str] = set()
        assert emit.unique_id("foo", used) == "foo"

    def test_collision_gets_a_numeric_suffix(self):
        used = {"foo"}
        assert emit.unique_id("foo", used) == "foo_2"
        assert "foo_2" in used

    def test_repeated_collisions_keep_incrementing(self):
        used = {"foo", "foo_2"}
        assert emit.unique_id("foo", used) == "foo_3"


class TestDiscovery:
    def test_finds_at_least_one_project_doc(self, document: dict):
        assert len(document["jobs"]) >= 1

    def test_one_job_per_discovered_doc(self, document: dict):
        records = emit.discover_project_docs(SUBJECT_DIR.resolve())
        assert len(document["jobs"]) == len(records)

    def test_job_ids_are_unique(self, document: dict):
        ids = [job["id"] for job in document["jobs"]]
        assert len(ids) == len(set(ids))


class TestContractCommandPaths:
    def test_every_path_in_every_command_exists(self, document: dict):
        for job in document["jobs"]:
            command = job["contract"]["command"]
            python_abs, checker_abs, subject_rel, repo_root_abs = command[0:4]
            assert Path(python_abs).exists()
            assert Path(checker_abs).exists()
            assert Path(repo_root_abs).is_dir()
            assert (Path(repo_root_abs) / subject_rel).is_file()
            assert command[4] == "--standards"
            assert Path(command[5]).is_file()

    def test_subject_argument_is_repo_relative_not_absolute(self, document: dict):
        for job in document["jobs"]:
            subject_rel = job["contract"]["command"][2]
            assert not Path(subject_rel).is_absolute()


class TestJobShape:
    def test_requirements_params_cwd(self, document: dict):
        for job in document["jobs"]:
            assert job["requirements"] == {"params": ["cwd"]}

    def test_workspace_isolation_disabled(self, document: dict):
        for job in document["jobs"]:
            assert job["workspace"] == {"isolate": False}

    def test_directory_is_repo_root(self, document: dict):
        for job in document["jobs"]:
            assert job["directory"] == str(REPO_ROOT)

    def test_endpoint_preference_matches_input(self, document: dict):
        for job in document["jobs"]:
            assert job["endpoint_preference"] == ["sonnet", "opus", "luna"]

    def test_file_level_deny_floor_present(self, document: dict):
        assert "Write" in document["disallowed_tools"]
        assert "Edit" in document["disallowed_tools"]

    def test_max_parallel_carried_through(self, document: dict):
        assert document["max_parallel"] == 4


class TestAntiDriftGate:
    """The embedded worked example must pass the REAL checker's check_report,
    with that job's real subject string and real line count -- otherwise the
    prompt is describing a schema the checker would reject."""

    def test_every_embedded_example_passes_check_report(
        self, document: dict, contract: tuple[set[str], dict[str, str]]
    ):
        criteria, taxonomy = contract
        for job in document["jobs"]:
            user = job["prompt"]["user"]
            match = _FENCE_RE.search(user)
            assert match, f"no fenced JSON example in job {job['id']!r} prompt"
            example = json.loads(match.group(1))

            subject_rel = job["contract"]["command"][2]
            subject_abs = REPO_ROOT / subject_rel
            subject_lines = len(subject_abs.read_text(encoding="utf-8").splitlines())

            # Raises SystemExit(1) via checker.fail() on any grounding failure.
            checker.check_report(example, subject_rel, subject_lines, criteria, taxonomy)

    def test_example_subject_matches_contract_subject_exactly(self, document: dict):
        for job in document["jobs"]:
            user = job["prompt"]["user"]
            example = json.loads(_FENCE_RE.search(user).group(1))
            assert example["subject"] == job["contract"]["command"][2]


class TestAsciiOnly:
    def test_no_non_ascii_in_prompts(self, document: dict):
        for job in document["jobs"]:
            for text in (job["prompt"]["system"], job["prompt"]["user"]):
                assert all(ord(ch) < 128 for ch in text), job["id"]

    def test_no_non_ascii_in_rendered_document(self, document: dict):
        rendered = json.dumps(document, indent=2)
        assert all(ord(ch) < 128 for ch in rendered)


class TestYamlRoundTrip:
    def test_round_trips_through_yaml_safe_load(self, document: dict, tmp_path: Path):
        yaml = pytest.importorskip("yaml")
        rendered = json.dumps(document, indent=2)
        out = tmp_path / "jobs.yaml"
        out.write_text(rendered, encoding="utf-8")
        loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert loaded == document

    def test_round_trips_through_job_kit_load_job_file(self, document: dict, tmp_path: Path):
        pytest.importorskip("yaml")
        job_kit_lib = REPO_ROOT / "plugins" / "job-kit" / "lib"
        try:
            import sys

            if str(job_kit_lib) not in sys.path:
                sys.path.insert(0, str(job_kit_lib))
            import job_kit.model as job_kit_model
        except (ImportError, SystemExit):
            pytest.skip("job_kit is not importable in this environment")

        rendered = json.dumps(document, indent=2)
        out = tmp_path / "jobs.yaml"
        out.write_text(rendered, encoding="utf-8")
        job_file = job_kit_model.load_job_file(out)
        assert len(job_file.jobs) == len(document["jobs"])
        job_ids = {job.id for job in job_file.jobs}
        assert job_ids == {job["id"] for job in document["jobs"]}


class TestCliEntryPoint:
    def test_main_writes_to_out_path(self, tmp_path: Path):
        out_path = tmp_path / "out.yaml"
        rc = emit.main(
            [
                str(SUBJECT_DIR),
                "--repo-root",
                str(REPO_ROOT),
                "--out",
                str(out_path),
            ]
        )
        assert rc == 0
        assert out_path.is_file()
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert len(data["jobs"]) >= 1

    def test_main_rejects_non_directory_subject(self, tmp_path: Path):
        missing = tmp_path / "does-not-exist"
        rc = emit.main([str(missing), "--repo-root", str(REPO_ROOT)])
        assert rc == 2

    def test_report_dir_prefix_printed_to_stderr(self, tmp_path: Path, capsys):
        out_path = tmp_path / "out.yaml"
        emit.main(
            [
                str(SUBJECT_DIR),
                "--repo-root",
                str(REPO_ROOT),
                "--out",
                str(out_path),
                "--report-dir",
                str(tmp_path / "reports"),
            ]
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "JOB_KIT_REPORT_DIR=" in captured.err
        assert "job-kit run" in captured.err


class TestLimit:
    def test_limit_caps_job_count(self):
        doc = emit.build_job_file(
            subject_dir=SUBJECT_DIR.resolve(),
            repo_root=REPO_ROOT,
            standards=checker.DEFAULT_STANDARDS,
            endpoints=["sonnet"],
            max_parallel=1,
            limit=1,
        )
        assert len(doc["jobs"]) == 1
