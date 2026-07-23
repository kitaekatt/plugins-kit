"""Guards the content_pipeline skeleton: every subpackage must import cleanly.

The library is currently a scaffold (docstring-only modules and obvious
function-signature stubs that raise NotImplementedError on call). This test
does not exercise behavior -- it exercises the import graph: importing any
one subpackage must not accidentally drag in another (see the top-level
package docstring's "strict DAG" claim) and must not raise at import time.
"""

import ast
import importlib
from pathlib import Path

import pytest

_SUBPACKAGES = [
    "content_pipeline",
    "content_pipeline.config",
    "content_pipeline.config.loader",
    "content_pipeline.store",
    "content_pipeline.store.attributed",
    "content_pipeline.store.intermediary",
    "content_pipeline.store.candidate",
    "content_pipeline.store.projection",
    "content_pipeline.freshness",
    "content_pipeline.freshness.hashing",
    "content_pipeline.freshness.classify",
    "content_pipeline.freshness.tier",
    "content_pipeline.freshness.ensure",
    "content_pipeline.freshness.seed",
    "content_pipeline.validate",
    "content_pipeline.validate.contract",
    "content_pipeline.validate.riders",
    "content_pipeline.validate.floor_guard",
    "content_pipeline.providers",
    "content_pipeline.providers.registry",
    "content_pipeline.providers.assembly",
    "content_pipeline.llm",
    "content_pipeline.llm.platform",
    "content_pipeline.llm.backends",
    "content_pipeline.llm.convergence",
    "content_pipeline.pipeline",
    "content_pipeline.pipeline.stage",
    "content_pipeline.pipeline.single_pass",
    "content_pipeline.pipeline.convergence_loop",
    "content_pipeline.pipeline.workunit",
    "content_pipeline.deliver",
    "content_pipeline.deliver.inplace",
    "content_pipeline.deliver.projection",
    "content_pipeline.vcs",
    "content_pipeline.vcs.seam",
    "content_pipeline.vcs.null_vcs",
    "content_pipeline.vcs.git_vcs",
    "content_pipeline.roundtrip",
    "content_pipeline.roundtrip.questions",
    "content_pipeline.roundtrip.returns",
    "content_pipeline.audit",
    "content_pipeline.audit.auditor",
    "content_pipeline.audit.reasoning_chain",
    "content_pipeline.audit.report",
    "content_pipeline.cli",
    "content_pipeline.cli.scaffold",
    "content_pipeline.cli.budget",
    "content_pipeline.cli.bulk",
    "content_pipeline.cli.unsupported",
]


@pytest.mark.parametrize("module_name", _SUBPACKAGES)
def test_module_imports(plugin_root, module_name):
    importlib.import_module(module_name)


def test_top_level_package_reexports_nothing(plugin_root):
    """content_pipeline/__init__.py declares submodule-imports-only; enforce it.

    Note: ``dir(content_pipeline)`` is not a reliable check here -- once any
    test in the session imports a submodule (e.g. ``content_pipeline.store``),
    Python attaches it as an attribute of the parent package regardless of
    whether ``__init__.py`` imported it. The real invariant is source-level:
    ``__init__.py`` contains no ``from .x import y`` / ``import .x`` lines.
    """
    init_path = Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit" / "lib" / "content_pipeline" / "__init__.py"
    source = init_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    import_nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert import_nodes == [], (
        f"content_pipeline/__init__.py must not import any submodule; found: {import_nodes}"
    )


def test_null_vcs_satisfies_seam_shape(plugin_root):
    """The one concrete implementation in the skeleton -- NullVcs -- must be usable."""
    from content_pipeline.vcs.null_vcs import NullVcs

    backend = NullVcs()
    backend.open_for_edit("some/path")
    backend.add("some/path")
    cs = backend.make_changeset("test changeset")
    backend.move_into(cs, ["some/path"])
    backend.finalize_description(cs, "test changeset")
    backend.revert("some/path")
    backend.delete_if_empty(cs)
