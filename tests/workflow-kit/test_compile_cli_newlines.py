"""compile_workflow.py must emit LF-only output on every platform.

The Workflow tool refuses a script containing control characters (a CR is one),
and Windows text-mode writes translate "\\n" to "\\r\\n".
"""

import importlib.util
import os
import subprocess
import sys

from wk_testlib import EXAMPLES, PLUGIN_ROOT

WORKFLOW = EXAMPLES / "review-changes.workflow.yaml"
SCRIPT = PLUGIN_ROOT / "scripts" / "compile_workflow.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("workflow_kit_compile_cli_newlines", SCRIPT)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_out_file_has_no_carriage_return(tmp_path):
    out = tmp_path / "out.js"
    assert _load_cli().main([str(WORKFLOW), "-o", str(out)]) == 0
    data = out.read_bytes()
    assert b"\n" in data
    assert b"\r" not in data


def test_stdout_has_no_carriage_return():
    # A real child process: in-process capture bypasses the text-mode
    # translation Windows applies to a real stdout pipe.
    env = dict(os.environ, PYTHONPATH=str(PLUGIN_ROOT.parent / "bootstrap"))
    env["_BOOTSTRAP_GUARD_VENV_REEXEC"] = "1"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(WORKFLOW)], capture_output=True, env=env
    )
    assert proc.returncode == 0, proc.stderr
    assert b"\n" in proc.stdout
    assert b"\r" not in proc.stdout
