"""Environment gate: the pinned tools are installed in the running venv, at the pinned commits.

The plugin's pyproject.toml pins each tool to a git commit. uv records the
commit it installed in the distribution's ``direct_url.json``; a venv holding a
different commit (a pin moved but bootstrap has not re-synced, or a hand-built
venv) is refused rather than trusted, because the package version is 0.1.0 at
every commit and cannot tell them apart.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from . import MARKETPLACE, PLUGIN, parsers
from .errors import Refusal

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = PLUGIN_ROOT / "pyproject.toml"
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+) @ git\+https://\S+@(?P<sha>[0-9a-f]{40})$")


@dataclass(frozen=True)
class Tool:
    name: str
    path: Path
    commit: str


def venv_prefix() -> Path:
    """The environment this process runs in; the guard has re-exec'd into the plugin venv."""
    return Path(sys.prefix)


def site_path() -> list[str]:
    return list(sys.path)


def plugin_data_dir() -> Path:
    """``${CLAUDE_BOOTSTRAP_DATA_ROOT:-~/.claude/plugins/data}/plugins-kit/clean-code-kit``."""
    root = os.environ.get("CLAUDE_BOOTSTRAP_DATA_ROOT")
    base = Path(root) if root else Path.home() / ".claude" / "plugins" / "data"
    return base / MARKETPLACE / PLUGIN


def parser_cache_dir() -> Path:
    """The tree-sitter parser cache; the custom bootstrap script fills the same one."""
    return parsers.cache_dir(plugin_data_dir())


def tool_path(prefix: Path, tool: str) -> Path:
    if os.name == "nt":
        return prefix / "Scripts" / f"{tool}.exe"
    return prefix / "bin" / tool


def read_pins(pyproject: Path = PYPROJECT) -> dict[str, str]:
    """``{distribution: commit}`` for every git-pinned requirement in ``pyproject``."""
    with pyproject.open("rb") as handle:
        requirements = tomllib.load(handle)["project"]["dependencies"]
    pins = {}
    for text in requirements:
        match = _PIN.match(text)
        if match:
            pins[match.group("name")] = match.group("sha")
    return pins


def installed_commit(dist: str, path: list[str]) -> str:
    """The commit uv recorded for ``dist`` in its direct_url.json."""
    found = [d for d in metadata.distributions(path=path) if d.metadata["Name"] == dist]
    if not found:
        raise Refusal(f"{dist} is not installed in the venv at {venv_prefix()}; run bootstrap")
    raw = found[0].read_text("direct_url.json")
    if raw is None:
        raise Refusal(f"{dist} in the venv at {venv_prefix()} has no direct_url.json; "
                      "installed tool does not match the pinned commit; run bootstrap")
    commit = json.loads(raw).get("vcs_info", {}).get("commit_id")
    if not commit:
        raise Refusal(f"{dist}'s direct_url.json records no git commit; "
                      "installed tool does not match the pinned commit; run bootstrap")
    return commit


def gate(tools: list[str], pyproject: Path = PYPROJECT) -> dict[str, Tool]:
    """Every tool in ``tools`` is installed in this venv at its pinned commit, or Refusal."""
    pins = read_pins(pyproject)
    prefix = venv_prefix()
    path = site_path()
    found: dict[str, Tool] = {}
    for tool in tools:
        if tool not in pins:
            raise Refusal(f"{pyproject} pins no git commit for {tool}")
        exe = tool_path(prefix, tool)
        if not exe.is_file():
            raise Refusal(
                f"{tool} is not installed at {exe}: the {MARKETPLACE}:bootstrap plugin has not "
                f"provisioned {PLUGIN}'s venv. Start a new session (or run `bootstrap run`) "
                "so it can, then retry.")
        commit = installed_commit(tool, path)
        if commit != pins[tool]:
            raise Refusal(
                f"installed tool does not match the pinned commit; run bootstrap: {tool} installed "
                f"{commit}, pinned {pins[tool]} in {pyproject}")
        found[tool] = Tool(tool, exe, commit)
    return found
