"""Helpers for the clean-venv wheel-closure tests (test_wheel_closure.py).

A closure test installs ONLY the two distributions (katsoft-contracts and
katsoft-llm-kit) and their declared third-party dependencies into a fresh venv,
then runs consumer code under that venv's interpreter in isolated mode. Nothing
of this repository is reachable from it: no bootstrap_lib, no plugin tree, no
.pth link, no PYTHONPATH, and a scratch HOME.

Everything that can be judged is judged by a pure function over a probe report
(`environment_problems`, `surface_problems`), so the tests can also feed those
functions a deliberately broken environment and watch them report it.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import os
import subprocess
import sys
import threading
import tomllib
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROBES = Path(__file__).resolve().parent / "probes"
CONTRACTS_DIST = "katsoft-contracts"
KIT_DIST = "katsoft-llm-kit"
#: The extras the databench handoff installs (packaging-plan.md, "Databench handoff").
KIT_EXTRAS = "sdk,jobs,config"
PYTHON = "3.12"
FORBIDDEN_DISTRIBUTIONS = ("llm-kit", "bootstrap", "plugins-kit")
#: The llm_kit modules databench imports (index_pages_runner.py) or hashes
#: (index_pages_worktree.py _FINGERPRINT_KIT_MODULES). test_databench_drift keeps
#: this list in step with the databench source on a machine that has it.
KIT_MODULES = (
    "llm_kit.pipeline.execution.scheduler",
    "llm_kit.pipeline.execution.deadlines",
    "llm_kit.pipeline.execution.failure_cache",
    "llm_kit.storage.atomic_json",
    "llm_kit.pipeline.runtime.gate",
)
#: bootstrap_lib names databench imports; each maps to katsoft_contracts.<name>.
CONTRACT_MODULES = ("execution_event",)


def _load(path: Path, name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _contract_support():
    # contract_support imports its sibling contract_cases by bare name.
    here = str(REPO / "tests" / "contracts")
    if here not in sys.path:
        sys.path.insert(0, here)
    return _load(REPO / "tests" / "contracts" / "contract_support.py", "_closure_contract_support")


def _dist_support():
    return _load(REPO / "tests" / "llm-kit" / "dist_support.py", "_closure_dist_support")


def project_version(dist: str) -> str:
    text = (REPO / "packages" / dist / "pyproject.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)["project"]["version"]


# --- wheels -----------------------------------------------------------------

def build_local_wheels(scratch: Path) -> dict:
    """Build both wheels the way the release machinery does (its builders'
    recipe, a synthetic registry, SOURCE_DATE_EPOCH, release identity).

    Returns {"contracts": Path, "kit": Path, "commit": <the build commit both
    markers carry>}.
    """
    contracts, kit = _contract_support(), _dist_support()
    assert contracts.COMMIT == kit.COMMIT, "the two builders disagree on the build commit"
    return {
        "contracts": contracts.build_wheel(scratch / "contracts"),
        "kit": kit.build_wheel(scratch / "kit"),
        "commit": kit.COMMIT,
    }


def rewrite_wheel(wheel: Path, dest: Path, mutate) -> Path:
    """A copy of `wheel` under `dest` with its member dict edited by `mutate`."""
    with zipfile.ZipFile(wheel) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    mutate(members)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / wheel.name
    with zipfile.ZipFile(target, "w") as out:
        for name, data in members.items():
            out.writestr(name, data)
    return target


# --- venvs ------------------------------------------------------------------

def bin_dir(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def venv_python(venv: Path) -> Path:
    return bin_dir(venv) / ("python.exe" if os.name == "nt" else "python")


def uv(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(["uv", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"uv {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
    return result


def make_venv(root: Path, requirements: list[str], *, no_deps: bool = False) -> Path:
    """A fresh venv under `root` holding exactly `requirements` (and their deps)."""
    venv = root / "venv"
    root.mkdir(parents=True, exist_ok=True)
    uv("venv", "--python", PYTHON, "--no-config", str(venv), cwd=root)
    install = ["pip", "install", "--no-config", "--python", str(venv_python(venv))]
    if no_deps:
        install.append("--no-deps")
    uv(*install, *requirements, cwd=root)
    return venv


def local_requirements(wheels: dict, *, extras: str = KIT_EXTRAS) -> list[str]:
    """Requirements that install the two local wheels (and the kit's extras)."""
    kit = f"{KIT_DIST}[{extras}]" if extras else KIT_DIST
    return [str(wheels["contracts"]), f"{kit} @ {wheels['kit'].as_uri()}"]


def pypi_requirements(*, extras: str = KIT_EXTRAS) -> list[str]:
    """Requirements that install the two RELEASED distributions from PyPI."""
    kit = f"{KIT_DIST}[{extras}]" if extras else KIT_DIST
    return [f"{CONTRACTS_DIST}=={project_version(CONTRACTS_DIST)}",
            f"{kit}=={project_version(KIT_DIST)}"]


def released_commit(dist: str) -> str:
    """The commit packages/released.json records for the current version of `dist`."""
    ledger = json.loads((REPO / "packages" / "released.json").read_text(encoding="utf-8"))
    version = project_version(dist)
    entry = ledger.get(dist, {}).get(version)
    assert entry, (f"packages/released.json has no entry for {dist} {version}: the "
                   "pypi closure run needs a released package (docs/reference/package-release.md)")
    return entry["commit"]


# --- running code under the venv --------------------------------------------

def child_env(venv: Path, home: Path, **extra: str) -> dict:
    """The only environment a closure child sees: no PYTHONPATH, no repo, no claude."""
    system = ["/usr/bin", "/bin"] if os.name != "nt" else [os.environ.get("SystemRoot", "C:\\Windows") + "\\System32"]
    env = {"PATH": os.pathsep.join([str(bin_dir(venv)), *system]),
           "HOME": str(home), "USERPROFILE": str(home)}
    if os.name == "nt":
        env["SystemRoot"] = os.environ.get("SystemRoot", "C:\\Windows")
    env.update(extra)
    return env


def run_probe(venv: Path, script: str, *args: str, home: Path, cwd: Path) -> dict:
    proc = subprocess.run(
        [str(venv_python(venv)), "-I", str(PROBES / script), *args],
        capture_output=True, text=True, env=child_env(venv, home), cwd=cwd)
    lines = proc.stdout.strip().splitlines()
    assert proc.returncode == 0 and lines, f"{script} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    return json.loads(lines[-1])


def run_cli(venv: Path, via: str, argv: list[str], *, home: Path, cwd: Path, env: dict,
            stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run the llm-kit CLI under the venv: `script` is the console script,
    `module` is `python -I -m llm_kit.cli`."""
    if via == "script":
        command = [str(bin_dir(venv) / ("llm-kit.exe" if os.name == "nt" else "llm-kit"))]
    elif via == "module":
        command = [str(venv_python(venv)), "-I", "-m", "llm_kit.cli"]
    else:
        raise ValueError(via)
    return subprocess.run([*command, *argv], capture_output=True, text=True, input=stdin,
                          env=child_env(venv, home, **env), cwd=cwd, timeout=120)


# --- judging reports --------------------------------------------------------

def environment_problems(report: dict, *, expected_commits: dict, repo: Path = REPO) -> list[str]:
    """Why `report` (probes/environment.py) is not a closed install; empty if closed."""
    problems = []
    repo_text = str(repo)
    site_packages = [str(Path(p)) for p in report["site_packages"]]

    def in_site(path) -> bool:
        return bool(path) and any(str(path).startswith(s) for s in site_packages)

    leaked = [p for p in report["sys_path"] if repo_text in p or "/.claude/" in p.replace("\\", "/")]
    if leaked:
        problems.append(f"sys.path reaches the repository or ~/.claude: {leaked}")
    if report["bootstrap_lib_origin"] is not None:
        problems.append(f"bootstrap_lib is importable: {report['bootstrap_lib_origin']}")
    links = {p: lines for p, lines in report["pth"].items() if Path(p).name != "_virtualenv.pth"}
    if links:
        problems.append(f".pth links present: {links}")
    for dist in (KIT_DIST, CONTRACTS_DIST):
        if dist not in report["distributions"]:
            problems.append(f"distribution {dist} is not installed")
    for name in report["distributions"]:
        if name in FORBIDDEN_DISTRIBUTIONS or name.startswith("plugins-kit"):
            problems.append(f"unexpected first-party distribution installed: {name}")
    for name, error in report["errors"].items():
        problems.append(f"{name} failed: {error}")
    for module in ("llm_kit", "katsoft_contracts"):
        origin = report["origins"].get(module)
        if not in_site(origin):
            problems.append(f"{module} does not load from the venv's site-packages: {origin}")
    kit, contracts = report["markers"].get("llm_kit"), report["markers"].get("katsoft_contracts")
    for label, marker, dist in (("llm_kit", kit, KIT_DIST), ("katsoft_contracts", contracts, CONTRACTS_DIST)):
        if not marker:
            continue
        if marker["MODE"] != "package":
            problems.append(f"{label} marker MODE is {marker['MODE']!r}, not 'package'")
        if marker["DIST"] != dist:
            problems.append(f"{label} marker DIST is {marker['DIST']!r}")
        if marker["VERSION"] != report["distributions"].get(dist):
            problems.append(f"{label} marker VERSION {marker['VERSION']!r} != installed "
                            f"{report['distributions'].get(dist)!r}")
        if marker["COMMIT"] != expected_commits[dist]:
            problems.append(f"{label} marker COMMIT {marker['COMMIT']!r} != build commit "
                            f"{expected_commits[dist]!r}")
    if report["markers"].get("llm_kit_runtime_mode") != "package":
        problems.append(f"llm_kit runtime mode is {report['markers'].get('llm_kit_runtime_mode')!r}")
    return problems


def surface_problems(report: dict) -> list[str]:
    """Why `report` (probes/databench_surface.py) is not a clean run; empty if clean."""
    problems = [f"{name}: {result['error']}" for name, result in report.items()
                if isinstance(result, dict) and "error" in result]
    if report.get("_bootstrap_lib_modules"):
        problems.append(f"bootstrap_lib was imported: {report['_bootstrap_lib_modules']}")
    return problems


# --- databench source -------------------------------------------------------

DATABENCH_FILES = ("index_pages_runner.py", "index_pages_worktree.py")
_DOTTED = re.compile(r"^(llm_kit|bootstrap_lib)(\.\w+)*$")


def databench_root() -> Path:
    """$DEVROOT/databench (READ-ONLY here); DEVROOT defaults to this repo's parent."""
    return Path(os.environ.get("DEVROOT", REPO.parent)) / "databench"


def databench_references(sources: list[str]) -> set[str]:
    """Every dotted llm_kit / bootstrap_lib module name the sources import or name.

    Counts import statements and string constants that are exactly a dotted
    module name (importlib.import_module arguments, module-name tuples); prose
    in docstrings and comments is not a reference.
    """
    found: set[str] = set()
    for text in sources:
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module and _DOTTED.match(node.module):
                found.add(node.module)
                found.update(f"{node.module}.{alias.name}" for alias in node.names
                             if node.module == "bootstrap_lib")
            elif isinstance(node, ast.Import):
                found.update(a.name for a in node.names if _DOTTED.match(a.name))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and _DOTTED.match(node.value):
                found.add(node.value)
    return found


def uncovered_references(references: set[str]) -> list[str]:
    """References the closure surface does not exercise.

    An llm_kit module is covered when it is in KIT_MODULES or is a package
    above one; a bootstrap_lib reference is covered when it is the package
    itself or a name in CONTRACT_MODULES (databench's handoff change maps it
    to katsoft_contracts).
    """
    covered_kit = set(KIT_MODULES)
    for module in KIT_MODULES:
        parts = module.split(".")
        covered_kit.update(".".join(parts[:i]) for i in range(1, len(parts)))
    missing = []
    for ref in sorted(references):
        if ref.startswith("llm_kit"):
            if ref not in covered_kit:
                missing.append(ref)
        elif ref != "bootstrap_lib" and ref.split(".", 1)[1] not in CONTRACT_MODULES:
            missing.append(ref)
    return missing


# --- a stub OpenAI-compatible endpoint --------------------------------------

class StubEndpoint:
    """A local OpenAI-compatible server: GET /v1/models and POST /v1/chat/completions.

    Records (method, path, Authorization) per request. It is the stub endpoint
    the closure's `completion complete`, `models describe` and `jobs run` talk to.
    """

    ANSWER = "stub-answer"
    MODEL = "m-local"

    def __init__(self) -> None:
        self.requests: list[tuple] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D102 - keep pytest output clean
                pass

            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path, self.headers.get("Authorization")))
                self._send({"object": "list", "data": [{"id": outer.MODEL, "object": "model"}]})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.requests.append(("POST", self.path, self.headers.get("Authorization")))
                self._send({"id": "c1", "object": "chat.completion", "created": 1, "model": outer.MODEL,
                            "choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant", "content": outer.ANSWER}}],
                            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def registry(self) -> str:
        return (f"default: local\nmodels:\n  local: {{base_url: {self.base_url}, "
                f"model: {self.MODEL}, key_env: OPENROUTER_API_KEY}}\n")

