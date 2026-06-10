"""Tests for _detect_script_error -- commandlet stdout Python-error detection.

Regression for U12: the old implementation whitelisted 7 exception types, so a
KeyError/RuntimeError/IndexError/etc. raised by our script was invisible and
the run could be declared a success. The detector now flags any contiguous
"LogPython: Error:" block that references the script we ran, regardless of
exception type, while still ignoring startup-script (Content/Python/) failures
that don't involve our script.
"""

import sys
from pathlib import Path

# Add scripts/ and lib/ to path
_SKILL_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "unreal-kit" / "skills" / "ue-python-api"
_PLUGIN_DIR = _SKILL_DIR.parent.parent
_SCRIPTS_DIR = _SKILL_DIR / "scripts"
_LIB_DIR = _PLUGIN_DIR / "lib"
for p in (_SCRIPTS_DIR, _LIB_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ue_runner import _detect_script_error

SCRIPT = "C:/work/my_script.py"


class TestDetectScriptError:
    def test_clean_output_is_not_an_error(self):
        stdout = (
            "LogInit: Display: Engine is initialized.\n"
            "LogPython: ran fine\n"
            "LogExit: Exiting.\n"
        )
        assert _detect_script_error(stdout, SCRIPT) is False

    def test_direct_error_line_referencing_script(self):
        stdout = "LogPython: Error: Failed to run 'C:/work/my_script.py'\n"
        assert _detect_script_error(stdout, SCRIPT) is True

    def test_keyerror_traceback_detected(self):
        """KeyError was NOT in the old whitelist -- the regression case."""
        stdout = (
            "LogPython: Error: Traceback (most recent call last):\n"
            'LogPython: Error:   File "C:/work/my_script.py", line 10, in <module>\n'
            "LogPython: Error:     data['missing']\n"
            "LogPython: Error: KeyError: 'missing'\n"
            "LogExit: Exiting.\n"
        )
        assert _detect_script_error(stdout, SCRIPT) is True

    def test_runtimeerror_traceback_detected(self):
        """RuntimeError was NOT in the old whitelist either."""
        stdout = (
            "LogPython: Error: Traceback (most recent call last):\n"
            'LogPython: Error:   File "C:/work/my_script.py", line 4, in <module>\n'
            "LogPython: Error:     raise RuntimeError('boom')\n"
            "LogPython: Error: RuntimeError: boom\n"
        )
        # Block at end of output (no trailing non-error line) must also count.
        assert _detect_script_error(stdout, SCRIPT) is True

    def test_startup_script_error_is_ignored(self):
        """UE auto-runs Content/Python/ startup scripts that commonly fail in
        commandlet mode; their errors must not be charged to our script."""
        stdout = (
            "LogPython: Error: Traceback (most recent call last):\n"
            'LogPython: Error:   File "D:/Proj/Content/Python/init_unreal.py", line 3, in <module>\n'
            "LogPython: Error: ModuleNotFoundError: No module named 'debugpy'\n"
            "LogInit: Display: Engine is initialized.\n"
        )
        assert _detect_script_error(stdout, SCRIPT) is False

    def test_startup_error_block_then_our_error_block(self):
        """A startup failure followed by a genuine failure in our script:
        the second block must still be detected."""
        stdout = (
            "LogPython: Error: Traceback (most recent call last):\n"
            'LogPython: Error:   File "D:/Proj/Content/Python/init_unreal.py", line 3, in <module>\n'
            "LogPython: Error: ModuleNotFoundError: No module named 'debugpy'\n"
            "LogInit: Display: Engine is initialized.\n"
            "LogPython: Error: Traceback (most recent call last):\n"
            'LogPython: Error:   File "C:/work/my_script.py", line 7, in <module>\n'
            "LogPython: Error: IndexError: list index out of range\n"
        )
        assert _detect_script_error(stdout, SCRIPT) is True

    def test_script_name_outside_error_block_is_not_an_error(self):
        """A plain log line mentioning the script (e.g. the command echo) must
        not trip the detector."""
        stdout = (
            "LogPython: running script C:/work/my_script.py\n"
            "LogPython: Error: Traceback (most recent call last):\n"
            'LogPython: Error:   File "D:/Proj/Content/Python/other.py", line 1, in <module>\n'
            "LogPython: Error: ValueError: nope\n"
        )
        assert _detect_script_error(stdout, SCRIPT) is False
