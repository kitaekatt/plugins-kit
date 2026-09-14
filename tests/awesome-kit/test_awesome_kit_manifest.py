"""Static manifest invariants for awesome-kit after pdf-kit extraction."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "awesome-kit"
    / "bootstrap.json"
)
_PLUGIN_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "awesome-kit"
    / ".claude-plugin"
    / "plugin.json"
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_awesome_kit_keeps_yaml_only_in_check_imports() -> None:
    manifest = _read_json(_MANIFEST)

    assert manifest["venv"]["check_imports"] == ["yaml"]
    assert "script" not in manifest


def test_awesome_kit_description_matches_remaining_skills() -> None:
    manifest = _read_json(_PLUGIN_MANIFEST)
    description = manifest["description"]

    # Deliberately NOT pinned to an exact version: this test guards the
    # description against drifting away from the skills that remain after the
    # pdf-kit extraction, and an exact-version assert would fail on every
    # routine bump while proving nothing about that.
    assert manifest["version"].count(".") == 2
    assert "html-pdf" not in description
    skills_dir = Path(__file__).resolve().parents[2] / "plugins" / "awesome-kit" / "skills"
    skill_files = sorted(skills_dir.glob("*/SKILL.md"))
    assert skill_files
    for skill_md in skill_files:
        frontmatter = yaml.safe_load(skill_md.read_text(encoding="utf-8").split("---", 2)[1])
        assert frontmatter["name"] in description, skill_md


def test_description_guard_rejects_missing_debug_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _read_json(_PLUGIN_MANIFEST)
    manifest["description"] = manifest["description"].replace("debug-context, ", "")
    copied_manifest = tmp_path / "plugin.json"
    copied_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(__import__(__name__), "_PLUGIN_MANIFEST", copied_manifest)
    with pytest.raises(AssertionError):
        test_awesome_kit_description_matches_remaining_skills()


def test_shipped_skill_md_is_ascii() -> None:
    skills_dir = Path(__file__).resolve().parents[2] / "plugins" / "awesome-kit" / "skills"
    skill_files = sorted(skills_dir.glob("*/SKILL.md"))
    assert skill_files
    for skill_md in skill_files:
        skill_md.read_bytes().decode("ascii")


def test_readme_defers_to_the_landing_page_recipe() -> None:
    plugin_root = Path(__file__).resolve().parents[2] / "plugins" / "awesome-kit"
    readme = (plugin_root / "README.md").read_text(encoding="utf-8")
    assert "--marketplace plugins-kit --output ./index.html" not in readme
    assert "skills/plugin-ecosystem/SKILL.md#generating-a-marketplaces-landing-page" in readme
    skill = (plugin_root / "skills" / "plugin-ecosystem" / "SKILL.md").read_text(encoding="utf-8")
    assert "### Generating a marketplace's landing page" in skill


def test_no_private_paths_in_shipped_files() -> None:
    # plugins/awesome-kit ships to every consumer's plugin cache, and this
    # repo is deliberately public, so no private repo name or home path may
    # appear in a tracked file under that plugin. The forbidden strings are
    # built from fragments so this test file's own scan does not flag
    # itself for containing the literal strings it is checking for.
    repo_root = Path(__file__).resolve().parents[2]
    # Identifiers that must never ship. Append to this tuple as more are
    # found; the fragments keep this file's own scan from flagging itself.
    forbidden = (
        "christina" + "-norman",
        "~" + "/Dev/",
        "home" + "assistant",
        "env" + "-config",
        "bra" + "via",
    )

    result = subprocess.run(
        ["git", "ls-files", "plugins/awesome-kit"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_files = [
        line.strip() for line in result.stdout.splitlines() if line.strip()
    ]
    assert tracked_files, "expected tracked files under plugins/awesome-kit"

    offenders: list[str] = []
    for rel_path in tracked_files:
        path = repo_root / rel_path
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lowered = text.lower()
        if any(needle.lower() in lowered for needle in forbidden):
            offenders.append(rel_path)

    assert not offenders, (
        "tracked files under plugins/awesome-kit contain a private "
        f"identifier that must not ship: {offenders}"
    )
