"""Guard: a full engine pass writes only razor-approved names to $CLAUDE_ENV_FILE.

The razor is in plugins/bootstrap/skills/bootstrap/references/manifest-reference.md,
section "What bootstrap writes to the session env". A name earns a line in the
session env only when a Bash-tool reader ships in this marketplace, nothing else
provides it, and it is present whenever it is read. Adding an export therefore
means editing ``ALLOWED_FAMILIES`` here, which is the moment to apply the razor.

The pass runs against a fake tree that exercises every producer that once wrote
to the session env: a plugin with a provisioned venv and a recorded tool.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

from bootstrap.test_engine_multiplugin import (
    BOOTSTRAP_ROOT,
    _isolated_env,
    _write_minimal_defaults,
    run_engine,
)
from bootstrap.link_compat import link_tree
from bootstrap_lib.env_var_check import plugin_root_env_var_name

# The allowed families. Each entry is a name or a predicate-free rule applied below.
ALLOWED_EXACT = {"BOOTSTRAP_PYTHON", "BOOTSTRAP_PROJECT_PYTHON"}
PLUGINS = ["bootstrap", "venv-kit"]
DECLARED_ENV_VARS = ["RAZOR_GUARD_DECLARED"]  # user env_vars in a layered manifest

_EXPORT_RE = re.compile(r"^export ([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)


def _build_tree(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    fake_root = plugins_dir / "bootstrap"
    fake_root.mkdir()
    link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
    link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
    _write_minimal_defaults(fake_root)
    (fake_root / "bootstrap.json").write_text(json.dumps({}))

    # A plugin with a venv (once exported as <PLUGIN>_VENV) and a recordable
    # tool (once exported as BOOTSTRAP_BIN_<TOOL>).
    kit = plugins_dir / "venv-kit"
    kit.mkdir()
    (kit / "bootstrap.json").write_text(json.dumps({
        "venv": {"check_imports": ["os", "sys"]},
        "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
    }))
    kit_data = tmp_path / "data" / "kit" / "venv-kit"
    kit_data.mkdir(parents=True)
    subprocess.run(["uv", "venv", str(kit_data / ".venv")],
                   check=True, capture_output=True)

    registry = {"plugins": {
        "kit:bootstrap": [{"installPath": "./bootstrap", "version": "1.0.0"}],
        "kit:venv-kit": [{"installPath": "./venv-kit", "version": "1.0.0"}],
    }}
    (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

    data_dir = tmp_path / "data" / "kit" / "bootstrap"
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({
        "schema_version": 3,
        "enabled_plugins": ["kit:bootstrap", "kit:venv-kit"],
        "log_level": "info", "log_success_shell": False, "log_success_checks": True,
    }))
    return fake_root, data_dir, kit_data


def _run_pass(tmp_path, declare_env_vars):
    fake_root, data_dir, kit_data = _build_tree(tmp_path)
    env = _isolated_env(tmp_path)
    env["USERPROFILE"] = env["HOME"]
    env_file = tmp_path / "claude_env_file"
    env_file.write_text("")
    env["CLAUDE_ENV_FILE"] = str(env_file)
    declared = []
    if declare_env_vars:
        layered = tmp_path / "_home" / ".claude"
        layered.mkdir(parents=True, exist_ok=True)
        (layered / "bootstrap.json").write_text(json.dumps({
            "env_vars": [{"name": n, "value": "razor-guard"} for n in DECLARED_ENV_VARS],
        }))
        declared = DECLARED_ENV_VARS
    result = run_engine(str(data_dir), plugin_root=str(fake_root), env=env)
    assert result.returncode == 0, result.stderr
    names = _EXPORT_RE.findall(env_file.read_text())
    return names, declared, kit_data, data_dir


def _allowed(names, declared):
    allowed = set(ALLOWED_EXACT) | set(declared)
    allowed |= {plugin_root_env_var_name(p) for p in PLUGINS}
    return allowed


# A user env_vars entry on Windows persists to the real user environment
# through the registry, which a test must not do. The family is exercised on
# POSIX only; the allowlist still names it.
_DECLARE = sys.platform != "win32"


class TestSessionEnvAllowlist:
    def test_every_name_written_belongs_to_an_allowed_family(self, tmp_path):
        names, declared, kit_data, data_dir = _run_pass(tmp_path, _DECLARE)

        # Not vacuous: the pass exercised the producers the razor removed.
        assert (kit_data / ".venv").is_dir()
        # The tool record is kept: only its session-env export was removed.
        record = tmp_path / "_home" / ".claude" / "plugins" / "data" / "plugins-kit"             / "bootstrap" / "tool_paths.json"
        assert "git" in json.loads(record.read_text(encoding="utf-8"))["tools"]
        assert plugin_root_env_var_name("venv-kit") in names

        unexpected = sorted(set(names) - _allowed(names, declared))
        assert unexpected == [], (
            "names written to the session env outside the razor's allowlist: "
            f"{unexpected}. Apply the razor in manifest-reference.md, "
            "'What bootstrap writes to the session env', before extending "
            "ALLOWED_EXACT."
        )

    def test_no_plugin_venv_name_is_written(self, tmp_path):
        names, _, _, _ = _run_pass(tmp_path, False)
        venv_names = [n for n in names if n.endswith("_VENV")]
        assert venv_names == [], f"<PLUGIN>_VENV is not a session-env name: {venv_names}"

    def test_no_tool_bin_name_is_written(self, tmp_path):
        names, _, _, _ = _run_pass(tmp_path, False)
        bin_names = [n for n in names if n.startswith("BOOTSTRAP_BIN_")]
        assert bin_names == [], f"BOOTSTRAP_BIN_<TOOL> is not a session-env name: {bin_names}"
