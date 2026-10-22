"""Shared helpers for tests/contracts: module loading, golden comparison, wheel build."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import contract_cases
import contract_goldens

REPO = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO / "packages" / "katsoft-contracts"
CANONICAL_DIR = PACKAGE_DIR / "src" / "katsoft_contracts"
COPY_DIR = REPO / "plugins" / "bootstrap" / "bootstrap_lib"
SYNC_SCRIPT = REPO / "scripts" / "sync_contracts_copy.py"
RELEASE_BUILD = REPO / "packages" / "_build" / "release_build.py"
MODULES = ("execution_event", "interrupt_contract", "model_declaration")

DIST = "katsoft-contracts"
VERSION = "1.0.0"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
EPOCH = "1767225600"
SYNTHETIC_REGISTRY = (
    'schema = 1\n'
    'repository = "https://github.com/kitaekatt/plugins-kit"\n\n'
    '[[package]]\n'
    f'name = "{DIST}"\n'
    'import = "katsoft_contracts"\n'
    'marker = "katsoft_contracts/_release.py"\n'
    'sources = []\n'
    'surface = { all = "packages/katsoft-contracts/src/katsoft_contracts/__init__.py" }\n'
)


def load_file(path: Path, name: str):
    """Import one file as a standalone module (registered, for dataclasses)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_modules(directory: Path, tag: str, patch: tuple[str, str] | None = None) -> SimpleNamespace:
    """The three contract modules from `directory`, optionally with one source edit.

    `patch` is (old, new): the text is replaced in every module and the edit
    must land, so a mutant test cannot pass by patching nothing.
    """
    if patch is not None:
        assert any(patch[0] in (directory / f"{n}.py").read_text(encoding="utf-8") for n in MODULES), \
            f"patch text {patch[0]!r} occurs in no module"
    loaded = {}
    for name in MODULES:
        text = (directory / f"{name}.py").read_text(encoding="utf-8")
        if patch is not None:
            text = text.replace(*patch)
        spec = importlib.util.spec_from_loader(f"_contract_{tag}_{name}", loader=None)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        exec(compile(text, str(directory / f"{name}.py"), "exec"), module.__dict__)
        loaded[name] = module
    return SimpleNamespace(**loaded)


def golden_mismatches(mods: SimpleNamespace, scratch: Path) -> list[str]:
    """Case names whose outcome differs from the frozen golden, plus set differences."""
    actual = contract_cases.run_all(contract_cases.Ctx(mods, scratch))
    frozen = contract_goldens.GOLDENS
    problems = [f"no golden for case {n}" for n in sorted(set(actual) - set(frozen))]
    problems += [f"golden without a case {n}" for n in sorted(set(frozen) - set(actual))]
    # repr, not ==, so tuple-vs-list and True-vs-1 differences are mismatches.
    problems += [n for n in actual if n in frozen and repr(actual[n]) != repr(frozen[n])]
    return problems


# --- wheel -------------------------------------------------------------------

def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", "dist", "build", ".venv", "*.egg-info"))


def release_build_module():
    return load_file(RELEASE_BUILD, "_contract_tests_release_build")


def build_wheel(scratch: Path, out_name: str = "out") -> Path:
    """Build the package the way the release machinery does, in `scratch`.

    The working tree is copied into a throwaway repo-shaped directory with a
    synthetic registry entry (the real registry is empty while the package is
    dormant) and built with the shared hook, pinned build constraints,
    SOURCE_DATE_EPOCH and the three release-identity variables.
    """
    rb = release_build_module()
    registry = rb.parse_registry(SYNTHETIC_REGISTRY)
    entry = registry.entry(DIST)
    root = scratch / "repo"
    (root / "packages").mkdir(parents=True)
    _copy_tree(PACKAGE_DIR, root / "packages" / DIST)
    _copy_tree(REPO / "packages" / "_build", root / "packages" / "_build")
    (root / "packages" / "registry.toml").write_text(SYNTHETIC_REGISTRY, encoding="utf-8")
    tag = rb.tag_name(DIST, VERSION)
    env = dict(os.environ)
    env.update({"SOURCE_DATE_EPOCH": EPOCH, "RELEASE_TAG": tag, "RELEASE_COMMIT": COMMIT,
                "RELEASE_TAG_URL": rb.tag_url(registry.repository, tag)})
    out = scratch / out_name
    result = subprocess.run(
        ["uv", "build", "--wheel", "--no-config", "--python", rb.BUILD_PYTHON,
         "--build-constraint", str(root / "packages" / "_build" / "constraints.txt"),
         "--out-dir", str(out), str(root / "packages" / DIST)],
        capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"uv build failed:\n{result.stdout}\n{result.stderr}"
    wheels = sorted(out.glob("*.whl"))
    assert [w.name for w in wheels] == [rb.wheel_filename(DIST, VERSION)], wheels
    return wheels[0]


EXPECTED_MEMBERS = frozenset({
    "katsoft_contracts/__init__.py",
    "katsoft_contracts/_release.py",
    "katsoft_contracts/execution_event.py",
    "katsoft_contracts/interrupt_contract.py",
    "katsoft_contracts/model_declaration.py",
    f"katsoft_contracts-{VERSION}.dist-info/METADATA",
    f"katsoft_contracts-{VERSION}.dist-info/RECORD",
    f"katsoft_contracts-{VERSION}.dist-info/WHEEL",
    f"katsoft_contracts-{VERSION}.dist-info/licenses/LICENSE",
})


def wheel_problems(wheel: Path) -> list[str]:
    """Every way `wheel` departs from the contracts distribution's contract."""
    rb = release_build_module()
    registry = rb.parse_registry(SYNTHETIC_REGISTRY)
    entry = registry.entry(DIST)
    tag = rb.tag_name(DIST, VERSION)
    url = rb.tag_url(registry.repository, tag)
    problems: list[str] = []
    try:
        rb.check_wheel(wheel, entry, VERSION, tag, COMMIT, url)
    except rb.BuildError as exc:
        problems.append(f"release wheel-content test: {exc}")
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        if names != EXPECTED_MEMBERS:
            problems.append(f"members differ: extra {sorted(names - EXPECTED_MEMBERS)}, "
                            f"missing {sorted(EXPECTED_MEMBERS - names)}")
        for module in MODULES:
            member = f"katsoft_contracts/{module}.py"
            if member in names and zf.read(member) != (CANONICAL_DIR / f"{module}.py").read_bytes():
                problems.append(f"{member} is not the canonical source")
        meta_name = f"katsoft_contracts-{VERSION}.dist-info/METADATA"
        if meta_name in names:
            meta = zf.read(meta_name).decode("utf-8")
            if "Requires-Dist:" in meta:
                problems.append("wheel declares dependencies; the package is stdlib-only")
            if "Requires-Python: >=3.12" not in meta:
                problems.append("Requires-Python is not >=3.12")
            if "License-Expression: MIT" not in meta:
                problems.append("License-Expression is not MIT")
        for member in sorted(names):
            if member.endswith(".py"):
                problems += [f"{member}: {p}" for p in third_party_imports(zf.read(member).decode("utf-8"))]
    return problems


def third_party_imports(source: str) -> list[str]:
    """Top-level imports in `source` that are not standard library (or __future__)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots = [node.module.split(".")[0]]
        else:
            continue
        found += [f"imports non-standard-library module {r!r}" for r in roots
                  if r not in sys.stdlib_module_names]
    return found


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
