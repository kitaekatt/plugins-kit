"""Shared helpers for the katsoft-llm-kit distribution tests.

The wheel is built the way the release machinery builds it
(packages/_build/release_build.py: shared hatchling hook, pinned build
constraints, SOURCE_DATE_EPOCH, release identity) from a throwaway copy of the
working tree with a synthetic registry entry. The real registry stays empty
while the package is dormant. One build per test session, removed at exit.

A built wheel is a valid site directory once unzipped (its dist-info carries
METADATA and RECORD), so the runtime-mode tests run a subprocess against an
unpacked wheel instead of installing anything.
"""
from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO / "packages" / "katsoft-llm-kit"
LIB = PACKAGE_DIR / "src"
SOURCE_DIR = LIB / "llm_kit"
CONTRACTS_SRC = REPO / "packages" / "katsoft-contracts" / "src"
RELEASE_BUILD = REPO / "packages" / "_build" / "release_build.py"
PLUGIN_PYPROJECT = REPO / "plugins" / "llm-kit" / "pyproject.toml"
DIST = "katsoft-llm-kit"
DIST_INFO_STEM = "katsoft_llm_kit"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
EPOCH = "1767225600"
SYNTHETIC_REGISTRY = (
    'schema = 1\n'
    'repository = "https://github.com/kitaekatt/plugins-kit"\n\n'
    '[[package]]\n'
    f'name = "{DIST}"\n'
    'import = "llm_kit"\n'
    'marker = "llm_kit/_deployment.py"\n'
    'sources = []\n'
    'surface = { json = "plugins/llm-kit/contract/public-surface.json" }\n'
)

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "dist", "build", ".venv", "*.egg-info")
_CACHE: dict[str, Path] = {}


def release_build_module():
    spec = importlib.util.spec_from_file_location("_dist_tests_release_build", RELEASE_BUILD)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def package_version() -> str:
    import tomllib
    return tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _uv_build(scratch: Path, kind: str, out_name: str):
    """`uv build --<kind>` over a throwaway copy of the package, the shared
    hook directory and a synthetic registry, with the release identity set."""
    rb = release_build_module()
    registry = rb.parse_registry(SYNTHETIC_REGISTRY)
    root = scratch / "repo"
    (root / "packages").mkdir(parents=True)
    shutil.copytree(PACKAGE_DIR, root / "packages" / DIST, ignore=_IGNORE)
    shutil.copytree(REPO / "packages" / "_build", root / "packages" / "_build", ignore=_IGNORE)
    (root / "packages" / "registry.toml").write_text(SYNTHETIC_REGISTRY, encoding="utf-8")
    tag = rb.tag_name(DIST, package_version())
    env = dict(os.environ)
    env.update({"SOURCE_DATE_EPOCH": EPOCH, "RELEASE_TAG": tag, "RELEASE_COMMIT": COMMIT,
                "RELEASE_TAG_URL": rb.tag_url(registry.repository, tag)})
    out = scratch / out_name
    result = subprocess.run(
        ["uv", "build", f"--{kind}", "--no-config", "--python", rb.BUILD_PYTHON,
         "--build-constraint", str(root / "packages" / "_build" / "constraints.txt"),
         "--out-dir", str(out), str(root / "packages" / DIST)],
        capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"uv build failed:\n{result.stdout}\n{result.stderr}"
    return rb, out


def build_wheel(scratch: Path, out_name: str = "out") -> Path:
    """Build the distribution the way the release machinery does, in `scratch`."""
    rb, out = _uv_build(scratch, "wheel", out_name)
    wheels = sorted(out.glob("*.whl"))
    assert [w.name for w in wheels] == [rb.wheel_filename(DIST, package_version())], wheels
    return wheels[0]


def build_sdist(scratch: Path, out_name: str = "out") -> Path:
    """Build the source distribution from the same staged copy, in `scratch`."""
    _rb, out = _uv_build(scratch, "sdist", out_name)
    archives = sorted(out.glob("*.tar.gz"))
    assert [a.name for a in archives] == [f"{DIST_INFO_STEM}-{package_version()}.tar.gz"], archives
    return archives[0]


def _session_dir() -> Path:
    if "dir" not in _CACHE:
        base = Path(tempfile.mkdtemp(prefix="llm-kit-dist-"))
        atexit.register(shutil.rmtree, base, ignore_errors=True)
        _CACHE["dir"] = base
    return _CACHE["dir"]


def shared_wheel() -> Path:
    """The session's one built wheel (removed at interpreter exit)."""
    if "wheel" not in _CACHE:
        scratch = _session_dir() / "wheel-build"
        scratch.mkdir()
        _CACHE["wheel"] = build_wheel(scratch)
    return _CACHE["wheel"]


def unpack_site(wheel: Path, dest: Path) -> Path:
    """Unzip the wheel into `dest`: a site directory holding llm_kit and its dist-info."""
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(dest)
    return dest


def fresh_site(tmp_path: Path, name: str = "site") -> Path:
    return unpack_site(shared_wheel(), tmp_path / name)


def dist_info(site: Path) -> Path:
    found = sorted(site.glob(f"{DIST_INFO_STEM}-*.dist-info"))
    assert len(found) == 1, found
    return found[0]


_PRELUDE = '''
import json, sys, traceback
for _p in reversed(json.loads(sys.argv[1])):
    sys.path.insert(0, _p)
_blocked = json.loads(sys.argv[2])
class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _blocked:
            raise AssertionError("blocked import: " + name)
        return None
sys.meta_path.insert(0, _Block())
result = None
try:
{body}
    print(json.dumps({{"ok": result, "modules": sorted(m for m in sys.modules if m.split(".")[0] in _blocked)}}))
except BaseException as exc:
    print(json.dumps({{"error": type(exc).__name__, "message": str(exc),
                       "cause": type(exc.__cause__).__name__ if exc.__cause__ else None,
                       "modules": sorted(m for m in sys.modules if m.split(".")[0] in _blocked)}}))
'''


def run(body: str, paths: list, *, blocked: tuple = (), env: dict | None = None,
        cwd: Path | None = None) -> dict:
    """Run `body` (statements; assign `result`) in a fresh isolated interpreter.

    `paths` go first on sys.path; `blocked` top-level modules raise on import.
    Returns the parsed outcome: {"ok": value} or {"error": ClassName, ...}.
    """
    indented = "\n".join("    " + line for line in body.strip("\n").splitlines())
    script = _PRELUDE.format(body=indented)
    child_env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/nonexistent")}
    child_env.update(env or {})
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script, json.dumps([str(p) for p in paths]), json.dumps(list(blocked))],
        capture_output=True, text=True, env=child_env, cwd=cwd)
    lines = proc.stdout.strip().splitlines()
    assert proc.returncode == 0 and lines, f"child failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    return json.loads(lines[-1])
