"""Git dependency clone validation and remediation."""

import os
import subprocess
from typing import Any, List, Mapping, Optional, Tuple

from .result import Result

# Suppress interactive credential prompts for HTTPS remotes.
# Public repos work anonymously; prompting would block non-interactive sessions.
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def _git_dep_result(passed, message, repo_name, target_path, remediation_cmd=None,
                    reason=None):
    """Result for git-dep checks: subject is the repo name; target_path and the
    structured failure ``reason`` (missing | not-git | pin | branch | origin |
    sparse | check-error | None when passed) ride in extras."""
    return Result(
        passed=passed,
        subject=repo_name,
        message=message,
        remediation_cmd=remediation_cmd,
        extras={"target_path": target_path, "reason": reason},
    )


def check_git_dep(
    data_dir: str,
    url: str,
    branch: str,
    sparse_paths: Optional[List[str]] = None,
    commit: Optional[str] = None,
) -> Result:
    """Check if a git dependency is cloned correctly.

    Args:
        data_dir: Plugin data directory (clones go to <data_dir>/github/<repo_name>/)
        url: Git repository URL
        branch: Expected branch name
        sparse_paths: Optional list of paths for sparse checkout
        commit: Optional commit SHA to pin to (checked out after clone)

    Returns:
        Result with pass/fail and optional remediation command
    """
    repo_name = _extract_repo_name(url)
    target_path = os.path.join(data_dir, "github", repo_name)

    # Build remediation command
    remediation = _build_clone_cmd(url, branch, target_path, sparse_paths, commit)

    # Check directory exists
    if not os.path.isdir(target_path):
        return _git_dep_result(
            passed=False,
            message=f"{repo_name} not cloned",
            repo_name=repo_name,
            target_path=target_path,
            remediation_cmd=remediation,
            reason="missing",
        )

    # Check it's a git repo
    git_dir = os.path.join(target_path, ".git")
    if not os.path.exists(git_dir):
        return _git_dep_result(
            passed=False,
            message=f"{repo_name} exists but is not a git repo",
            repo_name=repo_name,
            target_path=target_path,
            remediation_cmd=remediation,
            reason="not-git",
        )

    # If commit pinning, check HEAD matches expected SHA
    if commit:
        try:
            result = subprocess.run(
                ["git", "-C", target_path, "rev-parse", "HEAD"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                raise subprocess.SubprocessError("rev-parse HEAD failed")
            current_sha = result.stdout.strip()
            # A pin is a case-insensitive prefix of HEAD, whatever its length:
            # a 40-char pin therefore has to match in full.
            if not current_sha.lower().startswith(commit.strip().lower()):
                return _git_dep_result(
                    passed=False,
                    message=f"{repo_name} at {current_sha[:7]}, expected {commit[:7]}",
                    repo_name=repo_name,
                    target_path=target_path,
                    remediation_cmd=f"git -C {target_path} fetch && git -C {target_path} checkout {commit}",
                    reason="pin",
                )
        except (subprocess.SubprocessError, OSError):
            return _git_dep_result(
                passed=False,
                message=f"could not check commit for {repo_name}",
                repo_name=repo_name,
                target_path=target_path,
                remediation_cmd=remediation,
                reason="check-error",
            )
    else:
        # Check branch
        try:
            result = subprocess.run(
                ["git", "-C", target_path, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                raise subprocess.SubprocessError("rev-parse --abbrev-ref HEAD failed")
            current_branch = result.stdout.strip()
            if current_branch != branch:
                return _git_dep_result(
                    passed=False,
                    message=f"{repo_name} on branch {current_branch}, expected {branch}",
                    repo_name=repo_name,
                    target_path=target_path,
                    remediation_cmd=f"git -C {target_path} checkout {branch}",
                    reason="branch",
                )
        except (subprocess.SubprocessError, OSError):
            return _git_dep_result(
                passed=False,
                message=f"could not check branch for {repo_name}",
                repo_name=repo_name,
                target_path=target_path,
                remediation_cmd=remediation,
                reason="check-error",
            )

    try:
        remote_result = subprocess.run(
            ["git", "-C", target_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if remote_result.returncode != 0 or not remote_result.stdout.strip():
            raise subprocess.SubprocessError("remote get-url origin failed")
        if _normalize_remote(remote_result.stdout) != _normalize_remote(url):
            return _git_dep_result(
                passed=False,
                message=f"{repo_name} origin differs from manifest URL",
                repo_name=repo_name,
                target_path=target_path,
                remediation_cmd=remediation,
                reason="origin",
            )
    except (subprocess.SubprocessError, OSError):
        return _git_dep_result(
            passed=False,
            message=f"could not check origin for {repo_name}",
            repo_name=repo_name,
            target_path=target_path,
            remediation_cmd=remediation,
            reason="check-error",
        )

    sparse_note = ""
    if sparse_paths:
        try:
            sparse_result = subprocess.run(
                ["git", "-C", target_path, "sparse-checkout", "list"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            )
            sparse_error = (sparse_result.stderr or "").lower()
            if sparse_result.returncode != 0 and (
                "is not a git command" in sparse_error
                or "unknown subcommand" in sparse_error
            ):
                sparse_note = " (sparse-checkout unavailable; sparse paths not checked)"
            elif sparse_result.returncode != 0:
                return _git_dep_result(
                    passed=False,
                    message=f"could not check sparse checkout for {repo_name}",
                    repo_name=repo_name,
                    target_path=target_path,
                    remediation_cmd=remediation,
                    reason="check-error",
                )
            else:
                actual_paths = {
                    line.strip() for line in sparse_result.stdout.splitlines() if line.strip()
                }
                if actual_paths != set(sparse_paths):
                    return _git_dep_result(
                        passed=False,
                        message=f"{repo_name} sparse paths differ from manifest",
                        repo_name=repo_name,
                        target_path=target_path,
                        remediation_cmd=remediation,
                        reason="sparse",
                    )
        except (subprocess.SubprocessError, OSError) as exc:
            if isinstance(exc, FileNotFoundError):
                sparse_note = " (sparse-checkout unavailable; sparse paths not checked)"
            else:
                return _git_dep_result(
                    passed=False,
                    message=f"could not check sparse checkout for {repo_name}",
                    repo_name=repo_name,
                    target_path=target_path,
                    remediation_cmd=remediation,
                    reason="check-error",
                )

    return _git_dep_result(
        passed=True,
        message=f"{repo_name} cloned on {branch}" + (f" at {commit[:7]}" if commit else "") + sparse_note,
        repo_name=repo_name,
        target_path=target_path,
    )



def _normalize_remote(value: str) -> str:
    """Normalize only the URL suffix differences accepted by the manifest."""
    normalized = value.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4].rstrip("/")
    return normalized


def ensure_git_dep(dep_def: Mapping[str, Any], data_dir: str) -> Tuple[Result, List[str]]:
    """Converge one git dependency and finish with an authoritative check."""
    url = str(dep_def["url"])
    branch = str(dep_def["branch"])
    sparse_paths = dep_def.get("sparse_paths")
    commit = dep_def.get("commit")
    initial = check_git_dep(data_dir, url, branch, sparse_paths, commit)
    if initial.passed:
        return initial, []

    target_path = initial.target_path
    action: Optional[str] = None
    action_message = ""
    try:
        if not os.path.isdir(target_path):
            ok, action_message = clone_git_dep(url, branch, target_path, sparse_paths, commit)
            action = "clone"
        elif commit:
            fetch = subprocess.run(
                ["git", "-C", target_path, "fetch"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, env=_GIT_ENV,
            )
            if fetch.returncode != 0:
                ok, action_message = False, fetch.stderr.strip() or "fetch failed"
            else:
                checkout = subprocess.run(
                    ["git", "-C", target_path, "checkout", str(commit)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=30, env=_GIT_ENV,
                )
                ok = checkout.returncode == 0
                action_message = checkout.stderr.strip() if not ok else ""
            action = "checkout"
        elif initial.reason == "branch":
            checkout = subprocess.run(
                ["git", "-C", target_path, "checkout", branch],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, env=_GIT_ENV,
            )
            ok = checkout.returncode == 0
            action_message = checkout.stderr.strip() if not ok else ""
            action = "checkout"
        elif initial.reason == "sparse":
            sparse = subprocess.run(
                ["git", "-C", target_path, "sparse-checkout", "set"] + list(sparse_paths),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, env=_GIT_ENV,
            )
            ok = sparse.returncode == 0
            action_message = sparse.stderr.strip() if not ok else ""
            action = "sparse"
        else:
            ok, action_message = False, initial.message
    except (subprocess.SubprocessError, OSError) as exc:
        ok, action_message = False, str(exc)

    final = check_git_dep(data_dir, url, branch, sparse_paths, commit)
    if not final.passed:
        return final, [f"git remediation did not converge; re-check: {final.message}"]
    if action == "clone":
        return final, [f"cloned {url}"]
    if action == "checkout":
        return final, [f"checked out {branch if not commit else str(commit)[:7]}"]
    if action == "sparse":
        return final, ["updated sparse checkout"]
    return final, []


def clone_git_dep(url: str, branch: str, target_path: str, sparse_paths=None, commit=None) -> tuple:
    """Clone a git dependency. Returns (success, message)."""
    try:
        if sparse_paths:
            # Sparse checkout: clone with no-checkout, set sparse paths, checkout
            result = subprocess.run(
                ["git", "clone", "--no-checkout", "--branch", branch, url, target_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=_GIT_ENV,
            )
            if result.returncode != 0:
                return False, result.stderr.strip() or "clone failed"
            result = subprocess.run(
                ["git", "-C", target_path, "sparse-checkout", "set"] + sparse_paths,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=_GIT_ENV,
            )
            if result.returncode != 0:
                return False, result.stderr.strip() or "sparse-checkout set failed"
            result = subprocess.run(
                ["git", "-C", target_path, "checkout", branch],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=_GIT_ENV,
            )
            if result.returncode != 0:
                return False, result.stderr.strip() or "checkout failed"
        else:
            result = subprocess.run(
                ["git", "clone", "--branch", branch, url, target_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=_GIT_ENV,
            )
            if result.returncode != 0:
                return False, result.stderr.strip() or "clone failed"
        if commit:
            result = subprocess.run(
                ["git", "-C", target_path, "checkout", commit],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=_GIT_ENV,
            )
            if result.returncode != 0:
                return False, result.stderr.strip() or f"checkout {commit} failed"
        return True, f"cloned to {target_path}"
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)


def pull_git_dep(target_path: str) -> tuple:
    """Pull latest changes in an existing git dep. Returns (success, message)."""
    try:
        result = subprocess.run(
            ["git", "-C", target_path, "pull"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=_GIT_ENV,
        )
        if result.returncode == 0:
            return True, "pulled latest"
        return False, result.stderr.strip() or "pull failed"
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)


def _extract_repo_name(url: str) -> str:
    """Extract repository name from URL.

    Normalizes backslashes to forward slashes first so a local-path dep on
    Windows (e.g. ``C:\\path\\to\\repo``) splits correctly -- otherwise the
    bare ``rsplit("/")`` returns the whole drive path, and the downstream
    ``os.path.join(data_dir, "github", <name>)`` resets to that absolute path,
    pointing the dep at the wrong location entirely.
    """
    # Handle URLs like https://github.com/octocat/Hello-World or .git suffix
    name = url.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def _build_clone_cmd(
    url: str,
    branch: str,
    target_path: str,
    sparse_paths: Optional[List[str]] = None,
    commit: Optional[str] = None,
) -> str:
    """Build the git clone command string."""
    if sparse_paths:
        # Sparse checkout: clone with no-checkout, set sparse paths, checkout
        paths_str = " ".join(sparse_paths)
        cmd = (
            f"git clone --no-checkout --branch {branch} {url} {target_path} && "
            f"cd {target_path} && "
            f"git sparse-checkout set {paths_str} && "
            f"git checkout {branch}"
        )
    else:
        cmd = f"git clone --branch {branch} {url} {target_path}"
    if commit:
        cmd += f" && git -C {target_path} checkout {commit}"
    return cmd
