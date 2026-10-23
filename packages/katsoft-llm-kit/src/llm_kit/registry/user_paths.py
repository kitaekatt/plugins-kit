"""Harness-neutral locations of the user-scoped files (consent, endpoint registry, keys).

A person who installs the library with ``pip`` and never runs an AI harness
needs the per-user files somewhere that names no harness. They live under one
directory, resolved on every call (never at import, so a test or a caller that
changes ``HOME`` or ``XDG_CONFIG_HOME`` is honoured):

    $XDG_CONFIG_HOME/llm-kit/        (fallback: $HOME/.config/llm-kit/)
        consent.json                 cost-class consent        (``LLM_KIT_CONSENT_FILE`` overrides)
        model-endpoints.yaml         endpoint registry         (``MODEL_ENDPOINTS_REGISTRY`` overrides)
        .env                         user API keys             (``LLM_KIT_ENV_FILE`` overrides)

``XDG_CONFIG_HOME`` is honoured only when it is an absolute path, as the XDG
specification requires; an empty or relative value is ignored.

The files an earlier release kept under ``~/.claude`` remain readable as a
lower-precedence LEGACY location: a reader uses the neutral file when it exists
and falls back to the legacy file only when it does not (each owner module
states its own legacy path and the precedence). A writer always targets the
neutral path or the explicit override, never the legacy one. Reading a path that
does not exist creates nothing; only a write creates the neutral directory.

This module names no harness, imports nothing outside the standard library, and
is the one place the neutral directory is spelled.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

#: The directory name under the config home.
NEUTRAL_DIRNAME = "llm-kit"

#: Environment variables that replace a neutral default with an explicit file.
ENV_CONSENT_FILE = "LLM_KIT_CONSENT_FILE"
ENV_ENV_FILE = "LLM_KIT_ENV_FILE"

CONSENT_FILENAME = "consent.json"
REGISTRY_FILENAME = "model-endpoints.yaml"
ENV_FILENAME = ".env"

#: Per-project key file, relative to the project root.
PROJECT_ENV_RELATIVE = Path(".llm-kit") / ".env"


def _environ(environ: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def config_home(environ: Optional[Mapping[str, str]] = None) -> Path:
    """``$XDG_CONFIG_HOME`` when absolute, else ``$HOME/.config`` (``Path.home()``)."""
    configured = (_environ(environ).get("XDG_CONFIG_HOME") or "").strip()
    if configured and os.path.isabs(configured):
        return Path(configured)
    return Path.home() / ".config"


def neutral_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    """The directory holding the neutral user files."""
    return config_home(environ) / NEUTRAL_DIRNAME


def neutral_consent_file(environ: Optional[Mapping[str, str]] = None) -> Path:
    return neutral_dir(environ) / CONSENT_FILENAME


def neutral_registry_file(environ: Optional[Mapping[str, str]] = None) -> Path:
    return neutral_dir(environ) / REGISTRY_FILENAME


def neutral_user_env_file(environ: Optional[Mapping[str, str]] = None) -> Path:
    return neutral_dir(environ) / ENV_FILENAME


def neutral_project_env_file(project_root: Path) -> Path:
    """The per-project key file: ``<project_root>/.llm-kit/.env``."""
    return Path(project_root) / PROJECT_ENV_RELATIVE


def override_path(name: str, environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """The path named by environment variable ``name`` (``~`` expanded), or None when unset or blank."""
    value = (_environ(environ).get(name) or "").strip()
    if not value:
        return None
    return Path(os.path.expanduser(value))


def user_env_file(environ: Optional[Mapping[str, str]] = None) -> Path:
    """Where ``account set-key`` writes: ``$LLM_KIT_ENV_FILE`` else the neutral ``.env``."""
    return override_path(ENV_ENV_FILE, environ) or neutral_user_env_file(environ)


__all__ = [
    "CONSENT_FILENAME",
    "ENV_CONSENT_FILE",
    "ENV_ENV_FILE",
    "ENV_FILENAME",
    "NEUTRAL_DIRNAME",
    "PROJECT_ENV_RELATIVE",
    "REGISTRY_FILENAME",
    "config_home",
    "neutral_consent_file",
    "neutral_dir",
    "neutral_project_env_file",
    "neutral_registry_file",
    "neutral_user_env_file",
    "override_path",
    "user_env_file",
]
