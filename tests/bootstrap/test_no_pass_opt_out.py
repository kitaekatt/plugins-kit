"""BOOTSTRAP_NO_PASS: a launched helper session runs no bootstrap pass.

A tool that starts Claude or Codex sessions for its own work (a review lane in
a frozen copy of a repository, a consultation in a scratch directory) sets
BOOTSTRAP_NO_PASS=1 in their environment. Each hook entry then stands down: no
engine pass, no harvest, no SessionStart rescue, no display relay. The
SessionStart hook still writes the interpreter names and plugin-root
variables first, so the session's
commands keep BOOTSTRAP_PYTHON. An explicit `bootstrap run` (console mode)
ignores the variable.
"""
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "plugins" / "bootstrap"
SESSION_HOOK = BOOTSTRAP / "hooks" / "sessionstart" / "session-bootstrap.sh"
DISPLAY_HOOK = BOOTSTRAP / "hooks" / "userpromptsubmit" / "bootstrap-display.sh"


def _place(hook: Path, plugin_root: Path) -> Path:
    dest = plugin_root / hook.relative_to(BOOTSTRAP)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(hook.read_bytes())
    (plugin_root / "hooks" / "marketplace-name.sh").write_bytes(
        (BOOTSTRAP / "hooks" / "marketplace-name.sh").read_bytes())
    return dest


def _env(tmp_path: Path, data_root: Path) -> dict:
    env = dict(os.environ)
    env.pop("CLAUDE_PLUGIN_TEST", None)
    env.update({
        "HOME": str(tmp_path / "home"),
        "CLAUDE_BOOTSTRAP_DATA_ROOT": str(data_root),
        "BOOTSTRAP_NO_PASS": "1",
    })
    return env


def test_sessionstart_runs_no_pass(tmp_path):
    root = tmp_path / "mkt" / "plugins" / "bootstrap"
    hook = _place(SESSION_HOOK, root)
    data_root = tmp_path / "data"
    project = tmp_path / "project"
    project.mkdir()
    res = subprocess.run(
        ["bash", str(hook)], cwd=project, env=_env(tmp_path, data_root),
        input=json.dumps({"session_id": "lane-one"}),
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"continue": True, "suppressOutput": True}
    data = data_root
    found = [p for p in data.rglob("*") if p.is_file()] if data.exists() else []
    assert not [p for p in found if p.name == "bootstrap.log"
                or p.name.startswith("last_run_epoch.")
                or p.parent.name == "sessions"], found


def test_userpromptsubmit_relays_nothing_and_rescues_nothing(tmp_path):
    root = tmp_path / "mkt" / "plugins" / "bootstrap"
    hook = _place(DISPLAY_HOOK, root)
    data_root = tmp_path / "data"
    # With no marketplace.json the marketplace name is the directory "mkt".
    data = data_root / "mkt" / "bootstrap"
    data.mkdir(parents=True)
    pending = data / "bootstrap_display.pending"
    pending.write_text('{"continue": true, "systemMessage": "another session"}')
    res = subprocess.run(
        ["bash", str(hook)], env=_env(tmp_path, data_root),
        input=json.dumps({"session_id": "lane-one"}),
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == ""
    assert pending.exists()
    assert not list(data.rglob("rescue_launched.*"))


def _load_cli():
    path = BOOTSTRAP / "scripts" / "bootstrap_cli.py"
    spec = importlib.util.spec_from_file_location("bootstrap_no_pass_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codex_hook_runs_no_engine(tmp_path, monkeypatch, capsys):
    cli = _load_cli()
    monkeypatch.setenv("BOOTSTRAP_NO_PASS", "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({
        "cwd": str(tmp_path), "session_id": "lane-one"})))
    monkeypatch.setattr(cli, "marketplaces", lambda: ["plugins-kit"])
    monkeypatch.setattr(cli, "plugin_data_dir", lambda _: str(tmp_path / "data"))
    monkeypatch.setattr(cli, "find_plugin_root", lambda *_: str(tmp_path / "plugin"))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: calls.append(a))
    assert cli.main(["--plugin-root", str(tmp_path / "plugin"), "codex-hook"]) == 0
    assert calls == []
    assert capsys.readouterr().out == ""
