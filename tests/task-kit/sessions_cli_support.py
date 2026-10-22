"""A rig for the working-session CLI tests: the real ``task.py`` run as a subprocess under a stand-in Claude process.

Layout under ``base``:

    home/.claude/task.local.yaml     sessions: {host: <HOST>}; HOME of every subprocess
    data/plugins-kit/task-kit/.venv  a venv that sees the test interpreter's packages, so ``datadir`` resolves
    remote.git                       a bare remote holding the tasks repository
    project/dev/tasks                a clone of the remote (the durable task root, a git work tree with an upstream)
    project/tmp                      the machine-local task root

:class:`Harness` stands in for the Claude Code process. It is a Python process whose id is ``CLAUDE_PID``; each
command runs as ``bash -c "<snapshot comment>; <command>"`` beneath it (the shape of a Bash tool command), so the
CLI's identity resolution and ancestry check run for real. Several commands run under ONE harness, so a ``work``
and a later ``release`` share a process; :meth:`Harness.clear` changes the session id the way ``/clear`` does.
Running this file as a script is the harness.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

from fake_claude import tool_shell_script
from git_remote_support import BRANCH, commit_all, git, make_remote, push

HOST = "testhost"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "plugins" / "task-kit" / "skills" / "task" / "scripts"
TASK_CLI = SCRIPTS / "task.py"
SKILLS_KIT = REPO_ROOT / "plugins" / "skills-kit"


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


class Harness:
    """One stand-in Claude Code process that runs commands in order, in its own session id."""

    def __init__(self, session_id: str, env: dict, cwd: Path) -> None:
        environment = dict(env)
        for name in ("CLAUDE_PID", "CLAUDE_CODE_SESSION_ID"):
            environment.pop(name, None)
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), session_id], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True, env=environment, cwd=cwd)
        self.pid = int(self.process.stdout.readline())
        self.session_id = session_id

    def run(self, command: str) -> Result:
        self.process.stdin.write(json.dumps({"command": command}) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("the stand-in Claude process exited")
        data = json.loads(line)
        return Result(data["returncode"], data["stdout"], data["stderr"])

    def clear(self, new_session_id: str) -> None:
        """What ``/clear`` does: the session id changes, the process does not."""
        self.process.stdin.write(json.dumps({"session": new_session_id}) + "\n")
        self.process.stdin.flush()
        self.process.stdout.readline()
        self.session_id = new_session_id

    def close(self) -> None:
        """Exit the harness (a registration it made stays, and reads as not running)."""
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def _harness_main(session_id: str) -> int:
    os.environ["CLAUDE_PID"] = str(os.getpid())
    os.environ["CLAUDE_CODE_SESSION_ID"] = session_id
    print(os.getpid(), flush=True)
    for raw in sys.stdin:
        message = json.loads(raw)
        if "session" in message:
            os.environ["CLAUDE_CODE_SESSION_ID"] = message["session"]
            print("{}", flush=True)
            continue
        done = subprocess.run(["bash", "-c", tool_shell_script(message["command"])], capture_output=True, text=True)
        print(json.dumps({"returncode": done.returncode, "stdout": done.stdout, "stderr": done.stderr}), flush=True)
    return 0


class Rig:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.set_config(f"sessions:\n  host: {HOST}\n")
        self.data_root = base / "data"
        self.data_dir = self.data_root / "plugins-kit" / "task-kit"
        self.python = self._provisioned_venv()
        self.empty_gitconfig = base / "gitconfig-empty"
        self.empty_gitconfig.write_text("", encoding="utf-8")
        self.remote = make_remote(base)
        self.project = base / "project"
        (self.project / "dev").mkdir(parents=True)
        (self.project / "tmp").mkdir()
        git(self.project / "dev", "clone", "-q", str(self.remote), "tasks")
        self.tasks = self.project / "dev" / "tasks"
        git(self.tasks, "config", "user.name", "rig")
        git(self.tasks, "config", "user.email", "rig@example.invalid")
        git(self.tasks, "config", "commit.gpgsign", "false")
        self.harnesses: list[Harness] = []

    # -- environment ---------------------------------------------------------------------------------------

    def set_config(self, text: str) -> None:
        (self.home / ".claude" / "task.local.yaml").write_text(text, encoding="utf-8")

    def _provisioned_venv(self) -> Path:
        venv = self.data_dir / ".venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
        purelib = Path(subprocess.run(
            [str(self._venv_python(venv)), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            check=True, capture_output=True, text=True).stdout.strip())
        (purelib / "test_interpreter.pth").write_text(sysconfig.get_paths()["purelib"] + "\n", encoding="utf-8")
        return self._venv_python(venv)

    @staticmethod
    def _venv_python(venv: Path) -> Path:
        return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def env(self, extra: dict | None = None) -> dict:
        environment = dict(os.environ)
        for name in ("CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "TASK_KIT_AGENT_DEPTH", "TASK_KIT_RUN_TOKEN",
                     "DEVROOT"):
            environment.pop(name, None)
        environment.update({
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "CLAUDE_BOOTSTRAP_DATA_ROOT": str(self.data_root),
            "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1",
            "PYTHONPATH": str(SKILLS_KIT),
            "GIT_CONFIG_GLOBAL": str(self.empty_gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            environment.pop(name, None)
        environment.update(extra or {})
        return environment

    # -- tasks ---------------------------------------------------------------------------------------------

    def command(self, args: list[str]) -> str:
        return shlex.join([str(self.python), str(TASK_CLI), *args, "--root", str(self.project)])

    def plain(self, args: list[str], *, extra_env: dict | None = None) -> Result:
        """Run the CLI with no harness around it (not a Claude Code subprocess)."""
        done = subprocess.run([str(self.python), str(TASK_CLI), *args, "--root", str(self.project)],
                              capture_output=True, text=True, env=self.env(extra_env), cwd=self.project)
        return Result(done.returncode, done.stdout, done.stderr)

    def harness(self, session_id: str, *, extra_env: dict | None = None) -> Harness:
        self.transcript(session_id)
        started = Harness(session_id, self.env(extra_env), self.project)
        self.harnesses.append(started)
        return started

    def cli(self, harness: Harness, args: list[str]) -> Result:
        return harness.run(self.command(args))

    def add_task(self, stub: str, location: str = "dev/tasks", *, commit: bool = True, push_it: bool = True) -> Path:
        """A valid active task, created by the real ``init``, committed (and pushed) when it is under dev/tasks."""
        done = self.plain(["init", stub, "--dest", location])
        assert done.returncode == 0, done.stderr
        done = self.plain(["update", f"{location}/{stub}", "--description", f"The {stub} task.",
                           "--summary", f"The {stub} task."])
        assert done.returncode in (0, 1), done.stderr
        folder = self.project / location / stub
        if location == "dev/tasks" and commit:
            git(self.tasks, "add", "-A", "--", stub)
            git(self.tasks, "commit", "-q", "-m", f"add {stub}", "--", stub)
            if push_it:
                push(self.tasks)
        return folder

    def session_file(self, host: str = HOST) -> Path:
        return self.tasks / ".sessions" / f"{host}.yaml"

    def records(self, host: str = HOST) -> list[dict]:
        import yaml

        path = self.session_file(host)
        if not path.exists():
            return []
        return yaml.safe_load(path.read_text(encoding="utf-8"))["sessions"]

    def local_files(self) -> list[str]:
        """Every file under ``<data dir>/sessions/`` that is not a lock file (there must be none: git is the truth)."""
        directory = self.data_dir / "sessions"
        if not directory.is_dir():
            return []
        return sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*")
                      if p.is_file() and not (p.parent.name == "locks" and p.suffix == ".lock"))

    def remote_text(self, rel: str) -> str:
        return git(self.remote, "show", f"{BRANCH}:{rel}").stdout

    def remote_has(self, rel: str) -> bool:
        listed = git(self.remote, "ls-tree", "-r", "--name-only", BRANCH).stdout.splitlines()
        return rel in listed

    def go_offline(self) -> str:
        """Point the tasks clone's origin at a path that does not exist; returns the old URL."""
        old = git(self.tasks, "remote", "get-url", "origin").stdout.strip()
        git(self.tasks, "remote", "set-url", "origin", str(self.base / "no-such-remote.git"))
        return old

    def go_online(self, url: str) -> None:
        git(self.tasks, "remote", "set-url", "origin", url)

    def transcript(self, session_id: str) -> Path:
        """A transcript file for ``session_id`` under the rig's HOME, as Claude Code would write it."""
        path = self.home / ".claude" / "projects" / "-rig-project" / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        return path

    def close(self) -> None:
        for started in self.harnesses:
            started.close()


def tree_digest(*roots: Path) -> dict[str, bytes]:
    """Every file under the roots (relative path -> bytes), skipping .git directories and bytecode caches."""
    found: dict[str, bytes] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts:
                found[f"{root.name}/{path.relative_to(root).as_posix()}"] = path.read_bytes()
    return found


if __name__ == "__main__":
    raise SystemExit(_harness_main(sys.argv[1]))
