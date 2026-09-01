"""Static manifest and path invariants for pdf-kit."""

import json
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PDF_ROOT = _REPO_ROOT / "plugins" / "pdf-kit"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_pdf_kit_declares_bootstrap_dependency() -> None:
    manifest = _read_json(_PDF_ROOT / ".claude-plugin" / "plugin.json")

    assert manifest["dependencies"] == ["bootstrap"]


def test_html_pdf_skill_resolves_under_pdf_kit() -> None:
    skill_path = _PDF_ROOT / "skills" / "html-pdf" / "SKILL.md"
    old_skill_path = (
        _REPO_ROOT / "plugins" / "awesome-kit" / "skills" / "html-pdf" / "SKILL.md"
    )

    assert skill_path.is_file()
    assert not old_skill_path.exists()
    assert "CLAUDE_PLUGIN_ROOT" in skill_path.read_text(encoding="utf-8")


def test_pdf_kit_manifest_declares_venv_and_script() -> None:
    manifest = _read_json(_PDF_ROOT / "bootstrap.json")

    assert manifest["venv"]["check_imports"] == ["playwright", "pypdf"]
    assert manifest["script"] == {
        "path": "custom_bootstrap.py",
        "entry_point": "bootstrap",
    }
