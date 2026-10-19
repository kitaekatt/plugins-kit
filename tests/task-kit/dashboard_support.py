"""Shared fixtures for the task-kit dashboard tests.

Everything builds under pytest ``tmp_path``: the real repo's ``dev/tasks``,
``tmp`` and ``~/.claude`` are never read or written.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "task-kit"
SCRIPTS_DIR = PLUGIN_ROOT / "skills" / "task" / "scripts"
ENTRYPOINT = PLUGIN_ROOT / "bin" / "task_kit_entrypoint.py"
TASK_CLI = SCRIPTS_DIR / "task.py"
DASHBOARD_DIR = SCRIPTS_DIR / "task_system" / "dashboard"


def items_block(items: list[dict]) -> str:
    return "```yaml\n" + yaml.safe_dump({"task_items": {"items": items}}, sort_keys=False) + "```\n"


def make_task(
    project_root: Path,
    location: str,
    stub: str,
    *,
    title: str = "A task",
    status: str = "active",
    priority: str | None = "P2",
    items: list[dict] | None = None,
    sections: dict[str, str] | None = None,
    log: str = "placeholder\n",
    summary: str | None = None,
    blocked_by: list[str] | None = None,
) -> Path:
    """A hand-off task folder; ``sections`` maps item id -> its plan.md body."""
    folder = project_root / location / stub
    folder.mkdir(parents=True)
    block: dict = {
        "_schema_version": "1",
        "type": "hand-off",
        "title": title,
        "status": status,
    }
    if priority:
        block["priority"] = priority
    if summary:
        block["summary"] = summary
    if blocked_by:
        block["blocked_by"] = blocked_by
    (folder / "task.yaml").write_text(yaml.safe_dump({"task": block}, sort_keys=False), encoding="utf-8")
    plan = "# Plan\n\n## Forward overview\n\n" + items_block(items or [])
    for item_id, body in (sections or {}).items():
        plan += f"\n### {item_id} -- section\n\n{body}\n"
    (folder / "plan.md").write_text(plan, encoding="utf-8")
    (folder / "CLAUDE.md").write_text("placeholder\n", encoding="utf-8")
    (folder / "log.md").write_text(log, encoding="utf-8")
    return folder


def make_devroot(tmp_path: Path, **projects: list[tuple]) -> Path:
    """A DEVROOT with the named projects (empty directories)."""
    devroot = tmp_path / "devroot"
    devroot.mkdir()
    for name in projects:
        (devroot / name).mkdir()
    return devroot


def link_dir(link: Path, target: Path) -> None:
    """A directory link: a symlink, or a junction on Windows (no privilege needed)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True, capture_output=True,
        )
    else:
        os.symlink(target, link, target_is_directory=True)


def tree_digest(*roots: Path) -> str:
    """SHA-256 over every file (path and bytes) under the roots, order-independent."""
    digest = hashlib.sha256()
    entries = []
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            if path.is_file():
                entries.append((path.relative_to(root).as_posix(), path.read_bytes()))
    for rel, data in sorted(entries):
        digest.update(rel.encode())
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


class Running:
    """An in-process dashboard server on a thread, plus a raw HTTP client."""

    def __init__(self, data_dir: Path, parents: tuple[Path, ...], busy=None, edge=None,
                 config_path: Path | None = None) -> None:
        from task_system.dashboard.inventory import Inventory
        from task_system.dashboard.server import DashboardServer

        self.data_dir = Path(data_dir)
        self.log = io.StringIO()
        self.inventory = Inventory(parents=parents, min_interval=0.0)
        self.server = DashboardServer(
            data_dir=data_dir, scripts_dir=SCRIPTS_DIR, inventory=self.inventory,
            busy=busy, log=self.log, edge=edge, config_path=config_path,
        )
        self.state = self.server.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.state.port

    @property
    def token(self) -> str:
        return self.state.token

    def request(self, method: str, path: str, *, token: str | None | bool = True,
                headers: dict | None = None, body: bytes | None = None, host: str | None = None):
        """(status, headers dict, body bytes). ``token=True`` sends the real token."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", host if host is not None else f"127.0.0.1:{self.port}")
            if token is True:
                connection.putheader("X-Task-Kit-Token", self.token)
            elif token:
                connection.putheader("X-Task-Kit-Token", token)
            for key, value in (headers or {}).items():
                connection.putheader(key, value)
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            data = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, data
        finally:
            connection.close()

    def json(self, method: str, path: str, **kw):
        status, headers, data = self.request(method, path, **kw)
        return status, headers, json.loads(data.decode("utf-8"))

    @property
    def scheduler(self):
        return self.server.scheduler

    def post(self, path: str, body: dict, **kw):
        """A same-origin JSON mutation: (status, headers, parsed body)."""
        return self.json(
            "POST", path, body=json.dumps(body).encode("utf-8"),
            headers={"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"}, **kw)

    def settle(self, run_id: str, *, timeout_s: float = 90.0, states=None):
        """Tick the scheduler until the run reaches a terminal state; the final Run."""
        import time

        from task_system.dashboard import runtime

        wanted = tuple(states) if states else runtime.TERMINAL_STATES
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.scheduler.tick()
            run = self.scheduler.db.get(run_id)
            if run is not None and run.state in wanted:
                return run
            time.sleep(0.1)
        run = self.scheduler.db.get(run_id)
        raise AssertionError(f"run {run_id} did not settle: {run}")

    def stop(self) -> None:
        self.server.request_shutdown()
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "server thread did not stop"


def run_task_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """task.py in a subprocess, hermetically; stdout/stderr as BYTES."""
    from bootstrap_guard import _REEXEC_SKIP_ENV

    env = os.environ.copy()
    env[_REEXEC_SKIP_ENV] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT / "plugins" / "skills-kit")
    return subprocess.run([sys.executable, str(TASK_CLI), *args], cwd=cwd, capture_output=True, env=env)


# ---------------------------------------------------------------------------
# autonomous-run fixtures
# ---------------------------------------------------------------------------

FAKE_LLM = Path(__file__).with_name("fake_llm.py")


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def init_repo(root: Path) -> None:
    """A git repository on branch main with one commit (task folders live under dev/tasks/, ignored)."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("project\n", encoding="utf-8")
    (root / ".gitignore").write_text("tmp/\ndev/tasks/\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")


def runner_environment(monkeypatch) -> None:
    """Child processes the scheduler spawns import the task system from the working copy."""
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join([str(REPO_ROOT / "plugins" / "skills-kit"), str(SCRIPTS_DIR)]),
    )


class FakeEdge:
    """A stand-in for the llm-kit edge whose 'job runner' is tests/task-kit/fake_llm.py.

    ``behavior`` is what the fake agent does when its run starts (see fake_llm.py).
    """

    def __init__(self, *, state: str = "ok", detail: str = "", behavior: str = "done", entries: dict | None = None,
                 invalid_job_file: str | None = None) -> None:
        from task_system.dashboard.jobs_edge import EntryInfo

        self.state = state
        self.detail = detail or ("llm-kit is available" if state == "ok" else f"llm-kit is {state}")
        self.behavior = behavior
        self.invalid_job_file = invalid_job_file
        self.entries = entries if entries is not None else {
            "opus": EntryInfo(id="opus", kind="harness", harness="claude", model="opus"),
            "sonnet": EntryInfo(id="sonnet", kind="harness", harness="claude", model="sonnet"),
            "sol": EntryInfo(id="sol", kind="harness", harness="codex", model="gpt-5.6-sol"),
            "or-qwen": EntryInfo(id="or-qwen", kind="transport", harness=None, model="qwen/qwen3-32b"),
        }
        self.job_files: list[Path] = []

    def status(self):
        from task_system.dashboard.jobs_edge import EdgeStatus

        return EdgeStatus(self.state, self.detail)

    def entry(self, name):
        return self.entries.get(name)

    def validate_job_file(self, path: Path) -> None:
        from task_system.dashboard.jobs_edge import EdgeError

        self.job_files.append(Path(path))
        if self.invalid_job_file:
            raise EdgeError(self.invalid_job_file)

    def runner_argv(self, job_file: Path, store: Path, timeout_s: float, run_id: str) -> list[str]:
        (Path(job_file).parent / "behavior.json").write_text(json.dumps({"behavior": self.behavior}), encoding="utf-8")
        return [sys.executable, str(FAKE_LLM), str(job_file), str(store), run_id]


def agent_world(tmp_path: Path, *, repo: bool = True, items=None, sections=None, status: str = "active",
                task_location: str = "tmp", stub: str = "alpha") -> tuple[Path, Path, Path]:
    """(devroot, project root, task folder) with one runnable item 'do-it' (and a sibling 'later-one')."""
    devroot = tmp_path / "devroot"
    project = devroot / "proj"
    if repo:
        init_repo(project)
    else:
        project.mkdir(parents=True)
    folder = make_task(
        project, task_location, stub, title="Alpha", status=status,
        items=items if items is not None else [
            {"id": "do-it", "title": "Do it", "state": "available", "priority": "P1"},
            {"id": "later-one", "title": "Later one", "state": "available", "priority": "P2"}],
        sections=sections if sections is not None else {
            "do-it": "Make the thing.\n\nDone when it exists.", "later-one": "Make the other thing."},
        log="- 2026-10-01 09:00: began\n",
    )
    return devroot, project, folder


def dashboard_world(tmp_path: Path, **kw) -> tuple[Path, Path, Path]:
    """``agent_world`` with the task under dev/tasks, the only root the dashboard reads."""
    kw.setdefault("task_location", "dev/tasks")
    return agent_world(tmp_path, **kw)
