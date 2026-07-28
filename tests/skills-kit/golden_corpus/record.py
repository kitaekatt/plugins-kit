"""Re-record the golden corpus: run every case against the staged fixtures
and overwrite expected/<case>.json.

    uv run python tests/skills-kit/golden_corpus/record.py

Run this ONLY when an output change is intended (a new rule, a fixed
message, a fixture edit). Review the resulting diff like code: every changed
line in expected/ is a behavior change the next release ships.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import corpus_runner


def main() -> int:
    corpus_runner.EXPECTED.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        staged = corpus_runner.stage(Path(tmp))
        for case_id in corpus_runner.CASES:
            result = corpus_runner.run_case(case_id, staged)
            out = corpus_runner.EXPECTED / f"{case_id}.json"
            out.write_text(
                json.dumps(result, indent=2, sort_keys=False) + "\n",
                encoding="utf-8", newline="\n",
            )
            print(f"recorded {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
