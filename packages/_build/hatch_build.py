"""Hatchling hooks shared by every distribution under packages/.

A registered distribution's pyproject.toml points both hooks here:

    [project]
    dynamic = ["urls"]

    [tool.hatch.metadata.hooks.custom]
    path = "../_build/hatch_build.py"

    [tool.hatch.build.targets.wheel.hooks.custom]
    path = "../_build/hatch_build.py"

The build hook writes the release marker (the registry entry's `marker` path)
from exactly three inputs -- RELEASE_TAG, RELEASE_COMMIT, RELEASE_TAG_URL --
and refuses to build when any is missing, so no wheel exists without a release
identity. The metadata hook sets [project.urls] Source to RELEASE_TAG_URL.
Neither reads the clock, the machine or the network: the same inputs give the
same bytes (packages/_build/release_build.py, "Build (B3)").
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.metadata.plugin.interface import MetadataHookInterface

_HERE = Path(__file__).resolve().parent


def _release_build():
    spec = importlib.util.spec_from_file_location(
        "_katsoft_release_build", _HERE / "release_build.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: dataclasses resolve their module by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_rb = _release_build()


class ReleaseMetadataHook(MetadataHookInterface):
    PLUGIN_NAME = "custom"

    def update(self, metadata: dict) -> None:
        identity = _rb.identity_from_env(os.environ)
        urls = dict(metadata.get("urls", {}))
        urls["Source"] = identity["RELEASE_TAG_URL"]
        metadata["urls"] = urls


class ReleaseBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            raise _rb.BuildError("packages/ distributions build wheels only")
        identity = _rb.identity_from_env(os.environ)
        registry = _rb.parse_registry((_HERE.parent / "registry.toml").read_text(encoding="utf-8"))
        entry = registry.entry(self.metadata.core.raw_name)
        dist_version = self.metadata.version
        _rb.check_identity(identity, entry.name, dist_version, registry.repository)
        self._marker_dir = tempfile.mkdtemp(prefix="release-marker-")
        marker = Path(self._marker_dir) / "marker.py"
        marker.write_bytes(_rb.render_marker(
            entry.name, dist_version, identity["RELEASE_TAG"],
            identity["RELEASE_COMMIT"], identity["RELEASE_TAG_URL"]).encode("utf-8"))
        build_data["force_include"][str(marker)] = entry.marker

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:
        shutil.rmtree(self._marker_dir)
