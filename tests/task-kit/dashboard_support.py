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
                 config_path: Path | None = None, min_interval: float = 0.0, cli_observer=False, sessions=None) -> None:
        from task_system.dashboard.inventory import Inventory
        from task_system.dashboard.server import DashboardServer
        from task_system.dashboard.sessions_view import SessionsMaintainer

        self.data_dir = Path(data_dir)
        self.log = io.StringIO()
        self.inventory = Inventory(parents=parents, min_interval=min_interval, sessions=sessions)
        # A test drives the maintainer itself (``server.maintainer.once()``): its own thread never fires.
        maintainer = None if sessions is None else SessionsMaintainer(
            sessions, lambda: self.inventory.snapshot().session_roots, interval_s=3600.0, first_delay_s=3600.0,
            data_dir=self.data_dir, log=lambda message: self.log.write(message + "\n"))
        self.server = DashboardServer(
            data_dir=data_dir, scripts_dir=SCRIPTS_DIR, inventory=self.inventory,
            busy=busy, log=self.log, edge=edge, config_path=config_path, cli_observer=cli_observer,
            sessions=sessions, maintainer=maintainer,
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

    def send(self, method: str, path: str, body: dict | None = None, **kw):
        """A same-origin JSON request of any verb: (status, headers, parsed body)."""
        return self.json(
            method, path, body=json.dumps(body if body is not None else {}).encode("utf-8"),
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


# The variables a dashboard run's command inherits. A subprocess a test builds for the task-kit launcher must not
# carry them (a worker running these very tests has them set), so such environments are built through clean_env().
RUN_MARKERS = ("TASK_KIT_AGENT_DEPTH", "TASK_KIT_RUN_TOKEN", "TASK_KIT_DASHBOARD_ROOT")


def clean_env(extra: dict | None = None, *, base: dict | None = None) -> dict:
    """An environment for a subprocess the test builds: ``base`` (default: this process's) without the run markers."""
    env = dict(os.environ if base is None else base)
    for name in RUN_MARKERS:
        env.pop(name, None)
    env.update(extra or {})
    return env


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
                 invalid_job_file: str | None = None, sleep_s: float = 3.0, behavior_args: dict | None = None) -> None:
        from task_system.dashboard.jobs_edge import EntryInfo

        self.state = state
        self.detail = detail or ("llm-kit is available" if state == "ok" else f"llm-kit is {state}")
        self.behavior = behavior
        self.behavior_args = dict(behavior_args or {})  # extra keys for fake_llm's behavior.json (a helper-call script, say)
        self.sleep_s = sleep_s
        self.transport_calls: list[dict] = []  # every complete_transport call the server made
        self.transport_gate = None  # a callable run inside complete_transport (a test holds a call open with it)
        self.transport_answer = lambda prompt: f"echo: {prompt}"
        self.invalid_job_file = invalid_job_file
        self.entries = entries if entries is not None else {
            "opus": EntryInfo(id="opus", kind="harness", harness="claude", model="opus"),
            "sonnet": EntryInfo(id="sonnet", kind="harness", harness="claude", model="sonnet"),
            "sol": EntryInfo(id="sol", kind="harness", harness="codex", model="gpt-5.6-sol"),
            "or-qwen": EntryInfo(id="or-qwen", kind="transport", harness=None, model="qwen/qwen3-32b"),
        }
        self.job_files: list[Path] = []
        self.worker_argv = None
        self.argv_calls: list[str] = []

    def status(self):
        from task_system.dashboard.jobs_edge import EdgeStatus

        return EdgeStatus(self.state, self.detail)

    def entry(self, name, project_root=None):
        return self.entries.get(name)

    def complete_transport(self, models, *, system, prompt, timeout_s, project_root, **_kw):
        """A scripted tool-less completion: the first TRANSPORT entry of ``models`` answers, none -> ``unroutable``."""
        from task_system.dashboard.jobs_edge import TransportOutcome

        self.transport_calls.append({"models": list(models), "system": system, "prompt": prompt, "timeout_s": timeout_s,
                                     "project_root": str(project_root)})
        if self.transport_gate is not None:
            self.transport_gate()
        usable = [m for m in models if self.entries.get(m) is not None and self.entries[m].kind == "transport"]
        if not usable:
            floor = {"kind": "no-usable-routing-target", "caller": "process",
                     "dispositions": [{"entry": m, "usable": False, "reason": "not a registry entry" if m not in self.entries else "not a transport entry"}
                                      for m in models]}
            return TransportOutcome(status="unroutable", entry=None, text="", attempts=(), usage={}, reported_cost_usd=None,
                                    detail=f"no usable routing target in [{', '.join(models)}]", floor=floor)
        return TransportOutcome(
            status="completed", entry=usable[0], text=self.transport_answer(prompt),
            attempts=({"entry": usable[0], "number": 1, "outcome": "completed"},),
            usage={"input_tokens": 3, "output_tokens": 2}, reported_cost_usd=0.0001)

    EFFORTS = ("low", "medium", "high", "xhigh", "max")

    def effort_menu(self, entry):
        found = self.entries.get(entry)
        if found is None:
            from task_system.dashboard.jobs_edge import EdgeError

            raise EdgeError(f"no entry {entry!r}")
        return self.EFFORTS if found.harness in ("claude", "codex") else ()

    def claude_worker_argv(self, entry, *, effort, disallowed_tools, cwd):
        """The argv a Claude worker would launch; ``worker_argv`` (a callable) supplies it, else the edge cannot build one.

        With no ``worker_argv`` the dashboard's per-binary observation ends ``unobserved`` at once, without
        spawning anything, which keeps every end-to-end test off the real ``claude`` binary.
        """
        from task_system.dashboard.jobs_edge import EdgeError

        if self.worker_argv is None:
            raise EdgeError("the fake edge builds no worker argv")
        self.argv_calls.append(disallowed_tools)
        return self.worker_argv(entry, effort=effort, disallowed_tools=disallowed_tools, cwd=cwd)

    def validate_job_file(self, path: Path) -> None:
        from task_system.dashboard.jobs_edge import EdgeError

        self.job_files.append(Path(path))
        if self.invalid_job_file:
            raise EdgeError(self.invalid_job_file)

    def runner_argv(self, job_file: Path, store: Path, timeout_s: float, run_id: str) -> list[str]:
        (Path(job_file).parent / "behavior.json").write_text(
            json.dumps({"behavior": self.behavior, "sleep_s": self.sleep_s, **self.behavior_args}), encoding="utf-8")
        return [sys.executable, str(FAKE_LLM), str(job_file), str(store), run_id]

    def cancel_argv(self, store: Path, run_id: str, reason: str) -> list[str]:
        return [sys.executable, str(FAKE_LLM), "--cancel", str(store), run_id, reason]

    def ledger_snapshot(self, store: Path, run_id: str):
        """The stand-in ledger fake_llm.py keeps beside the store, or None before the job started."""
        from task_system.dashboard.jobs_edge import LedgerView

        path = Path(store).parent / "fake-ledger.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return LedgerView(run_id=data["run"], jobs=tuple((j["id"], j["state"]) for j in data["jobs"]))

    def ledger_events(self, store: Path, run_id: str) -> list[dict]:
        return []


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


def make_checkouts(tmp_path: Path, names: tuple[str, ...] = ("proj-a", "proj-b"), *, items=None, sections=None,
                   stub: str = "alpha") -> tuple[Path, dict[str, tuple[Path, Path]]]:
    """(devroot, {name: (project root, task folder)}): git checkouts that each hold the same-shaped task.

    Every checkout is its own project to the scheduler (its lease and its slot). Items default to three
    available ones (``do-it``, ``later-one``, ``third``) with a plan.md section each.
    """
    devroot = tmp_path / "devroot"
    ids = ("do-it", "later-one", "third")
    made = {}
    for name in names:
        project = devroot / name
        init_repo(project)
        folder = make_task(
            project, "dev/tasks", stub, title=f"Task in {name}", status="active",
            items=items if items is not None else [
                {"id": i, "title": i.replace("-", " ").title(), "state": "available", "priority": "P2"} for i in ids],
            sections=sections if sections is not None else {i: f"Do {i}.\n\nDone when {i} exists." for i in ids},
            log="- 2026-10-01 09:00: began\n",
        )
        made[name] = (project, folder)
    return devroot, made
