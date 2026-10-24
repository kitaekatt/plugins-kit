"""Consumer target import, offline validation, and publication dependency gates."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import _packages as targets

spec = importlib.util.spec_from_file_location("import_package_targets", ROOT / "scripts/import_package_targets.py")
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


def ledger(*versions: str) -> dict:
    return {"katsoft-example": {version: {"version": version, "tag": f"katsoft-example-v{version}",
        "commit": "b" * 40, "wheel_sha256": "c" * 64, "verified_at": "2026-01-01T00:00:00Z",
        "filename": f"katsoft_example-{version}-py3-none-any.whl"} for version in versions}}


def test_projection_keeps_latest_in_every_major():
    artifact = importer.project_targets(ledger("1.0.0", "1.7.0", "2.1.0"), "a" * 40)
    assert artifact["packages"] == {"katsoft-example": {"1.7.0": {"version": "1.7.0"}, "2.1.0": {"version": "2.1.0"}}}
    assert artifact["source"]["commit"] == "a" * 40


def test_unverified_entry_is_refused():
    data = ledger("1.0.0")
    del data["katsoft-example"]["1.0.0"]["wheel_sha256"]
    with pytest.raises(ValueError, match="unverified"):
        importer.project_targets(data, "a" * 40)


def test_yanked_or_hash_mismatched_wheel_is_refused(monkeypatch):
    import io
    data = ledger("1.0.0")["katsoft-example"]["1.0.0"]
    monkeypatch.setattr(importer.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps({"urls": [{
        "filename": data["filename"], "yanked": True, "digests": {"sha256": data["wheel_sha256"]}}]}).encode()))
    with pytest.raises(ValueError, match="non-yanked"):
        importer.verify_pypi("katsoft-example", "1.0.0", data)


class MemoryRepo:
    def __init__(self, requirement: str, artifact: dict):
        self.files = {targets.TARGETS_PATH: json.dumps(artifact), "plugins/test/pyproject.toml":
                      f"[project]\ndependencies = [{requirement!r}]\n"}

    def show(self, ref: str, path: str) -> str | None:
        return self.files.get(path)


def test_publish_uses_local_targets_and_refuses_unreleased_minimum(monkeypatch):
    artifact = importer.project_targets(ledger("1.7.0"), "a" * 40)
    def no_network(*args, **kwargs):
        raise AssertionError("publication validation must be offline")
    monkeypatch.setattr(importer.urllib.request, "urlopen", no_network)
    repo = MemoryRepo("katsoft-example>=1.0,<2", artifact)
    assert targets.plan(repo, "fixed", only=None, shipping_plugins={"test"}, master="master").release_set == []
    repo.files["plugins/test/pyproject.toml"] = "[project]\ndependencies = ['katsoft-example>=1.8,<2']\n"
    with pytest.raises(targets.PackageError, match="no imported released target"):
        targets.plan(repo, "fixed", only=None, shipping_plugins={"test"}, master="master")


def test_missing_schema_fails_loudly():
    repo = MemoryRepo("katsoft-example>=1.0,<2", {"katsoft-example": {"1.7.0": {"version": "1.7.0"}}})
    with pytest.raises(targets.PackageError, match="schema 1"):
        targets.read_targets(repo, "fixed")


@pytest.mark.parametrize("schema", [True, 1.0, "1"])
def test_schema_requires_an_exact_integer(schema):
    artifact = importer.project_targets(ledger("1.7.0"), "a" * 40)
    artifact["schema"] = schema
    repo = MemoryRepo("katsoft-example>=1.0,<2", artifact)
    with pytest.raises(targets.PackageError, match="schema 1"):
        targets.read_targets(repo, "fixed")


def test_target_only_change_is_publishable_without_plugin_bump(monkeypatch):
    spec = importlib.util.spec_from_file_location("publish_target_test", ROOT / "scripts/publish.py")
    publish = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish)
    monkeypatch.setattr(publish, "local_plugins", lambda **kwargs: {})
    assert publish._require_version_bump(publishable=True) == []


def test_fresh_publish_validation_needs_no_installed_bootstrap(tmp_path):
    """Run outside pytest's pythonpath, site packages, and all shared .pth files."""
    artifact = importer.project_targets(ledger("1.7.0"), "a" * 40)
    code = f'''import runpy
from types import SimpleNamespace
module = runpy.run_path({str(ROOT / "scripts/_packages.py")!r})
files = {{"data/package-targets.json": {json.dumps(artifact)!r},
         "plugins/test/pyproject.toml": "[project]\\ndependencies = ['katsoft-example>=1.0,<2']\\n"}}
repo = SimpleNamespace(show=lambda ref, path: files.get(path))
module["plan"](repo, "fixed", only=None, shipping_plugins={{"test"}}, master="master")
'''
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
