"""Workspace identity verification for Unreal remote connections.

Multiple Unreal Editors on one host share the same remote-execution multicast
group (and may share a project *name*), so "the editor that answers first" is
not necessarily the editor for the workspace a tool was launched in. The real
disambiguator is the absolute project directory (``project_root``), which UE
advertises in its remote-execution pong payload.

This module is intentionally tiny and protocol-agnostic: it only normalizes and
compares project paths. The connection/handshake mechanics differ per transport
(upyrc multicast for the python runner, WebSocket for the MCP client) and are
deliberately NOT abstracted here.
"""

from __future__ import annotations

import os


class ExpectedProjectMismatch(Exception):
    """Raised/returned when a responding editor is not the expected workspace.

    The message is shaped so callers across transports surface one consistent
    diagnostic instead of inventing their own.
    """


def normalize_project_path(path: str) -> str:
    """Normalize a project path for comparison.

    Lowercases, converts to forward slashes, strips a trailing slash, and
    collapses ``.``/``..`` segments. Returns ``""`` for falsy input so callers
    can treat "missing path" as a non-match rather than crashing.
    """
    if not path:
        return ""
    # os.path.normpath collapses redundant separators and ./.. segments using
    # the host separator; normalize that to forward slashes afterwards.
    normalized = os.path.normpath(str(path))
    normalized = normalized.replace("\\", "/").rstrip("/")
    return normalized.lower()


def project_paths_match(expected: str, actual: str) -> bool:
    """True if two project paths refer to the same workspace directory.

    A pong may advertise either the project *directory* or the ``.uproject``
    *file*; treat the file's parent directory as equivalent to the directory so
    callers don't have to know which form the editor reported.
    """
    exp = normalize_project_path(expected)
    act = normalize_project_path(actual)
    if not exp or not act:
        return False
    if exp == act:
        return True
    # Tolerate file-vs-dir form: compare the .uproject parent on either side.
    if act.endswith(".uproject") and normalize_project_path(os.path.dirname(act)) == exp:
        return True
    if exp.endswith(".uproject") and normalize_project_path(os.path.dirname(exp)) == act:
        return True
    return False


def compare_project_paths(expected: str, actual: str) -> str | None:
    """Return a human-readable mismatch reason, or ``None`` on a match.

    Callers use the return value directly as an error/diagnostic string and
    branch on ``None`` for the success case.
    """
    if project_paths_match(expected, actual):
        return None
    if not actual:
        return (
            f"editor did not advertise a project path; expected workspace "
            f"'{normalize_project_path(expected)}'"
        )
    return (
        f"editor belongs to a different workspace: expected "
        f"'{normalize_project_path(expected)}', got "
        f"'{normalize_project_path(actual)}'"
    )
