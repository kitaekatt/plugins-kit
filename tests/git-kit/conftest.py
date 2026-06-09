"""Fixtures for git-kit tests."""

import importlib.util
import os
import subprocess
import sys

import pytest

PLUGIN_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "git-kit")
)
SCRIPTS_PATH = os.path.join(PLUGIN_ROOT, "scripts")

# prepare_review.py calls bootstrap_guard.reexec_under_plugin_venv() at import
# time. On a machine where git-kit's provisioned venv exists, that would
# os.execv the pytest process into the plugin venv. Setting the loop-guard
# env flag up front makes the re-exec a no-op under tests.
os.environ.setdefault("_BOOTSTRAP_GUARD_VENV_REEXEC", "1")

# Appended (not inserted at index 0) so p4-kit's identically-named scripts
# keep winning their own suite's bare `import prepare_review`. git-kit's
# modules are loaded under unique names via importlib below.
if SCRIPTS_PATH not in sys.path:
    sys.path.append(SCRIPTS_PATH)


def _load_script(module_name: str, filename: str):
    """Load a git-kit script under a unique module name.

    p4-kit's test suite imports its own `prepare_review` (same filename)
    through sys.path; loading git-kit's copy under a distinct name avoids
    the sys.modules collision when the full suite runs in one process.
    """
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(SCRIPTS_PATH, filename)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_script("git_kit_prepare_review", "prepare_review.py")
_load_script("git_kit_bootstrap", "bootstrap.py")


@pytest.fixture
def plugin_root():
    """Path to the git-kit plugin."""
    return PLUGIN_ROOT


class GitRepo:
    """A real throwaway git repository plus a command helper."""

    def __init__(self, path):
        self.path = path

    def git(self, *args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=self.path,
            check=check,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )

    def commit_file(self, name, content, message=None):
        (self.path / name).write_text(content, encoding="utf-8")
        self.git("add", "--", name)
        self.git("commit", "-qm", message or f"edit {name}")


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """Init a real git repo (isolated from user/system git config) and chdir in.

    Isolation matters on populated dev boxes: a user-level core.quotepath or
    diff.* setting would change git's output format and flake these tests.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    monkeypatch.chdir(repo_path)
    repo = GitRepo(repo_path)
    repo.git("init", "-q", "-b", "main")
    repo.git("config", "user.email", "test@example.com")
    repo.git("config", "user.name", "Test User")
    repo.git("config", "commit.gpgsign", "false")
    return repo
