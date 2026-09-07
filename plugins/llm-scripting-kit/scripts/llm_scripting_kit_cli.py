"""Compatibility launcher for the installed :mod:`llm_scripting_kit.cli`.

This file has no logic of its own -- it exists so ``bin/llm-scripting-kit``
(and its Windows ``.cmd`` twin) has a stable, interpreter-agnostic script path
to invoke. It puts the plugin's bundled ``lib/`` on ``sys.path`` (so the
package resolves without an install step) and delegates straight to
:func:`llm_scripting_kit.cli.main`, which owns every subcommand -- ``status``,
``set-key``, ``which``, ``endpoints``, ``probe``, ``usage``, ``choose``,
``seats``, ``models``, ``resolve``, ``complete``, and ``request-schema``.
See ``README.md`` for the full verb set and exit-code contract.

Runs under the plugin venv when bootstrap has provisioned it (PyYAML there lets
the layered config.yaml be read), and degrades to stdlib-only otherwise: without
PyYAML the shipped model baseline is used and a warning goes to stderr, but key
management keeps working. The shims in ``bin/`` pick the interpreter.
"""

import sys
from pathlib import Path

# Make the bundled lib/ importable when invoked directly.
_HERE = Path(__file__).resolve().parent
_LIB_DIR = _HERE.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


if __name__ == "__main__":
    from llm_scripting_kit.cli import main as package_main

    sys.exit(package_main())
