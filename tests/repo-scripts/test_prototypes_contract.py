"""Keep prototypes' empty manifest aligned with cross-plugin documentation."""

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOTYPES_MANIFEST = (
    REPO_ROOT / "plugins" / "prototypes" / ".claude-plugin" / "plugin.json"
)
SKILLS_KIT_DOC = REPO_ROOT / "plugins" / "skills-kit" / "CLAUDE.md"
STABLE_CHANNEL_DOC = (
    REPO_ROOT / "docs" / "planning" / "bootstrap" / "stable-channel-design.md"
)


def _prototypes_manifest() -> dict:
    return json.loads(PROTOTYPES_MANIFEST.read_text(encoding="utf-8"))


def _audit_framework_contract(doc: str) -> str:
    start = doc.index("    - id: audit_framework_paths_are_cross_plugin_api")
    end = doc.index("    - rule:", start)
    return doc[start:end]


def test_prototypes_without_skills_kit_dependency_has_no_verified_edge():
    """A manifest without skills-kit cannot support a documented edge."""
    manifest = _prototypes_manifest()
    assert "skills-kit" not in manifest.get("dependencies", [])

    stable_channel_doc = STABLE_CHANNEL_DOC.read_text(encoding="utf-8")
    assert "prototypes -> skills-kit" not in stable_channel_doc


def test_audit_framework_literal_path_consumers_are_actual_consumers():
    """The path-contract consumer list names only plugins that reference it."""
    manifest = _prototypes_manifest()
    assert "skills-kit" not in manifest.get("dependencies", [])

    contract = _audit_framework_contract(
        SKILLS_KIT_DOC.read_text(encoding="utf-8")
    )
    assert "awesome-kit" in contract
    assert "prototypes" not in contract
