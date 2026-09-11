"""Execute review consumers with only their declared dependencies, offline.

Copy installed distributions into private site-packages, then link bootstrap
source with a .pth file. Isolated Python omits the repository test environment.
This checks declaration closure, not dependency installation or version solving.
"""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import tomllib

from packaging.requirements import Requirement
import pytest


ROOT = Path(__file__).resolve().parents[2]


def _copy_dependencies(requirements: list[str], target: Path) -> None:
    """Copy only declared distributions and their active transitive requirements."""
    pending = list(requirements)
    copied: set[str] = set()
    while pending:
        requirement = Requirement(pending.pop())
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        distribution = importlib.metadata.distribution(requirement.name)
        name = distribution.metadata["Name"]
        if name in copied:
            continue
        assert not requirement.extras, "Extend this fixture before testing dependency extras"
        assert distribution.version in requirement.specifier
        copied.add(name)
        pending.extend(distribution.requires or [])
        assert distribution.files, f"No installed file manifest for {name}"
        for relative in distribution.files:
            if ".." in relative.parts or "__pycache__" in relative.parts:
                continue
            source = Path(distribution.locate_file(relative))
            if source.is_file():
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)


def _assemble_in_consumer(
    plugin: str, tmp_path: Path, *, omit: str | None = None,
) -> subprocess.CompletedProcess[str]:
    project = tomllib.loads(
        (ROOT / "plugins" / plugin / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    requirements = [
        req for req in project["dependencies"] if Requirement(req).name != omit
    ]
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    _copy_dependencies(requirements, site_packages)
    (site_packages / "bootstrap.pth").write_text(
        str(ROOT / "plugins" / "bootstrap") + "\n", encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    config = workspace / ".claude" / "mechanical_checks.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "checks:\n  - id: closure_probe\n    phrase: closure probe\n"
        "    pattern: closure_marker\n    applies_to: ['*.txt']\n",
        encoding="utf-8",
    )
    program = textwrap.dedent("""\
        from pathlib import Path
        import site
        import sys

        site.addsitedir(sys.argv[1])
        from bootstrap_lib.code_review.pipeline import assemble_bundle

        # Pipeline import alone succeeds without regex; config resolution must run.
        print('pipeline imported', flush=True)
        root = Path(sys.argv[2])
        result = assemble_bundle(
            preamble='',
            sections=[{'identifier': 'probe.txt',
                       'text': '@@ -0,0 +1 @@\\n+closure_marker\\n'}],
            files=[{'identifier': 'probe.txt', 'local': None}],
            bundle_dir=root / 'bundle', max_chunk_bytes=4096,
            workspace_root=root,
        )
        findings = result['diff_chunks'][0]['mechanical_scan']['files'][0]['findings']
        assert any(f['check'] == 'closure_probe' for f in findings), result
        assert len(result['diff_chunks']) == 1, result
        print('bundle assembled')
    """)
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, str(site_packages), str(workspace)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
        check=False,
    )


@pytest.mark.parametrize("plugin", ["git-kit", "p4-kit"])
def test_declared_consumer_dependencies_assemble_bundle(plugin: str, tmp_path: Path) -> None:
    result = _assemble_in_consumer(plugin, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "bundle assembled" in result.stdout


@pytest.mark.parametrize("plugin", ["git-kit", "p4-kit"])
@pytest.mark.parametrize("dependency", ["regex", "markdown-it-py"])
def test_consumer_closure_rejects_missing_dependency(
    plugin: str, dependency: str, tmp_path: Path,
) -> None:
    result = _assemble_in_consumer(plugin, tmp_path, omit=dependency)
    assert result.returncode != 0
    import_name = "markdown_it" if dependency == "markdown-it-py" else dependency
    assert f"No module named '{import_name}'" in result.stderr
    if dependency == "regex":
        assert "pipeline imported" in result.stdout
