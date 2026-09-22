"""Keep prototypes' empty manifest aligned with cross-plugin documentation."""

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOTYPES_MANIFEST = (
    REPO_ROOT / "plugins" / "prototypes" / ".claude-plugin" / "plugin.json"
)
MARKETPLACE_MANIFEST = REPO_ROOT / ".claude-plugin" / "marketplace.json"
ROOT_GUIDANCE = REPO_ROOT / "CLAUDE.md"
SKILLS_KIT_DOC = REPO_ROOT / "plugins" / "skills-kit" / "CLAUDE.md"
STABLE_CHANNEL_DOC = (
    REPO_ROOT / "docs" / "planning" / "bootstrap" / "stable-channel-design.md"
)
PROTOTYPES_README = REPO_ROOT / "plugins" / "prototypes" / "README.md"


def _prototypes_manifest() -> dict:
    return json.loads(PROTOTYPES_MANIFEST.read_text(encoding="utf-8"))


def _marketplace_manifest() -> dict:
    return json.loads(MARKETPLACE_MANIFEST.read_text(encoding="utf-8"))


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


def test_empty_prototypes_is_inactive_and_not_installable():
    """An empty package must not be published or invite a no-op install."""
    manifest = _prototypes_manifest()
    assert manifest["version"] == "0.4.1"
    assert manifest["published"] is False

    marketplace = _marketplace_manifest()
    assert all(plugin["name"] != "prototypes" for plugin in marketplace["plugins"])

    readme = PROTOTYPES_README.read_text(encoding="utf-8")
    assert "/plugin install prototypes" not in readme
    assert "not published" in readme.lower()

    root_guidance = ROOT_GUIDANCE.read_text(encoding="utf-8")
    assert "**prototypes** (inactive experimental nursery/archive)" in root_guidance


def test_prototypes_readme_describes_an_inactive_archive_without_install_commands():
    """The empty package README must direct readers to its archive status."""
    readme = PROTOTYPES_README.read_text(encoding="utf-8").lower()

    assert "inactive" in readme
    assert "archive" in readme
    assert "/plugin install prototypes" not in readme
    assert "/plugin marketplace add" not in readme


def test_root_roster_places_prototypes_with_dev_only_plugins():
    """The human-readable roster must agree with the manifest publication flag."""
    guidance = ROOT_GUIDANCE.read_text(encoding="utf-8")
    published_roster = guidance.split("Published plugins: ", 1)[1].split(
        " Dev-only (", 1
    )[0]
    dev_only_start = guidance.index("### Dev-only plugins -- do not publish to master")
    dev_only_end = guidance.index("Commits for a dev-only plugin need no action", dev_only_start)
    dev_only_roster = guidance[dev_only_start:dev_only_end]

    assert "**prototypes**" not in published_roster
    assert "- **prototypes**" in dev_only_roster
