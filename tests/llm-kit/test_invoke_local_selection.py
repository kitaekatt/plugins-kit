"""Protocol 2 reads ``request.selection`` through the personal layer (``<stem>.local.yaml``).

Local wins per key, a key only the local file names resolves, and a refusal that
comes from the local file names it. Built on the fake transports of
``test_invoke_protocol`` (no network, no CLI, throwaway HOME).
"""

from __future__ import annotations

import json
from pathlib import Path

from test_cli_harness import home  # noqa: F401 -- fixture the world fixture depends on
from test_invoke_protocol import allow, call, world  # noqa: F401 -- world is a fixture
from llm_kit.registry import project_models as pm

LOCAL = ".llm-kit/models.local.yaml"


def _override(root: Path, **selections) -> None:
    pm.write_local_overrides({"version": 1, "selections": selections}, project_root=str(root))


def test_a_local_override_changes_the_effort_used_for_a_selection(world, capsys):
    """Red on revert: read the tracked document only and the tracked effort 'max' is sent."""
    allow()
    _override(world.root, art={"model": "deepseek", "effort": "low"})
    code, doc, err = call(world, capsys, selection="art")
    assert code == 0, (doc, err)
    assert doc["resolved"]["effort"] == {"requested": "low", "sent": "low", "source": "selection"}
    assert world.http[0]["extra_body"]["reasoning_effort"] == "low"


def test_a_local_override_changes_the_model_used_for_a_selection(world, capsys):
    allow()
    _override(world.root, chat={"model": "deepseek", "effort": "high"})  # tracked: sonnet, no effort
    code, doc, err = call(world, capsys, selection="chat")
    assert code == 0, (doc, err)
    assert doc["selection"] == {"id": "chat", "model": "deepseek"}
    assert doc["resolved"]["requested_model"] == "deepseek/deepseek-v4.1-flash"
    assert doc["resolved"]["effort"]["requested"] == "high"
    assert len(world.http) == 1 and not world.claude


def test_a_key_only_the_local_file_names_resolves(world, capsys):
    allow()
    _override(world.root, mine={"model": "deepseek", "effort": "none"})
    code, doc, err = call(world, capsys, selection="mine")
    assert code == 0, (doc, err)
    assert doc["selection"] == {"id": "mine", "model": "deepseek"}
    assert doc["resolved"]["effort"]["requested"] == "none"


def test_a_selection_the_local_file_does_not_name_keeps_the_tracked_value(world, capsys):
    allow()
    _override(world.root, art={"model": "deepseek", "effort": "low"})
    code, doc, _ = call(world, capsys, selection="draft.generate.primary")
    assert code == 0
    assert doc["resolved"]["requested_model"] == "gpt-6-luna"
    assert doc["resolved"]["effort"] == {"requested": "high", "sent": "high", "source": "selection"}


def test_an_explicit_request_effort_still_beats_the_overridden_selection(world, capsys):
    allow()
    _override(world.root, art={"model": "deepseek", "effort": "low"})
    code, doc, _ = call(world, capsys, selection="art", effort="high")
    assert code == 0
    assert doc["resolved"]["effort"] == {"requested": "high", "sent": "high", "source": "request"}


def test_an_unknown_selection_names_both_files_and_every_known_key(world, capsys):
    allow()
    _override(world.root, mine={"model": "deepseek", "effort": "low"})
    code, doc, err = call(world, capsys, selection="ghost")
    assert code == 4 and doc is None and err["error"]["kind"] == "selection"
    message = err["error"]["message"]
    assert LOCAL in message and ".llm-kit/models.yaml" in message and "mine" in message and "art" in message
    assert world.runner_calls == 0


def test_an_override_naming_an_unknown_catalogue_id_refuses_and_names_the_local_file(world, capsys):
    """Red on revert: validate nothing in the local layer and the unknown id would reach the lookup unnamed."""
    allow()
    path = world.root / LOCAL
    path.write_text("version: 1\nselections:\n  art: {model: ghost, effort: null}\n", encoding="utf-8")
    code, doc, err = call(world, capsys, selection="art")
    assert code == 4 and doc is None
    assert err["error"]["kind"] == "selection"
    assert LOCAL in err["error"]["message"] and "'ghost' is not a catalog id" in err["error"]["message"]
    assert world.runner_calls == 0
    json.dumps(err)  # the refusal is a JSON document


def test_a_broken_local_file_does_not_block_a_call_that_names_a_model(world, capsys):
    """The personal layer only matters to a selection; a row id never reads it."""
    allow()
    (world.root / LOCAL).write_text("version: 1\nselections:\n  art: {model: ghost, effort: null}\n", encoding="utf-8")
    code, doc, err = call(world, capsys, model="deepseek", effort="low")
    assert code == 0, (doc, err)
