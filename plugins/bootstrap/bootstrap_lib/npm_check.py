"""Node ``node_modules`` validation and remediation (the project_npm phase).

The Node sibling of venv_check: for a project that declares ``project_npm``,
make sure ``node_modules`` exists and reflects the current lockfile, running
npm only when it does not.

Two mechanics here are deliberate and load-bearing:

1. **npm is always spawned with an explicit ``cwd``.** npm's own local-prefix
   resolution walks UP the tree and stops at the first ancestor holding a
   ``package.json`` or a bare ``node_modules``. A run started from the wrong
   directory therefore installs into an unrelated project and rewrites that
   project's tracked lockfile (and ``npm ci`` deletes its ``node_modules``
   first). So the resolved directory is asserted to hold a ``package.json``
   BEFORE anything spawns, and is passed as ``cwd``.
2. **The freshness check spawns nothing.** ``node_modules/.package-lock.json``
   is npm's own hidden lockfile, written last by reify to describe the tree it
   actually installed; comparing its mtime against the visible lockfile is
   three ``stat`` calls. The subprocess alternatives were measured at
   0.7-1.1 s, and ``npm ci --dry-run`` is worse than slow -- it exits 0 with
   ``node_modules`` entirely absent, so it cannot be used as a freshness
   signal at all.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

from .result import Result

# npm's own registry fetch timeout is 300s with two retries, so a shorter
# ceiling would cut a legitimately slow but healthy install in half. This
# deliberately diverges from venv_check's 120s uv timeout: npm is slower.
NPM_TIMEOUT = 600

# Lockfiles that mean another package manager owns this tree. The guard is not
# cosmetic: npm READS yarn.lock (arborist ships a yarn-lock parser) and would
# happily install from it and write a competing package-lock.json into a yarn
# repo.
_COMPETING_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lock", "bun"),
    ("bun.lockb", "bun"),
)

# npm reads npm-shrinkwrap.json in preference to package-lock.json.
_NPM_LOCKFILES = ("npm-shrinkwrap.json", "package-lock.json")

_DEP_FIELDS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)


def _npm_result(passed: bool, message: str, node_modules: str,
                remediation_cmd: Optional[str] = None) -> Result:
    """Result for npm checks: subject is the node_modules path."""
    return Result(
        passed=passed,
        subject=node_modules,
        message=message,
        remediation_cmd=remediation_cmd,
    )


def find_npm() -> Optional[str]:
    """Locate the npm executable, as an ABSOLUTE path.

    The absolute path matters on Windows and is not a nicety: ``shutil.which``
    honours PATHEXT and returns ``npm.CMD``, but ``subprocess.run(["npm", ...])``
    with ``shell=False`` raises ``FileNotFoundError`` because CreateProcess does
    no PATHEXT resolution of its own. Resolving once here and passing the result
    as argv[0] is what makes the same shell-free spawn work on every platform.
    (Python 3.8+ applies CVE-2024-24576 batch-file argument quoting to ``.cmd``
    targets, so passing a shim this way is safe; ``shell=True`` never is.)

    Mirrors ``venv_check.find_uv``, including its direct ``~/.local/bin`` probe
    for the same-session case where PATH has not caught up yet.
    """
    npm_bin = shutil.which("npm")
    if npm_bin:
        return os.path.abspath(npm_bin)
    local_bin = os.path.expanduser("~/.local/bin")
    for name in ("npm", "npm.cmd", "npm.CMD", "npm.exe"):
        candidate = os.path.join(local_bin, name)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def find_npm_lockfile(project_dir: str) -> Optional[str]:
    """Return the path to the visible npm lockfile, or None if there is none."""
    for name in _NPM_LOCKFILES:
        candidate = os.path.join(project_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def detect_other_manager(project_dir: str,
                         root: Optional[str] = None) -> Optional[str]:
    """Name the non-npm package manager that owns this tree, if any.

    Reads both the competing lockfiles and package.json's ``packageManager``
    field (corepack's declaration).

    The search WALKS UP from ``project_dir`` to ``root`` (the project root,
    inclusive) rather than stopping at the first directory. That is required,
    not defensive: in a pnpm or yarn workspace the lockfile exists ONLY at the
    repo root and a workspace package's own package.json carries no
    ``packageManager`` field, so a ``subdir`` pointed at a workspace package
    would see neither signal, and npm would then write the competing
    package-lock.json this guard exists to prevent. Walking up is also how npm
    itself resolves a workspace, so the two agree about which tree is in play.

    ``root`` defaults to ``project_dir`` (no walk), which keeps the single-
    directory behaviour for callers that have no separate project root.
    """
    root = os.path.abspath(root or project_dir)
    current = os.path.abspath(project_dir)

    while True:
        for name, manager in _COMPETING_LOCKFILES:
            if os.path.isfile(os.path.join(current, name)):
                return manager

        declared = _read_package_json(current).get("packageManager")
        if isinstance(declared, str) and declared.strip():
            # "pnpm@8.6.0" / "yarn@4.1.0+sha224...."
            manager = declared.strip().split("@", 1)[0].strip()
            if manager and manager != "npm":
                return manager

        if current == root:
            return None
        parent = os.path.dirname(current)
        if parent == current:
            # Filesystem root reached without meeting `root` -- only possible
            # if project_dir is not under root, which the caller's containment
            # guard already rejects. Stop rather than loop.
            return None
        current = parent


def _read_package_json(project_dir: str) -> dict:
    """Parse package.json, returning {} when absent or unreadable.

    Unreadable is treated as "nothing declared" on purpose: this is only ever
    used for advisory guards, and a malformed package.json is npm's error to
    report, with npm's wording, not a place for this module to invent one.
    """
    data, _error = _read_package_json_with_error(project_dir)
    return data


def _read_package_json_with_error(project_dir: str) -> tuple[dict, Optional[str]]:
    """Return package metadata and a parse error, if the file is malformed."""
    try:
        with open(os.path.join(project_dir, "package.json"), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as exc:
        return {}, str(exc)
    if not isinstance(data, dict):
        return {}, "top-level value must be an object"
    return data, None


def _declares_dependencies(project_dir: str) -> bool:
    """Does package.json declare any dependency at all?"""
    pkg = _read_package_json(project_dir)
    return any(pkg.get(field) for field in _DEP_FIELDS)


def check_node_modules(project_dir: str) -> Result:
    """Is ``node_modules`` present and current? No subprocess, ~0 ms.

    Compares npm's hidden lockfile (``node_modules/.package-lock.json``, which
    reify writes last to describe the tree it installed) against the visible
    lockfile. Newer-or-equal hidden lockfile means the tree matches.

    A project that declares no dependencies at all passes with no
    ``node_modules``: verified empirically, ``npm install`` on such a project
    exits 0 and creates no ``node_modules`` directory, so requiring one would
    mean re-running npm every session and then failing the re-check forever.

    Args:
        project_dir: Directory holding package.json.

    Returns:
        Result whose subject is the node_modules path.
    """
    node_modules = os.path.join(project_dir, "node_modules")
    hidden = os.path.join(node_modules, ".package-lock.json")
    lockfile = find_npm_lockfile(project_dir)

    if not os.path.isfile(hidden):
        package_json, parse_error = _read_package_json_with_error(project_dir)
        if parse_error:
            return _npm_result(
                passed=False,
                message=f"package.json parse error: {parse_error}",
                node_modules=node_modules,
                remediation_cmd="npm ci" if lockfile else "npm install",
            )
        if not any(package_json.get(field) for field in _DEP_FIELDS):
            return _npm_result(
                passed=True,
                message="no dependencies declared, nothing to install",
                node_modules=node_modules,
            )
        return _npm_result(
            passed=False,
            message=f"node_modules not installed at {node_modules}",
            node_modules=node_modules,
            remediation_cmd="npm ci" if lockfile else "npm install",
        )

    if lockfile and os.stat(hidden).st_mtime < os.stat(lockfile).st_mtime:
        return _npm_result(
            passed=False,
            message=(
                f"node_modules is stale ({os.path.basename(lockfile)} is newer "
                f"than node_modules/.package-lock.json)"
            ),
            node_modules=node_modules,
            remediation_cmd="npm ci",
        )

    return _npm_result(
        passed=True,
        message="node_modules up to date",
        node_modules=node_modules,
    )


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """Best-effort termination of the npm process AND its descendants.

    ``Popen.kill`` ends only the direct child -- on Windows that is the
    ``npm.cmd`` shim, whose ``node`` and lifecycle-script grandchildren survive
    it. They are not merely leaked: a survivor that inherited the output handle
    keeps it open, which is what makes an unbounded drain hang. Killing the
    tree is therefore part of honouring the timeout, not tidiness.

    Every failure here is swallowed: this runs on the timeout path, where the
    caller already has a real error to report and a secondary one would only
    mask it.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except (subprocess.SubprocessError, OSError):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (OSError, PermissionError):
            pass

    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=30)
    except (subprocess.SubprocessError, OSError):
        pass


def _npm_warnings(output: str) -> List[str]:
    """Advisory npm lines worth surfacing even on a successful install.

    ``npm warn allow-scripts`` (a dependency wanted to run a lifecycle script)
    and EBADENGINE (installed anyway despite an engines mismatch) are advisory
    only in npm 11, which means a runner that only reports failures hides
    exactly the two things a user needs to know about.
    """
    found = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "allow-scripts" in stripped or "EBADENGINE" in stripped:
            found.append(stripped)
    return found


def _npm_error_code(output: str) -> Optional[str]:
    """Extract npm's own error code (``npm error code EUSAGE``) from output."""
    match = re.search(r"npm error code (\w+)", output)
    return match.group(1) if match else None


def _is_ci_out_of_sync(output: str) -> bool:
    """Did ``npm ci`` refuse because package.json and the lockfile disagree?"""
    return "can only install packages when your package.json and package-lock.json" in output


def ensure_node_modules(project_dir: str,
                        ignore_scripts: bool = False,
                        root: Optional[str] = None) -> Tuple[Result, List[str]]:
    """Check node_modules and remediate via npm: check -> npm -> re-check.

    Guards (each SKIPS the phase rather than failing it -- a project that does
    not use npm is not a broken project):
        - no package.json in ``project_dir``
        - another package manager owns the tree (pnpm/yarn/bun)
        - npm is not on PATH (bootstrap does not install Node)

    Args:
        project_dir: Directory holding package.json. npm runs with this as its
            cwd; see the module docstring for why that is non-negotiable.
        ignore_scripts: Pass ``--ignore-scripts`` to npm. Default False --
            skipping lifecycle scripts breaks esbuild/sharp/node-gyp/Prisma
            SILENTLY (exit 0, subtly broken tree), which is worse than an
            honest failure. The manifest opt-in exists for users who
            specifically want it.
        root: Project root, the boundary for the competing-package-manager
            walk-up. Defaults to ``project_dir``. Pass the real project root
            whenever ``project_dir`` is a subdir, or a pnpm/yarn workspace
            package will not be recognised as one -- its lockfile lives at the
            root, not beside it.

    Returns:
        (result, entries): the final Result, plus UNPREFIXED action messages.
        Callers add the phase label. Entries are empty when the check already
        passed (silent steady state); truncation of a long npm log is the
        display layer's job (messages.py), never this capture site's.
    """
    entries: List[str] = []
    node_modules = os.path.join(project_dir, "node_modules")

    # Guard 1: the cwd assertion. Nothing spawns without a package.json HERE.
    if not os.path.isfile(os.path.join(project_dir, "package.json")):
        return _npm_result(
            passed=True,
            message=f"skipped - no package.json at {project_dir}",
            node_modules=node_modules,
        ), entries

    # Guard 2: another package manager owns this tree (checked up to the
    # project root, so a workspace package sees its root's lockfile).
    other = detect_other_manager(project_dir, root=root)
    if other:
        return _npm_result(
            passed=True,
            message=f"skipped - project uses {other}",
            node_modules=node_modules,
        ), entries

    result = check_node_modules(project_dir)
    if result.passed:
        return result, entries

    # Guard 3: no npm. Bootstrap does not install Node.
    npm_bin = find_npm()
    if not npm_bin:
        return _npm_result(
            passed=True,
            message="skipped - npm not on PATH",
            node_modules=node_modules,
        ), entries

    lockfile = find_npm_lockfile(project_dir)
    sub = "ci" if lockfile else "install"
    argv = [npm_bin, sub, "--no-audit", "--no-fund", "--no-progress",
            "--no-update-notifier", "--loglevel=warn"]
    if ignore_scripts:
        argv.append("--ignore-scripts")
    npm_cmd = "npm " + " ".join(argv[1:])
    entries.append(f"not ready, running `npm {sub}` in {project_dir}")

    env = dict(os.environ, CI="1", NO_COLOR="1", npm_config_yes="true")

    # Output goes to a temp FILE, never a PIPE, and that is load-bearing on the
    # timeout path. With a pipe, CPython's own timeout handling deadlocks here:
    # subprocess.run kills the direct child and then, on Windows, calls
    # communicate() with NO timeout (Lib/subprocess.py, the `if _mswindows`
    # branch of run's TimeoutExpired handler). kill() ends only the npm.cmd
    # shim, so a surviving node/postinstall grandchild still holds the pipe's
    # write handle, the reader never sees EOF, and that unbounded communicate()
    # hangs the SessionStart hook forever instead of reporting the timeout. A
    # file has no such reader, so the wait is genuinely bounded.
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                    errors="replace") as out:
            popen_kwargs = {}
            if sys.platform != "win32":
                # Own process group, so the whole tree can be signalled.
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(
                argv,
                cwd=project_dir,
                shell=False,
                # DEVNULL, not just "npm does not prompt": a postinstall
                # lifecycle script is arbitrary third-party code, and this runs
                # from a SessionStart hook where a blocked read would hang the
                # session.
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                env=env,
                **popen_kwargs,
            )
            try:
                returncode = proc.wait(timeout=NPM_TIMEOUT)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                out.seek(0)
                partial = out.read().strip()
                entries.append(
                    f"npm {sub} timed out after {NPM_TIMEOUT}s in "
                    f"{project_dir}: {partial}"
                )
                return _npm_result(
                    passed=False,
                    message=f"npm {sub} timed out after {NPM_TIMEOUT}s",
                    node_modules=node_modules,
                    remediation_cmd=npm_cmd,
                ), entries
            out.seek(0)
            output = out.read().strip()
    except (subprocess.SubprocessError, OSError) as exc:
        entries.append(f"npm {sub} error: {exc}")
        return _npm_result(
            passed=False,
            message=f"npm {sub} error: {exc}",
            node_modules=node_modules,
            remediation_cmd=npm_cmd,
        ), entries

    # Advisory lines are surfaced even on success -- see _npm_warnings.
    for warning in _npm_warnings(output):
        entries.append(f"npm {sub}: {warning}")

    # Exit codes are NOT reliably 1: npm has been observed returning raw libuv
    # errnos (-4058 for ENOENT). Branch on zero vs non-zero only.
    if returncode != 0:
        if sub == "ci" and _is_ci_out_of_sync(output):
            # Deliberately NO fallback to `npm install`. Falling back would let
            # a background SessionStart hook rewrite the user's tracked
            # lockfile; the user runs that themselves, knowingly.
            msg = (
                f"npm ci refused: package.json and the lockfile are out of sync "
                f"in {project_dir}. Run `npm install` there yourself to update "
                f"the lockfile (bootstrap will not rewrite a tracked lockfile)"
            )
            entries.append(f"{msg}. npm output: {output}")
            return _npm_result(
                passed=False, message=msg, node_modules=node_modules,
                remediation_cmd="npm install",
            ), entries

        code = _npm_error_code(output)
        code_part = f" [{code}]" if code else ""
        msg = f"npm {sub} failed (exit {returncode}){code_part} in {project_dir}"
        # The FULL captured output, as with uv sync: the line that explains the
        # failure is routinely well past any fixed cut, and the entry is the
        # only copy. Shortening is the display layer's job.
        entries.append(f"{msg}: {output}")
        return _npm_result(
            passed=False, message=msg, node_modules=node_modules,
            remediation_cmd=npm_cmd,
        ), entries

    result = check_node_modules(project_dir)
    if result.passed:
        entries.append("created")
    else:
        entries.append(
            f"npm {sub} completed but node_modules is still not current: "
            f"{result.message}"
        )
        result = _npm_result(
            passed=False,
            message=(f"npm {sub} exited 0 but node_modules is still not current: "
                     f"{result.message}"),
            node_modules=node_modules,
            remediation_cmd=npm_cmd,
        )
    return result, entries
