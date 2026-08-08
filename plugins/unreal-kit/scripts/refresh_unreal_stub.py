"""Explicitly refresh the consuming project's durable Unreal API stub."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPT_DIR.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from bootstrap_guard import reexec_under_plugin_venv, require_bootstrap  # noqa: E402

reexec_under_plugin_venv("unreal-kit")
require_bootstrap("unreal-kit", feature="Unreal API stub refresh")

try:
    from unreal_stub import load_effective_config, refresh_durable_stub  # noqa: E402
except ImportError:
    require_bootstrap(
        "unreal-kit",
        feature="Unreal API stub refresh",
        missing="bootstrap_lib",
        force=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Refresh the consuming project's durable Unreal API stub."
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Consuming project root (default: current directory).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Resolve the durable destination, announce it, and copy the stub."""
    args = parse_args(argv)
    project_root = args.project_root.resolve()
    try:
        config = load_effective_config(project_root)
        destination = refresh_durable_stub(project_root, config, print)
    except ValueError as exc:
        print(f"Cannot refresh enriched Unreal API stub: {exc}.", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(
            "Cannot refresh enriched Unreal API stub: generated source not found at "
            f"{exc.filename or exc}. Enable Developer Mode and complete a full "
            "compile, then retry.",
            file=sys.stderr,
        )
        return 2
    print(f"Refreshed enriched Unreal API stub at {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
