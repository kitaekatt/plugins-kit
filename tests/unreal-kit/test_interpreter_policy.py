"""Static invariants for unreal-kit's interpreter policy and doc/manifest drift.

U5 -- one interpreter policy: every HOST-side script that hard-imports plugin
libs (pyyaml consumers in particular) re-execs under the bootstrap-provisioned
plugin venv via bootstrap_guard.reexec_under_plugin_venv; ue-runner.cmd execs
the plugin venv python instead of building a `uv run --with` overlay.
(UE-side scripts -- apply_fixups.py, discover_redirectors.py -- run inside the
Editor's embedded Python and must NOT re-exec.)

U9 -- no module shadowing between scripts/ and lib/.

U4 -- the fix-up-redirectors SKILL.md lock-retry instructions must match what
apply_fixups.py actually prints (p4 delete, not p4 reconcile).

U6 -- bootstrap.json's project_config paths must mirror the single canonical
definition in lib/ue_runner_config.py.

U8 -- the dead remote.timeout_seconds config field stays deleted.
"""

import json
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "unreal-kit"
_LIB_DIR = _PLUGIN_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

# Host-side scripts that must re-exec under the plugin venv before importing
# plugin libs. UE-side scripts are deliberately absent.
_HOST_SCRIPTS = (
    "skills/ue-python-api/scripts/ue_runner.py",
    "scripts/ue_env_cli.py",
    "scripts/refresh_unreal_stub.py",
    "scripts/search_unreal_stub.py",
    "skills/fix-up-redirectors/scripts/classify_safety.py",
    "skills/fix-up-redirectors/scripts/filter_safe_by_code_refs.py",
    "skills/fix-up-redirectors/scripts/scan_code_references.py",
    "skills/fix-up-redirectors/scripts/pick_one_per_dir.py",
)

_UE_SIDE_SCRIPTS = (
    "skills/fix-up-redirectors/scripts/apply_fixups.py",
    "skills/fix-up-redirectors/scripts/discover_redirectors.py",
)


class TestReexecPolicy:
    def test_every_host_script_reexecs_under_plugin_venv(self):
        missing = []
        for rel in _HOST_SCRIPTS:
            src = (_PLUGIN_DIR / rel).read_text(encoding="utf-8")
            if 'reexec_under_plugin_venv("unreal-kit")' not in src:
                missing.append(rel)
        assert not missing, f"host scripts missing the venv re-exec guard: {missing}"

    def test_ue_side_scripts_do_not_reexec(self):
        """Commandlet/remote scripts run inside UE's embedded Python; a
        re-exec there would tear down the Editor process."""
        offending = []
        for rel in _UE_SIDE_SCRIPTS:
            src = (_PLUGIN_DIR / rel).read_text(encoding="utf-8")
            if "reexec_under_plugin_venv" in src:
                offending.append(rel)
        assert not offending, f"UE-side scripts must not re-exec: {offending}"

    def test_ue_runner_cmd_uses_plugin_venv(self):
        src = (_PLUGIN_DIR / "skills/ue-python-api/scripts/ue-runner.cmd").read_text(encoding="utf-8")
        assert r".venv\Scripts\python.exe" in src
        # No executable `uv run` line (a comment may mention it).
        code_lines = [l for l in src.splitlines() if not l.strip().startswith("::")]
        assert not any("uv run" in l for l in code_lines), (
            "ue-runner.cmd must exec the plugin venv python, not a uv overlay"
        )

    def test_ue_runner_cmd_has_deterministic_standalone_first_chain(self):
        """The Windows shim must select a real interpreter before PATH lookup.

        This is a command-structure test on POSIX. It does not pretend that a
        shell on this host can execute cmd.exe, while still pinning the order,
        validation, argument forwarding, and missing-runtime diagnosis.
        """
        src = (_PLUGIN_DIR / "skills/ue-python-api/scripts/ue-runner.cmd").read_text(encoding="utf-8")
        standalone = 'set "STANDALONE_PY=%USERPROFILE%\\.local\\share\\python-standalone\\python\\python.exe"'
        venv = 'set "VENV_PY=%USERPROFILE%\\.claude\\plugins\\data\\plugins-kit\\unreal-kit\\.venv\\Scripts\\python.exe"'
        assert standalone in src
        assert venv in src
        assert src.index(standalone) < src.index(venv)
        assert "if exist \"%STANDALONE_PY%\" set \"PY=%STANDALONE_PY%\"" in src
        assert "BOOTSTRAP_PYTHON" in src
        assert "_STANDALONE_DIR" in src
        assert ".." in src  # traversal is rejected in the validated env branch
        assert '"%PY%" "%SCRIPT_DIR%ue_runner.py" %*' in src
        assert "BOOTSTRAP_PYTHON" in src.split("if not defined PY", 1)[-1]
        assert "no usable Python interpreter" in src

    def test_host_entrypoints_run_guard_before_plugin_imports(self):
        """The re-exec guard is executable ordering, not a comment claim."""
        watched = {
            "path_repair", "ue_discovery", "ue_runner_config", "ue_env",
            "p4cli", "code_refs", "redirector_record", "unreal_stub",
            "bootstrap_lib",
        }
        for rel in _HOST_SCRIPTS:
            tree = ast.parse((_PLUGIN_DIR / rel).read_text(encoding="utf-8"), filename=rel)
            guard_imports = [
                node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module == "bootstrap_guard"
            ]
            assert guard_imports, f"{rel} does not import bootstrap_guard"
            guard_line = min(guard_imports)
            for node in ast.walk(tree):
                module = None
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".", 1)[0] in watched:
                            module = alias.name
                            break
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.split(".", 1)[0] in watched:
                        module = node.module
                if module:
                    assert guard_line < node.lineno, (
                        f"{rel} imports {module} before its bootstrap guard"
                    )

    def test_host_documentation_uses_bootstrap_interpreter_expression(self):
        """Host commands use the bootstrap contract; UE console `py` is separate."""
        expression = "BOOTSTRAP_PROJECT_PYTHON"
        for rel in (
            "README.md",
            "skills/ue-python-api/SKILL.md",
            "skills/ue-python-api/references/architecture.md",
            "skills/ue-python-api/references/bootstrapped-setup.md",
            "skills/ue-python-api/references/project-setup.md",
            "skills/ue-python-api/references/script-bootstrap.md",
            "skills/ue-python-api/references/script-execution.md",
        ):
            text = (_PLUGIN_DIR / rel).read_text(encoding="utf-8")
            assert expression in text, f"{rel} does not name the host interpreter contract"
            assert "uv run python" not in text, f"{rel} adds a consumer uv interpreter"
        assert re.search(r"(?m)^\s*py \"", (_PLUGIN_DIR / "skills/ue-python-api/references/architecture.md").read_text(encoding="utf-8"))
        assert re.search(r"(?m)^\s*py \"", (_PLUGIN_DIR / "skills/ue-python-api/references/script-execution.md").read_text(encoding="utf-8"))

    def test_bootstrap_host_command_preserves_sentinel_argv(self, tmp_path):
        """A documented host command reaches its target with spaces intact."""
        sentinel = tmp_path / "sentinel-interpreter"
        args_file = tmp_path / "args.txt"
        sentinel.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$SENTINEL_ARGS\"\n",
            encoding="ascii",
        )
        sentinel.chmod(0o755)
        command = (
            '"${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}}" '
            '"${CLAUDE_PLUGIN_ROOT}/skills/ue-python-api/scripts/ue_runner.py" '
            '"script with spaces.py" --copy-output "output dir with spaces"'
        )
        env = os.environ.copy()
        env.update({
            "BOOTSTRAP_PROJECT_PYTHON": str(sentinel),
            "BOOTSTRAP_PYTHON": str(tmp_path / "missing-bootstrap-python"),
            "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_DIR),
            "SENTINEL_ARGS": str(args_file),
        })
        run = subprocess.run(command, shell=True, executable="/bin/sh", env=env,
                             capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
        assert args_file.read_text(encoding="ascii").splitlines() == [
            str(_PLUGIN_DIR / "skills/ue-python-api/scripts/ue_runner.py"),
            "script with spaces.py",
            "--copy-output",
            "output dir with spaces",
        ]

        env.pop("BOOTSTRAP_PROJECT_PYTHON")
        env.pop("BOOTSTRAP_PYTHON")
        missing = subprocess.run(command, shell=True, executable="/bin/sh", env=env,
                                 capture_output=True, text=True)
        assert missing.returncode != 0
        assert "requires bootstrap >= 0.120.0" in missing.stderr


class TestNoModuleShadowing:
    def test_scripts_and_lib_share_no_module_names(self):
        """U9: scripts/ue_env.py used to shadow lib/ue_env.py -- `from ue_env
        import ...` only resolved to lib because of insert(0) ordering."""
        scripts = {p.stem for p in (_PLUGIN_DIR / "scripts").glob("*.py")}
        libs = {p.stem for p in _LIB_DIR.glob("*.py")}
        overlap = scripts & libs
        assert not overlap, f"module name(s) shadowed between scripts/ and lib/: {overlap}"


class TestLockRetryDocMatchesScript:
    """U4: the SKILL.md Phase 4 tail and apply_fixups.py must agree that the
    post-exit lock retry is `p4 -x - delete -c`, never reconcile-first."""

    def test_skill_md_phase4_tail_uses_p4_delete(self):
        skill = (_PLUGIN_DIR / "skills/fix-up-redirectors/SKILL.md").read_text(encoding="utf-8")
        tail = skill.split("### Phase 4 tail", 1)[1].split("## Phase 5", 1)[0]
        assert "p4 -x - delete -c <CL>" in tail
        # reconcile may only appear as the explicit file-not-found fallback
        assert "p4 -x - reconcile -c <CL> <" not in tail.split("Fallback", 1)[0]

    def test_apply_fixups_prints_p4_delete_retry(self):
        src = (_PLUGIN_DIR / "skills/fix-up-redirectors/scripts/apply_fixups.py").read_text(encoding="utf-8")
        assert "p4 -x - delete -c" in src

    def test_apply_fixups_does_not_count_unconfirmed_files(self):
        src = (_PLUGIN_DIR / "skills/fix-up-redirectors/scripts/apply_fixups.py").read_text(encoding="utf-8")
        assert "outcome == 'confirmed'" in src
        assert "Deletion incomplete; confirmed" in src


class TestManifestConfigDrift:
    """U6: the legacy-path list is defined exactly once, in ue_runner_config;
    bootstrap.json's project_config block must mirror it."""

    def test_bootstrap_json_mirrors_canonical_config_paths(self):
        from ue_runner_config import LEGACY_PROJECT_CONFIG_NAMES, PROJECT_CONFIG_NAME

        manifest = json.loads((_PLUGIN_DIR / "bootstrap.json").read_text(encoding="utf-8"))
        project_config = manifest["project_config"]
        assert project_config["file"] == PROJECT_CONFIG_NAME
        assert project_config["legacy_file"] in LEGACY_PROJECT_CONFIG_NAMES

    def test_stale_hook_has_no_private_config_resolution(self):
        """The hook must import the canonical resolver, not re-implement it."""
        src = (_PLUGIN_DIR / "hooks/pretooluse/detect-editor-stale.py").read_text(encoding="utf-8")
        assert "find_project_config" in src
        # The old re-implementation hardcoded the legacy unreal-kit.yaml path.
        assert "unreal-kit.yaml" not in src


class TestDeadConfigStaysDeleted:
    def test_timeout_seconds_removed_everywhere(self):
        """U8: remote.timeout_seconds was parsed and documented but never
        wired into RemoteExecutionConfig; it is deleted, not half-supported."""
        from ue_runner_config import RemoteConfig

        assert not hasattr(RemoteConfig(), "timeout_seconds")
        for rel in ("lib/ue_runner_config.py", "skills/ue-python-api/ue_runner_config.yaml"):
            src = (_PLUGIN_DIR / rel).read_text(encoding="utf-8")
            assert "timeout_seconds" not in src, f"timeout_seconds resurfaced in {rel}"
