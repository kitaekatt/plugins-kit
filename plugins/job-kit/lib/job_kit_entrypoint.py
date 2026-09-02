"""Bootstrap the job-kit venv before loading the package CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence

from bootstrap_guard import reexec_under_plugin_venv


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the job-kit CLI after selecting its provisioned interpreter."""
    original_argv = list(sys.argv)
    shim_path = str(Path(__file__).resolve())
    # reexec_under_plugin_venv treats argv[0] as a script path. The setuptools
    # console wrapper is not Python source, so point the replacement interpreter
    # at this real shim instead. Preserve explicit arguments for the child.
    sys.argv = [shim_path, *(argv if argv is not None else sys.argv[1:])]
    try:
        reexec_under_plugin_venv("job-kit")
    finally:
        sys.argv = original_argv

    from job_kit.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main"]
