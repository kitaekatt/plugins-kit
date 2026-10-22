"""Real git repositories for the session-publish tests: a bare remote, clones of it, and plain helpers.

No network: the "remote" is a bare repository on the local disk. "Offline" is a remote URL that does not exist.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

BRANCH = "main"


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def isolate_git_environment(monkeypatch, tmp_path: Path) -> None:
    """No user or system git config (hooks, signing, identity) leaks into the repositories under test."""
    empty = tmp_path / "gitconfig-empty"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


def _configure(clone: Path, name: str) -> None:
    git(clone, "config", "user.name", name)
    git(clone, "config", "user.email", f"{name}@example.invalid")
    git(clone, "config", "commit.gpgsign", "false")


def make_remote(tmp_path: Path, name: str = "remote.git") -> Path:
    """A bare remote whose ``main`` holds one seed commit (``README.md``)."""
    remote = tmp_path / name
    remote.mkdir()
    git(remote, "init", "-q", "--bare", f"--initial-branch={BRANCH}")
    seed = tmp_path / f"{name}-seed"
    seed.mkdir()
    git(seed, "init", "-q", f"--initial-branch={BRANCH}")
    _configure(seed, "seed")
    (seed / "README.md").write_text("tasks repo\n", encoding="utf-8")
    git(seed, "add", "README.md")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "origin", BRANCH)
    return remote


def clone(tmp_path: Path, remote: Path, name: str) -> Path:
    """A clone tracking ``origin/main`` with its own identity."""
    work = tmp_path / name
    git(tmp_path, "clone", "-q", str(remote), str(work))
    _configure(work, name)
    return work


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def commit_all(root: Path, rel: str, text: str, message: str) -> str:
    """Write ``rel``, commit exactly it, return the commit sha."""
    write(root, rel, text)
    git(root, "add", "--", rel)
    git(root, "commit", "-q", "-m", message, "--", rel)
    return git(root, "rev-parse", "HEAD").stdout.strip()


def push(root: Path) -> None:
    git(root, "push", "-q", "origin", f"HEAD:{BRANCH}")


def session_path(project: str, host: str) -> str:
    """The repository-relative path of a host's session file under ``<project>/``."""
    return f"{project}/.sessions/{host}.yaml"


def session_text(host: str, revision: int = 1) -> str:
    return f"_schema_version: 1\nhost: {host}\nsessions: []\n# revision {revision}\n"


def remote_files(remote: Path) -> set[str]:
    """Every path in the remote's ``main`` tree."""
    listed = git(remote, "ls-tree", "-r", "--name-only", BRANCH).stdout
    return {line for line in listed.splitlines() if line}


def remote_show(remote: Path, rel: str) -> str:
    return git(remote, "show", f"{BRANCH}:{rel}").stdout


def break_remote(clone_root: Path, tmp_path: Path) -> str:
    """Point ``origin`` at a path that does not exist (the offline case); returns the old URL."""
    old = git(clone_root, "remote", "get-url", "origin").stdout.strip()
    git(clone_root, "remote", "set-url", "origin", str(tmp_path / "no-such-remote.git"))
    return old
