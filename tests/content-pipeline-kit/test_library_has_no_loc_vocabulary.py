"""The library files written since this gate exists carry no consumer vocabulary.

Scoped to an explicit file list: other library modules predate this check and
already say "glossary" (providers/assembly.py, providers/registry.py,
validate/__init__.py). Every NEW library module belongs on this list -- a file
that is merely absent from it is unguarded, which is indistinguishable from a
file that passes.
"""

import re
from pathlib import Path

LIB = Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit" / "lib" / "content_pipeline"
SCOPED = [
    "llm/convergence.py",
    "llm/platform.py",
    "llm/spend_ledger.py",
    "pipeline/convergence_loop.py",
    "pipeline/cell_policy.py",
    "store/candidate.py",
    "provenance/__init__.py",
    "provenance/record.py",
    "provenance/call_audit.py",
    "provenance/snapshot.py",
    "provenance/replay.py",
]
TERMS = re.compile(r"glossary|\bD7\b|LOCK_FLOOR|unproducible|fills_blocked", re.I)


def test_library_has_no_loc_vocabulary():
    hits = []
    for rel in SCOPED:
        text = (LIB / rel).read_text(encoding="utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if TERMS.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "\n".join(hits)
