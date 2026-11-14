"""skills_kit_lib.include_dirs: the shared --include-dir / env resolution."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins" / "skills-kit"))

from skills_kit_lib import include_dirs as mod  # noqa: E402


def test_cli_wins(monkeypatch):
    monkeypatch.setenv(mod.INCLUDE_DIRS_ENV, "fromenv")
    assert mod.resolve_include_dirs(["fromcli"]) == ["fromcli"]


def test_env_fallback_splits_and_drops_empties(monkeypatch):
    monkeypatch.setenv(mod.INCLUDE_DIRS_ENV, "a" + os.pathsep + os.pathsep + "b")
    assert mod.resolve_include_dirs(None) == ["a", "b"]


def test_unset_is_empty(monkeypatch):
    monkeypatch.delenv(mod.INCLUDE_DIRS_ENV, raising=False)
    assert mod.resolve_include_dirs([]) == []


def test_discover_scripts_share_the_helper():
    scripts = Path(__file__).resolve().parents[2] / "plugins/skills-kit/skills/md-domain/scripts"
    for name in ("discover_claude_md", "discover_project_doc", "discover_skill"):
        text = (scripts / f"{name}.py").read_text(encoding="utf-8")
        assert "from skills_kit_lib.include_dirs import" in text, name
        assert "os.environ.get(INCLUDE_DIRS_ENV)" not in text, name
