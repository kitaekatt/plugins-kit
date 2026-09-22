"""Contract tests for unreal-kit's bootstrap manifest boundary."""

import json
from pathlib import Path

from bootstrap_lib.manifest_merge import merge_manifests


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "plugins" / "unreal-kit" / "bootstrap.json"


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_project_only_unreal_kit_consumer_scope_survives_manifest_merge():
    """A plugin manifest must not turn a consumer's project choice into user scope."""
    manifest = _manifest()
    consumer = {
        "plugins": [{
            "ref": "plugins-kit:unreal-kit",
            "enabled": True,
            "scope": "project",
        }],
    }

    merged = merge_manifests(consumer, manifest)

    declaration = next(
        item for item in merged["plugins"]
        if item["ref"] == "plugins-kit:unreal-kit"
    )
    assert declaration["scope"] == "project"


def test_removing_self_declaration_preserves_unreal_bootstrap_declarations():
    """The scope fix must leave the plugin's other bootstrap requirements intact."""
    manifest = _manifest()
    consumer = {
        "plugins": [{
            "ref": "plugins-kit:unreal-kit",
            "enabled": True,
            "scope": "project",
        }],
    }

    merged = merge_manifests(consumer, manifest)

    for key in (
        "$comment",
        "requires_bootstrap",
        "tools",
        "venv",
        "shared_lib_imports",
        "project_config",
        "config",
        "ini_settings",
        "git_deps",
        "sync_to_data",
        "pypi_packages",
        "script",
    ):
        assert merged[key] == manifest[key]
