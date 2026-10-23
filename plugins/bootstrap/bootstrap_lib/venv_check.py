"""Python venv validation and remediation."""

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .result import Result


def _venv_result(passed: bool, message: str, venv_path: str, remediation_cmd: Optional[str] = None) -> Result:
    """Result for venv checks: subject is the venv path."""
    return Result(
        passed=passed,
        subject=venv_path,
        message=message,
        remediation_cmd=remediation_cmd,
    )


_FINDER_IMPORT_RE = re.compile(r"\bimport\s+(__editable___[A-Za-z0-9_]+_finder)\b")


def site_packages_dirs(venv_path: str) -> List[Path]:
    """Every site-packages directory inside a venv (Windows and POSIX layouts)."""
    venv = Path(venv_path)
    dirs: List[Path] = []
    win = venv / "Lib" / "site-packages"
    if win.is_dir():
        dirs.append(win)
    posix_lib = venv / "lib"
    if posix_lib.is_dir():
        for entry in sorted(posix_lib.iterdir()):
            candidate = entry / "site-packages"
            if candidate.is_dir():
                dirs.append(candidate)
    return dirs


def _normalize(path: Path) -> str:
    """Resolve links and case so two spellings of one directory compare equal.

    Load-bearing: ``~/.claude`` is a symlink or junction on many machines, so
    the same cache directory is recorded under several spellings. Comparing the
    raw strings would report every plugin on such a machine as stale.
    """
    return os.path.normcase(os.path.realpath(str(path)))


def _is_inside(child: Path, parent: Path) -> bool:
    """True when ``child`` is ``parent`` or lives beneath it."""
    c = _normalize(child)
    p = _normalize(parent)
    return c == p or c.startswith(p + os.sep)


def _read_editable_paths(pth: Path) -> Tuple[List[Path], Optional[str]]:
    """Extract the source paths an editable-install ``.pth`` resolves to.

    Two shapes are produced by the build backends in use:

    - direct: the file's lines are bare filesystem paths appended to sys.path.
    - finder: the file is an ``import __editable___<name>_finder; ...install()``
      one-liner, and the sibling finder module holds a ``MAPPING`` dict of
      package name -> source directory.

    Returns ``(paths, error)``. A non-None ``error`` means the file could not be
    understood and must be left alone.
    """
    try:
        text = pth.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], f"unreadable: {exc}"

    finder_names = _FINDER_IMPORT_RE.findall(text)
    if finder_names:
        paths: List[Path] = []
        for finder_name in finder_names:
            finder_py = pth.parent / f"{finder_name}.py"
            if not finder_py.is_file():
                return [], f"finder module {finder_name}.py not found"
            mapping, error = _read_finder_mapping(finder_py)
            if error:
                return [], error
            paths.extend(mapping)
        if not paths:
            return [], f"finder module for {finder_names[0]} records no paths"
        return paths, None

    paths = []
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("import ") or entry.startswith("from "):
            # An executable line that is not the known finder shape.
            return [], "unrecognized executable .pth line"
        paths.append(Path(entry))
    if not paths:
        return [], "no paths recorded"
    return paths, None


def _read_finder_mapping(finder_py: Path) -> Tuple[List[Path], Optional[str]]:
    """Read the ``MAPPING`` literal out of an editable finder module.

    Parsed with ``ast`` rather than imported: the module is third-party
    generated code and a check must never execute it.
    """
    try:
        tree = ast.parse(finder_py.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError) as exc:
        return [], f"{finder_py.name} could not be parsed: {exc}"

    for node in tree.body:
        targets = []
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        if "MAPPING" not in targets or value is None:
            continue
        try:
            mapping = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return [], f"{finder_py.name} MAPPING is not a literal"
        if not isinstance(mapping, dict):
            return [], f"{finder_py.name} MAPPING is not a dict"
        return [Path(str(v)) for v in mapping.values()], None
    return [], f"{finder_py.name} declares no MAPPING"


def scan_editable_installs(
    venv_path: str, project_dir: str
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """Find editable installs in a venv that no longer point at ``project_dir``.

    A plugin's venv lives at a version-independent path while its source lives
    in a version-keyed cache directory, so an editable install written for one
    version keeps resolving to that version's directory after the plugin
    updates. The old directory is still on disk, so the import succeeds and the
    venv silently serves the previous release's code.

    Detection is a containment test, not an equality test: build backends record
    a package subdirectory (``<project_dir>/lib``) rather than the project root,
    and both are legitimate.

    Fails closed. A ``.pth`` whose recorded paths cannot be determined is
    reported as unreadable and never treated as stale, so an unfamiliar shape
    produces a note rather than a rewritten venv.

    Returns:
        ``(stale, unreadable)``, each a list of ``(pth_name, detail)``.
    """
    stale: List[Tuple[str, str]] = []
    unreadable: List[Tuple[str, str]] = []
    project = Path(project_dir)
    for site_dir in site_packages_dirs(venv_path):
        for pth in sorted(site_dir.glob("__editable__*.pth")):
            paths, error = _read_editable_paths(pth)
            if error:
                unreadable.append((pth.name, error))
                continue
            outside = [p for p in paths if not _is_inside(p, project)]
            if outside:
                stale.append((
                    pth.name,
                    f"points at {outside[0]}, expected under {project_dir}",
                ))
    return stale, unreadable


def check_venv(plugin_data_dir: str, plugin_root: str, check_imports: List[str],
               extras: Sequence[str] = (), venv_path: Optional[str] = None) -> Result:
    """Check if a Python venv exists and required imports are available.

    Also fails when an editable install still points outside ``plugin_root``
    (see ``scan_editable_installs``): such a venv passes every import while
    serving a superseded source directory, so only the recorded path reveals it.

    Args:
        plugin_data_dir: Plugin data directory (venv lives at <data_dir>/.venv)
        plugin_root: Plugin root directory (for uv sync --project)
        check_imports: List of module names to try importing

    Returns:
        Result with pass/fail and optional remediation command
    """
    venv_path = venv_path or os.path.join(plugin_data_dir, ".venv")
    _extra_flags = "".join(f" --extra {e}" for e in extras)
    remediation = f"uv sync --project {plugin_root}{_extra_flags}"

    # Check venv directory exists
    if not os.path.isdir(venv_path):
        return _venv_result(
            passed=False,
            message=f"venv not found at {venv_path}",
            venv_path=venv_path,
            remediation_cmd=remediation,
        )

    # Find python binary
    python_bin = _find_python(venv_path)
    if not python_bin:
        return _venv_result(
            passed=False,
            message=f"no python binary in {venv_path}",
            venv_path=venv_path,
            remediation_cmd=remediation,
        )

    # Check python works
    try:
        proc = subprocess.run(
            [python_bin, "-c", "import sys; sys.exit(0)"],
            capture_output=True, timeout=10,
        )
        if proc.returncode != 0:
            raise subprocess.SubprocessError(f"exit code {proc.returncode}")
    except (subprocess.SubprocessError, OSError):
        return _venv_result(
            passed=False,
            message=f"python binary not functional at {python_bin}",
            venv_path=venv_path,
            remediation_cmd=remediation,
        )

    # Check each import
    for module in check_imports:
        try:
            result = subprocess.run(
                [python_bin, "-c", f"import {module}"],
                capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                return _venv_result(
                    passed=False,
                    message=f"import {module} failed in venv",
                    venv_path=venv_path,
                    remediation_cmd=remediation,
                )
        except (subprocess.SubprocessError, OSError):
            return _venv_result(
                passed=False,
                message=f"failed to check import {module}",
                venv_path=venv_path,
                remediation_cmd=remediation,
            )

    # An editable install pinned to a superseded source directory imports
    # cleanly, so it survives every check above; it has to be detected on the
    # recorded path rather than on behavior.
    stale, unreadable = scan_editable_installs(venv_path, plugin_root)
    if stale:
        detail = "; ".join(f"{name} {why}" for name, why in stale)
        return _venv_result(
            passed=False,
            message=f"stale editable install: {detail}",
            venv_path=venv_path,
            remediation_cmd=remediation,
        )

    message = f"venv ok ({len(check_imports)} imports verified)"
    if unreadable:
        # Verbose-only, and deliberately not a failure: an unparsed .pth is left
        # exactly as found.
        notes = "; ".join(f"{name}: {why}" for name, why in unreadable)
        message += f" [editable install left alone - {notes}]"
    return _venv_result(
        passed=True,
        message=message,
        venv_path=venv_path,
    )


def venv_python(venv_path: str) -> Optional[str]:
    """Find the python binary in a venv."""
    candidates = [
        os.path.join(venv_path, "bin", "python"),
        os.path.join(venv_path, "Scripts", "python.exe"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


_find_python = venv_python


def _venv_sync_stamp_path(venv_path: str) -> Path:
    """Return the dependency-content stamp path beside the venv."""
    return Path(venv_path).parent / ".venv.sync.sha256"


def _read_stamp(path: Path) -> Optional[str]:
    """Read a stamp without allowing a filesystem race to escape a check."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _project_content_hash(
    project_dir: str, targets: Sequence[Tuple[str, str]] = ()
) -> Optional[str]:
    """Hash the dependency declarations that control ``uv sync``.

    ``targets`` are the ``(distribution, version)`` pairs the released-package
    ledger resolved for this project (``package_targets``). They are folded in
    AFTER the files, so a project with no registered package hashes exactly as
    it did before targets existed (no venv re-syncs because of the fold), and a
    new release inside a requirement's range changes the hash without any
    change to ``pyproject.toml`` or ``uv.lock``.
    """
    digest = hashlib.sha256()
    found = False
    for name in ("pyproject.toml", "uv.lock"):
        path = Path(project_dir) / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            digest.update(b"MISSING\0")
            continue
        except OSError:
            return None
        found = True
        digest.update(content)
        digest.update(b"\0")
    for dist, version in sorted(targets):
        digest.update(f"target\0{dist}\0{version}\0".encode("utf-8"))
    return digest.hexdigest() if found else None


# ---------------------------------------------------------------------------
# Released-package targets (the ledger fold)
# ---------------------------------------------------------------------------
#
# A plugin that declares a first-party package registered in the marketplace's
# ``packages/released.json`` ledger (for example ``katsoft-llm-kit``) gets the
# highest released version its requirement admits, with no plugin release. The
# requirement is the only input the plugin authors, and it is restricted to one
# form so this stdlib-only code can read it exactly:
#
#     name[extras]>=A.B[.C],<N
#
# ``>=`` states the lowest version the plugin's code needs; ``<N`` is the next
# major version, so minors arrive automatically and a major needs a plugin
# release that raises N. scripts/_packages.py enforces the same form at publish.

_RESTRICTED_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_RESTRICTED_RE = re.compile(
    r"^\s*(?P<name>" + _RESTRICTED_NAME + r")\s*"
    r"(?:\[(?P<extras>[A-Za-z0-9._,\s-]*)\])?\s*"
    r">=\s*(?P<lo>\d+(?:\.\d+){1,2})\s*,\s*<\s*(?P<hi>\d+)\s*$"
)
_REQ_NAME_RE = re.compile(r"^\s*(" + _RESTRICTED_NAME + r")")
_RELEASE_RE = re.compile(r"^\d+(?:\.\d+){0,2}$")


def normalize_dist(name: str) -> str:
    """PEP 503 normalization of a distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _release_tuple(version: str) -> Optional[Tuple[int, int, int]]:
    """``(major, minor, patch)`` of a final release version, else None.

    Pre-, post- and dev-releases (and anything with more than three parts) are
    not eligible targets: a requirement of this form never selects them.
    """
    if not isinstance(version, str) or not _RELEASE_RE.match(version):
        return None
    parts = [int(p) for p in version.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def parse_restricted_requirement(text: str) -> Optional[Tuple[str, Tuple[int, int, int], int]]:
    """``(name, minimum, exclusive_major)`` for a ``name[extras]>=A.B[.C],<N``
    requirement, else None."""
    match = _RESTRICTED_RE.match(text) if isinstance(text, str) else None
    if match is None:
        return None
    return (match.group("name"), _release_tuple(match.group("lo")), int(match.group("hi")))


def load_released_ledger(path: str) -> Tuple[Optional[Dict[str, Dict[str, dict]]], Optional[str]]:
    """``(ledger, error)`` for ``packages/released.json``.

    An absent file is ``(None, None)``: nothing is registered, so no package
    target exists. A file that is present but unreadable, not JSON, or not
    ``distribution -> version -> entry`` is an error, never a quiet skip.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, f"released-package ledger {path} is unreadable: {exc}"
    if not isinstance(data, dict) or not all(
        isinstance(versions, dict) and all(isinstance(e, dict) for e in versions.values())
        for versions in data.values()
    ):
        return None, (
            f"released-package ledger {path} must map distribution -> version -> entry"
        )
    return data, None


def package_targets(
    project_dir: str, ledger_path: str, extras: Sequence[str] = ()
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """The ledger target for each registered package ``project_dir`` requires.

    Reads the project's direct requirements (``[project].dependencies`` plus
    the optional-dependency groups named by ``extras``). A requirement whose
    distribution is a key of the ledger is registered: it must be in the
    restricted form (module comment above), and its target is the highest
    ledger version that satisfies every such requirement on that distribution.

    Returns ``(targets, errors)``; ``targets`` is a sorted list of
    ``(ledger distribution name, version)``. An error covers an unreadable
    ledger or pyproject, a registered requirement outside the restricted form,
    and a range no released version satisfies. Errors are never skipped: the
    caller turns them into a failed venv result.
    """
    ledger, error = load_released_ledger(ledger_path)
    if error:
        return [], [error]
    if not ledger:
        return [], []
    registered = {normalize_dist(name): name for name in ledger}
    pyproject = os.path.join(project_dir, "pyproject.toml")
    try:
        import tomllib
        with open(pyproject, "rb") as f:
            project = tomllib.load(f).get("project", {})
    except FileNotFoundError:
        return [], []
    except (OSError, ImportError, ValueError) as exc:
        return [], [f"cannot read {pyproject} to resolve released packages: {exc}"]
    requirements = list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        requirements.extend(optional.get(extra, []))

    errors: List[str] = []
    wanted: Dict[str, List[Tuple[str, Tuple[int, int, int], int]]] = {}
    for text in requirements:
        named = _REQ_NAME_RE.match(text) if isinstance(text, str) else None
        if named is None or normalize_dist(named.group(1)) not in registered:
            continue
        parsed = parse_restricted_requirement(text)
        if parsed is None:
            errors.append(
                f"{pyproject}: requirement {text!r} names a released package but is not "
                f"in the form name[extras]>=A.B[.C],<N"
            )
            continue
        wanted.setdefault(registered[normalize_dist(parsed[0])], []).append(
            (text, parsed[1], parsed[2])
        )

    targets: List[Tuple[str, str]] = []
    for dist in sorted(wanted):
        eligible = []
        for version in ledger[dist]:
            release = _release_tuple(version)
            if release is not None and all(
                release >= low and release[0] < hi for _t, low, hi in wanted[dist]
            ):
                eligible.append((release, version))
        if not eligible:
            errors.append(
                f"no released version of {dist} satisfies "
                f"{', '.join(t for t, _l, _h in wanted[dist])} "
                f"(released: {', '.join(ledger[dist]) or 'none'})"
            )
            continue
        targets.append((dist, max(eligible)[1]))
    return targets, errors


def installed_version(venv_path: str, dist: str) -> Optional[str]:
    """The version of distribution ``dist`` installed in the venv, else None.

    Read from the ``<name>-<version>.dist-info`` directory name, which the
    installer writes and which needs no interpreter to read.
    """
    wanted = normalize_dist(dist)
    for site_dir in site_packages_dirs(venv_path):
        try:
            entries = sorted(os.listdir(site_dir))
        except OSError:
            continue
        for entry in entries:
            stem, ext = os.path.splitext(entry)
            if ext != ".dist-info" or "-" not in stem:
                continue
            name, version = stem.rsplit("-", 1)
            if normalize_dist(name) == wanted:
                return version
    return None


def find_uv() -> Optional[str]:
    """Locate the uv binary: PATH first, then ~/.local/bin directly.

    The direct ~/.local/bin probe covers the same-session case where the tools
    phase just installed uv but the inherited PATH doesn't include it yet.
    """
    uv_bin = shutil.which("uv")
    if uv_bin:
        return uv_bin
    local_bin = os.path.expanduser("~/.local/bin")
    for name in ("uv", "uv.exe", "uv.EXE"):
        candidate = os.path.join(local_bin, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _target_failure(venv_path: str, messages: Sequence[str]) -> Result:
    """The failed result for an unmet released-package target.

    The remediation is ``bootstrap run``: the usual cause is a ledger that is
    ahead of the index (a release still propagating) or a clone that has not
    refreshed, and a full pass re-reads both.
    """
    return _venv_result(
        passed=False,
        message="released package target not met: " + "; ".join(messages),
        venv_path=venv_path,
        remediation_cmd="bootstrap run",
    )


def ensure_venv(
    project_dir: str,
    venv_path: str,
    extras: Sequence[str] = (),
    check_imports: Sequence[str] = (),
    always_sync: bool = False,
    released_ledger: Optional[str] = None,
) -> Tuple[Result, List[str]]:
    """Check a venv and remediate via ``uv sync``: check -> sync -> re-check.

    The single venv-remediation path shared by the engine's self-setup,
    per-plugin manifest, and project_venv phases (previously three diverging
    copies — B9).

    Args:
        project_dir: Directory holding the pyproject.toml (``uv sync --project``).
        venv_path: Target venv directory (``<dir>/.venv``); also exported as
            UV_PROJECT_ENVIRONMENT so the sync lands here rather than in
            project_dir/.venv.
        extras: Optional dependency extras (``--extra <name>`` each).
        check_imports: Module names that must import inside the venv.
        always_sync: Run ``uv sync`` even when the check already passes
            (self-setup keeps the engine venv current every session; a no-op
            sync is ~100ms).
        released_ledger: Path to the marketplace's ``packages/released.json``.
            When given, each registered package the project requires is held
            at its ledger target (``package_targets``): the targets join the
            stamp hash, a venv whose installed version differs is synced with
            ``uv sync -P <dist>==<target>``, and a target that cannot be
            resolved or installed is a FAILED result with no stamp written.

    Returns:
        (result, entries): the final check Result, plus unprefixed action
        messages describing every remediation step or failure. Per the
        logging contract, any attempted work or error produces an entry —
        nothing is swallowed. Callers prefix entries with their phase label
        (e.g. ``"venv: "``) and route the final result to ok/action entries.
    """
    data_dir = os.path.dirname(venv_path)
    entries: List[str] = []
    existed = os.path.isdir(venv_path)
    check_kwargs = {"extras": extras, "venv_path": venv_path}
    targets: List[Tuple[str, str]] = []
    target_errors: List[str] = []
    if released_ledger:
        targets, target_errors = package_targets(project_dir, released_ledger, extras)

    def _finish(final: Result) -> Tuple[Result, List[str]]:
        """Every return goes through here: an unresolved target is a failure.

        Whatever path ran (early exit, no uv, uv error, sync failure), a passing
        result with unresolved targets is converted, so no path can report a
        converged venv while a released-package target is unmet.
        """
        if target_errors and final.passed:
            entries.extend(e for e in target_errors if e not in entries)
            final = _target_failure(venv_path, target_errors)
        return final, entries

    result = check_venv(data_dir, project_dir, list(check_imports), **check_kwargs)
    content_hash = _project_content_hash(project_dir, targets)
    stamp_path = _venv_sync_stamp_path(venv_path)
    content_changed = content_hash is not None and _read_stamp(stamp_path) != content_hash
    behind = [
        (dist, version, installed_version(venv_path, dist))
        for dist, version in targets
        if installed_version(venv_path, dist) != version
    ]

    if result.passed and not always_sync and not content_changed and not behind:
        return _finish(result)

    extra_flags = " ".join(f"--extra {e}" for e in extras)
    pin_flags = " ".join(f"-P {dist}=={version}" for dist, version, _ in behind)
    uv_cmd = (f"uv sync --project {project_dir}" + (f" {extra_flags}" if extra_flags else "")
              + (f" {pin_flags}" if pin_flags else ""))
    if not result.passed:
        # The reason is part of the action entry: a re-sync triggered by a stale
        # editable install is otherwise indistinguishable from any other repair.
        entries.append(f"not ready, running `{uv_cmd}` - {result.message}")
    elif behind:
        for dist, version, installed in behind:
            entries.append(
                f"{dist} installed {installed or 'none'}, released target {version}; "
                f"running `{uv_cmd}`"
            )
    elif content_changed and not always_sync:
        entries.append(f"dependency declarations changed, running `{uv_cmd}`")

    uv_bin = find_uv()
    if not uv_bin:
        entries.append("uv not found on PATH or in ~/.local/bin")
        if behind:
            result = _target_failure(
                venv_path, [f"{dist} {version} (uv not found)" for dist, version, _i in behind]
            )
        return _finish(result)

    cmd = [uv_bin, "sync", "--project", project_dir]
    for e in extras:
        cmd.extend(["--extra", e])
    for dist, version, _installed in behind:
        cmd.extend(["-P", f"{dist}=={version}"])
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=venv_path)
    # Ensure ~/.local/bin is on PATH for uv's own child processes.
    local_bin = os.path.expanduser("~/.local/bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError) as exc:
        entries.append(f"uv sync error: {exc}")
        if behind:
            result = _target_failure(venv_path, [f"uv sync error: {exc}"])
        return _finish(result)

    was_passing = result.passed
    result = check_venv(data_dir, project_dir, list(check_imports), **check_kwargs)
    if proc.returncode != 0:
        stderr = proc.stderr or b""
        stderr_text = (
            stderr.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes) else str(stderr)
        ).strip()
        # The FULL stderr, not the first 200 chars. uv reports the resolution
        # conflict that actually explains the failure well past that cut, and
        # the tail was unrecoverable -- the entry was the only copy. Length is
        # now a rendering concern: the record and the log keep everything, and
        # the collated display line shortens it (see messages.py).
        entries.append(f"uv sync failed (exit {proc.returncode}): {stderr_text}")
        if behind:
            # A pinned target that uv could not resolve or install is a miss,
            # however healthy the old venv still is.
            result = _target_failure(
                venv_path,
                [f"{dist} {version} (uv sync exit {proc.returncode})"
                 for dist, version, _installed in behind],
            )
        elif always_sync:
            result = _venv_result(
                passed=False,
                message=f"uv sync failed (exit {proc.returncode}): {stderr_text}",
                venv_path=venv_path,
                remediation_cmd=uv_cmd,
            )
    elif result.passed:
        missed = [
            f"{dist} is {installed_version(venv_path, dist) or 'not installed'}, "
            f"target {version}"
            for dist, version in targets
            if installed_version(venv_path, dist) != version
        ]
        if missed:
            entries.extend(missed)
            result = _target_failure(venv_path, missed)
        elif target_errors:
            entries.extend(target_errors)
            result = _target_failure(venv_path, target_errors)
        else:
            if not was_passing or behind:
                entries.append("created" if not existed else "re-synced")
            if content_hash is not None:
                try:
                    stamp_path.parent.mkdir(parents=True, exist_ok=True)
                    stamp_path.write_text(content_hash + "\n", encoding="utf-8")
                except OSError as exc:
                    entries.append(f"could not write venv dependency stamp: {exc}")
    else:
        entries.append(f"uv sync completed but re-check failed: {result.message}")
    return _finish(result)
