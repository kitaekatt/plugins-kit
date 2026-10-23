"""Launcher target for ``bin/llm-kit`` and ``bin/llm-kit.cmd``.

This file has no logic of its own. It delegates to :func:`llm_kit.cli.main`,
imported from whichever environment the launcher chose: the plugin venv, where
``llm_kit`` is the installed ``katsoft-llm-kit`` package, or the standalone
interpreter, where bootstrap's shared-library link provides it. It puts no
directory of this plugin on ``sys.path``, so the library that runs is the
installed one and never a copy beside the launcher.
"""
from __future__ import annotations


def _run() -> int:
    from llm_kit.cli import main  # noqa: PLC0415 -- resolved from the chosen interpreter

    return main()


if __name__ == "__main__":
    raise SystemExit(_run())
