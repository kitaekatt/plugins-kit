"""The task-kit SessionStart hook: ``hooks.json``, the shell fast path, and the Python entry.

Three layers, each with its own tests:

* ``hooks.json`` shape (two matcher groups, ``resume`` and ``clear``, both async, the one script).
* The real shell script, run with a fake interpreter planted where the forced interpreter chain looks first
  (``$HOME/.local/share/python-standalone/python/bin/python3``). The fake writes a marker, so "Python was never
  started" is a file that does not exist. The data root is redirected to a temporary directory.
* The entry module (``task_session_start.py``): ``run`` with injected identity, machine and publisher against real
  record files, and ``main`` for the never-fails-the-session contract.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import task_session_start as hook
from git_remote_support import clone, commit_all, git, isolate_git_environment, make_remote
from task_system import harness, procstamp, sessions
from task_system.machine import Machine

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "task-kit"
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"
SCRIPT = PLUGIN / "hooks" / "sessionstart" / "session-start.sh"
ENTRY = PLUGIN / "skills" / "task" / "scripts" / "task_session_start.py"

HOST = Machine("testhost", "abcdef0123456789", "macos")
SHELLS = [pytest.param(["bash"], id="bash"), pytest.param(["sh"], id="sh")]
posix_only = pytest.mark.skipif(os.name == "nt", reason="the fake interpreter is a POSIX shell script")


def hook_input(source: str, session_id: str, transcript: str | None = None, cwd: str | Path = "/nowhere") -> str:
    data = {"session_id": session_id, "transcript_path": transcript or f"/nowhere/{session_id}.jsonl",
            "cwd": str(cwd), "hook_event_name": "SessionStart", "source": source}
    return json.dumps(data)


# -- hooks.json ------------------------------------------------------------------------------------------------


def test_hooks_json_has_two_async_matcher_groups_for_one_script():
    document = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    assert list(document["hooks"]) == ["SessionStart"], "task-kit ships no other hook"
    groups = document["hooks"]["SessionStart"]
    assert sorted(group["matcher"] for group in groups) == ["clear", "resume"]
    for group in groups:
        (entry,) = group["hooks"]
        assert entry["type"] == "command"
        assert entry["async"] is True
        assert entry["command"] == 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/sessionstart/session-start.sh"'
    assert SCRIPT.is_file()


def test_script_uses_only_portable_sh_constructs():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for banned in ("<<<", "read -p", "local ", "function ", "declare ", "mapfile", "readarray", "${!", "=(",
                   "${BASH_", "source ", "$RANDOM", "&>"):
        assert banned not in code, f"bash-only construct {banned!r} in session-start.sh"
    assert not re.search(r"(^|\s)\[\[\s", code, re.MULTILINE), "the [[ ]] test is bash-only"
    text.encode("ascii")


@pytest.mark.parametrize("shell", SHELLS)
def test_script_parses_under_the_shell(shell):
    if shutil.which(shell[0]) is None:
        pytest.skip(f"{shell[0]} is not installed")
    subprocess.run([*shell, "-n", str(SCRIPT)], check=True)


# -- the real script, with a fake interpreter --------------------------------------------------------------------


class Rig:
    """A HOME holding a fake standalone interpreter, a redirected data root, and a project whose git-tracked
    ``.sessions`` files are the only registrations (the hook's fast path reads them, never the data directory)."""

    def __init__(self, base: Path, *, fake_status: int = 0, with_python: bool = True) -> None:
        self.home = base / "home"
        self.data_root = base / "data"
        self.data_dir = self.data_root / "plugins-kit" / "task-kit"
        self.project = base / "project"
        self.project.mkdir()
        self.marker = base / "marker.txt"
        self.stdin_copy = base / "stdin.txt"
        self.home.mkdir()
        if with_python:
            python = self.home / ".local" / "share" / "python-standalone" / "python" / "bin" / "python3"
            python.parent.mkdir(parents=True)
            python.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{self.marker}'\n"
                f"cat > '{self.stdin_copy}'\n"
                + (f"echo 'fake interpreter failure' >&2\nexit {fake_status}\n" if fake_status else "exit 0\n"),
                encoding="utf-8")
            python.chmod(python.stat().st_mode | stat.S_IXUSR)

    def session_file(self, session_id: str, *, host: str = "testhost", location: str = "dev/tasks") -> Path:
        """A session file naming ``session_id``; any host name, since the fast path cannot derive this machine's."""
        path = self.project / location / ".sessions" / f"{host}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"_schema_version: 1\nhost: {host}\nsessions:\n- session_id: {session_id}\n  task: alpha\n",
                        encoding="utf-8")
        return path

    def payload(self, source: str, session_id: str) -> str:
        return hook_input(source, session_id, cwd=self.project)

    def run(self, shell: list[str], payload: str, *, project_dir: Path | None = None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items()
               if k not in ("BOOTSTRAP_PYTHON", "CLAUDE_BOOTSTRAP_DATA_ROOT", "CLAUDE_PROJECT_DIR")}
        env.update(HOME=str(self.home), CLAUDE_BOOTSTRAP_DATA_ROOT=str(self.data_root))
        if project_dir is not None:
            env["CLAUDE_PROJECT_DIR"] = str(project_dir)
        return subprocess.run([*shell, str(SCRIPT)], input=payload, capture_output=True, text=True, env=env,
                              timeout=60)

    @property
    def local_files(self) -> list[Path]:
        """Every file the hook left under the data directory."""
        return sorted(p for p in self.data_dir.rglob("*") if p.is_file()) if self.data_dir.exists() else []

    @property
    def log(self) -> str:
        path = self.data_dir / "sessions" / "session-start-hook.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_resume_without_a_file_naming_the_session_never_starts_python(rig, shell):
    rig.session_file("some-other-session")
    done = rig.run(shell, rig.payload("resume", "sess-one"))
    assert done.returncode == 0 and done.stdout == ""
    assert not rig.marker.exists(), "Python was started although no session file names this session"
    assert rig.local_files == [], "a quiet fast path must leave nothing in the data directory"


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_resume_with_no_session_files_never_starts_python(rig, shell):
    assert rig.run(shell, rig.payload("resume", "sess-one")).returncode == 0
    assert not rig.marker.exists()


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_resume_with_a_file_naming_the_session_starts_the_entry_with_the_input_on_stdin(rig, shell):
    rig.session_file("sess-one")
    payload = rig.payload("resume", "sess-one")
    done = rig.run(shell, payload)
    assert done.returncode == 0 and done.stdout == ""
    assert rig.marker.read_text(encoding="utf-8").strip().endswith("task_session_start.py")
    assert json.loads(rig.stdin_copy.read_text(encoding="utf-8"))["source"] == "resume"
    assert rig.local_files == [], "a successful run leaves nothing in the data directory"


@posix_only
@pytest.mark.parametrize("location", ["dev/tasks", "tmp"])
def test_resume_finds_the_session_in_either_task_root(rig, location):
    rig.session_file("sess-one", host="whatever-the-host-is", location=location)
    assert rig.run(["bash"], rig.payload("resume", "sess-one")).returncode == 0
    assert rig.marker.exists()


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_clear_without_any_record_never_starts_python(rig, shell):
    path = rig.session_file("ignored")
    path.write_text("_schema_version: 1\nhost: testhost\nsessions: []\n", encoding="utf-8")  # released: no record
    done = rig.run(shell, rig.payload("clear", "sess-two"))
    assert done.returncode == 0
    assert not rig.marker.exists(), "Python was started although the project holds no registration"


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_clear_with_any_record_starts_the_entry(rig, shell):
    rig.session_file("previous-session")
    assert rig.run(shell, rig.payload("clear", "sess-two")).returncode == 0
    assert rig.marker.exists()
    assert json.loads(rig.stdin_copy.read_text(encoding="utf-8"))["source"] == "clear"


@posix_only
def test_the_project_is_claude_project_dir_when_set_else_the_inputs_cwd(rig, tmp_path):
    rig.session_file("sess-one")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert rig.run(["bash"], hook_input("resume", "sess-one", cwd=elsewhere)).returncode == 0
    assert not rig.marker.exists(), "the cwd holds no session file"
    assert rig.run(["bash"], hook_input("resume", "sess-one", cwd=elsewhere), project_dir=rig.project).returncode == 0
    assert rig.marker.exists(), "CLAUDE_PROJECT_DIR is preferred over the input's cwd"


@posix_only
def test_a_session_registered_in_another_project_than_the_cwd_is_not_covered(rig, tmp_path):
    """The one case the files-only design no longer covers: the registration lives in a different project."""
    other = tmp_path / "other-project"
    (other / "dev" / "tasks" / ".sessions").mkdir(parents=True)
    (other / "dev" / "tasks" / ".sessions" / "testhost.yaml").write_text("- session_id: sess-one\n", encoding="utf-8")
    assert rig.run(["bash"], rig.payload("resume", "sess-one")).returncode == 0
    assert not rig.marker.exists()


@posix_only
@pytest.mark.parametrize("source", ["fork", "startup", "compact", ""])
def test_other_sources_do_nothing_even_when_registrations_exist(rig, source):
    rig.session_file("sess-one")
    assert rig.run(["bash"], rig.payload(source, "sess-one")).returncode == 0
    assert not rig.marker.exists()


@posix_only
@pytest.mark.parametrize("payload", ["", "not json", "{}", '{"source": "resume"}',
                                     '{"source": "resume", "session_id": "../escape"}'])
def test_unusable_input_does_nothing_and_exits_zero(rig, payload):
    rig.session_file("sess-one")
    done = rig.run(["bash"], payload)
    assert done.returncode == 0 and not rig.marker.exists()


@posix_only
def test_a_failing_interpreter_never_fails_the_session_and_is_logged(tmp_path):
    rig = Rig(tmp_path, fake_status=7)
    rig.session_file("sess-one")
    done = rig.run(["bash"], rig.payload("resume", "sess-one"))
    assert done.returncode == 0
    assert "fake interpreter failure" in rig.log and "exited 7" in rig.log


@posix_only
def test_a_missing_interpreter_never_fails_the_session_and_is_logged(tmp_path):
    rig = Rig(tmp_path, with_python=False)
    rig.session_file("sess-one")
    done = rig.run(["bash"], rig.payload("resume", "sess-one"))
    assert done.returncode == 0
    assert "no approved interpreter" in rig.log


@posix_only
def test_the_real_entry_fails_loudly_into_the_log_but_exits_zero_through_the_script(tmp_path):
    """The script runs the real entry; the redirected data root holds no task-kit venv, so the venv re-exec refuses
    (exit 2) and says why on stderr; the script puts that in the log and the session is unharmed."""
    rig = Rig(tmp_path)
    python = rig.home / ".local" / "share" / "python-standalone" / "python" / "bin" / "python3"
    python.write_text(f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n", encoding="utf-8")
    rig.session_file("sess-one")
    done = rig.run(["bash"], rig.payload("resume", "sess-one"))
    assert done.returncode == 0
    assert "redirected plugin interpreter is missing" in rig.log and "stopped with exit code 2" in rig.log


# -- the entry: run() against real record files --------------------------------------------------------------------


class World:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        isolate_git_environment(monkeypatch, tmp_path)
        self.home = tmp_path / "home"
        self.home.mkdir()
        monkeypatch.setenv("HOME", str(self.home))
        self.data = tmp_path / "data"
        self.remote = make_remote(tmp_path)
        self.project = clone(tmp_path, self.remote, "project")
        for stub in ("alpha", "beta"):
            commit_all(self.project, f"dev/tasks/{stub}/task.yaml", "task:\n  status: active\n", f"add {stub}")
        git(self.project, "push", "-q", "origin", "HEAD:main")
        self.procs: list[subprocess.Popen] = []
        self.publishes: list[tuple] = []

    @property
    def root(self) -> Path:
        return Path(os.path.realpath(self.project / "dev" / "tasks"))

    def ident(self, session_id: str, proc: subprocess.Popen | None = None) -> harness.Identity:
        if proc is None:
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
            self.procs.append(proc)
        return harness.Identity(session_id, proc.pid, procstamp.stamp_of(proc.pid))

    def register(self, ident, ref="alpha"):
        return sessions.register(ident, HOST, self.project, ref, data_dir=self.data, env={})

    def records(self):
        return sessions.read_root(self.root, HOST).records

    def keys(self) -> list[tuple[str, str]]:
        return sorted((r.task, r.session_id) for r in self.records())

    def local_files(self) -> list[str]:
        """Every file under the data directory that is not a lock file (there must be none)."""
        directory = self.data / "sessions"
        return sorted(str(p.relative_to(directory)) for p in directory.rglob("*")
                      if p.is_file() and p.parent.name != "locks") if directory.is_dir() else []

    def record_publish(self, changed, machine, data_dir, log):
        self.publishes.append(tuple(changed))

    def run(self, source, session_id, ident, transcript=None, publish=None, lines=None):
        return hook.run(hook_input(source, session_id, transcript, cwd=self.project), {}, data_dir=self.data, machine=HOST,
                        identity=ident, publish=publish or self.record_publish,
                        log=(lines.append if lines is not None else (lambda text: None)))

    def snapshot(self) -> dict[str, bytes]:
        return {str(p): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file() and ".sessions" in p.parts}

    def close(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = World(tmp_path, monkeypatch)
    try:
        yield built
    finally:
        built.close()


def test_resume_refreshes_the_stamp_and_transcript_then_publishes_the_changed_file(world):
    old = world.ident("sess-one")
    world.register(old)
    new = world.ident("sess-one")  # the resumed session: same id, a new process
    transcript = world.home / ".claude" / "projects" / "-proj" / "sess-one.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")

    outcome = world.run("resume", "sess-one", new, transcript=str(transcript))

    (record,) = world.records()
    assert record.stamp == new.stamp and record.stamp != old.stamp
    assert record.transcript_path == "~/.claude/projects/-proj/sess-one.jsonl"
    assert outcome.startswith("resume sess-one"[:15]) and "refreshed 1" in outcome
    assert world.publishes == [(world.root / ".sessions" / "testhost.yaml",)]


def test_resume_with_no_record_for_the_session_writes_and_publishes_nothing(world):
    world.register(world.ident("sess-one"))
    before = world.snapshot()
    outcome = world.run("resume", "sess-unrelated", world.ident("sess-unrelated"))
    assert "refreshed 0" in outcome
    assert world.snapshot() == before
    assert world.publishes == []


def test_clear_rekeys_every_registration_of_the_process_then_publishes(world):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    world.procs.append(proc)
    world.register(world.ident("sess-one", proc), "alpha")
    world.register(world.ident("sess-one", proc), "beta")
    assert world.keys() == [("alpha", "sess-one"), ("beta", "sess-one")]
    assert world.local_files() == []

    outcome = world.run("clear", "sess-two", world.ident("sess-two", proc))

    assert world.keys() == [("alpha", "sess-two"), ("beta", "sess-two")], "both registrations move, not one"
    assert world.local_files() == [], "the re-key is found and written through the git-tracked files alone"
    assert "re-keyed 2" in outcome
    assert world.publishes == [(world.root / ".sessions" / "testhost.yaml",)]


def test_the_project_is_found_through_claude_project_dir_before_the_inputs_cwd(world):
    old = world.ident("sess-one")
    world.register(old)
    new = world.ident("sess-one")
    outcome = hook.run(hook_input("resume", "sess-one", cwd="/nowhere"), {"CLAUDE_PROJECT_DIR": str(world.project)},
                       data_dir=world.data, machine=HOST, identity=new, publish=world.record_publish)
    assert "refreshed 1" in outcome
    assert world.records()[0].stamp == new.stamp


def test_a_resume_from_a_project_holding_no_registration_of_the_session_does_nothing(world, tmp_path):
    old = world.ident("sess-one")
    world.register(old)
    other = tmp_path / "other"
    other.mkdir()
    before = world.snapshot()
    outcome = hook.run(hook_input("resume", "sess-one", cwd=other), {}, data_dir=world.data, machine=HOST,
                       identity=world.ident("sess-one"), publish=world.record_publish)
    assert "refreshed 0" in outcome and world.snapshot() == before and world.publishes == []


def test_a_fork_changes_nothing_and_publishes_nothing(world):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    world.procs.append(proc)
    world.register(world.ident("sess-one", proc))
    before = world.snapshot()
    outcome = world.run("fork", "sess-fork", world.ident("sess-fork", proc))
    assert outcome == "ignored source 'fork'"
    assert world.snapshot() == before and world.local_files() == [] and world.publishes == []


def test_a_dashboard_run_changes_nothing(world):
    world.register(world.ident("sess-one"))
    before = world.snapshot()
    outcome = hook.run(hook_input("resume", "sess-one", cwd=world.project), {"TASK_KIT_AGENT_DEPTH": "1"}, data_dir=world.data,
                       machine=HOST, identity=world.ident("sess-one"), publish=world.record_publish)
    assert outcome.startswith("ignored") and world.snapshot() == before and world.publishes == []


def test_the_real_publisher_delivers_the_refreshed_record_to_the_remote(world):
    old = world.ident("sess-one")
    world.register(old)
    world.run("resume", "sess-one", world.ident("sess-one"), publish=hook.publish_changed)
    fetched = git(world.remote, "ls-tree", "-r", "--name-only", "main").stdout
    assert "dev/tasks/.sessions/testhost.yaml" in fetched
    assert world.local_files() == [], "publishing leaves no log or other record in the data directory"


def test_publish_changed_publishes_once_per_repository_and_skips_tmp_roots(tmp_path, monkeypatch):
    from task_system import session_publish as sp

    repo, calls = tmp_path / "repo", []
    a, b, tmp_root = repo / "dev" / "tasks", repo / "other" / "tasks", repo / "tmp"
    files = [root / ".sessions" / "testhost.yaml" for root in (a, b, tmp_root)]

    def fake_preflight(root, *, machine=None, **kw):
        kind = sp.KIND_LOCAL if root == tmp_root else sp.KIND_DURABLE
        return sp.Preflight(True, kind, None if root == tmp_root else repo)

    monkeypatch.setattr(sp, "preflight", fake_preflight)
    monkeypatch.setattr(sp, "publish", lambda top, machine, data_dir=None, **kw: calls.append(top) or sp.PublishResult(
        sp.PUBLISHED, "ok", True, True, 1, (), top))
    lines: list[str] = []
    hook.publish_changed(files, HOST, tmp_path, lines.append)
    assert calls == [repo]
    assert lines == [], "a completed publication is not a failure: nothing is logged"


def test_publish_changed_logs_a_publication_that_did_not_complete(tmp_path, monkeypatch):
    from task_system import session_publish as sp

    repo = tmp_path / "repo"
    monkeypatch.setattr(sp, "preflight", lambda root, **kw: sp.Preflight(True, sp.KIND_DURABLE, repo))
    monkeypatch.setattr(sp, "publish", lambda top, machine, data_dir=None, **kw: sp.PublishResult(
        sp.PUSH_FAILED, "committed; not published: push failed: nope", True, False, 1, (), top))
    lines: list[str] = []
    hook.publish_changed([repo / "dev" / "tasks" / ".sessions" / "testhost.yaml"], HOST, tmp_path, lines.append)
    assert len(lines) == 1 and "push-failed" in lines[0] and "nope" in lines[0]


def test_publish_changed_reports_a_refusal_and_publishes_nothing(tmp_path, monkeypatch):
    from task_system import session_publish as sp

    monkeypatch.setattr(sp, "preflight", lambda root, **kw: sp.Preflight(False, sp.KIND_DURABLE, None, "no upstream"))
    monkeypatch.setattr(sp, "publish", lambda *a, **k: pytest.fail("a refused root must not publish"))
    lines: list[str] = []
    hook.publish_changed([tmp_path / "dev" / "tasks" / ".sessions" / "testhost.yaml"], HOST, tmp_path, lines.append)
    assert lines and "no upstream" in lines[0]


# -- main(): never fails the session ------------------------------------------------------------------------------


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    from task_system import datadir

    data = tmp_path / "data" / "plugins-kit" / "task-kit"
    data.mkdir(parents=True)
    monkeypatch.setattr(datadir, "resolve_data_dir", lambda *a, **k: data)
    return data


@pytest.mark.parametrize("payload", ["", "not json", "[]", hook_input("resume", "no/slash")])
def test_main_returns_zero_and_logs_the_error_for_a_bad_input(main_env, monkeypatch, payload):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert hook.main() == 0
    log = (main_env / "sessions" / hook.LOG_NAME).read_text(encoding="utf-8")
    assert "failed" in log and "Traceback" in log


def test_main_returns_zero_and_logs_when_the_library_raises(main_env, monkeypatch):
    import io

    from task_system import machine

    def boom(**kw):
        raise machine.MachineError("cannot identify this machine")

    monkeypatch.setattr(machine, "current", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(hook_input("resume", "sess-one")))
    assert hook.main() == 0
    assert "cannot identify this machine" in (main_env / "sessions" / hook.LOG_NAME).read_text(encoding="utf-8")


def test_a_quiet_main_run_writes_nothing_to_the_data_directory(main_env, monkeypatch):
    """The hook log holds failures only: an ignored source (or any run that succeeds) creates no file."""
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(hook_input("fork", "sess-one")))
    assert hook.main() == 0
    assert not (main_env / "sessions").exists()
