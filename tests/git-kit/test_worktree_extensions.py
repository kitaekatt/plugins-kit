"""Lifecycle script contracts.

Run: uv run --extra dev pytest tests/git-kit/test_worktree_extensions.py -q.
Triggers: failed lifecycle scripts, declared background processes, and relocation.
All script effects and launcher links use the lifecycle fixture's temporary home.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from test_worktree_lifecycle import git, holder, lifecycle, write_json


def configured(obj: Any, phase: str, code: str) -> Any:
    """Commit repository-owned commands, then reload the lifecycle settings."""
    import worktree_lifecycle as lc

    config = dict(obj.settings.raw)
    config[phase] = [[sys.executable, "-c", code]]
    write_json(obj.repo / ".claude/bootstrap.json", {"git_kit": {"worktrees": config}})
    git(obj.repo, "add", ".claude/bootstrap.json")
    git(obj.repo, "commit", "-qm", f"configure {phase}")
    git(obj.repo, "push", "origin", "main")
    return lc.Lifecycle(obj.repo)


@pytest.mark.parametrize("phase", ["after_take", "before_release"])
def test_extension_failures_preserve_claim(lifecycle, phase: str) -> None:
    obj = configured(lifecycle, phase, "import sys; print('script failed', flush=True); sys.exit(9)")
    obj.maintain()
    if phase == "after_take":
        with pytest.raises(ValueError, match="after_take.*log|log.*after_take"):
            obj.take("script-failure", "s", process=holder())
    else:
        taken = obj.take("script-failure", "s", process=holder())
        with pytest.raises(ValueError, match="before_release.*log|log.*before_release"):
            obj.release(taken["claim"])
    record = next(r for r in obj.read_records() if (r.get("holder") or {}).get("agent") == "script-failure")
    assert record["state"] == "taken"
    assert record["holder"]["claim"]
    assert Path(record["path"]).is_dir()
    run = record["extension"][phase][-1]
    assert run["exit"] == 9 and run["outcome"] == "failed"
    assert record["error"]["exit"] == 9
    assert record["error"]["log"] == run["log"]
    assert "script failed" in Path(run["log"]).read_text(encoding="utf-8")


def test_extension_processes_block_retirement(lifecycle) -> None:
    identity = holder()
    code = (
        "import json,os; from pathlib import Path; "
        f"identity={identity!r}; identity['runtime_paths']=[str(Path.cwd()/'output')]; "
        "Path(os.environ['GIT_KIT_EXTENSION_RESULT']).write_text(json.dumps({"
        "'processes':[identity], 'resources':[{'project_slot':'alpha'}], 'result':{'prepared':True}}))"
    )
    obj = configured(lifecycle, "after_take", code)
    obj.maintain()
    taken = obj.take("process-owner", "s", process=identity)
    record = obj.get(taken["id"])
    assert record["processes"] == [{**identity, "runtime_paths": [str(Path(taken["path"]) / "output")]}]
    run = record["extension"]["after_take"][-1]
    assert run["resources"] == [{"project_slot": "alpha"}]
    assert run["result"] == {"prepared": True}
    with pytest.raises(ValueError, match="live.*declared process"):
        obj.release(taken["claim"])
    observed = next(r for r in obj.status()["records"] if r["id"] == taken["id"])["observed"]
    assert any("declared process" in blocker for blocker in observed["process_blockers"])
    record = obj.get(taken["id"])
    assert record["holder"]["claim"] == taken["claim"]
    record.update(state="parked", holder=None)
    obj.save(record)
    obj.maintain()
    assert obj.get(taken["id"])["state"] == "parked"
    assert Path(taken["path"]).is_dir()


def test_relocation_preserves_global_launchers(lifecycle) -> None:
    launchers = Path.home() / ".local/bin"
    launchers.mkdir(parents=True)
    target = lifecycle.repo / "launcher"
    target.write_text("project launcher", encoding="utf-8")
    links = [launchers / name for name in ("afterplay-dev", "afterplay-pb")]
    for link in links:
        link.symlink_to(target)
    before = {link: os.readlink(link) for link in links}
    code = (
        "import json,os; from pathlib import Path; "
        "Path('output/extension-path').write_text(str(Path.cwd())); "
        "Path(os.environ['GIT_KIT_EXTENSION_RESULT']).write_text(json.dumps({'result':{'cwd':str(Path.cwd())}}))"
    )
    obj = configured(lifecycle, "after_take", code)
    obj.maintain()
    taken = obj.take("relocated", "s", process=holder())
    assert (Path(taken["path"]) / "output/extension-path").read_text() == taken["path"]
    assert (Path(taken["path"]) / "output/cwd").read_text() == taken["path"]
    assert {link: os.readlink(link) for link in links} == before
    record = obj.get(taken["id"])
    assert record["extension"]["after_take"][-1]["result"]["cwd"] == taken["path"]
    record.update(state="parked", holder=None, last_holder=record["holder"], waiting="relocation check")
    obj.save(record)
    adopted = obj.adopt(taken["id"], "relocated-again", "s2", process=holder())
    assert adopted["path"] != taken["path"]
    assert (Path(adopted["path"]) / "output/extension-path").read_text() == adopted["path"]
    assert (Path(adopted["path"]) / "output/cwd").read_text() == adopted["path"]
    assert {link: os.readlink(link) for link in links} == before


@pytest.mark.parametrize("report", ["{'processes':'bad'}", "{'processes':[], 'unknown':True}", "None"])
def test_invalid_extension_reports_preserve_claim(lifecycle, monkeypatch, report: str) -> None:
    import worktree_migration as migration
    monkeypatch.setattr(migration, "claude_processes", lambda: [])
    code = (
        "import json,os; from pathlib import Path; "
        f"Path(os.environ['GIT_KIT_EXTENSION_RESULT']).write_text(json.dumps({report}))"
    )
    obj = configured(lifecycle, "after_take", code)
    obj.maintain()
    with pytest.raises(ValueError, match="after_take.*log"):
        obj.take("bad-report", "s", process=holder())
    record = next(r for r in obj.read_records() if (r.get("holder") or {}).get("agent") == "bad-report")
    assert record["holder"]["claim"] and Path(record["path"]).exists()
    assert record["extension"]["after_take"][-1]["outcome"] == "failed"
    record["holder"].update(pid=99999999, start="exited", executable="claude", command="claude")
    obj.save(record)
    obj.maintain()
    assert obj.get(record["id"])["state"] == "parked"
    assert Path(record["path"]).is_dir()


def test_empty_report_cannot_erase_prior_processes(lifecycle) -> None:
    identity = holder()
    code = (
        "import json,os; from pathlib import Path; "
        f"report={dict(processes=[identity])!r}; "
        "Path(os.environ['GIT_KIT_EXTENSION_RESULT']).write_text(json.dumps(report))"
    )
    obj = configured(lifecycle, "after_take", code)
    obj = configured(obj, "before_release", "pass")
    obj.maintain()
    taken = obj.take("process-history", "s", process=identity)
    record = obj.get(taken["id"])
    assert record["processes"] == [{**identity, "runtime_paths": []}]
    with pytest.raises(ValueError, match="live.*declared process"):
        obj.release(taken["claim"])
    assert obj.get(taken["id"])["processes"] == record["processes"]
    assert obj.get(taken["id"])["extension"]["before_release"][-1]["outcome"] == "complete"


@pytest.mark.parametrize("commands", [[[]], [["bad\0arg"]], ["shell text"]])
def test_extension_commands_validate_at_boundary(lifecycle, commands: list) -> None:
    from worktree_config import validate_config

    with pytest.raises(ValueError):
        validate_config({**lifecycle.settings.raw, "after_take": commands}, lifecycle.repo)


@pytest.mark.parametrize("operation", ["maintain", "adopt", "discard"])
def test_interrupted_extension_report_blocks_lifecycle(lifecycle, operation: str) -> None:
    obj = lifecycle
    obj.maintain()
    taken = obj.take("interrupted", "s", process=holder())
    record = obj.get(taken["id"])
    result_path = obj.state / "extensions/interrupted.json"
    write_json(result_path, {"processes": [{**holder(), "runtime_paths": [str(obj.repo)]}],
                             "resources": [{"project_slot": "still-owned"}]})
    record.update(state="parked", holder=None, worker=None,
                  extension={"after_take": [{"outcome": "running", "result_path": str(result_path),
                                             "log": str(obj.state / "logs/interrupted.log")} ]})
    obj.save(record)
    if operation == "maintain":
        obj.maintain()
        assert obj.get(taken["id"])["state"] == "parked"
        parked = next(r for r in obj.parked()["parked"] if r["id"] == taken["id"])
        assert any("uncertain extension" in blocker for blocker in parked["observed"]["process_blockers"])
    elif operation == "adopt":
        with pytest.raises(ValueError, match="uncertain.*extension"):
            obj.adopt(taken["id"], "intruder", "s2", process=holder())
    else:
        with pytest.raises(ValueError, match="uncertain.*extension"):
            obj.discard(taken["id"])
    assert Path(taken["path"]).is_dir()
    assert result_path.is_file()


def test_before_release_runs_outside_claim_lock(lifecycle) -> None:
    script_dir = Path(__file__).resolve().parents[2] / "plugins/git-kit/scripts"
    code = (
        "import os,sys; from pathlib import Path; "
        f"sys.path.insert(0,{str(script_dir)!r}); "
        "from worktree_pool import _file_lock; "
        "lock=Path(os.environ['GIT_KIT_EXTENSION_RESULT']).parents[1]/'claim.lock'; "
        "context=_file_lock(lock,blocking=False); held=context.__enter__(); "
        "context.__exit__(None,None,None); sys.exit(0 if held else 19)"
    )
    obj = configured(lifecycle, "before_release", code)
    obj.maintain()
    taken = obj.take("release-lock", "s", process=holder())
    assert obj.release(taken["claim"])["state"] == "released"


@pytest.mark.parametrize("output", ["malformed", "dangling"])
def test_unreadable_extension_report_blocks_retirement(lifecycle, monkeypatch, output: str) -> None:
    import worktree_migration as migration
    monkeypatch.setattr(migration, "claude_processes", lambda: [])
    code = "import os; from pathlib import Path; result=Path(os.environ['GIT_KIT_EXTENSION_RESULT']); "
    code += "result.write_text('{unfinished')" if output == "malformed" else "result.symlink_to(result.parent/'missing')"
    obj = configured(lifecycle, "after_take", code)
    obj.maintain()
    with pytest.raises(ValueError, match="after_take.*log"):
        obj.take("unreadable-report", "s", process=holder())
    record = next(r for r in obj.read_records() if (r.get("holder") or {}).get("agent") == "unreadable-report")
    assert record["extension"]["after_take"][-1]["report_uncertain"] is True
    record["holder"].update(pid=99999999, start="exited", executable="claude", command="claude")
    obj.save(record)
    obj.maintain()
    assert obj.get(record["id"])["state"] == "parked"
    assert Path(record["path"]).is_dir()
