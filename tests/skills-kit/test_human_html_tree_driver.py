"""Tests for the human-html tree placement and generation driver."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from skills_kit_lib import human_html as hh


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "skills-kit"
MD_DOMAIN = PLUGIN_ROOT / "skills" / "md-domain"
SCRIPTS = MD_DOMAIN / "scripts"
DRIVER_PATH = SCRIPTS / "human_html_tree.py"


def _load(name: str, path: Path):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


driver = _load("human_html_tree", DRIVER_PATH)
discover = sys.modules["discover_human_html"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, message: str = "change") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _tree_snapshot(repo: Path) -> dict[str, bytes]:
    return {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file() and path.relative_to(repo).parts[0] != ".git"
    }


@pytest.fixture(autouse=True)
def isolated_standards_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-config"))


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    (root / "a" / "leaf").mkdir(parents=True)
    (root / "b").mkdir()
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "tests")
    (root / "README.md").write_text("root\n", encoding="ascii")
    (root / "a" / "note.txt").write_text("a\n", encoding="ascii")
    (root / "a" / "leaf" / "item.txt").write_text("leaf\n", encoding="ascii")
    (root / "b" / "item.txt").write_text("b\n", encoding="ascii")
    _commit(root, "corpus")
    return root


@pytest.fixture
def root_only(tmp_path: Path) -> Path:
    root = tmp_path / "root-only"
    root.mkdir()
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "tests")
    (root / "README.md").write_text("root\n", encoding="ascii")
    _commit(root, "corpus")
    return root


@pytest.fixture
def framework(tmp_path: Path) -> Path:
    path = tmp_path / "framework-F.md"
    path.write_text("# Framework F\n\nRoute the reader.\n", encoding="ascii")
    return path


def _entry(repo: Path, directory: str) -> dict:
    return next(
        item
        for item in discover.scan(repo, directory)["directories"]
        if item["directory"] == directory
    )


def _write_fresh_record(
    repo: Path,
    directory: str,
    decision: str,
    *,
    identity: str | None = None,
    instructions: str = "",
    references: tuple[tuple[str, str], ...] = (),
) -> hh.Record:
    entry = _entry(repo, directory)
    record = hh.Record(
        directory=directory,
        decision=decision,
        source_sha=entry["source_sha"],
        dirty=entry["dirty"],
        identity=(identity or "The %s subsystem." % directory)
        if decision == hh.DECISION_PAGE
        else "",
        instructions=instructions,
        references=tuple(
            hh.Reference(slug=slug, title=title, file=hh.reference_filename(slug))
            for slug, title in references
        ),
    )
    return hh.write_record(
        hh.record_path(repo, directory),
        record,
        preserve_instructions=False,
    )


def _settle(corpus: Path) -> None:
    decisions = {
        "a/leaf": hh.DECISION_PAGE,
        "a": hh.DECISION_NONE,
        "b": hh.DECISION_NONE,
        ".": hh.DECISION_PAGE,
    }
    for directory in discover.scan(corpus)["directories"]:
        path = directory["directory"]
        _write_fresh_record(corpus, path, decisions[path])


def _href(source: str, target: str) -> str:
    source_parts = [] if source == "." else source.split("/")
    target_parts = [] if target == "." else target.split("/")
    common = 0
    while (
        common < min(len(source_parts), len(target_parts))
        and source_parts[common] == target_parts[common]
    ):
        common += 1
    return "/".join([".."] * (len(source_parts) - common) + target_parts[common:] + ["human.html"])


def _write_page(
    repo: Path,
    directory: str,
    *,
    references: tuple[tuple[str, str], ...] = (),
) -> None:
    record = hh.load_record(hh.record_path(repo, directory))
    entry = _entry(repo, directory)
    targets = ([entry["nearest_page_ancestor"]] if entry["nearest_page_ancestor"] else []) + entry[
        "nearest_page_descendants"
    ]
    nav = ['<nav data-human-html-chrome="nav"><ul>']
    for target in targets:
        target_record = hh.load_record(hh.record_path(repo, target))
        nav.extend(
            [
                '<li><a href="%s">' % _href(directory, target),
                '<span class="hh-nav-label">%s</span>' % hh.navigation_label(target),
                '<span class="hh-nav-identity">%s</span>' % target_record.identity,
                "</a></li>",
            ]
        )
    nav.extend(["</ul></nav>"])
    reference_links = [
        '<a href="%s">%s</a>' % (hh.reference_filename(slug), title)
        for slug, title in references
    ]
    html = "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            hh.marker(record, hh.KIND_PAGE),
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta name="color-scheme" content="dark">',
            "<style data-human-html-style>",
            hh.asset_css(),
            "</style>",
            "</head><body>",
            *nav,
            "<main><h1>Orientation</h1><h2>Where to start</h2>",
            "<p>Open the files that define this territory.</p>",
            *reference_links,
            "</main><script>",
            hh.announce_script(record, hh.PAGE_FILENAME, hh.KIND_PAGE),
            "</script></body></html>",
        ]
    )
    base = repo if directory == "." else repo / directory
    (base / hh.PAGE_FILENAME).write_text(html, encoding="ascii")

    for slug, title in references:
        filename = hh.reference_filename(slug)
        reference_html = "\n".join(
            [
                "<!doctype html>",
                '<html lang="en">',
                "<head>",
                hh.marker(record, hh.KIND_REFERENCE, slug),
                '<meta charset="utf-8">',
                '<meta name="viewport" content="width=device-width, initial-scale=1">',
                '<meta name="color-scheme" content="dark">',
                "<style data-human-html-style>",
                hh.asset_css(),
                "</style>",
                "</head><body>",
                '<nav data-human-html-chrome="nav"><ul><li>',
                '<a href="human.html">',
                '<span class="hh-nav-label">%s</span>' % hh.navigation_label(directory),
                '<span class="hh-nav-identity">%s</span>' % record.identity,
                "</a></li></ul></nav>",
                "<main><h1>%s</h1><h2>Reference</h2><p>Reference detail.</p></main>" % title,
                "<script>",
                hh.announce_script(record, filename, hh.KIND_REFERENCE, slug),
                "</script></body></html>",
            ]
        )
        (base / filename).write_text(reference_html, encoding="ascii")


class TestPlacementPlan:
    def test_empty_territory_is_not_a_plan_wide_blocker(self, tmp_path: Path) -> None:
        root = tmp_path / "phantom"
        (root / "boundary" / "child").mkdir(parents=True)
        _git(root.parent, "init", "-q", str(root))
        _git(root, "config", "user.email", "tests@example.invalid")
        _git(root, "config", "user.name", "tests")
        (root / "README.md").write_text("root\n", encoding="ascii")
        (root / "boundary" / "child" / "item.txt").write_text("child\n", encoding="ascii")
        _commit(root, "phantom territory")

        child_entry = _entry(root, "boundary/child")
        _write_fresh_record(root, "boundary/child", hh.DECISION_PAGE)
        boundary_sha, boundary_dirty = hh.source_stamp(root, "boundary")
        hh.write_record(
            hh.record_path(root, "boundary"),
            hh.Record(
                directory="boundary",
                decision=hh.DECISION_PAGE,
                source_sha=boundary_sha,
                dirty=boundary_dirty,
                identity="The boundary subsystem.",
                instructions="",
                references=(),
            ),
            preserve_instructions=False,
        )

        plan = driver.placement_plan(root)

        assert child_entry["source_sha"] is not None
        assert "boundary" not in plan["directory_order"]
        assert plan["status"] == driver.STATUS_WORK
        assert plan["blockers"] == []
        assert plan["diagnostics"][0]["code"] == discover.EMPTY_TERRITORY

    def test_reuses_discovery_order_and_returns_one_deepest_item(self, corpus: Path) -> None:
        discovered = discover.scan(corpus)
        plan = driver.placement_plan(corpus)

        assert plan["directory_order"] == [item["directory"] for item in discovered["directories"]]
        assert plan["subject_count"] == 4
        assert plan["pending_count"] == 4
        assert plan["next"]["directory"] == "a/leaf"
        assert plan["next"]["territory"] == discovered["directories"][0]["territory"]

    def test_fresh_records_are_skipped(self, corpus: Path) -> None:
        _settle(corpus)
        plan = driver.placement_plan(corpus)

        assert plan["status"] == driver.STATUS_COMPLETE
        assert plan["fresh_count"] == 4
        assert plan["work"] == []

    def test_recorded_leaf_is_skipped_and_recomputes_parent_territory(
        self,
        corpus: Path,
    ) -> None:
        first = driver.placement_plan(corpus)["next"]
        assert first["directory"] == "a/leaf"

        driver.record_placement(
            corpus,
            first["directory"],
            hh.DECISION_PAGE,
            "The leaf subsystem.",
            first["source_sha"],
            first["dirty"],
            first["brief_sha256"],
        )
        resumed = driver.placement_plan(corpus)

        assert resumed["fresh_count"] == 1
        assert resumed["pending_count"] == 3
        assert resumed["next"]["directory"] == "a"
        assert resumed["next"]["territory"]["owned_directories"] == ["a"]
        assert resumed["next"]["territory"]["excluded_directories"] == ["a/leaf"]
        assert resumed["next"]["territory"]["child_pages"] == [
            {
                "directory": "a/leaf",
                "identity": "The leaf subsystem.",
                "relationship": "nearest-page-descendant",
            }
        ]

    @pytest.mark.parametrize(
        ("decision", "identity", "expected_references"),
        [
            (hh.DECISION_PAGE, "A refreshed root subsystem.", ["human.protocol.html"]),
            (hh.DECISION_NONE, "", []),
        ],
    )
    def test_record_placement_owns_only_placement_fields(
        self,
        root_only: Path,
        decision: str,
        identity: str,
        expected_references: list[str],
    ) -> None:
        _write_fresh_record(
            root_only,
            ".",
            hh.DECISION_PAGE,
            instructions="Keep this exact instruction.",
            references=(("protocol", "Protocol"),),
        )
        (root_only / "README.md").write_text("changed\n", encoding="ascii")
        _commit(root_only, "change root")
        job = driver.placement_plan(root_only)["next"]

        driver.record_placement(
            root_only,
            ".",
            decision,
            identity,
            job["source_sha"],
            job["dirty"],
            job["brief_sha256"],
        )

        record = hh.load_record(hh.record_path(root_only, "."))
        assert record.decision == decision
        assert record.identity == identity
        assert record.source_sha == job["source_sha"]
        assert record.instructions == "Keep this exact instruction."
        assert [reference.file for reference in record.references] == expected_references
        assert not list((root_only / ".databench").glob(".human-record-*.tmp"))

    def test_record_placement_rejects_order_and_changed_brief(self, corpus: Path) -> None:
        job = driver.placement_plan(corpus)["next"]
        with pytest.raises(driver.DriverError, match="order violation"):
            driver.record_placement(
                corpus,
                ".",
                hh.DECISION_NONE,
                "",
                job["source_sha"],
                job["dirty"],
                job["brief_sha256"],
            )
        with pytest.raises(driver.DriverError, match="brief changed"):
            driver.record_placement(
                corpus,
                job["directory"],
                hh.DECISION_NONE,
                "",
                job["source_sha"],
                job["dirty"],
                "0" * 64,
            )

    def test_invalid_record_blocks_without_overwriting_it(self, root_only: Path) -> None:
        path = hh.record_path(root_only, ".")
        path.parent.mkdir(parents=True, exist_ok=True)
        original = '{"instructions":"Keep this text","decision":"invalid"}\n'
        path.write_text(original, encoding="ascii")

        plan = driver.placement_plan(root_only)

        assert plan["status"] == driver.STATUS_BLOCKED
        assert plan["blockers"][0]["code"] == "decision-record-invalid"
        assert path.read_text(encoding="ascii") == original

    def test_decision_flip_reports_each_affected_page_as_info_stale(
        self,
        corpus: Path,
    ) -> None:
        _settle(corpus)
        (corpus / "a" / "note.txt").write_text("changed\n", encoding="ascii")
        _commit(corpus, "change a")
        job = driver.placement_plan(corpus)["next"]
        assert job["directory"] == "a"

        result = driver.record_placement(
            corpus,
            "a",
            hh.DECISION_PAGE,
            "The a subsystem.",
            job["source_sha"],
            job["dirty"],
            job["brief_sha256"],
        )

        assert result["affected_pages"] == [".", "a/leaf"]
        assert [(item["level"], item["code"], item["directory"]) for item in result["findings"]] == [
            ("INFO", "STALE", "."),
            ("INFO", "STALE", "a/leaf"),
        ]
        rendered = driver.render_record_placement(result)
        assert "INFO STALE .:" in rendered
        assert "INFO STALE a/leaf:" in rendered


class TestGenerationPlan:
    def test_refuses_an_incomplete_placement_pass(
        self,
        corpus: Path,
        framework: Path,
    ) -> None:
        plan = driver.generation_plan(corpus, framework)

        assert plan["status"] == driver.STATUS_BLOCKED
        assert plan["placement_pending_count"] == 4
        assert plan["work"] == []
        assert plan["blockers"][0]["code"] == "placement-incomplete"

    def test_missing_checkpoint_schedules_every_page_leaf_first(
        self,
        corpus: Path,
        framework: Path,
    ) -> None:
        _settle(corpus)
        _write_page(corpus, "a/leaf")
        _write_page(corpus, ".")

        plan = driver.generation_plan(corpus, framework)

        assert plan["status"] == driver.STATUS_WORK
        assert plan["order"] == "leaf-first"
        assert plan["checkpoint"]["status"] == "missing"
        assert [job["directory"] for job in plan["work"]] == ["a/leaf", "."]
        assert plan["next"]["navigation"]["up"]["directory"] == "."
        root_job = next(job for job in plan["work"] if job["directory"] == ".")
        assert root_job["territory"]["owned_directories"] == [".", "a", "b"]
        assert root_job["territory"]["excluded_directories"] == ["a/leaf"]
        assert root_job["territory"]["child_pages"][0]["relationship"] == "nearest-page-descendant"
        assert root_job["framework"]["delivery"] == "verbatim"
        assert root_job["run_key"] == plan["run_key"]

    def test_checkpoint_resumes_and_framework_change_starts_a_full_pass(
        self,
        corpus: Path,
        framework: Path,
    ) -> None:
        _settle(corpus)
        _write_page(corpus, "a/leaf")
        _write_page(corpus, ".")
        plan = driver.generation_plan(corpus, framework)
        job = plan["next"]

        result = driver.complete_generation(
            corpus,
            job["directory"],
            framework,
            job["framework"]["sha256"],
            job["source_sha"],
            job["run_key"],
            "[]",
        )

        assert result["status"] == driver.STATUS_COMPLETE
        resumed = driver.generation_plan(corpus, framework)
        assert resumed["checkpoint"]["status"] == "active"
        assert resumed["checkpoint"]["finished_count"] == 1
        assert resumed["next"]["directory"] == "."

        framework.write_text("# Framework F\n\nUse another route.\n", encoding="ascii")
        restarted = driver.generation_plan(corpus, framework)
        assert restarted["checkpoint"]["status"] == "stale"
        assert [job["directory"] for job in restarted["work"]] == ["a/leaf", "."]

    def test_start_resumes_an_incomplete_run(
        self,
        corpus: Path,
        framework: Path,
    ) -> None:
        _settle(corpus)
        _write_page(corpus, "a/leaf")
        _write_page(corpus, ".")
        started = driver.start_generation(corpus, framework)
        assert started["mode"] == "start"
        job = started["next"]
        driver.complete_generation(
            corpus,
            job["directory"],
            framework,
            job["framework"]["sha256"],
            job["source_sha"],
            job["run_key"],
            "[]",
        )

        resumed = driver.start_generation(corpus, framework)

        assert resumed["mode"] == "resume"
        assert resumed["checkpoint"]["finished_count"] == 1
        assert resumed["next"]["directory"] == "."

    def test_start_restarts_a_completed_run(
        self,
        root_only: Path,
        framework: Path,
    ) -> None:
        _write_fresh_record(root_only, ".", hh.DECISION_PAGE)
        _write_page(root_only, ".")
        started = driver.start_generation(root_only, framework)
        job = started["next"]
        driver.complete_generation(
            root_only,
            ".",
            framework,
            job["framework"]["sha256"],
            job["source_sha"],
            job["run_key"],
            "[]",
        )
        assert driver.generation_plan(root_only, framework)["status"] == driver.STATUS_COMPLETE

        restarted = driver.start_generation(root_only, framework)

        assert restarted["mode"] == "restart"
        assert restarted["checkpoint"]["finished_count"] == 0
        assert restarted["pending_count"] == 1
        assert restarted["next"]["directory"] == "."

    def test_start_reports_if_placement_changes_before_the_refreshed_plan(
        self,
        root_only: Path,
        framework: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_fresh_record(root_only, ".", hh.DECISION_PAGE)
        _write_page(root_only, ".")
        original_plan = driver.generation_plan
        calls = 0

        def change_before_second_plan(repo_root, framework_path):
            nonlocal calls
            calls += 1
            if calls == 2:
                (root_only / "README.md").write_text("changed\n", encoding="ascii")
                _commit(root_only, "change during start")
            return original_plan(repo_root, framework_path)

        monkeypatch.setattr(driver, "generation_plan", change_before_second_plan)

        result = driver.start_generation(root_only, framework)

        assert calls == 2
        assert result["status"] == driver.STATUS_BLOCKED
        assert result["mode"] == "start"
        assert result["blockers"][0]["code"] == "placement-incomplete"

    def test_start_restarts_all_pages_when_a_completed_page_is_damaged(
        self,
        corpus: Path,
        framework: Path,
    ) -> None:
        _settle(corpus)
        _write_page(corpus, "a/leaf")
        _write_page(corpus, ".")
        started = driver.start_generation(corpus, framework)
        while started["next"] is not None:
            job = started["next"]
            driver.complete_generation(
                corpus,
                job["directory"],
                framework,
                job["framework"]["sha256"],
                job["source_sha"],
                job["run_key"],
                "[]",
            )
            plan = driver.generation_plan(corpus, framework)
            started = {"next": plan["next"]}
        completed = driver.generation_plan(corpus, framework)
        assert completed["checkpoint"]["run_state"] == "complete"

        (corpus / hh.PAGE_FILENAME).write_text("damaged\n", encoding="ascii")
        restarted = driver.start_generation(corpus, framework)

        assert restarted["mode"] == "restart"
        assert restarted["pending_count"] == 2
        assert restarted["next"]["directory"] == "a/leaf"

    def test_resume_reopens_only_an_invalid_finished_page(
        self,
        corpus: Path,
        framework: Path,
    ) -> None:
        decisions = {
            "a/leaf": hh.DECISION_PAGE,
            "a": hh.DECISION_PAGE,
            "b": hh.DECISION_PAGE,
            ".": hh.DECISION_PAGE,
        }
        for entry in discover.scan(corpus)["directories"]:
            _write_fresh_record(corpus, entry["directory"], decisions[entry["directory"]])
        for directory in ("a/leaf", "a", "b", "."):
            _write_page(corpus, directory)
        started = driver.start_generation(corpus, framework)
        for expected in ("a/leaf", "a"):
            job = driver.generation_plan(corpus, framework)["next"]
            assert job["directory"] == expected
            driver.complete_generation(
                corpus,
                expected,
                framework,
                job["framework"]["sha256"],
                job["source_sha"],
                job["run_key"],
                "[]",
            )
        assert started["mode"] == "start"

        (corpus / "a" / "leaf" / hh.PAGE_FILENAME).write_text("damaged\n", encoding="ascii")
        resumed = driver.start_generation(corpus, framework)

        assert resumed["mode"] == "resume"
        assert resumed["reopened"] == ["a/leaf"]
        plan = driver.generation_plan(corpus, framework)
        assert plan["checkpoint"]["invalidated"] == []
        assert plan["checkpoint"]["finished"] == ["a"]
        assert [job["directory"] for job in plan["work"]] == [
            "a/leaf",
            "b",
            ".",
        ]
        _write_page(corpus, "a/leaf")
        job = driver.generation_plan(corpus, framework)["next"]
        assert job["directory"] == "a/leaf"
        result = driver.complete_generation(
            corpus,
            "a/leaf",
            framework,
            job["framework"]["sha256"],
            job["source_sha"],
            job["run_key"],
            "[]",
        )
        assert result["status"] == driver.STATUS_COMPLETE
        assert driver.generation_plan(corpus, framework)["next"]["directory"] == "b"

    def test_changed_complete_prompt_input_starts_a_full_pass(
        self,
        root_only: Path,
        framework: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        contract = tmp_path / "generation-contract.md"
        contract.write_text("first contract\n", encoding="ascii")
        monkeypatch.setattr(
            driver,
            "GENERATION_INPUT_PATHS",
            (("generation-contract", contract),),
        )
        _write_fresh_record(root_only, ".", hh.DECISION_PAGE)
        _write_page(root_only, ".")
        started = driver.start_generation(root_only, framework)
        job = started["next"]
        driver.complete_generation(
            root_only,
            ".",
            framework,
            job["framework"]["sha256"],
            job["source_sha"],
            job["run_key"],
            "[]",
        )

        contract.write_text("changed contract\n", encoding="ascii")
        restarted = driver.generation_plan(root_only, framework)

        assert restarted["checkpoint"]["status"] == "stale"
        assert restarted["generation_inputs"][0]["delivery"] == "complete-file"
        assert restarted["next"]["directory"] == "."

    def test_completion_refuses_a_parent_before_its_page_child(
        self,
        corpus: Path,
        framework: Path,
    ) -> None:
        _settle(corpus)
        _write_page(corpus, "a/leaf")
        _write_page(corpus, ".")
        plan = driver.generation_plan(corpus, framework)
        root_job = next(job for job in plan["work"] if job["directory"] == ".")

        with pytest.raises(driver.DriverError, match="order violation"):
            driver.complete_generation(
                corpus,
                ".",
                framework,
                root_job["framework"]["sha256"],
                root_job["source_sha"],
                root_job["run_key"],
                "[]",
            )

    def test_generation_blocks_if_discovery_changes_during_planning(
        self,
        corpus: Path,
        framework: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _settle(corpus)
        _write_page(corpus, "a/leaf")
        _write_page(corpus, ".")
        original_scan = driver.discover.scan
        calls = 0

        def changing_scan(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original_scan(*args, **kwargs)
            if calls == 3:
                changed = dict(result["directories"][-1])
                changed["dirty"] = not changed["dirty"]
                result = {**result, "directories": [*result["directories"][:-1], changed]}
            return result

        monkeypatch.setattr(driver.discover, "scan", changing_scan)

        plan = driver.generation_plan(corpus, framework)

        assert calls == 3
        assert plan["status"] == driver.STATUS_BLOCKED
        assert plan["blockers"][0]["code"] == "tree-changed-during-plan"

    def test_completion_refuses_a_directory_that_is_not_pending(
        self,
        corpus: Path,
        framework: Path,
    ) -> None:
        _settle(corpus)
        _write_page(corpus, "a/leaf")
        _write_page(corpus, ".")
        plan = driver.generation_plan(corpus, framework)
        job = plan["next"]
        driver.complete_generation(
            corpus,
            job["directory"],
            framework,
            job["framework"]["sha256"],
            job["source_sha"],
            job["run_key"],
            "[]",
        )

        with pytest.raises(driver.DriverError, match="not pending generation"):
            driver.complete_generation(
                corpus,
                job["directory"],
                framework,
                job["framework"]["sha256"],
                job["source_sha"],
                job["run_key"],
                "[]",
            )


class TestGenerationCompletion:
    def test_changes_only_references_and_checkpoints_after_ck1(
        self,
        root_only: Path,
        framework: Path,
    ) -> None:
        _write_fresh_record(
            root_only,
            ".",
            hh.DECISION_PAGE,
            instructions="Retain this instruction exactly.",
        )
        _write_page(root_only, ".", references=(("protocol", "Protocol"),))
        plan = driver.generation_plan(root_only, framework)
        job = plan["next"]
        before = hh.load_record(hh.record_path(root_only, ".")).to_dict()

        result = driver.complete_generation(
            root_only,
            ".",
            framework,
            job["framework"]["sha256"],
            job["source_sha"],
            job["run_key"],
            json.dumps(
                [
                    {
                        "slug": "protocol",
                        "title": "Protocol",
                        "file": "human.protocol.html",
                    }
                ]
            ),
        )

        after = hh.load_record(hh.record_path(root_only, ".")).to_dict()
        assert result["fail_count"] == 0
        assert result["checkpoint_path"] == ".databench/human-tree-generation.json"
        assert not (root_only / ".databench" / ".human-tree-generation.json.tmp").exists()
        assert not list((root_only / ".databench").glob(".human-record-*.tmp"))
        assert after["references"] == [
            {"slug": "protocol", "title": "Protocol", "file": "human.protocol.html"}
        ]
        assert {key: value for key, value in after.items() if key != "references"} == {
            key: value for key, value in before.items() if key != "references"
        }

    def test_failed_ck1_remains_pending_and_writes_no_checkpoint(
        self,
        root_only: Path,
        framework: Path,
    ) -> None:
        _write_fresh_record(root_only, ".", hh.DECISION_PAGE)
        plan = driver.generation_plan(root_only, framework)
        job = plan["next"]

        result = driver.complete_generation(
            root_only,
            ".",
            framework,
            job["framework"]["sha256"],
            job["source_sha"],
            job["run_key"],
            "[]",
        )

        assert result["status"] == driver.STATUS_BLOCKED
        assert result["fail_count"] == 1
        assert "page-missing" in [f["code"] for f in result["findings"]]
        assert not (root_only / driver.CHECKPOINT_RELATIVE_PATH).exists()
        assert driver.generation_plan(root_only, framework)["next"]["directory"] == "."

    def test_changed_run_key_is_rejected(
        self,
        root_only: Path,
        framework: Path,
    ) -> None:
        _write_fresh_record(root_only, ".", hh.DECISION_PAGE)
        _write_page(root_only, ".")
        job = driver.generation_plan(root_only, framework)["next"]

        with pytest.raises(driver.DriverError, match="generation inputs changed"):
            driver.complete_generation(
                root_only,
                ".",
                framework,
                job["framework"]["sha256"],
                job["source_sha"],
                "0" * 64,
                "[]",
            )

    def test_invalid_checkpoint_fails_loudly(
        self,
        root_only: Path,
        framework: Path,
    ) -> None:
        _write_fresh_record(root_only, ".", hh.DECISION_PAGE)
        path = root_only / driver.CHECKPOINT_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json\n", encoding="ascii")

        with pytest.raises(driver.DriverError, match="checkpoint is unreadable"):
            driver.generation_plan(root_only, framework)

    def test_malformed_finished_set_fails_even_when_checkpoint_is_stale(
        self,
        root_only: Path,
        framework: Path,
    ) -> None:
        _write_fresh_record(root_only, ".", hh.DECISION_PAGE)
        path = root_only / driver.CHECKPOINT_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_key": "0" * 64,
                    "state": "in-progress",
                    "finished": [7],
                }
            ),
            encoding="ascii",
        )

        with pytest.raises(driver.DriverError, match="non-string finished item"):
            driver.generation_plan(root_only, framework)

    def test_boolean_checkpoint_schema_version_fails_loudly(
        self,
        root_only: Path,
        framework: Path,
    ) -> None:
        _write_fresh_record(root_only, ".", hh.DECISION_PAGE)
        path = root_only / driver.CHECKPOINT_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": True,
                    "run_key": "0" * 64,
                    "state": "in-progress",
                    "finished": [],
                }
            ),
            encoding="ascii",
        )

        with pytest.raises(driver.DriverError, match="schema_version must be 1"):
            driver.generation_plan(root_only, framework)

    def test_stale_at_completion_stays_pending(
        self,
        root_only: Path,
        framework: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_fresh_record(root_only, ".", hh.DECISION_PAGE)
        _write_page(root_only, ".")
        started = driver.start_generation(root_only, framework)
        job = started["next"]
        original_check = driver.checker.check
        calls = 0

        def stale_on_completion(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original_check(*args, **kwargs)
            if calls == 2:
                finding = {
                    "level": "INFO",
                    "code": "STALE",
                    "directory": ".",
                    "message": "source changed during completion",
                    "file": "human.html",
                }
                result = {
                    **result,
                    "findings": [*result["findings"], finding],
                    "info_count": result["info_count"] + 1,
                }
            return result

        monkeypatch.setattr(driver.checker, "check", stale_on_completion)

        result = driver.complete_generation(
            root_only,
            ".",
            framework,
            job["framework"]["sha256"],
            job["source_sha"],
            job["run_key"],
            "[]",
        )

        assert calls == 2
        assert result["status"] == driver.STATUS_BLOCKED
        checkpoint = driver.generation_plan(root_only, framework)["checkpoint"]
        assert checkpoint["run_state"] == "in-progress"
        assert checkpoint["finished_count"] == 0

    def test_none_output_is_removed_before_successful_completion(
        self,
        root_only: Path,
        framework: Path,
    ) -> None:
        _write_fresh_record(root_only, ".", hh.DECISION_NONE)
        page = root_only / hh.PAGE_FILENAME
        reference = root_only / "human.extra.html"
        page.write_text("generated page\n", encoding="ascii")
        reference.write_text("generated reference\n", encoding="ascii")
        started = driver.start_generation(root_only, framework)
        job = started["next"]

        assert job["action"] == "remove"
        page.unlink()
        reference.unlink()
        result = driver.complete_generation(
            root_only,
            ".",
            framework,
            job["framework"]["sha256"],
            job["source_sha"],
            job["run_key"],
            "[]",
        )

        assert result["status"] == driver.STATUS_COMPLETE
        assert result["checkpoint_path"] == ".databench/human-tree-generation.json"
        assert driver.generation_plan(root_only, framework)["status"] == driver.STATUS_COMPLETE

    def test_checkpoint_path_does_not_collide_with_a_subject_record(
        self,
        tmp_path: Path,
        framework: Path,
    ) -> None:
        root = tmp_path / "namespace"
        unicode_directory = "caf" + chr(233)
        (root / "tree-generation.json").mkdir(parents=True)
        (root / ".decision.yaml.tmp").mkdir()
        (root / unicode_directory).mkdir()
        _git(root.parent, "init", "-q", str(root))
        _git(root, "config", "user.email", "tests@example.invalid")
        _git(root, "config", "user.name", "tests")
        (root / "README.md").write_text("root\n", encoding="ascii")
        (root / "tree-generation.json" / "item.txt").write_text("item\n", encoding="ascii")
        (root / ".decision.yaml.tmp" / "item.txt").write_text("item\n", encoding="ascii")
        (root / unicode_directory / "item.txt").write_text("item\n", encoding="ascii")
        _commit(root, "corpus")
        _write_fresh_record(root, "tree-generation.json", hh.DECISION_NONE)
        _write_fresh_record(root, ".decision.yaml.tmp", hh.DECISION_NONE)
        _write_fresh_record(root, unicode_directory, hh.DECISION_NONE)
        _write_fresh_record(root, ".", hh.DECISION_PAGE)
        (root / unicode_directory / "item.txt").write_text("changed\n", encoding="ascii")
        _commit(root, "change unicode directory")
        child_placement = driver.placement_plan(root)["next"]
        assert child_placement["directory"] == unicode_directory
        driver.record_placement(
            root,
            unicode_directory,
            hh.DECISION_NONE,
            "",
            child_placement["source_sha"],
            child_placement["dirty"],
            child_placement["brief_sha256"],
        )
        placement = driver.placement_plan(root)["next"]
        assert placement["directory"] == "."
        driver.record_placement(
            root,
            ".",
            hh.DECISION_PAGE,
            "The root subsystem.",
            placement["source_sha"],
            placement["dirty"],
            placement["brief_sha256"],
        )
        _write_page(root, ".")

        result = driver.start_generation(root, framework)

        assert result["status"] == driver.STATUS_WORK
        assert hh.record_path(root, "tree-generation.json").is_file()
        assert hh.record_path(root, ".decision.yaml.tmp").is_file()
        assert hh.record_path(root, unicode_directory).is_file()
        assert (root / driver.CHECKPOINT_RELATIVE_PATH).is_file()
        assert hh.record_path(root, "tree-generation.json") != (
            root / driver.CHECKPOINT_RELATIVE_PATH
        )


def test_cli_placement_plan_is_read_only_json(corpus: Path) -> None:
    before = _tree_snapshot(corpus)
    proc = subprocess.run(
        [sys.executable, str(DRIVER_PATH), "placement", str(corpus), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    after = _tree_snapshot(corpus)

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["next"]["directory"] == "a/leaf"
    assert after == before


def test_cli_generation_plan_is_read_only_json(root_only: Path, framework: Path) -> None:
    _write_fresh_record(root_only, ".", hh.DECISION_PAGE)
    _write_page(root_only, ".")
    plan = driver.generation_plan(root_only, framework)
    driver._write_checkpoint(root_only, plan["run_key"], set(), "in-progress")
    before = _tree_snapshot(root_only)

    proc = subprocess.run(
        [
            sys.executable,
            str(DRIVER_PATH),
            "generation",
            str(root_only),
            "--framework",
            str(framework),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    after = _tree_snapshot(root_only)

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["next"]["directory"] == "."
    assert after == before


def test_cli_start_generation_creates_a_resumable_run(
    root_only: Path,
    framework: Path,
) -> None:
    _write_fresh_record(root_only, ".", hh.DECISION_PAGE)
    _write_page(root_only, ".")

    proc = subprocess.run(
        [
            sys.executable,
            str(DRIVER_PATH),
            "start-generation",
            str(root_only),
            "--framework",
            str(framework),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    result = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert result["mode"] == "start"
    assert result["next"]["directory"] == "."
    assert (root_only / driver.CHECKPOINT_RELATIVE_PATH).is_file()


def test_every_generation_input_exists_and_is_declared_by_the_lane() -> None:
    """The generating agent's complete-file inputs are a contract, not an implementation detail.

    Nothing else pins WHICH files reach the agent, so dropping one -- or adding one
    the lane never tells the agent to read -- fails silently: pages still generate,
    just without the input. The prose contract is the motivating case; it was added
    after a corpus was generated with no writing guidance reaching the agent at all.
    """
    names = [name for name, _ in driver.GENERATION_INPUT_PATHS]
    assert names == [
        "generation-lane",
        "human-html-standards",
        "human-html-presentation",
        "technical-english",
    ]

    for name, path in driver.GENERATION_INPUT_PATHS:
        assert path.is_file(), "generation input %s is missing: %s" % (name, path)
        assert path.read_bytes().strip(), "generation input %s is empty: %s" % (name, path)

    generation = (MD_DOMAIN / "references" / "lanes" / "generation-lane.md").read_text(
        encoding="ascii"
    )
    for _, path in driver.GENERATION_INPUT_PATHS:
        if path.name == "generation-lane.md":
            continue
        assert path.name in generation, (
            "generation-lane.md never tells the agent to read %s, so the file is "
            "hashed into the run key but not requested" % path.name
        )


def test_lane_and_skill_contract_name_the_tree_driver() -> None:
    coverage = (MD_DOMAIN / "references" / "lanes" / "coverage-lane.md").read_text(
        encoding="ascii"
    )
    generation = (MD_DOMAIN / "references" / "lanes" / "generation-lane.md").read_text(
        encoding="ascii"
    )
    skill = (MD_DOMAIN / "SKILL.md").read_text(encoding="ascii")
    standards = (
        MD_DOMAIN / "references" / "standards" / "human-html-standards.md"
    ).read_text(encoding="ascii")
    assert "human_html_tree.py placement" in coverage
    assert "human_html_tree.py generation" in generation
    assert "human_html_tree.py start-generation" in generation
    assert "complete framework file" in generation
    assert "byte-for-byte" in generation
    assert "generate human-html <repository-root> --tree --framework <path>" in skill
    assert ".databench/human-tree-generation.json" in generation
    assert "tool: scripts/human_html_tree.py" in skill
    assert "analyze human-html <directory> [--tree] [--json]" in standards
