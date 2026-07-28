"""Shared paths for the workflow-kit test suite.

A plain uniquely-named module (NOT conftest): `from conftest import ...`
resolves to whichever suite's conftest.py lands first on sys.path when several
test directories run in one pytest invocation (e.g.
`pytest tests/workflow-kit tests/llm-scripting-kit`), which shadowed these names
with llm-scripting-kit's conftest.
"""

from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "workflow-kit"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = PLUGIN_ROOT / "examples"
