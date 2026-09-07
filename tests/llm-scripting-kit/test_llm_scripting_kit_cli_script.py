"""Pins scripts/llm_scripting_kit_cli.py's ``__main__`` launcher.

Nothing pinned the launcher before this file: its dead ~150-line body
(main/_cmd_status/_cmd_set_key/_cmd_which/_resolve_endpoint_or_exit) was
unreachable -- ``if __name__ == "__main__":`` delegates straight to
``llm_scripting_kit.cli.main`` -- but nothing proved that delegation actually
runs the script the way ``bin/llm-scripting-kit`` invokes it (as ``__main__``
via a subprocess).
"""

import subprocess
import sys
from pathlib import Path


def test_running_the_script_as_main_delegates_to_the_package_cli(capsys, plugin_root):
    """``python scripts/llm_scripting_kit_cli.py status`` == ``llm_scripting_kit.cli.main(["status"])``.

    No key is configured in the isolated-HOME sandbox, so both paths take the
    deterministic "missing key" branch (exit 1, no interactive prompt) rather
    than needing a real credential to compare output.
    """
    script = Path(plugin_root) / "scripts" / "llm_scripting_kit_cli.py"

    result = subprocess.run(
        [sys.executable, str(script), "status"],
        text=True,
        capture_output=True,
        check=False,
    )

    from llm_scripting_kit.cli import main as package_main

    direct_exit = package_main(["status"])
    direct = capsys.readouterr()

    assert result.returncode == direct_exit
    assert result.stdout == direct.out
