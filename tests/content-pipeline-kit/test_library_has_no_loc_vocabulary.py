"""The files the convergence-extension and provenance units added or changed carry no consumer vocabulary.

Scoped to those files: other library modules predate this check and already say
"glossary" (providers/assembly.py, providers/registry.py, validate/__init__.py).
"""

import re
from pathlib import Path

LIB = Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit" / "lib" / "content_pipeline"
SCOPED = [
    "llm/convergence.py",
    "llm/platform.py",
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
