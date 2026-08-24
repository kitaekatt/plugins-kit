"""End-to-end tests for the task-system ``validate`` verb (spec section 9).

Covers every error condition, every warning condition, every classification
outcome (including the absent-folder tri-state), the remote short-circuit,
stub resolution, and the CLI exit-code contract.

All fixtures build task folders under pytest tmp_path with project_root
pointed there -- the real repo's tmp/ and dev/ are never touched. Host
detection is injected (local_host=LOCAL) so nothing depends on the machine's
hostname.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap_guard import _REEXEC_GUARD_ENV
from task_system.validate import validate_ref

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TASK_CLI = (
    _REPO_ROOT
    / "plugins"
    / "awesome-kit"
    / "skills"
    / "task"
    / "scripts"
    / "task.py"
)

LOCAL = "testhost"
OTHER = "otherhost"


PLAN_MD_SCAFFOLD = "# Plan\n\n```yaml\ntask_items:\n  items: []\n```\n"


def make_task(
    project_root: Path,
    rel: str,
    *,
    task_block: dict | None = None,
    scaffolding: bool = True,
    yaml_text: str | None = None,
    **fields,
) -> Path:
    """Create a task folder at <project_root>/<rel> with a hand-off layout."""
    folder = project_root / rel
    folder.mkdir(parents=True)
    if yaml_text is not None:
        (folder / "task.yaml").write_text(yaml_text, encoding="utf-8")
    else:
        block = {
            "_schema_version": "1",
            "type": "hand-off",
            "title": "A task",
            "status": "active",
        }
        block.update(fields)
        if task_block is not None:
            block = task_block
        (folder / "task.yaml").write_text(
            yaml.safe_dump({"task": block}), encoding="utf-8"
        )
    if scaffolding:
        (folder / "plan.md").write_text(PLAN_MD_SCAFFOLD, encoding="utf-8")
        for fname in ("CLAUDE.md", "log.md"):
            (folder / fname).write_text("placeholder\n", encoding="utf-8")
    return folder


def v(ref: str, project_root: Path, **kw):
    kw.setdefault("local_host", LOCAL)
    return validate_ref(ref, project_root, **kw)


@pytest.fixture
def git_root(tmp_path):
    """A temp project root that is a real git repo."""
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True
    )
    return tmp_path


def _commit_all(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "snapshot",
        ],
        check=True,
    )


class TestClassificationOutcomes:
    def test_active_tmp_task_clean(self, tmp_path):
        make_task(tmp_path, "tmp/foo")
        result = v("tmp/foo", tmp_path)
        assert result.classification == "active"
        assert result.clean
        assert result.canonical == "tmp/foo"

    def test_stored_blocked_status(self, tmp_path):
        make_task(tmp_path, "tmp/foo", status="blocked")
        assert v("tmp/foo", tmp_path).classification == "blocked"

    def test_stored_closed_status(self, tmp_path):
        make_task(tmp_path, "tmp/foo", status="closed")
        assert v("tmp/foo", tmp_path).classification == "closed"

    def test_tmp_archived_folder_kept_no_warning(self, tmp_path):
        # tmp archive keeps the folder + marks it; only NON-tmp archived
        # folders warn.
        make_task(tmp_path, "tmp/foo", status="archived")
        result = v("tmp/foo", tmp_path)
        assert result.classification == "archived"
        assert result.clean

    def test_nonempty_blocked_by_reads_as_blocked(self, tmp_path):
        # blocked_by entry is a non-tmp absent path: reads as archived per the
        # tri-state, so it is NOT dangling -- the result stays warning-free.
        make_task(
            tmp_path, "tmp/foo", status="active", blocked_by=["dev/tasks/gone"]
        )
        result = v("tmp/foo", tmp_path)
        assert result.classification == "blocked"
        assert result.clean


class TestTriState:
    def test_nontmp_absent_is_archived_no_findings(self, tmp_path):
        result = v("dev/tasks/long-done", tmp_path)
        assert result.classification == "archived"
        assert result.clean

    def test_tmp_absent_no_host_is_orphaned_with_warning(self, tmp_path):
        result = v("tmp/vanished", tmp_path)
        assert result.classification == "orphaned"
        assert result.errors == []
        assert len(result.warnings) == 1
        assert "orphaned tmp reference" in result.warnings[0]

    def test_tmp_absent_local_host_is_orphaned(self, tmp_path):
        result = v("tmp/vanished", tmp_path, ref_host=LOCAL)
        assert result.classification == "orphaned"
        assert any("orphaned tmp reference" in w for w in result.warnings)

    def test_tmp_other_host_is_remote(self, tmp_path):
        result = v("tmp/elsewhere", tmp_path, ref_host=OTHER)
        assert result.classification == "remote"
        assert result.clean


class TestRemoteShortCircuit:
    def test_garbage_task_yaml_does_not_matter(self, tmp_path):
        make_task(
            tmp_path,
            "tmp/spike",
            yaml_text="task: [unclosed\n",
            scaffolding=False,
        )
        result = v("tmp/spike", tmp_path, ref_host=OTHER)
        assert result.classification == "remote"
        assert result.clean

    def test_absent_folder_still_remote(self, tmp_path):
        result = v("tmp/spike", tmp_path, ref_host=OTHER)
        assert result.classification == "remote"
        assert result.clean

    def test_host_on_nontmp_path_is_ignored(self, tmp_path):
        # host is only meaningful for tmp paths (spec 2.3).
        make_task(tmp_path, "tmp/foo")
        # dev/tasks ref with mismatching host validates normally (tri-state).
        result = v("dev/tasks/gone", tmp_path, ref_host=OTHER)
        assert result.classification == "archived"


class TestErrors:
    def _assert_invalid(self, result, fragment):
        assert result.classification == "invalid"
        assert any(fragment in e for e in result.errors), result.errors

    def test_missing_task_yaml(self, tmp_path):
        folder = tmp_path / "tmp" / "foo"
        folder.mkdir(parents=True)
        (folder / "CLAUDE.md").write_text("x\n", encoding="utf-8")
        self._assert_invalid(v("tmp/foo", tmp_path), "missing task.yaml")

    def test_unparseable_yaml(self, tmp_path):
        make_task(tmp_path, "tmp/foo", yaml_text="task: [unclosed\n")
        self._assert_invalid(v("tmp/foo", tmp_path), "unparseable YAML")

    def test_yaml_root_not_mapping(self, tmp_path):
        make_task(tmp_path, "tmp/foo", yaml_text="- a\n- b\n")
        self._assert_invalid(v("tmp/foo", tmp_path), "schema violation")

    def test_missing_required_field(self, tmp_path):
        block = {"_schema_version": "1", "type": "hand-off", "status": "active"}
        make_task(tmp_path, "tmp/foo", task_block=block)
        self._assert_invalid(v("tmp/foo", tmp_path), "task.title")

    def test_wrong_type_field(self, tmp_path):
        make_task(tmp_path, "tmp/foo", title=42)
        self._assert_invalid(v("tmp/foo", tmp_path), "task.title")

    def test_status_outside_vocabulary(self, tmp_path):
        make_task(tmp_path, "tmp/foo", status="paused")
        self._assert_invalid(v("tmp/foo", tmp_path), "state vocabulary")

    def test_priority_outside_pattern(self, tmp_path):
        make_task(tmp_path, "tmp/foo", priority="P9")
        self._assert_invalid(v("tmp/foo", tmp_path), "task.priority")

    def test_unknown_schema_version(self, tmp_path):
        make_task(tmp_path, "tmp/foo", _schema_version="99")
        self._assert_invalid(v("tmp/foo", tmp_path), "unknown version")

    def test_missing_scaffolding_file(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        (folder / "plan.md").unlink()
        self._assert_invalid(
            v("tmp/foo", tmp_path), "missing scaffolding file: plan.md"
        )

    def test_unknown_type(self, tmp_path):
        make_task(tmp_path, "tmp/foo", type="sprint")
        self._assert_invalid(v("tmp/foo", tmp_path), "unknown type")

    def test_ref_outside_known_roots(self, tmp_path):
        self._assert_invalid(
            v("docs/foo", tmp_path), "not a known task location"
        )

    def test_ref_escaping_project_root(self, tmp_path):
        self._assert_invalid(
            v("../foo/bar", tmp_path), "escapes the project root"
        )


class TestWarnings:
    def test_nontmp_archived_folder_warns(self, git_root):
        make_task(git_root, "dev/tasks/done", status="archived")
        _commit_all(git_root)
        result = v("dev/tasks/done", git_root)
        assert result.classification == "archived"
        assert result.errors == []
        assert len(result.warnings) == 1
        assert "status: archived" in result.warnings[0]

    def test_uncommitted_dev_tasks_folder_warns(self, git_root):
        make_task(git_root, "dev/tasks/wip")
        result = v("dev/tasks/wip", git_root)
        assert result.classification == "active"
        assert any("uncommitted dev/tasks folder" in w for w in result.warnings)

    def test_committed_dev_tasks_folder_clean(self, git_root):
        make_task(git_root, "dev/tasks/done")
        _commit_all(git_root)
        result = v("dev/tasks/done", git_root)
        assert result.classification == "active"
        assert result.clean

    def test_modified_committed_folder_warns(self, git_root):
        folder = make_task(git_root, "dev/tasks/done")
        _commit_all(git_root)
        (folder / "plan.md").write_text("edited\n", encoding="utf-8")
        result = v("dev/tasks/done", git_root)
        assert any("uncommitted dev/tasks folder" in w for w in result.warnings)

    def test_dev_tasks_outside_any_git_repo_notes_not_warns(self, tmp_path):
        # The task system has no dependency on git: outside a git repo the
        # script cannot verify VCS state (the workspace may use Perforce or
        # another VCS), so this is an advisory NOTE in neutral language --
        # never a blocking warning.
        make_task(tmp_path, "dev/tasks/norepo")
        result = v("dev/tasks/norepo", tmp_path)
        assert result.classification == "active"
        assert result.warnings == []
        assert any(
            "version-control state unverified" in n for n in result.notes
        )

    def test_git_ignored_dev_tasks_folder_notes_not_warns(self, git_root):
        # `git status --porcelain` is SILENT about an ignored path, so an
        # ignored folder used to read as CLEAN -- validate reported the
        # durable work as saved when git will never carry it. It must be
        # reported, but as a NOTE: warnings gate `work`, and a project that
        # deliberately gitignores its task root would have every task blocked.
        (git_root / ".gitignore").write_text("dev/\n", encoding="utf-8")
        _commit_all(git_root)
        make_task(git_root, "dev/tasks/scratch")
        result = v("dev/tasks/scratch", git_root)
        assert result.classification == "active"
        assert result.warnings == []
        assert result.clean
        assert any(
            "version control will not carry" in n for n in result.notes
        )

    def test_git_ignored_is_not_reported_as_unverified(self, git_root):
        # The two notes make OPPOSITE claims and must not be confused:
        # no-repo means "cannot check here"; ignored means "checked, and git
        # will never hold it".
        (git_root / ".gitignore").write_text("dev/\n", encoding="utf-8")
        _commit_all(git_root)
        make_task(git_root, "dev/tasks/scratch")
        result = v("dev/tasks/scratch", git_root)
        assert not any(
            "version-control state unverified" in n for n in result.notes
        )

    def test_tmp_task_never_gets_git_warnings(self, tmp_path):
        make_task(tmp_path, "tmp/foo")
        assert v("tmp/foo", tmp_path).clean

    def test_dangling_orphaned_tmp_dep_warns(self, tmp_path):
        make_task(tmp_path, "tmp/foo", depends_on=["tmp/never-there"])
        result = v("tmp/foo", tmp_path)
        assert result.classification == "active"
        assert any(
            "dangling depends_on entry 'tmp/never-there'" in w
            for w in result.warnings
        )

    def test_dangling_malformed_dep_warns(self, tmp_path):
        make_task(tmp_path, "tmp/foo", blocked_by=["elsewhere/thing"])
        result = v("tmp/foo", tmp_path)
        assert any(
            "dangling blocked_by entry" in w for w in result.warnings
        )

    def test_dangling_nonstring_dep_warns(self, tmp_path):
        make_task(tmp_path, "tmp/foo", depends_on=[123])
        result = v("tmp/foo", tmp_path)
        assert any("not a path string" in w for w in result.warnings)

    def test_nontmp_absent_dep_is_not_dangling(self, tmp_path):
        # Tri-state: a non-tmp absent path reads as archived.
        make_task(tmp_path, "tmp/foo", depends_on=["dev/tasks/shipped"])
        assert v("tmp/foo", tmp_path).clean

    def test_extant_deps_are_not_dangling(self, tmp_path):
        make_task(tmp_path, "tmp/dep")
        make_task(tmp_path, "dev/tasks/dep2", scaffolding=True)
        make_task(
            tmp_path, "tmp/foo", depends_on=["tmp/dep", "dev/tasks/dep2"]
        )
        result = v("tmp/foo", tmp_path)
        assert not any("dangling" in w for w in result.warnings)


def _filler(n: int) -> str:
    """n lines of section-free filler."""
    return "".join(f"line {i}\n" for i in range(n))


def _sectioned(sections: list[tuple[str, int]]) -> str:
    """A doc of ## sections; each (title, total) counts its heading line."""
    parts = []
    for title, total in sections:
        parts.append(f"## {title}\n" + "".join("x\n" for _ in range(total - 1)))
    return "".join(parts)


def _write(folder: Path, name: str, text: str) -> None:
    (folder / name).write_text(text, encoding="utf-8")


class TestDocSizeBudgets:
    """The two-tier document-size budgets (validate.py constants):
    note at the healthy target, blocking warning at the ceiling;
    log.md/log-*.md exempt; dominant-section + diary notes."""

    def test_claude_md_over_ceiling_warns_with_remedy(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        _write(
            folder,
            "CLAUDE.md",
            _sectioned([("Where we are today", 300), ("Protocols", 80), ("Behaviors", 30)]),
        )
        result = v("tmp/foo", tmp_path)
        assert not result.clean
        (warning,) = [w for w in result.warnings if "oversized document" in w]
        assert "tmp/foo/CLAUDE.md is 410 lines (ceiling 400)" in warning
        # Remedy clause: the largest sections with individual line counts.
        assert "'## Where we are today' 300" in warning
        assert "'## Protocols' 80" in warning
        assert "'## Behaviors' 30" in warning
        assert "handoff-template.md" in warning
        assert "Rotation strategy" in warning

    def test_claude_md_at_ceiling_notes_but_does_not_warn(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        _write(folder, "CLAUDE.md", _filler(400))
        result = v("tmp/foo", tmp_path)
        assert result.clean  # over target, at ceiling: note only
        (note,) = [n for n in result.notes if "approaching budget" in n]
        assert "tmp/foo/CLAUDE.md is 400 lines (healthy target 250, ceiling 400)" in note
        assert "rotate now while it is cheap" in note

    def test_claude_md_just_over_target_notes(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        _write(folder, "CLAUDE.md", _filler(251))
        result = v("tmp/foo", tmp_path)
        assert result.clean
        assert any("approaching budget" in n for n in result.notes)

    def test_claude_md_at_target_fully_clean(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        _write(folder, "CLAUDE.md", _filler(250))
        result = v("tmp/foo", tmp_path)
        assert result.clean
        assert result.notes == []

    def test_plan_md_tiers(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        # plan.md keeps its task_items block; filler goes after it.
        _write(folder, "plan.md", PLAN_MD_SCAFFOLD + _filler(300))
        result = v("tmp/foo", tmp_path)
        assert result.clean
        (note,) = [n for n in result.notes if "approaching budget" in n]
        assert "tmp/foo/plan.md" in note
        assert "healthy target 300, ceiling 400" in note

        _write(folder, "plan.md", PLAN_MD_SCAFFOLD + _filler(400))
        result = v("tmp/foo", tmp_path)
        assert any(
            "oversized document: tmp/foo/plan.md" in w and "(ceiling 400)" in w
            for w in result.warnings
        )

    def test_other_doc_ceiling_800_no_note_tier(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        _write(folder, "notes.md", _filler(800))
        result = v("tmp/foo", tmp_path)
        assert result.clean
        assert result.notes == []  # reference docs have no note tier

        _write(folder, "notes.md", _filler(801))
        result = v("tmp/foo", tmp_path)
        (warning,) = [w for w in result.warnings if "oversized document" in w]
        assert "tmp/foo/notes.md is 801 lines (ceiling 800)" in warning

    def test_log_md_exempt(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        _write(folder, "log.md", _filler(5000))
        result = v("tmp/foo", tmp_path)
        assert result.clean
        assert result.notes == []

    def test_split_log_files_exempt(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        _write(folder, "log-decisions.md", _filler(1200))
        _write(folder, "log-dead-ends.md", _filler(900))
        result = v("tmp/foo", tmp_path)
        assert result.clean
        assert result.notes == []

    def test_dominant_section_notes(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        _write(
            folder,
            "CLAUDE.md",
            _sectioned([("Where we are today", 100), ("Rest", 50)]),
        )
        result = v("tmp/foo", tmp_path)
        assert result.clean  # note tier only -- never blocks
        (note,) = [n for n in result.notes if "dominant section" in n]
        assert "'## Where we are today' is 100 of 150 lines" in note
        assert "over half the document" in note

    def test_dominant_section_below_floor_silent(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        _write(
            folder,
            "CLAUDE.md",
            _sectioned([("Where we are today", 99), ("Rest", 50)]),
        )
        result = v("tmp/foo", tmp_path)  # 149 lines: under the 150 floor
        assert not any("dominant section" in n for n in result.notes)

    def test_balanced_sections_no_dominance_note(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        _write(
            folder,
            "CLAUDE.md",
            _sectioned([("A", 75), ("B", 75), ("C", 50)]),
        )
        result = v("tmp/foo", tmp_path)
        assert not any("dominant section" in n for n in result.notes)

    def test_no_dominance_check_on_other_docs(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        _write(folder, "notes.md", _sectioned([("Huge", 400), ("Tiny", 10)]))
        result = v("tmp/foo", tmp_path)
        assert not any("dominant section" in n for n in result.notes)

    def test_heading_inside_code_fence_not_a_section(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        # One real 60-line section; a fenced 100-line block containing '## '
        # lines that must not be parsed as headings (else they'd dominate).
        fenced = "## Real\n" + "x\n" * 59 + "```\n" + "## fake heading\n" * 98 + "```\n"
        _write(folder, "CLAUDE.md", fenced)
        result = v("tmp/foo", tmp_path)
        assert not any("'## fake heading'" in n for n in result.notes)
        # The real section holds every non-heading line incl. the fence: dominant.
        assert any("'## Real'" in n for n in result.notes if "dominant section" in n)

    def test_diary_detector_notes_over_three_markers(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        diary = "".join(
            f"**2026-07-{10 + i}** did a thing.\n\nfiller\n" for i in range(4)
        )
        _write(folder, "CLAUDE.md", diary)
        result = v("tmp/foo", tmp_path)
        assert result.clean
        (note,) = [n for n in result.notes if "session diary" in n]
        assert "4 dated session-narrative markers" in note
        assert "log.md" in note

    def test_diary_detector_three_markers_silent(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        diary = "".join(f"**2026-07-{10 + i}** note\n" for i in range(3))
        _write(folder, "CLAUDE.md", diary)
        result = v("tmp/foo", tmp_path)
        assert not any("session diary" in n for n in result.notes)

    def test_diary_detector_only_on_claude_md(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        diary = "".join(f"**2026-07-{10 + i}** note\n" for i in range(6))
        _write(folder, "notes.md", diary)
        result = v("tmp/foo", tmp_path)
        assert not any("session diary" in n for n in result.notes)


class TestStubResolution:
    def test_unique_stub_resolves(self, tmp_path):
        make_task(tmp_path, "tmp/uniq")
        result = v("uniq", tmp_path)
        assert result.classification == "active"
        assert result.canonical == "tmp/uniq"

    def test_unique_dev_tasks_stub_resolves(self, git_root):
        make_task(git_root, "dev/tasks/durable")
        _commit_all(git_root)
        result = v("durable", git_root)
        assert result.classification == "active"
        assert result.canonical == "dev/tasks/durable"

    def test_ambiguous_stub_errors_with_candidates(self, tmp_path):
        make_task(tmp_path, "tmp/dup")
        make_task(tmp_path, "dev/tasks/dup")
        result = v("dup", tmp_path)
        assert result.classification == "invalid"
        assert len(result.errors) == 1
        assert "ambiguous stub" in result.errors[0]
        assert "tmp/dup" in result.errors[0]
        assert "dev/tasks/dup" in result.errors[0]

    def test_unmatched_stub_errors(self, tmp_path):
        result = v("nothing-here", tmp_path)
        assert result.classification == "invalid"
        assert any("matches no task folder" in e for e in result.errors)


class TestCanonicalization:
    def test_dot_segments_resolve(self, tmp_path):
        make_task(tmp_path, "tmp/foo")
        result = v("./tmp/../tmp/foo", tmp_path)
        assert result.classification == "active"
        assert result.canonical == "tmp/foo"

    def test_absolute_path_under_root_resolves(self, tmp_path):
        make_task(tmp_path, "tmp/foo")
        result = v(str(tmp_path / "tmp" / "foo"), tmp_path)
        assert result.canonical == "tmp/foo"

    def test_absolute_path_outside_root_errors(self, tmp_path):
        result = v("/definitely/elsewhere/foo", tmp_path)
        assert result.classification == "invalid"


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run task.py in a subprocess, hermetically.

    The re-exec guard env var is pre-set so the CLI never re-execs into a
    machine-provisioned venv; PYTHONPATH supplies skills_kit_lib from the
    working tree (pyyaml comes from the dev venv running pytest).
    """
    env = os.environ.copy()
    env[_REEXEC_GUARD_ENV] = "1"
    env["PYTHONPATH"] = str(_REPO_ROOT / "plugins" / "skills-kit")
    return subprocess.run(
        [sys.executable, str(_TASK_CLI), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


class TestCLI:
    def test_clean_task_exits_zero(self, tmp_path):
        make_task(tmp_path, "tmp/foo")
        proc = run_cli(["validate", "tmp/foo", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "active"
        assert proc.stderr.strip() == ""

    def test_root_defaults_to_cwd(self, tmp_path):
        make_task(tmp_path, "tmp/foo")
        proc = run_cli(["validate", "tmp/foo"], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "active"

    def test_error_exits_nonzero_findings_on_stderr(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        (folder / "plan.md").unlink()
        proc = run_cli(["validate", "tmp/foo", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode != 0
        assert proc.stdout.strip() == "invalid"
        assert "error: missing scaffolding file: plan.md" in proc.stderr

    def test_note_only_exits_zero(self, tmp_path):
        # Advisory tier: a doc over its healthy target (but under the
        # ceiling) prints a note: line yet does not count as a finding.
        folder = make_task(tmp_path, "tmp/foo")
        (folder / "CLAUDE.md").write_text(_filler(300), encoding="utf-8")
        proc = run_cli(["validate", "tmp/foo", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "active"
        assert "note: approaching budget: tmp/foo/CLAUDE.md" in proc.stderr
        assert "warning:" not in proc.stderr

    def test_update_surfaces_oversized_doc_warning(self, tmp_path):
        # The update verb re-validates and its exit code reflects findings
        # (fix-forward: the write persists) -- the size warning must surface
        # at write time.
        folder = make_task(tmp_path, "tmp/foo")
        (folder / "CLAUDE.md").write_text(_filler(401), encoding="utf-8")
        proc = run_cli(
            [
                "update",
                "tmp/foo",
                "--root",
                str(tmp_path),
            ],
            tmp_path,
        )
        assert proc.returncode != 0
        assert proc.stdout.strip() == "active"
        assert "warning: oversized document: tmp/foo/CLAUDE.md is 401 lines" in proc.stderr

    def test_warning_only_exits_nonzero(self, tmp_path):
        # Orphaned tmp reference: no errors, one warning -- still blocks.
        proc = run_cli(
            ["validate", "tmp/vanished", "--root", str(tmp_path)], tmp_path
        )
        assert proc.returncode != 0
        assert proc.stdout.strip() == "orphaned"
        assert "warning: orphaned tmp reference" in proc.stderr
        assert "error:" not in proc.stderr


class TestGitPathResolution:
    """A task folder reached through a symlink or Windows junction (the
    standard ``dev/tasks`` -> private tasks repo setup) used to misreport as
    ``"no-repo"``: git's own ``rev-parse``/``chdir`` resolve the symlink to
    the folder's REAL path, while an unresolved logical pathspec argument
    still names the link path, so git rejects it as outside the repository
    and the caught failure read as "not a git repo" (false). The fix
    resolves ``folder`` before it is ever handed to a git subprocess, for
    both the ``cwd`` argument and the pathspec argument, so the two always
    agree.

    A real symlink/junction is the only way to reproduce the mismatch (it
    requires the OS to resolve a reparse point during ``chdir`` while argv
    stays literal), and creating one needs a privilege this sandbox does not
    have (Windows ``SeCreateSymbolicLinkPrivilege``) -- see
    ``docs`` on ``migrate-claude-dir.sh`` for the same constraint elsewhere
    in this fleet. So this tests the path-resolution behavior directly:
    every git-invoking helper must pass ``folder.resolve()`` -- not the
    original ``folder`` -- as both ``cwd`` and the trailing pathspec."""

    @staticmethod
    def _capture_run(monkeypatch, module):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((list(cmd), kw))

            class _Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Result()

        monkeypatch.setattr(module.subprocess, "run", fake_run)
        return calls

    def test_git_status_porcelain_resolves_folder(self, monkeypatch, tmp_path):
        from task_system import validate as validate_mod

        calls = self._capture_run(monkeypatch, validate_mod)
        real = tmp_path / "real-target"
        real.mkdir()
        monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: real)
        alias = tmp_path / "alias-link"

        validate_mod._git_status_porcelain(alias)

        assert len(calls) == 1
        cmd, kw = calls[0]
        assert cmd[-1] == str(real)
        assert kw["cwd"] == real

    def test_git_ignores_path_resolves_folder(self, monkeypatch, tmp_path):
        from task_system import validate as validate_mod

        calls = self._capture_run(monkeypatch, validate_mod)
        real = tmp_path / "real-target"
        real.mkdir()
        monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: real)
        alias = tmp_path / "alias-link"

        validate_mod.git_ignores_path(alias)

        assert len(calls) == 1
        cmd, kw = calls[0]
        assert cmd[-1] == str(real)
        assert kw["cwd"] == real

    def test_git_tracks_nothing_in_resolves_folder(self, monkeypatch, tmp_path):
        from task_system import validate as validate_mod

        calls = self._capture_run(monkeypatch, validate_mod)
        real = tmp_path / "real-target"
        real.mkdir()
        monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: real)
        alias = tmp_path / "alias-link"

        validate_mod._git_tracks_nothing_in(alias)

        assert len(calls) == 1
        cmd, kw = calls[0]
        assert cmd[-1] == str(real)
        assert kw["cwd"] == real
