"""content_pipeline.__version__ equals the plugin manifest and pyproject versions."""

import json
import re
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit"


def test_version_matches_plugin_json():
    sys.path.insert(0, str(PLUGIN / "lib"))
    import content_pipeline

    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert content_pipeline.__version__ == manifest["version"]
    pyproject = (PLUGIN / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "%s"' % re.escape(manifest["version"]), pyproject, re.M)
