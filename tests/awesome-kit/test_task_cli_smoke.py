"""End-to-end CLI smoke test for the task skill (Step 6 -- skill wiring).

One sandboxed lifecycle driven ONLY through the CLI exactly as the task
SKILL.md documents it (subprocess invocations of scripts/task.py):

    init (tmp) -> validate -> update (field edits) -> work (pointer set,
    Skill lines emitted) -> list / show / current / status substrate ->
    close -> reopen -> move to dev/tasks inside a git repo fixture
    (task_list reference rewrite across a doc) -> commit -> archive
    (final state committed; folder deleted; version control is the record)

plus a delete path for a second tmp task. Everything runs under pytest
tmp_path with an injected pointer (--pointer) -- the real repo's tmp/,
dev/, and ~/.claude are never touched.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from bootstrap_guard import _REEXEC_GUARD_ENV

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

STUB = "reterminate-office-closet"
TITLE = "Reterminate office closet"
SCAFFOLD_FILES = ("CLAUDE.md", "plan.md", "log.md", "task.yaml")

DOC_WITH_REF = f"""# Notes

```yaml
task_list:
  refs:
    - {{ path: tmp/{STUB} }}
```
"""


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run task.py in a subprocess, hermetically (same pattern as the
    Step 2-5 suites: re-exec guard armed, skills_kit_lib on PYTHONPATH)."""
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


@pytest.fixture
def ptr(tmp_path) -> Path:
    """An injected pointer path -- never the user-global default."""
    return tmp_path / "pointer" / "current"


@pytest.fixture
def git_root(tmp_path) -> Path:
    """A temp project root that is a real git repo (the move/archive legs
    need git: the uncommitted guard and 'git is the record')."""
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
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "commit.gpgsign", "false"],
        check=True,
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


class TestCliLifecycle:
    def test_full_lifecycle(self, git_root, ptr):
        root = git_root
        rootflag = ["--root", str(root)]
        ptrflag = ["--pointer", str(ptr)]

        # --- init (tmp): prints the absolute folder path; scaffolds ------
        res = run_cli(["init", STUB, *rootflag], cwd=root)
        assert res.returncode == 0, res.stderr
        folder = Path(res.stdout.strip())
        assert folder == (root / "tmp" / STUB).absolute()
        for fname in SCAFFOLD_FILES:
            assert (folder / fname).is_file(), fname

        # --- validate: clean active task, exit 0 -------------------------
        res = run_cli(["validate", f"tmp/{STUB}", *rootflag], cwd=root)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "active"

        # --- update (field edits): persists + re-validates clean ---------
        res = run_cli(
            [
                "update",
                f"tmp/{STUB}",
                "--priority",
                "P2",
                "--description",
                "Data path on the closet run is dead.",
                "--skill-to-invoke",
                "home-domain",
                "--agent-hint",
                "backend-developer",
                *rootflag,
                *ptrflag,
            ],
            cwd=root,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "active"

        # --- work: pointer set; one init block emitted -------------------
        res = run_cli(["work", f"tmp/{STUB}", *rootflag, *ptrflag], cwd=root)
        assert res.returncode == 0, res.stderr
        assert "== task init" in res.stdout
        assert 'Skill(skill: "awesome-kit:orchestrate")' in res.stdout
        assert 'Skill(skill: "home-domain")' in res.stdout
        assert "agent_hint: backend-developer" in res.stdout
        assert "do not implement inline in the main context" in res.stdout
        assert ptr.read_text(encoding="utf-8").strip() == str(folder.resolve())

        # --- current: id + classification + title ------------------------
        res = run_cli(["current", *ptrflag], cwd=root)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == f"tmp/{STUB}  active  {TITLE}"

        # --- list: one parseable line, the documented projection ---------
        res = run_cli(["list", *rootflag], cwd=root)
        assert res.returncode == 0, res.stderr
        assert f"tmp/{STUB}  active  P2  {TITLE}" in res.stdout.splitlines()

        # --- show: selected task.yaml fields, no inference ---------------
        res = run_cli(["show", f"tmp/{STUB}", *rootflag], cwd=root)
        assert res.returncode == 0, res.stderr
        assert f"id: tmp/{STUB}" in res.stdout
        assert "priority: P2" in res.stdout
        assert "agent_hint: backend-developer" in res.stdout

        # --- status: the script-side substrate only ----------------------
        res = run_cli(["status", f"tmp/{STUB}", *rootflag], cwd=root)
        assert res.returncode == 0, res.stderr
        assert "classification: active" in res.stdout
        assert "findings: none" in res.stdout
        assert "task.yaml:" in res.stdout
        assert "documents:" in res.stdout
        assert "inference verb" in res.stdout  # the skill-layer reminder

        # --- close: folder kept, pointer cleared -------------------------
        res = run_cli(["close", f"tmp/{STUB}", *rootflag, *ptrflag], cwd=root)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == f"closed: tmp/{STUB}"
        assert folder.is_dir()
        res = run_cli(["current", *ptrflag], cwd=root)
        assert res.stdout.strip() == "none"

        # --- reopen: back to active --------------------------------------
        res = run_cli(["reopen", f"tmp/{STUB}", *rootflag, *ptrflag], cwd=root)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "active"

        # --- move to dev/tasks: folder relocated, task_list ref rewritten
        # Under a task root: move's rewrite scans the whole tree, but list's
        # reference scan is scoped to tmp/ + dev/tasks/ (spec 8 step 1), so
        # the surviving-ref assertion below needs the doc inside one.
        doc = root / "tmp" / "notes.md"
        doc.parent.mkdir(exist_ok=True)
        doc.write_text(DOC_WITH_REF, encoding="utf-8")
        res = run_cli(
            ["move", f"tmp/{STUB}", "dev/tasks", *rootflag, *ptrflag], cwd=root
        )
        assert res.returncode == 0, res.stderr
        assert f"moved: tmp/{STUB} -> dev/tasks/{STUB}" in res.stdout
        assert "rewrote 1 document(s)" in res.stdout
        assert not folder.exists()
        new_folder = root / "dev" / "tasks" / STUB
        assert new_folder.is_dir()
        doc_text = doc.read_text(encoding="utf-8")
        assert f"dev/tasks/{STUB}" in doc_text
        assert f"tmp/{STUB}" not in doc_text

        # Uncommitted dev/tasks folder: validate warns (and gates work).
        res = run_cli(["validate", f"dev/tasks/{STUB}", *rootflag], cwd=root)
        assert res.returncode == 1
        assert res.stdout.strip() == "active"
        assert "uncommitted dev/tasks folder" in res.stderr

        # --- commit, then archive: folder deleted, git is the record -----
        _commit_all(root)
        res = run_cli(
            ["archive", f"dev/tasks/{STUB}", *rootflag, *ptrflag], cwd=root
        )
        assert res.returncode == 0, res.stderr
        assert (
            res.stdout.strip()
            == f"archived: dev/tasks/{STUB} (final state committed; folder "
            "deleted; version control is the record)"
        )
        assert not new_folder.exists()

        # The surviving reference now reads as archived (folderless non-tmp).
        res = run_cli(["list", *rootflag], cwd=root)
        assert res.returncode == 0, res.stderr
        assert f"dev/tasks/{STUB}  archived  -  -" in res.stdout.splitlines()

    def test_delete_second_tmp_task(self, tmp_path, ptr):
        root = tmp_path
        rootflag = ["--root", str(root)]
        ptrflag = ["--pointer", str(ptr)]

        res = run_cli(["init", "scratch-spike", *rootflag], cwd=root)
        assert res.returncode == 0, res.stderr
        folder = Path(res.stdout.strip())
        assert folder.is_dir()

        # delete: archive semantics, then the folder is removed even in tmp.
        res = run_cli(
            ["delete", "tmp/scratch-spike", *rootflag, *ptrflag], cwd=root
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "deleted: tmp/scratch-spike"
        assert not folder.exists()
