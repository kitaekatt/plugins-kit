"""End-to-end tests for the task-system read ops (Step 3).

Covers the spec section 8 discovery algorithm per scope (project / user /
skill / file), folder-vs-reference candidate union, dedupe by canonical path,
remote opacity, the absent-folder tri-state projections (orphaned /
archived), status/priority filters, malformed-block and unreadable-file
skip-with-note behavior, the spec 2.6 ``current`` pointer (read / write /
clear / stale handling), and the CLI contracts for ``list`` / ``show`` /
``current`` / ``status``.

All fixtures build under pytest tmp_path -- the real repo's tmp/, dev/, and
~/.claude are never touched (the pointer path is always injected). Host
detection is injected (local_host=LOCAL) for direct discover() calls; CLI
tests use a remote host name no real machine will have.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap_guard import _REEXEC_GUARD_ENV
from task_system.discovery import DiscoveryError, discover
from task_system.pointer import clear_current, read_current, write_current

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
OTHER = "definitely-not-this-host"


def make_task(
    project_root: Path,
    rel: str,
    *,
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
        (folder / "task.yaml").write_text(
            yaml.safe_dump({"task": block}), encoding="utf-8"
        )
    for fname in ("CLAUDE.md", "plan.md", "log.md"):
        (folder / fname).write_text("placeholder\n", encoding="utf-8")
    return folder


def fenced_task_list(refs: list[dict]) -> str:
    """A markdown fragment embedding a task_list typed-unit block."""
    return (
        "```yaml\n" + yaml.safe_dump({"task_list": {"refs": refs}}) + "```\n"
    )


def write_doc(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Doc\n\n" + body, encoding="utf-8")
    return path


def d(scope: str, root: Path, **kw):
    kw.setdefault("local_host", LOCAL)
    return discover(scope, root, **kw)


def ids(records) -> set[str]:
    return {r.id for r in records}


class TestProjectScope:
    def test_folder_only_tasks_discovered(self, tmp_path):
        make_task(tmp_path, "tmp/a")
        make_task(tmp_path, "dev/tasks/b")
        assert ids(d("project", tmp_path)) == {"tmp/a", "dev/tasks/b"}

    def test_ref_only_task_discovered(self, tmp_path):
        write_doc(
            tmp_path / "notes.md",
            fenced_task_list([{"path": "dev/tasks/shipped"}]),
        )
        records = d("project", tmp_path)
        assert ids(records) == {"dev/tasks/shipped"}
        assert records[0].classification == "archived"

    def test_folder_and_ref_task_appears_once(self, tmp_path):
        make_task(tmp_path, "tmp/both")
        write_doc(
            tmp_path / "notes.md", fenced_task_list([{"path": "tmp/both"}])
        )
        records = d("project", tmp_path)
        assert [r.id for r in records] == ["tmp/both"]
        assert records[0].classification == "active"

    def test_skill_md_docs_are_scanned_in_project_scope(self, tmp_path):
        write_doc(
            tmp_path / "skills" / "foo" / "SKILL.md",
            fenced_task_list([{"path": "dev/tasks/from-skill"}]),
        )
        assert "dev/tasks/from-skill" in ids(d("project", tmp_path))

    def test_record_projection_fields(self, tmp_path):
        make_task(tmp_path, "tmp/full", title="Full task", priority="P1")
        (rec,) = d("project", tmp_path)
        assert rec.id == "tmp/full"
        assert rec.classification == "active"
        assert rec.title == "Full task"
        assert rec.priority == "P1"
        assert rec.host is None

    def test_records_sorted_by_id(self, tmp_path):
        make_task(tmp_path, "tmp/zeta")
        make_task(tmp_path, "tmp/alpha")
        make_task(tmp_path, "dev/tasks/mid")
        assert [r.id for r in d("project", tmp_path)] == [
            "dev/tasks/mid",
            "tmp/alpha",
            "tmp/zeta",
        ]


class TestUserScope:
    def test_user_scope_crawls_user_root_only(self, tmp_path):
        user_root = tmp_path / "userhome"
        project_root = tmp_path / "proj"
        project_root.mkdir()
        make_task(project_root, "tmp/project-task")
        make_task(user_root, "tmp/user-task")
        write_doc(
            user_root / "notes.md",
            fenced_task_list([{"path": "dev/tasks/user-ref"}]),
        )
        records = d("user", project_root, user_root=user_root)
        assert ids(records) == {"tmp/user-task", "dev/tasks/user-ref"}

    def test_user_refs_canonicalize_against_user_root(self, tmp_path):
        user_root = tmp_path / "userhome"
        make_task(user_root, "tmp/here")
        write_doc(
            user_root / "notes.md", fenced_task_list([{"path": "tmp/here"}])
        )
        records = d("user", tmp_path / "proj", user_root=user_root)
        assert [r.id for r in records] == ["tmp/here"]
        assert records[0].classification == "active"


class TestSkillScope:
    @pytest.fixture
    def skill_project(self, tmp_path):
        write_doc(
            tmp_path / "skills" / "alpha" / "SKILL.md",
            fenced_task_list([{"path": "dev/tasks/a-ref"}]),
        )
        write_doc(
            tmp_path / "skills" / "alpha" / "references" / "more.md",
            fenced_task_list([{"path": "dev/tasks/a-ref2"}]),
        )
        write_doc(
            tmp_path / "skills" / "beta" / "SKILL.md",
            fenced_task_list([{"path": "dev/tasks/b-ref"}]),
        )
        return tmp_path

    def test_skill_name_picks_up_skill_md_and_references(self, skill_project):
        records = d("skill", skill_project, target="alpha")
        assert ids(records) == {"dev/tasks/a-ref", "dev/tasks/a-ref2"}

    def test_sibling_skill_docs_not_picked_up(self, skill_project):
        assert "dev/tasks/b-ref" not in ids(
            d("skill", skill_project, target="alpha")
        )

    def test_skill_target_as_directory_path(self, skill_project):
        records = d("skill", skill_project, target="skills/alpha")
        assert ids(records) == {"dev/tasks/a-ref", "dev/tasks/a-ref2"}

    def test_skill_target_as_skill_md_path(self, skill_project):
        records = d("skill", skill_project, target="skills/alpha/SKILL.md")
        assert "dev/tasks/a-ref" in ids(records)

    def test_folder_tasks_appear_in_skill_scope(self, skill_project):
        # Spec 8 step 2: candidates are the UNION of folder crawl and the
        # scoped reference scan; skill scope narrows the document set only
        # ("roots as project").
        make_task(skill_project, "tmp/materialized")
        assert "tmp/materialized" in ids(d("skill", skill_project, target="alpha"))

    def test_unknown_skill_errors(self, skill_project):
        with pytest.raises(DiscoveryError, match="not found"):
            d("skill", skill_project, target="gamma")

    def test_missing_target_errors(self, tmp_path):
        with pytest.raises(DiscoveryError, match="requires a target"):
            d("skill", tmp_path)


class TestFileScope:
    def test_only_the_named_document_is_scanned(self, tmp_path):
        write_doc(
            tmp_path / "one.md", fenced_task_list([{"path": "dev/tasks/one"}])
        )
        write_doc(
            tmp_path / "two.md", fenced_task_list([{"path": "dev/tasks/two"}])
        )
        records = d("file", tmp_path, target="one.md")
        assert ids(records) == {"dev/tasks/one"}

    def test_missing_file_errors(self, tmp_path):
        with pytest.raises(DiscoveryError, match="not a readable file"):
            d("file", tmp_path, target="nope.md")

    def test_unknown_scope_errors(self, tmp_path):
        with pytest.raises(DiscoveryError, match="unknown scope"):
            d("everything", tmp_path)


class TestDedupe:
    def test_folder_plus_two_docs_yields_one_record(self, tmp_path):
        make_task(tmp_path, "tmp/dup")
        write_doc(tmp_path / "a.md", fenced_task_list([{"path": "tmp/dup"}]))
        write_doc(
            tmp_path / "b.md", fenced_task_list([{"path": "./tmp/../tmp/dup"}])
        )
        records = d("project", tmp_path)
        assert [r.id for r in records] == ["tmp/dup"]

    def test_host_tag_from_any_ref_is_retained(self, tmp_path):
        write_doc(tmp_path / "a.md", fenced_task_list([{"path": "tmp/spike"}]))
        write_doc(
            tmp_path / "b.md",
            fenced_task_list([{"path": "tmp/spike", "host": OTHER}]),
        )
        (rec,) = d("project", tmp_path)
        assert rec.host == OTHER
        assert rec.classification == "remote"


class TestRemote:
    def test_remote_ref_is_opaque_task_yaml_never_read(self, tmp_path):
        # Garbage task.yaml in a present folder must not matter: the remote
        # short-circuit means nothing local is read.
        make_task(tmp_path, "tmp/spike", yaml_text="task: [unclosed\n")
        write_doc(
            tmp_path / "notes.md",
            fenced_task_list([{"path": "tmp/spike", "host": OTHER}]),
        )
        (rec,) = d("project", tmp_path)
        assert rec.classification == "remote"
        assert rec.host == OTHER
        assert rec.title is None
        assert rec.priority is None

    def test_remote_ref_with_absent_folder(self, tmp_path):
        write_doc(
            tmp_path / "notes.md",
            fenced_task_list([{"path": "tmp/ghost", "host": OTHER}]),
        )
        (rec,) = d("project", tmp_path)
        assert rec.classification == "remote"

    def test_matching_host_is_local(self, tmp_path):
        make_task(tmp_path, "tmp/here")
        write_doc(
            tmp_path / "notes.md",
            fenced_task_list([{"path": "tmp/here", "host": LOCAL}]),
        )
        (rec,) = d("project", tmp_path)
        assert rec.classification == "active"
        assert rec.title == "A task"


class TestTriStateProjection:
    def test_orphaned_tmp_ref_no_host_folder_absent(self, tmp_path):
        write_doc(
            tmp_path / "notes.md", fenced_task_list([{"path": "tmp/vanished"}])
        )
        (rec,) = d("project", tmp_path)
        assert rec.classification == "orphaned"
        assert rec.title is None

    def test_folderless_nontmp_ref_is_archived(self, tmp_path):
        write_doc(
            tmp_path / "notes.md",
            fenced_task_list([{"path": "dev/tasks/long-done"}]),
        )
        (rec,) = d("project", tmp_path)
        assert rec.classification == "archived"


class TestFilters:
    @pytest.fixture
    def mixed(self, tmp_path):
        make_task(tmp_path, "tmp/open", status="active", priority="P1")
        make_task(tmp_path, "tmp/done", status="closed", priority="P2")
        make_task(tmp_path, "tmp/stuck", status="blocked", priority="P1")
        return tmp_path

    def test_status_filter(self, mixed):
        assert ids(d("project", mixed, status="closed")) == {"tmp/done"}

    def test_priority_filter(self, mixed):
        assert ids(d("project", mixed, priority="P1")) == {
            "tmp/open",
            "tmp/stuck",
        }

    def test_combined_filters(self, mixed):
        assert ids(
            d("project", mixed, status="active", priority="P1")
        ) == {"tmp/open"}

    def test_filters_match_computed_classification(self, tmp_path):
        write_doc(
            tmp_path / "notes.md", fenced_task_list([{"path": "tmp/vanished"}])
        )
        assert ids(d("project", tmp_path, status="orphaned")) == {"tmp/vanished"}
        assert d("project", tmp_path, status="active") == []


class TestSkipWithNote:
    def test_malformed_task_list_block_skipped_with_note(self, tmp_path):
        make_task(tmp_path, "tmp/good")
        write_doc(
            tmp_path / "bad.md",
            "```yaml\ntask_list:\n  refs: not-a-list\n```\n",
        )
        notes: list[str] = []
        records = d("project", tmp_path, notes=notes)
        assert ids(records) == {"tmp/good"}
        assert any("malformed task_list block" in n for n in notes)

    def test_ref_entry_missing_path_skipped_with_note(self, tmp_path):
        write_doc(
            tmp_path / "bad.md",
            fenced_task_list([{"host": OTHER}]),
        )
        notes: list[str] = []
        assert d("project", tmp_path, notes=notes) == []
        assert any("malformed task_list block" in n for n in notes)

    def test_unparseable_yaml_block_mentioning_task_list_noted(self, tmp_path):
        write_doc(tmp_path / "bad.md", "```yaml\ntask_list: [unclosed\n```\n")
        notes: list[str] = []
        d("project", tmp_path, notes=notes)
        assert any("unparseable YAML block" in n for n in notes)

    def test_unresolvable_ref_path_skipped_with_note(self, tmp_path):
        write_doc(
            tmp_path / "doc.md",
            fenced_task_list(
                [{"path": "elsewhere/thing"}, {"path": "tmp/ok-but-gone"}]
            ),
        )
        notes: list[str] = []
        records = d("project", tmp_path, notes=notes)
        assert ids(records) == {"tmp/ok-but-gone"}
        assert any("unresolvable ref 'elsewhere/thing'" in n for n in notes)

    def test_non_task_list_yaml_blocks_ignored_silently(self, tmp_path):
        write_doc(tmp_path / "doc.md", "```yaml\nother_unit:\n  a: 1\n```\n")
        notes: list[str] = []
        assert d("project", tmp_path, notes=notes) == []
        assert notes == []

    def test_nested_task_yaml_skipped_with_note(self, tmp_path):
        folder = make_task(tmp_path, "tmp/parent")
        nested = folder / "sub"
        nested.mkdir()
        (nested / "task.yaml").write_text("task: {}\n", encoding="utf-8")
        notes: list[str] = []
        records = d("project", tmp_path, notes=notes)
        assert ids(records) == {"tmp/parent"}
        assert any("non-canonical location" in n for n in notes)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
    def test_unreadable_document_skipped_with_note(self, tmp_path):
        make_task(tmp_path, "tmp/good")
        locked = write_doc(
            tmp_path / "locked.md", fenced_task_list([{"path": "tmp/good"}])
        )
        locked.chmod(0o000)
        try:
            notes: list[str] = []
            records = d("project", tmp_path, notes=notes)
            assert ids(records) == {"tmp/good"}
            assert any("unreadable document" in n for n in notes)
        finally:
            locked.chmod(0o644)


class TestPointer:
    def test_read_absent_is_none(self, tmp_path):
        assert read_current(tmp_path / "current") is None

    def test_write_read_roundtrip_creates_parents(self, tmp_path):
        # The pointer stores the ABSOLUTE folder path (spec 2.6), resolved
        # at write time -- the pointer file is user-global, so a relative
        # path would be ambiguous across projects.
        pointer = tmp_path / "deep" / "dir" / "current"
        folder = tmp_path / "tmp" / "foo"
        write_current(pointer, folder)
        stored = str(folder.resolve())
        assert read_current(pointer) == stored
        assert pointer.read_text(encoding="utf-8") == stored + "\n"

    def test_clear_makes_none(self, tmp_path):
        pointer = tmp_path / "current"
        write_current(pointer, tmp_path / "tmp" / "foo")
        clear_current(pointer)
        assert read_current(pointer) is None
        assert pointer.exists()  # blanked, not removed

    def test_clear_absent_is_a_noop(self, tmp_path):
        pointer = tmp_path / "current"
        clear_current(pointer)
        assert not pointer.exists()

    def test_blank_file_is_none(self, tmp_path):
        pointer = tmp_path / "current"
        pointer.write_text("   \n", encoding="utf-8")
        assert read_current(pointer) is None


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run task.py in a subprocess, hermetically (same pattern as Steps 1-2).

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


class TestListCLI:
    def test_empty_scope_exits_zero_no_output(self, tmp_path):
        proc = run_cli(["list", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""

    def test_line_shape(self, tmp_path):
        make_task(tmp_path, "tmp/foo", title="Fix the run", priority="P2")
        proc = run_cli(["list", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["tmp/foo  active  P2  Fix the run"]

    def test_absent_fields_render_as_dash(self, tmp_path):
        make_task(tmp_path, "tmp/bare")  # no priority
        proc = run_cli(["list", "--root", str(tmp_path)], tmp_path)
        assert proc.stdout.splitlines() == ["tmp/bare  active  -  A task"]

    def test_remote_line_shape(self, tmp_path):
        write_doc(
            tmp_path / "notes.md",
            fenced_task_list([{"path": "tmp/spike", "host": OTHER}]),
        )
        proc = run_cli(["list", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == [
            f"tmp/spike @{OTHER}  remote  -  -"
        ]

    def test_status_filter_flag(self, tmp_path):
        make_task(tmp_path, "tmp/open", status="active")
        make_task(tmp_path, "tmp/done", status="closed")
        proc = run_cli(
            ["list", "--status", "closed", "--root", str(tmp_path)], tmp_path
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["tmp/done  closed  -  A task"]

    def test_priority_filter_flag(self, tmp_path):
        make_task(tmp_path, "tmp/p1", priority="P1")
        make_task(tmp_path, "tmp/p2", priority="P2")
        proc = run_cli(
            ["list", "--priority", "P1", "--root", str(tmp_path)], tmp_path
        )
        assert proc.stdout.splitlines() == ["tmp/p1  active  P1  A task"]

    def test_scope_file_via_flags(self, tmp_path):
        write_doc(
            tmp_path / "one.md", fenced_task_list([{"path": "dev/tasks/one"}])
        )
        write_doc(
            tmp_path / "two.md", fenced_task_list([{"path": "dev/tasks/two"}])
        )
        proc = run_cli(
            [
                "list",
                "--scope",
                "file",
                "--target",
                "one.md",
                "--root",
                str(tmp_path),
            ],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["dev/tasks/one  archived  -  -"]

    def test_notes_go_to_stderr_exit_zero(self, tmp_path):
        write_doc(
            tmp_path / "bad.md",
            "```yaml\ntask_list:\n  refs: not-a-list\n```\n",
        )
        proc = run_cli(["list", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert "note:" in proc.stderr
        assert "malformed task_list block" in proc.stderr

    def test_bad_target_exits_nonzero(self, tmp_path):
        proc = run_cli(
            [
                "list",
                "--scope",
                "skill",
                "--target",
                "no-such-skill",
                "--root",
                str(tmp_path),
            ],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "error:" in proc.stderr


class TestShowCLI:
    def test_valid_task_prints_selected_fields(self, tmp_path):
        make_task(
            tmp_path,
            "tmp/foo",
            title="Fix the run",
            priority="P2",
            description="Re-terminate and re-test.",
            depends_on=["dev/tasks/prep"],
            agent_hint="backend-developer",
            skills_to_invoke=["home-domain"],
        )
        proc = run_cli(["show", "tmp/foo", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        assert "id: tmp/foo" in lines
        assert "type: hand-off" in lines
        assert "title: Fix the run" in lines
        assert "status: active" in lines
        assert "priority: P2" in lines
        assert "description: Re-terminate and re-test." in lines
        assert "depends_on: dev/tasks/prep" in lines
        assert "blocked_by: -" in lines
        assert "agent_hint: backend-developer" in lines
        assert "skills_to_invoke: home-domain" in lines

    def test_stub_ref_resolves(self, tmp_path):
        make_task(tmp_path, "tmp/uniq")
        proc = run_cli(["show", "uniq", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert "id: tmp/uniq" in proc.stdout.splitlines()

    def test_unresolvable_ref_exits_nonzero(self, tmp_path):
        proc = run_cli(["show", "docs/x", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode != 0
        assert proc.stdout == ""
        assert "error:" in proc.stderr

    def test_folder_not_readable_locally_exits_nonzero(self, tmp_path):
        # The not-locally-readable case covers archived (non-tmp absent),
        # orphaned, and remote refs alike: there is no folder to render.
        proc = run_cli(
            ["show", "dev/tasks/gone", "--root", str(tmp_path)], tmp_path
        )
        assert proc.returncode != 0
        assert "no task folder readable locally" in proc.stderr

    def test_unparseable_task_yaml_exits_nonzero(self, tmp_path):
        make_task(tmp_path, "tmp/broken", yaml_text="task: [unclosed\n")
        proc = run_cli(["show", "tmp/broken", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode != 0
        assert "task.yaml" in proc.stderr


class TestCurrentCLI:
    def test_no_pointer_reports_none(self, tmp_path):
        pointer = tmp_path / "pointer" / "current"
        proc = run_cli(
            ["current", "--root", str(tmp_path), "--pointer", str(pointer)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "none"

    def test_live_pointer_prints_path_and_summary(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo", title="Fix the run")
        pointer = tmp_path / "pointer" / "current"
        write_current(pointer, folder)
        proc = run_cli(
            ["current", "--root", str(tmp_path), "--pointer", str(pointer)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        # The project-relative id is derived from the stored absolute path.
        assert proc.stdout.strip() == "tmp/foo  active  Fix the run"
        # A live pointer is left alone.
        assert read_current(pointer) == str(folder.resolve())

    def test_stale_pointer_cleared_and_reports_none(self, tmp_path):
        pointer = tmp_path / "pointer" / "current"
        write_current(pointer, tmp_path / "tmp" / "gone")  # absolute, missing
        proc = run_cli(
            ["current", "--root", str(tmp_path), "--pointer", str(pointer)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "none"
        assert read_current(pointer) is None  # cleared, per spec 2.6

    def test_non_absolute_pointer_content_treated_as_stale(self, tmp_path):
        # Stored content must be an absolute task-folder path; anything else
        # (a relative or non-task path written by hand) is stale.
        pointer = tmp_path / "pointer" / "current"
        pointer.parent.mkdir(parents=True)
        pointer.write_text("elsewhere/not-a-task-path\n", encoding="utf-8")
        proc = run_cli(
            ["current", "--root", str(tmp_path), "--pointer", str(pointer)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "none"
        assert read_current(pointer) is None

    def test_absolute_non_task_pointer_content_treated_as_stale(self, tmp_path):
        # An absolute path that exists but is not a tmp/<stub> or
        # dev/tasks/<stub> shape cannot name a task -- stale.
        not_a_task = tmp_path / "docs" / "thing"
        not_a_task.mkdir(parents=True)
        pointer = tmp_path / "pointer" / "current"
        write_current(pointer, not_a_task)
        proc = run_cli(
            ["current", "--root", str(tmp_path), "--pointer", str(pointer)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "none"
        assert read_current(pointer) is None


class TestStatusCLI:
    def test_substrate_for_a_valid_task(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo", title="Fix the run")
        proc = run_cli(["status", "tmp/foo", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout
        lines = out.splitlines()
        assert "classification: active" in lines
        assert "id: tmp/foo" in lines
        assert "findings: none" in lines
        assert "task.yaml:" in lines
        assert "  title: Fix the run" in lines
        assert "  status: active" in lines
        assert "documents:" in lines
        for fname in ("CLAUDE.md", "plan.md", "log.md"):
            assert f"  {fname}: {folder / fname}" in lines
        # The inference split is disclosed: summarization is Step 6's job.
        assert "dispatched by the skill layer (Step 6)" in out
        assert "substrate" in out

    def test_substrate_includes_findings(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        (folder / "plan.md").unlink()
        proc = run_cli(["status", "tmp/foo", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert "classification: invalid" in proc.stdout
        assert "  error: missing scaffolding file: plan.md" in proc.stdout
        assert "  plan.md: " in proc.stdout
        assert "(missing)" in proc.stdout

    def test_substrate_for_orphaned_ref_has_no_material(self, tmp_path):
        proc = run_cli(
            ["status", "tmp/vanished", "--root", str(tmp_path)], tmp_path
        )
        assert proc.returncode == 0, proc.stderr
        assert "classification: orphaned" in proc.stdout
        assert "  warning: orphaned tmp reference" in proc.stdout
        assert "task.yaml:" not in proc.stdout
        assert "documents:" not in proc.stdout

    def test_unresolvable_ref_exits_nonzero(self, tmp_path):
        proc = run_cli(["status", "docs/x", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode != 0
        assert "classification: invalid" in proc.stdout
        assert "id: -" in proc.stdout
