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
import subprocess
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

# The audit_job entries skills-kit ships, restated here so these tests do not
# depend on the resolver's own code: the seam emit._lane_models_module is
# replaced by a fake returning them (see _fake_lane_models).
SHIPPED_AUDIT_JOB = [{"id": "luna", "effort": "high"}, {"id": "sonnet", "effort": "low"}]


def _entries(*ids: str, effort: str = "low") -> list[dict[str, str]]:
    return [{"id": model_id, "effort": effort} for model_id in ids]


def _fake_job_kit(root: Path, version: str | None) -> Path:
    """A job-kit plugin tree: bin/job-kit plus a plugin.json stating `version`
    (no manifest at all when version is None)."""
    (root / "bin").mkdir(parents=True, exist_ok=True)
    launcher = root / "bin" / "job-kit"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    if version is not None:
        (root / ".claude-plugin").mkdir(exist_ok=True)
        (root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "job-kit", "version": version}), encoding="utf-8"
        )
    return launcher


class _FakeLaneModelsError(ValueError):
    pass


def _fake_lane_models(resolved=None, error: Exception | None = None):
    """A stand-in for skills_kit_lib.lane_models with the frozen API."""

    class _Fake:
        LaneModelsError = _FakeLaneModelsError
        calls: list = []

        @staticmethod
        def load_lane_models(project_root=None, home=None):
            _Fake.calls.append(project_root)
            if error is not None:
                raise error
            return {"audit_job": list(SHIPPED_AUDIT_JOB)} if resolved is None else resolved

    return _Fake


@pytest.fixture(scope="module", autouse=True)
def _hermetic_user_config(tmp_path_factory):
    """Point CLAUDE_CONFIG_DIR at an empty tmp dir for every test in this file.

    The emitter resolves its adapter-admitted endpoint set through skills-kit's
    layered config, whose lowest layer is <user_dir>/skills-kit. Without this
    the suite would read the developer's real harness config and its result
    would depend on the machine it ran on.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("empty-config")))
    mp.setattr(emit, "_lane_models_module", lambda: _fake_lane_models())
    launcher = _fake_job_kit(tmp_path_factory.mktemp("job-kit"), "0.11.0")
    mp.setattr(emit, "_job_kit_launcher", lambda: str(launcher))
    yield
    mp.undo()


@pytest.fixture(scope="module")
def document() -> dict:
    return emit.build_job_file(
        subject_dir=SUBJECT_DIR.resolve(),
        repo_root=REPO_ROOT,
        standards=checker.DEFAULT_STANDARDS,
        models=SHIPPED_AUDIT_JOB,
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

    def test_models_and_efforts_match_the_entries_in_order(self, document: dict):
        for job in document["jobs"]:
            assert job["models"] == ["luna", "sonnet"]
            assert list(job["model_efforts"].items()) == [
                ("luna", "high"),
                ("sonnet", "low"),
            ]
            assert job["options"] == {}

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

            for lib in (
                job_kit_lib,
                REPO_ROOT / "plugins" / "llm-scripting-kit" / "lib",
                REPO_ROOT / "plugins" / "bootstrap",
            ):
                if str(lib) not in sys.path:
                    sys.path.insert(0, str(lib))
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
            models=_entries("sonnet"),
            max_parallel=1,
            limit=1,
        )
        assert len(doc["jobs"]) == 1


class TestAsciiGuardActuallyFires:
    """Regression: the guard used to scan the RENDERED json, where
    ensure_ascii=True had already escaped every non-ASCII character, so it
    could never fire. It must inspect the source strings instead."""

    def test_rendered_json_hides_non_ascii(self) -> None:
        # The reason the old guard was vacuous, pinned so it cannot regress.
        rendered = json.dumps({"a": "café"}, indent=2)
        assert not any(ord(ch) > 127 for ch in rendered)

    def test_guard_finds_non_ascii_in_a_nested_string(self) -> None:
        doc = {"jobs": [{"prompt": {"user": "café", "system": "ok"}}]}
        hits = emit._non_ascii_strings(doc)
        assert hits == ["jobs[0].prompt.user"]

    def test_guard_is_quiet_on_a_clean_document(self) -> None:
        doc = {"jobs": [{"prompt": {"user": "plain", "system": "ok"}}]}
        assert emit._non_ascii_strings(doc) == []


class TestEmptySubjectSkipped:
    """Regression: a zero-line document admits no acceptable finding, because
    the checker requires 1 <= line <= subject_lines."""

    def test_empty_document_is_not_emitted(self, tmp_path: Path) -> None:
        repo = tmp_path
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        docs = repo / "docs"
        docs.mkdir()
        (docs / "empty.md").write_text("", encoding="utf-8")
        (docs / "real.md").write_text("# Title\n\nBody text.\n", encoding="utf-8")

        document = emit.build_job_file(
            subject_dir=docs,
            repo_root=repo,
            standards=checker.DEFAULT_STANDARDS,
            models=_entries("sonnet"),
            max_parallel=1,
            limit=None,
        )
        emitted = {job["id"] for job in document["jobs"]}
        assert not any("empty" in job_id for job_id in emitted), emitted


def _tiny_repo(tmp_path: Path) -> Path:
    """A git repo holding one auditable project doc, for the adapter tests."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "real.md").write_text(
        "# Title\n\nBody text with a sentence in it.\n", encoding="utf-8"
    )
    return docs


def _emit(docs: Path, repo: Path, models: list[str], effort: str = "xhigh") -> dict:
    return emit.build_job_file(
        subject_dir=docs,
        repo_root=repo,
        standards=checker.DEFAULT_STANDARDS,
        models=_entries(*models, effort=effort),
        max_parallel=1,
        limit=None,
    )


# Placeholder endpoint ids. Nothing here names a real machine or endpoint: the
# admitted set is configuration, so the test supplies its own ids.
EP_A = "audit-endpoint-a"
EP_B = "audit-endpoint-b"


def _write_config(layer_dir: Path, filename: str, admitted: list[str]) -> None:
    """Write one config layer admitting `admitted` for the md-audit adapter."""
    yaml = pytest.importorskip("yaml")
    layer_dir.mkdir(parents=True, exist_ok=True)
    (layer_dir / filename).write_text(
        yaml.safe_dump(
            {"adapters": {emit.ADAPTER_ID: {"admitted_endpoints": admitted}}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _admit(repo: Path, admitted: list[str]) -> None:
    """Admit `admitted` at the project layer of `repo` (the highest-durability
    layer a test repo has, and the one build_job_file resolves against)."""
    _write_config(repo / ".claude" / "skills-kit", "config.yaml", admitted)


class TestAdapterAdmissionConfig:
    """The admitted set is CONFIGURATION with an empty default, read through
    skills-kit's layered config -- no second config file, no env var."""

    def test_default_is_empty(self, tmp_path: Path) -> None:
        assert emit.resolve_admitted_endpoints(tmp_path) == frozenset()

    def test_project_layer_config_is_read(self, tmp_path: Path) -> None:
        _admit(tmp_path, [EP_A, EP_B])
        assert emit.resolve_admitted_endpoints(tmp_path) == frozenset({EP_A, EP_B})

    def test_layers_resolve_in_documented_precedence_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """user config.yaml < user config.local.yaml < project config.yaml <
        project config.local.yaml. A list replaces wholesale (deep-merge only
        recurses into mappings), so the highest present layer wins outright."""
        config_dir = tmp_path / "config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        user_layer = config_dir / "skills-kit"
        project = tmp_path / "project"
        project_layer = project / ".claude" / "skills-kit"

        _write_config(user_layer, "config.yaml", ["ep-user"])
        assert emit.resolve_admitted_endpoints(project) == frozenset({"ep-user"})

        _write_config(user_layer, "config.local.yaml", ["ep-user-local"])
        assert emit.resolve_admitted_endpoints(project) == frozenset({"ep-user-local"})

        _write_config(project_layer, "config.yaml", ["ep-project"])
        assert emit.resolve_admitted_endpoints(project) == frozenset({"ep-project"})

        _write_config(project_layer, "config.local.yaml", ["ep-project-local"])
        assert emit.resolve_admitted_endpoints(project) == frozenset(
            {"ep-project-local"}
        )

    def test_a_malformed_admitted_list_is_loud(self, tmp_path: Path) -> None:
        yaml = pytest.importorskip("yaml")
        from skills_kit_lib.standards_resolve import StandardsConfigError

        layer = tmp_path / ".claude" / "skills-kit"
        layer.mkdir(parents=True)
        (layer / "config.yaml").write_text(
            yaml.safe_dump(
                {"adapters": {emit.ADAPTER_ID: {"admitted_endpoints": EP_A}}},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        with pytest.raises(StandardsConfigError):
            emit.resolve_admitted_endpoints(tmp_path)


class TestAdapterAdmission:
    """adapter_applies decides against the CONFIGURED set. The evidence pack is
    admitted only for the model-task pairs it was measured on (a locally hosted
    27B-class endpoint auditing markdown, 2026-09-04)."""

    def test_empty_set_admits_nothing_and_raises_nothing(self) -> None:
        assert emit.adapter_applies([EP_A, "sonnet"], frozenset()) is False

    def test_all_admitted_returns_true(self) -> None:
        assert emit.adapter_applies([EP_A, EP_B], frozenset({EP_A, EP_B})) is True

    def test_none_admitted_returns_false(self) -> None:
        assert (
            emit.adapter_applies(["sonnet", "opus", "luna"], frozenset({EP_A}))
            is False
        )

    def test_empty_list_returns_false(self) -> None:
        assert emit.adapter_applies([], frozenset({EP_A})) is False

    def test_mixed_list_raises_and_names_the_list(self) -> None:
        with pytest.raises(emit.MixedAdapterEndpointsError) as excinfo:
            emit.adapter_applies([EP_A, "opus"], frozenset({EP_A}))
        message = str(excinfo.value)
        assert EP_A in message
        assert "opus" in message


class TestAdapterAttachment:
    def test_empty_default_attaches_nothing_and_raises_nothing(
        self, tmp_path: Path
    ) -> None:
        docs = _tiny_repo(tmp_path)
        document = _emit(docs, tmp_path, [EP_A, "sonnet"])
        assert document["jobs"]
        for job in document["jobs"]:
            assert job["evidence_pack"] == {"attached": False}
            assert (
                "EVIDENCE (pre-computed facts about FILE"
                not in job["prompt"]["user"]
            )

    def test_all_admitted_attaches_the_pack(self, tmp_path: Path) -> None:
        docs = _tiny_repo(tmp_path)
        _admit(tmp_path, [EP_A])
        document = _emit(docs, tmp_path, [EP_A])
        assert document["jobs"]
        for job in document["jobs"]:
            record = job["evidence_pack"]
            assert record["attached"] is True
            assert len(record["sha256"]) == 64
            assert record["char_count"] > 0
            assert "error" not in record

    def test_attached_pack_sits_before_the_response_schema(
        self, tmp_path: Path
    ) -> None:
        docs = _tiny_repo(tmp_path)
        _admit(tmp_path, [EP_A])
        job = _emit(docs, tmp_path, [EP_A])["jobs"][0]
        user = job["prompt"]["user"]
        pack_at = user.index("EVIDENCE (pre-computed facts about FILE")
        schema_at = user.index("Worked example of the exact report shape")
        # An admitted endpoint gets the sources INLINED rather than named by
        # path, so the block that identifies the audited document is the
        # "SUBJECT DOCUMENT" heading. The invariant under test is unchanged: the
        # pack sits after the document and before the response schema, which is
        # the insertion point the adapter was measured at.
        subject_at = user.index("SUBJECT DOCUMENT (")
        assert subject_at < pack_at < schema_at

    def test_recorded_sha256_and_count_match_the_attached_text(
        self, tmp_path: Path
    ) -> None:
        import hashlib

        docs = _tiny_repo(tmp_path)
        _admit(tmp_path, [EP_A])
        job = _emit(docs, tmp_path, [EP_A])["jobs"][0]
        evidence = emit.load_evidence_pack_module()
        pack, error = emit.build_evidence_pack(evidence, tmp_path, "docs/real.md")
        assert error is None and pack is not None
        assert job["evidence_pack"]["sha256"] == hashlib.sha256(
            pack.encode("utf-8")
        ).hexdigest()
        assert job["evidence_pack"]["char_count"] == len(pack)

    def test_none_admitted_does_not_attach(self, tmp_path: Path) -> None:
        docs = _tiny_repo(tmp_path)
        _admit(tmp_path, [EP_A])
        document = _emit(docs, tmp_path, ["sonnet", "opus", "luna"])
        assert document["jobs"]
        for job in document["jobs"]:
            assert job["evidence_pack"] == {"attached": False}
            assert (
                "EVIDENCE (pre-computed facts about FILE"
                not in job["prompt"]["user"]
            )

    def test_mixed_list_fails_the_whole_emit(self, tmp_path: Path) -> None:
        docs = _tiny_repo(tmp_path)
        _admit(tmp_path, [EP_A])
        with pytest.raises(emit.MixedAdapterEndpointsError):
            _emit(docs, tmp_path, [EP_A, "sonnet"])

    def test_default_endpoints_do_not_attach(self, tmp_path: Path) -> None:
        ids = [entry["id"] for entry in emit.resolve_audit_job_models(tmp_path)]
        assert (
            emit.adapter_applies(ids, emit.resolve_admitted_endpoints(tmp_path))
            is False
        )

    def test_a_pack_job_sends_the_measured_effort_and_no_sidecar(
        self, tmp_path: Path
    ) -> None:
        docs = _tiny_repo(tmp_path)
        _admit(tmp_path, [EP_A])
        job = _emit(docs, tmp_path, [EP_A])["jobs"][0]
        assert job["options"]["extras"] == {"reasoning_effort": "xhigh"}
        assert "model_efforts" not in job

    def test_a_pack_admitted_entry_at_another_effort_fails_the_emit(
        self, tmp_path: Path
    ) -> None:
        docs = _tiny_repo(tmp_path)
        _admit(tmp_path, [EP_A])
        with pytest.raises(emit.AdapterEffortMismatchError) as excinfo:
            _emit(docs, tmp_path, [EP_A], effort="high")
        assert "xhigh" in str(excinfo.value)


class TestAdapterFailureDegrades:
    """A pack that cannot be built degrades that one document to no pack; the
    emit still produces every job."""

    def test_build_failure_degrades_without_aborting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        docs = _tiny_repo(tmp_path)
        (docs / "second.md").write_text("# Second\n\nMore body.\n", encoding="utf-8")

        class _Exploding:
            @staticmethod
            def build_pack(repo_root, rel_path, **kwargs):
                raise RuntimeError("pack builder went bang")

        monkeypatch.setattr(emit, "load_evidence_pack_module", lambda: _Exploding)

        _admit(tmp_path, [EP_A])
        document = _emit(docs, tmp_path, [EP_A])
        assert len(document["jobs"]) == 2
        for job in document["jobs"]:
            record = job["evidence_pack"]
            assert record["attached"] is False
            assert "pack builder went bang" in record["error"]
            assert (
                "EVIDENCE (pre-computed facts about FILE"
                not in job["prompt"]["user"]
            )

    def test_empty_pack_text_counts_as_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        docs = _tiny_repo(tmp_path)

        class _Blank:
            @staticmethod
            def build_pack(repo_root, rel_path, **kwargs):
                return "   \n"

        monkeypatch.setattr(emit, "load_evidence_pack_module", lambda: _Blank)

        _admit(tmp_path, [EP_B])
        job = _emit(docs, tmp_path, [EP_B])["jobs"][0]
        assert job["evidence_pack"]["attached"] is False
        assert "no text" in job["evidence_pack"]["error"]


class TestEmptyContractTableFailsLoudly:
    """A standards doc contributing no criterion id, no taxonomy id, or
    neither must fail with a named message -- not raise ZeroDivisionError deep
    inside build_example_report's modulo picker (`pick(seq, i) -> seq[i %
    len(seq)]`)."""

    def _repo_with_standards(self, tmp_path: Path, standards_body: str) -> tuple[Path, Path]:
        repo = tmp_path
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        docs = repo / "docs"
        docs.mkdir()
        (docs / "real.md").write_text("# Title\n\nBody text.\n", encoding="utf-8")
        standards = repo / "standards.md"
        standards.write_text(standards_body, encoding="utf-8")
        return docs, standards

    def test_build_job_file_on_a_standards_doc_with_no_id_tables_raises_systemexit(
        self, tmp_path: Path
    ) -> None:
        # check_project_doc_audit.load_contract itself already rejects a
        # missing/empty table with a SystemExit -- this is the end-to-end
        # path a real invocation takes, and it must never surface as
        # ZeroDivisionError regardless of which layer catches it first.
        docs, standards = self._repo_with_standards(
            tmp_path,
            "# Standards\n\nNo id tables here at all.\n",
        )
        with pytest.raises(SystemExit) as excinfo:
            emit.build_job_file(
                subject_dir=docs,
                repo_root=tmp_path,
                standards=standards,
                models=_entries("sonnet"),
                max_parallel=1,
                limit=None,
            )
        assert excinfo.value.code == 1

    def test_build_example_report_direct_call_with_empty_tables_raises_systemexit_not_zerodivisionerror(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        # The pick site itself, called directly (bypassing load_contract's own
        # guard entirely) -- this is where the ZeroDivisionError actually lived.
        with pytest.raises(SystemExit) as excinfo:
            emit.build_example_report("docs/real.md", 10, set(), {})
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "Criteria ids" in err
        assert "Taxonomy ids" in err

    def test_build_example_report_direct_call_with_only_taxonomy_empty_names_it(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit):
            emit.build_example_report("docs/real.md", 10, {"C-1"}, {})
        err = capsys.readouterr().err
        assert "Taxonomy ids" in err
        assert "Criteria ids" not in err

    def test_build_example_report_degrades_gracefully_with_exactly_one_id_of_each_kind(self) -> None:
        report = emit.build_example_report("docs/real.md", 10, {"C-1"}, {"T-1": "FIX"})
        assert report["findings"][0]["criterion"] == "C-1"
        assert report["findings"][1]["criterion"] == "C-1"
        assert report["findings"][0]["taxonomy"] == "T-1"


class TestSubjectLineCountReadOncePerFile:
    """subject_line_count(subject_abs) used to be called once in
    build_job_file's zero-line filter and again inside build_job for the same
    file -- two full file reads per subject instead of one."""

    def test_subject_line_count_function_called_once_per_subject(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        docs = _tiny_repo(tmp_path)

        calls: list[Path] = []
        real = emit.subject_line_count

        def _counting(subject_file: Path) -> int:
            calls.append(subject_file)
            return real(subject_file)

        monkeypatch.setattr(emit, "subject_line_count", _counting)

        _emit(docs, tmp_path, ["sonnet"])

        assert len(calls) == len(set(calls)), (
            f"subject_line_count called more than once for the same file: {calls}"
        )


class TestAuditJobLaneModels:
    """The models come from the lane_models `audit_job` slot, with each entry's
    effort carried to job-kit in the model_efforts sidecar."""

    def test_main_emits_the_slot_entries_in_order_with_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        docs = _tiny_repo(tmp_path)
        fake = _fake_lane_models()
        monkeypatch.setattr(emit, "_lane_models_module", lambda: fake)
        out = tmp_path / "jobs.json"
        rc = emit.main([str(docs), "--repo-root", str(tmp_path), "--out", str(out)])
        assert rc == 0
        job = json.loads(out.read_text(encoding="utf-8"))["jobs"][0]
        assert job["models"] == ["luna", "sonnet"]
        assert list(job["model_efforts"].items()) == [("luna", "high"), ("sonnet", "low")]
        assert fake.calls == [tmp_path.resolve()]

    def test_a_project_slot_replaces_the_shipped_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        docs = _tiny_repo(tmp_path)
        fake = _fake_lane_models(resolved={"audit_job": [{"id": "opus", "effort": "max"}]})
        monkeypatch.setattr(emit, "_lane_models_module", lambda: fake)
        out = tmp_path / "jobs.json"
        assert emit.main([str(docs), "--repo-root", str(tmp_path), "--out", str(out)]) == 0
        job = json.loads(out.read_text(encoding="utf-8"))["jobs"][0]
        assert job["models"] == ["opus"]
        assert job["model_efforts"] == {"opus": "max"}

    def test_a_malformed_slot_fails_the_emit_with_exit_6(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        docs = _tiny_repo(tmp_path)
        fake = _fake_lane_models(error=_FakeLaneModelsError("audit_job: missing effort"))
        monkeypatch.setattr(emit, "_lane_models_module", lambda: fake)
        rc = emit.main([str(docs), "--repo-root", str(tmp_path), "--out", str(tmp_path / "o")])
        assert rc == 6
        assert "missing effort" in capsys.readouterr().err
        assert not (tmp_path / "o").exists()

    def test_an_unimportable_resolver_fails_the_emit_with_exit_6(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        docs = _tiny_repo(tmp_path)

        def _missing():
            raise ImportError("no module named skills_kit_lib.lane_models")

        monkeypatch.setattr(emit, "_lane_models_module", _missing)
        rc = emit.main([str(docs), "--repo-root", str(tmp_path)])
        assert rc == 6
        assert "lane_models" in capsys.readouterr().err

    def test_the_models_flag_is_gone(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as excinfo:
            emit.parse_args([str(tmp_path), "--models", "sonnet"])
        assert excinfo.value.code == 2

    @pytest.mark.parametrize(
        "models",
        [["sonnet"], [{"id": "sonnet"}], [{"effort": "low"}], []],
    )
    def test_build_job_file_refuses_an_entry_without_id_and_effort(
        self, tmp_path: Path, models: list
    ) -> None:
        docs = _tiny_repo(tmp_path)
        with pytest.raises(ValueError):
            emit.build_job_file(
                subject_dir=docs,
                repo_root=tmp_path,
                standards=checker.DEFAULT_STANDARDS,
                models=models,
                max_parallel=1,
                limit=None,
            )

    def test_the_emitted_job_loads_in_job_kit_with_its_efforts(
        self, tmp_path: Path
    ) -> None:
        pytest.importorskip("yaml")
        import sys

        for lib in (
            REPO_ROOT / "plugins" / "job-kit" / "lib",
            REPO_ROOT / "plugins" / "llm-scripting-kit" / "lib",
            REPO_ROOT / "plugins" / "bootstrap",
        ):
            if str(lib) not in sys.path:
                sys.path.insert(0, str(lib))
        import job_kit.model as job_kit_model

        docs = _tiny_repo(tmp_path)
        document = emit.build_job_file(
            subject_dir=docs,
            repo_root=tmp_path,
            standards=checker.DEFAULT_STANDARDS,
            models=SHIPPED_AUDIT_JOB,
            max_parallel=1,
            limit=None,
        )
        out = tmp_path / "jobs.yaml"
        out.write_text(json.dumps(document), encoding="utf-8")
        job = job_kit_model.load_job_file(out).jobs[0]
        assert job.models == ("luna", "sonnet")
        assert dict(job.model_efforts) == {"luna": "high", "sonnet": "low"}


class TestJobKitVersionPreflight:
    """A job-kit older than 0.11.0 ignores model_efforts, so the emit refuses
    to write such jobs unless the job-kit on PATH is verified >= 0.11.0."""

    def _main(self, tmp_path: Path, monkeypatch, launcher) -> tuple[int, Path]:
        docs = _tiny_repo(tmp_path)
        monkeypatch.setattr(emit, "_job_kit_launcher", lambda: launcher)
        out = tmp_path / "jobs.json"
        rc = emit.main([str(docs), "--repo-root", str(tmp_path), "--out", str(out)])
        return rc, out

    @pytest.mark.parametrize("version", ["0.10.1", "0.9.99"])
    def test_an_older_job_kit_fails_the_emit_with_exit_7(
        self, tmp_path: Path, monkeypatch, capsys, version: str
    ) -> None:
        launcher = _fake_job_kit(tmp_path / "jk", version)
        rc, out = self._main(tmp_path, monkeypatch, str(launcher))
        assert rc == 7
        assert not out.exists()
        err = capsys.readouterr().err
        assert "0.11.0" in err and version in err

    @pytest.mark.parametrize("version", [None, "0.11", "dev"])
    def test_an_unreadable_version_fails_the_emit_with_exit_7(
        self, tmp_path: Path, monkeypatch, capsys, version
    ) -> None:
        launcher = _fake_job_kit(tmp_path / "jk", version)
        rc, out = self._main(tmp_path, monkeypatch, str(launcher))
        assert rc == 7
        assert not out.exists()
        assert "cannot verify job-kit >= 0.11.0" in capsys.readouterr().err

    def test_no_launcher_on_path_fails_the_emit_with_exit_7(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        rc, out = self._main(tmp_path, monkeypatch, None)
        assert rc == 7
        assert not out.exists()
        assert "0.11.0" in capsys.readouterr().err

    @pytest.mark.parametrize("version", ["0.11.0", "0.12.3", "1.0.0"])
    def test_a_new_enough_job_kit_emits(self, tmp_path: Path, monkeypatch, version) -> None:
        launcher = _fake_job_kit(tmp_path / "jk", version)
        rc, out = self._main(tmp_path, monkeypatch, str(launcher))
        assert rc == 0
        assert "model_efforts" in json.loads(out.read_text(encoding="utf-8"))["jobs"][0]

    def test_the_launcher_is_followed_through_a_symlink_to_its_plugin_root(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The PATH entry is a link in a directory that is not a plugin root;
        # only the link's TARGET sits beside a plugin.json.
        real = _fake_job_kit(tmp_path / "jk", "0.11.0")
        (tmp_path / "links" / "bin").mkdir(parents=True)
        link = tmp_path / "links" / "bin" / "job-kit"
        link.symlink_to(real)
        rc, _ = self._main(tmp_path, monkeypatch, str(link))
        assert rc == 0

    def test_jobs_without_model_efforts_need_no_check(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        docs = _tiny_repo(tmp_path)
        _admit(tmp_path, [EP_A])
        fake = _fake_lane_models(resolved={"audit_job": [{"id": EP_A, "effort": "xhigh"}]})
        monkeypatch.setattr(emit, "_lane_models_module", lambda: fake)
        monkeypatch.setattr(emit, "_job_kit_launcher", lambda: None)
        out = tmp_path / "jobs.json"
        rc = emit.main([str(docs), "--repo-root", str(tmp_path), "--out", str(out)])
        assert rc == 0
        assert "model_efforts" not in json.loads(out.read_text(encoding="utf-8"))["jobs"][0]
