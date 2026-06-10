"""Git dependency clone validation and remediation."""

import os
import subprocess
from typing import List, Optional

from .result import Result

# Suppress interactive credential prompts for HTTPS remotes.
# Public repos work anonymously; prompting would block non-interactive sessions.
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def _git_dep_result(passed, message, repo_name, target_path, remediation_cmd=None):
    """Result for git-dep checks: subject is the repo name; target_path rides in extras."""
    return Result(
        passed=passed,
        subject=repo_name,
        message=message,
        remediation_cmd=remediation_cmd,
        extras={"target_path": target_path},
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
        )

    # If commit pinning, check HEAD matches expected SHA
    if commit:
        try:
            result = subprocess.run(
                ["git", "-C", target_path, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            current_sha = result.stdout.strip()
            if not current_sha.startswith(commit[:7]):
                return _git_dep_result(
                    passed=False,
                    message=f"{repo_name} at {current_sha[:7]}, expected {commit[:7]}",
                    repo_name=repo_name,
                    target_path=target_path,
                    remediation_cmd=f"git -C {target_path} fetch && git -C {target_path} checkout {commit}",
                )
        except (subprocess.SubprocessError, OSError):
            return _git_dep_result(
                passed=False,
                message=f"could not check commit for {repo_name}",
                repo_name=repo_name,
                target_path=target_path,
                remediation_cmd=remediation,
            )
    else:
        # Check branch
        try:
            result = subprocess.run(
                ["git", "-C", target_path, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            current_branch = result.stdout.strip()
            if current_branch != branch:
                return _git_dep_result(
                    passed=False,
                    message=f"{repo_name} on branch {current_branch}, expected {branch}",
                    repo_name=repo_name,
                    target_path=target_path,
                    remediation_cmd=f"git -C {target_path} checkout {branch}",
                )
        except (subprocess.SubprocessError, OSError):
            return _git_dep_result(
                passed=False,
                message=f"could not check branch for {repo_name}",
                repo_name=repo_name,
                target_path=target_path,
                remediation_cmd=remediation,
            )

    return _git_dep_result(
        passed=True,
        message=f"{repo_name} cloned on {branch}" + (f" at {commit[:7]}" if commit else ""),
        repo_name=repo_name,
        target_path=target_path,
    )


def clone_git_dep(url: str, branch: str, target_path: str, sparse_paths=None, commit=None) -> tuple:
    """Clone a git dependency. Returns (success, message)."""
    try:
        if sparse_paths:
            # Sparse checkout: clone with no-checkout, set sparse paths, checkout
            result = subprocess.run(
                ["git", "clone", "--no-checkout", "--branch", branch, url, target_path],
                capture_output=True, text=True, timeout=120, env=_GIT_ENV,
            )
            if result.returncode != 0:
                return False, result.stderr.strip() or "clone failed"
            result = subprocess.run(
                ["git", "-C", target_path, "sparse-checkout", "set"] + sparse_paths,
                capture_output=True, text=True, timeout=30, env=_GIT_ENV,
            )
            if result.returncode != 0:
                return False, result.stderr.strip() or "sparse-checkout set failed"
            result = subprocess.run(
                ["git", "-C", target_path, "checkout", branch],
                capture_output=True, text=True, timeout=30, env=_GIT_ENV,
            )
            if result.returncode != 0:
                return False, result.stderr.strip() or "checkout failed"
        else:
            result = subprocess.run(
                ["git", "clone", "--branch", branch, url, target_path],
                capture_output=True, text=True, timeout=120, env=_GIT_ENV,
            )
            if result.returncode != 0:
                return False, result.stderr.strip() or "clone failed"
        if commit:
            result = subprocess.run(
                ["git", "-C", target_path, "checkout", commit],
                capture_output=True, text=True, timeout=30, env=_GIT_ENV,
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
            capture_output=True, text=True, timeout=60, env=_GIT_ENV,
        )
        if result.returncode == 0:
            return True, "pulled latest"
        return False, result.stderr.strip() or "pull failed"
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)


def _extract_repo_name(url: str) -> str:
    """Extract repository name from URL."""
    # Handle URLs like https://github.com/octocat/Hello-World or .git suffix
    name = url.rstrip("/").rsplit("/", 1)[-1]
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
