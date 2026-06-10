import sys

import pytest

from wk_testlib import PLUGIN_ROOT

# Make workflow_kit_lib importable when these tests run under the repo-root pytest.
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


@pytest.fixture
def write_workflow(tmp_path):
    """Write workflow YAML text to a temp .workflow.yaml file and return its path."""

    def _write(text, name="wf.workflow.yaml"):
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        return p

    return _write
