"""Activation check 2 of the llm-kit migration.

The signed design (docs/reference/llm-kit-architecture.md, "Skills, agents,
workflows, and CLIs") makes activation conditional on three machine-checkable
conditions. Condition 1, the maintained-reference scan, is
``tests/repo-scripts/test_maintained_references.py``. This module holds the
persisted-name tripwire (condition 2). Condition 3 (rollback to the old plugins)
ended with their deletion.

Where qualified names are persisted
-----------------------------------

A qualified skill, agent or workflow name (``<plugin>:<name>``) is persisted
only by a store that records which registration was selected. Measured over
``packages/katsoft-llm-kit/src/llm_kit``:

* the job ledger (schema 12) and the pipeline execution store (schema 10) hold
  ids, states, tokens, endpoints, models, JSON payloads and event rows; no
  column, ``definition_json`` field, ``environment`` snapshot, dispatch row or
  event payload carries a skill, agent or workflow name;
* the background launcher builds ``claude --bg <extra_launch_args> -- <prompt>``
  per launch, persists neither the flags nor the prompt, and the prompt names a
  run, unit, worker, answer path, fencing token and CLI invocations, never a
  registration;
* the workflow lane's ``args`` object holds a run id, batch id and per-unit
  packs; the spend ledger, verdict cache, credentials and config hold none.

What does carry an old plugin-qualified string is the event-type and schema
vocabulary (``job-kit:run-created``, ``content-pipeline-kit:lease-renewed``,
``job-kit.interrupt-request/v1``). Those are persisted protocol identities, not
registrations, and the design preserves them byte for byte ("a branding rename
is not a data migration"); both implementations emit and read the same bytes,
so no old-name translator is needed or exists.

The records that DO name a registration are harness-owned, outside this
repository: a Claude Code background session launched with ``--agent
<plugin>:pipeline-worker`` and a resumable Workflow run of
``<plugin>:run-ready-wave``. The harness resolves those names against installed
plugins, so an llm-kit translator cannot intercept them; the frozen legacy
registrations keep resolving them through their own unchanged implementations,
and the retirement gate (zero nonterminal records needing an old identity) is an
operator check at deletion, not a repository fixture.

So condition 2 reduces to a tripwire
(:func:`test_no_registration_name_is_persistable_from_llm_kit_code`) that fails
the moment llm-kit code holds a qualified registration name as a string
constant, which is the precondition for persisting one, so a future change
cannot start persisting names without adding the translator and its fixtures
first.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Dict, Iterator, List

import pytest

REPO = Path(__file__).resolve().parents[2]
LLM_KIT = REPO / "plugins" / "llm-kit"
import llm_kit
LIB = Path(llm_kit.__file__).resolve().parent

# Spelled as fragments so that this file does not match the scan for old
# qualified references (tests/llm-kit/ is exempt from it, but keep it honest).
OLD_PLUGINS = ("llm-scripting-" + "kit", "content-pipeline-" + "kit", "job-" + "kit")
REGISTERED = (
    "openrouter-account",
    "content-pipeline-domain",
    "background-pipeline",
    "execute-work-unit",
    "workflow-pipeline",
    "pipeline-worker",
    "run-ready-wave",
)
_PLUGINS = "|".join(re.escape(p) for p in ("llm-kit", *OLD_PLUGINS))
_NAMES = "|".join(re.escape(n) for n in REGISTERED)
QUALIFIED = re.compile(rf"(?<![\w-])(?:{_PLUGINS}):(?:{_NAMES})(?![\w-])")


# --- condition 2: no registration name is persistable from llm-kit code -----

def _docstring_nodes(tree: ast.AST) -> set:
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                nodes.add(id(body[0].value))
    return nodes


def qualified_string_constants(lib: Path) -> Iterator[str]:
    """``path:line: constant`` for each non-docstring string constant under
    ``lib`` that is a qualified registration name, old or final."""
    for path in sorted(lib.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                and QUALIFIED.search(node.value)
            ):
                yield f"{path.relative_to(lib.parent).as_posix()}:{node.lineno}: {QUALIFIED.search(node.value).group(0)}"


def test_no_registration_name_is_persistable_from_llm_kit_code() -> None:
    hits = list(qualified_string_constants(LIB))
    assert not hits, (
        "llm-kit code holds a qualified registration name as a string constant, so it can "
        "persist one. Before activation, add an old-name translator inside llm-kit and "
        "old-created and rollback resume fixtures (design: 'Skills, agents, workflows, "
        "and CLIs'), then record the persisting store in this module's docstring:\n"
        + "\n".join(hits)
    )


@pytest.mark.parametrize(
    "literal",
    [
        "claude --bg --agent " + "content-pipeline-" + "kit:pipeline-worker",
        "llm-kit:execute-work-unit",
        "{}:run-ready-wave".format("content-pipeline-" + "kit"),
    ],
)
def test_the_tripwire_can_fail(tmp_path: Path, literal: str) -> None:
    lib = tmp_path / "llm_kit"
    lib.mkdir()
    module = lib / "persist.py"
    module.write_text(f"AGENT = {literal!r}\n", encoding="utf-8")
    assert list(qualified_string_constants(lib)), "a planted qualified name was not seen"
    module.write_text("AGENT = 'pipeline-worker'\n", encoding="utf-8")  # restored
    assert not list(qualified_string_constants(lib))


def test_the_tripwire_ignores_docstrings_and_event_identities(tmp_path: Path) -> None:
    lib = tmp_path / "llm_kit"
    lib.mkdir()
    (lib / "ok.py").write_text(
        '"""Mentions llm-kit:execute-work-unit in prose."""\n'
        "EVENT = 'job-kit:run-created'\n"
        "SCHEMA = 'job-kit.interrupt-request/v1'\n",
        encoding="utf-8",
    )
    assert not list(qualified_string_constants(lib))


def test_persisted_event_identities_are_not_registrations() -> None:
    """The old-namespaced event types are the preserved protocol vocabulary.

    They must stay recognizable as identities and distinct from registrations,
    otherwise the tripwire's exclusion above would hide a real persisted name.
    """
    for identity in ("job-kit:run-created", "content-pipeline-kit:run-created"):
        assert not QUALIFIED.search(identity)
