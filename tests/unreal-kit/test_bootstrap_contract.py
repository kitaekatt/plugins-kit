"""Contract tests for unreal-kit's bootstrap manifest boundary."""

import json
import importlib.util
from pathlib import Path

from bootstrap_lib.manifest_merge import merge_manifests


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "plugins" / "unreal-kit" / "bootstrap.json"
CUSTOM_BOOTSTRAP_PATH = ROOT / "plugins" / "unreal-kit" / "custom_bootstrap.py"


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _load_custom_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "unreal_kit_custom_bootstrap_contract", CUSTOM_BOOTSTRAP_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_primary_unreal_capabilities_do_not_require_p4():
    """Python and MCP setup must not install a redirector-only VCS tool."""
    manifest = _manifest()

    assert all(tool.get("name") != "p4" for tool in manifest.get("tools", []))


def test_redirector_capability_declares_opt_in_p4_provider():
    """Redirector cleanup remains discoverably backed by the P4 owner plugin."""
    manifest = _manifest()

    declaration = next(
        item for item in manifest["plugins"]
        if item["ref"] == "plugins-kit:p4-kit"
    )
    assert declaration["install"] == "manual"


def test_p4_requirement_is_deferred_only_for_detected_workspace(
    tmp_path, monkeypatch
):
    """A P4 marker activates point-of-need guidance without affecting other projects."""
    custom_bootstrap = _load_custom_bootstrap()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".p4config").write_text("P4PORT=perforce:1666\n", encoding="utf-8")
    uproject = project / "Game.uproject"
    uproject.write_text("{}\n", encoding="utf-8")

    class Context:
        def __init__(self, project_dir, uproject):
            self.config = {"uproject": str(uproject)}
            self.project_dir = project_dir
            self.data_dir = tmp_path / "data"
            self.deferred = []
            self.logs = []

        def add_deferred_requirement(self, name, **kwargs):
            self.deferred.append({"name": name, **kwargs})

        def log(self, message):
            self.logs.append(message)

        def log_ok(self, message):
            self.logs.append(message)

    monkeypatch.setattr(custom_bootstrap.shutil, "which", lambda name: None)
    ctx = Context(project, uproject)
    custom_bootstrap.bootstrap(ctx)

    names = {item["name"] for item in ctx.deferred}
    assert "unreal_redirector_p4" in names

    non_p4 = tmp_path / "non-p4"
    non_p4.mkdir()
    non_p4_project = non_p4 / "Game.uproject"
    non_p4_project.write_text("{}\n", encoding="utf-8")
    ctx = Context(non_p4, non_p4_project)
    custom_bootstrap.bootstrap(ctx)
    assert "unreal_redirector_p4" not in {item["name"] for item in ctx.deferred}
    assert "redirectors: skipped - no Perforce workspace marker" in ctx.logs
