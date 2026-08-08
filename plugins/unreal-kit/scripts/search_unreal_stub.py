"""Search the best available Unreal API stub with graceful fallback."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPT_DIR.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from bootstrap_guard import reexec_under_plugin_venv, require_bootstrap  # noqa: E402

reexec_under_plugin_venv("unreal-kit")
require_bootstrap("unreal-kit", feature="Unreal API search")

try:
    from unreal_stub import (  # noqa: E402
        deferred_requirement_message,
        load_effective_config,
        select_search_stub,
    )
except ImportError:
    require_bootstrap(
        "unreal-kit",
        feature="Unreal API search",
        missing="bootstrap_lib",
        force=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Search the Unreal API stub.")
    parser.add_argument("pattern", help="Case-insensitive regular expression.")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Consuming project root (default: current directory).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Search enriched, then stock, and explain how to recover if neither exists."""
    args = parse_args(argv)
    project_root = args.project_root.resolve()
    config = load_effective_config(project_root)
    stub = select_search_stub(project_root, config)
    if stub is None:
        print(
            "Unreal API search is unavailable: neither the consuming project's "
            "enriched stub nor the machine-local stock stub exists.",
            file=sys.stderr,
        )
        recorded = deferred_requirement_message("unreal_enriched_stub")
        if recorded:
            print(recorded, file=sys.stderr)
        else:
            print(
                "Start a new Claude Code session to let bootstrap download the stock "
                "stub. For an enriched stub, enable Developer Mode, complete a full "
                "compile, then run `python ${CLAUDE_PLUGIN_ROOT}/scripts/"
                "refresh_unreal_stub.py --project-root <project-root>`.",
                file=sys.stderr,
            )
        return 2

    try:
        pattern = re.compile(args.pattern, re.IGNORECASE)
    except re.error as exc:
        print(f"Invalid search pattern: {exc}", file=sys.stderr)
        return 2

    found = False
    with stub.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            if pattern.search(line):
                found = True
                print(f"{stub}:{line_number}:{line.rstrip()}")
    return 0 if found else 1


if __name__ == "__main__":
    raise SystemExit(main())
