"""The plugin manifest and pyproject versions agree; __version__ is frozen, not tracked.

content-pipeline-kit is a frozen provider (docs/planning/llm-kit/migration-plan.md
section 5): a frozen-plugin fix is an installability, security or correctness
release that changes pyproject.toml and plugin.json and leaves the package
``__version__`` constant alone, because tests/llm-kit/repoint-map.json pins that
constant for the facade. ``__version__`` therefore records the release that
froze the runtime code, and may trail the manifest but never exceed it.
"""

import json
import re
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit"


def _tuple(version):
    return tuple(int(part) for part in version.split("."))


def test_version_matches_plugin_json():
    sys.path.insert(0, str(PLUGIN / "lib"))
    import content_pipeline

    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    pyproject = (PLUGIN / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "%s"' % re.escape(manifest["version"]), pyproject, re.M)
    assert _tuple(content_pipeline.__version__) <= _tuple(manifest["version"])
