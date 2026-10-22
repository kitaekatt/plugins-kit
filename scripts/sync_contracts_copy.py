#!/usr/bin/env python3
"""Keep bootstrap_lib's contract modules byte copies of katsoft_contracts.

The canonical source of three modules is packages/katsoft-contracts/src/
katsoft_contracts/. bootstrap_lib keeps full copies (it must work in a venv
that holds no katsoft-contracts), so an edit is made in the canonical file and
this script copies it over:

    uv run python scripts/sync_contracts_copy.py          # copy canonical -> bootstrap_lib
    uv run python scripts/sync_contracts_copy.py --check  # report drift, change nothing

tests/contracts/test_bootstrap_lib_copy_identical.py compares the same pairs.
Standard library only; paths resolve from this file, so any working directory
works.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CANONICAL_DIR = REPO / "packages" / "katsoft-contracts" / "src" / "katsoft_contracts"
COPY_DIR = REPO / "plugins" / "bootstrap" / "bootstrap_lib"
MODULES = ("execution_event", "interrupt_contract", "model_declaration")


def _shown(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return path.as_posix()


def pairs() -> list[tuple[Path, Path]]:
    """(canonical, copy) for every contract module."""
    return [(CANONICAL_DIR / f"{name}.py", COPY_DIR / f"{name}.py") for name in MODULES]


def drifted() -> list[Path]:
    """The copies whose bytes differ from, or are missing against, canonical."""
    out = []
    for canonical, copy in pairs():
        if not canonical.is_file():
            raise SystemExit(f"error: canonical module missing: {_shown(canonical)}")
        if not copy.is_file() or copy.read_bytes() != canonical.read_bytes():
            out.append(copy)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="report drift and exit 1; write nothing")
    args = parser.parse_args(argv)
    stale = drifted()
    if args.check:
        for copy in stale:
            print(f"drift: {_shown(copy)}")
        if stale:
            print("run scripts/sync_contracts_copy.py to refresh the copies", file=sys.stderr)
        return 1 if stale else 0
    for canonical, copy in pairs():
        if copy in stale:
            copy.write_bytes(canonical.read_bytes())
            print(f"updated: {_shown(copy)}")
    if not stale:
        print("bootstrap_lib copies already identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
