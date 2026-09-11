"""Executable parsing for reviewer-lane output.

Both native Agent lanes and endpoint lanes use the same prepared review bundle
to verify reviewer_a citations against the CLAUDE.md chain that governs the
reported file. This module stays LLM-neutral so every code-review kit can call
it without depending on llm-scripting-kit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from bootstrap_lib.code_review.lane_prompts import LaneOutputError, parse_issue_array


class LaneOutputInputError(ValueError):
    """A response or bundle file could not supply parser input."""


def claude_mds_by_file(bundle: Mapping[str, Any]) -> dict[str, Sequence[str]]:
    """Return governing chains keyed by every reportable file spelling."""
    result: dict[str, Sequence[str]] = {}
    changed_files = bundle.get("changed_files", ())
    if not isinstance(changed_files, list):
        raise LaneOutputInputError("bundle.changed_files must be an array")
    for index, entry in enumerate(changed_files):
        if not isinstance(entry, Mapping):
            raise LaneOutputInputError(
                f"bundle.changed_files[{index}] must be an object"
            )
        chain = entry.get("claude_mds", ())
        if not isinstance(chain, list) or not all(
            isinstance(path, str) for path in chain
        ):
            raise LaneOutputInputError(
                f"bundle.changed_files[{index}].claude_mds must be an array of strings"
            )
        for key in ("path", "depot", "local"):
            file_name = entry.get(key)
            if isinstance(file_name, str) and file_name:
                result[file_name] = tuple(chain)
    return result


def load_bundle(path: Path) -> Mapping[str, Any]:
    """Read one prepared review bundle from disk."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LaneOutputInputError(f"cannot read bundle {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LaneOutputInputError(
            f"bundle {path} is not valid JSON: {exc.msg} at line {exc.lineno}"
        ) from exc
    if not isinstance(value, Mapping):
        raise LaneOutputInputError(f"bundle {path} must contain a JSON object")
    return value


def parse_lane_output(
    text: str,
    *,
    lane: str,
    bundle: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Parse one lane response using chains from its prepared review bundle."""
    return parse_issue_array(
        text,
        lane=lane,
        claude_mds_by_file=claude_mds_by_file(bundle),
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="parse_review_lane",
        description="Validate one native reviewer lane's JSON output.",
    )
    parser.add_argument("--lane", required=True, help="reviewer lane name")
    parser.add_argument(
        "--response",
        required=True,
        type=Path,
        help="file containing the lane response",
    )
    parser.add_argument(
        "--bundle", required=True, type=Path, help="prepared review bundle.json"
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Print the verified issue array as JSON."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        response = args.response.read_text(encoding="utf-8")
        issues = parse_lane_output(
            response,
            lane=args.lane,
            bundle=load_bundle(args.bundle),
        )
    except OSError as exc:
        print(
            f"lane {args.lane}: cannot read response {args.response}: {exc}",
            file=sys.stderr,
        )
        return 2
    except (LaneOutputError, LaneOutputInputError) as exc:
        print(f"lane {args.lane}: {exc}", file=sys.stderr)
        return 1
    json.dump(issues, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


__all__ = [
    "LaneOutputInputError",
    "claude_mds_by_file",
    "load_bundle",
    "main",
    "parse_lane_output",
]


if __name__ == "__main__":
    sys.exit(main())
