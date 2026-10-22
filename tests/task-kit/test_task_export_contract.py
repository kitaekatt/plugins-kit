"""The task export's frozen contract: golden documents and the protocol-1 schema.

Every golden document validates against ``task-export.schema.json`` and equals
the export's document byte for byte after :func:`task_export_support.normalize`.
Revert checks: building the payload from the dashboard's view (or dropping a
protocol-1 field such as ``updates_total``) turns these red.
"""

from __future__ import annotations

import copy

import pytest
from task_export_support import (
    GOLDEN,
    check_golden,
    inject,
    load_schema,
    make_world,
    normalize,
    schema_errors,
)

from task_system import export
from task_system.export_cli import document

CHECKOUT = "<checkout:alpha>"


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def _cid(parent, name):
    return export.checkout_id(parent / name)


def _cases(parent):
    alpha = _cid(parent, "alpha")
    tasks = ["tasks", "--format", "json", "--protocol", "1", "--parent", str(parent)]
    item = ["item", "--format", "json", "--protocol", "1", "--parent", str(parent), "--checkout", alpha]
    return {
        "protocols": (["protocols", "--format", "json"], 0),
        "tasks": (tasks, 0),
        "item": (item + ["--task", "dev/tasks/build-export", "--item", "write-schema"], 0),
        "item-no-section": (item + ["--task", "dev/tasks/build-export", "--item", "write-cli"], 0),
        "error-unknown-checkout": (
            ["item", "--format", "json", "--protocol", "1", "--parent", str(parent), "--checkout", "0" * 16,
             "--task", "dev/tasks/build-export", "--item", "write-schema"], 3),
        "error-unknown-task": (item + ["--task", "dev/tasks/no-such-task", "--item", "write-schema"], 3),
        "error-unknown-item": (item + ["--task", "dev/tasks/build-export", "--item", "no-such-item"], 3),
        "error-protocol-unsupported": (["tasks", "--format", "json", "--protocol", "2"], 4),
        "error-usage": (["tasks", "--format", "json"], 2),
    }


CASE_NAMES = (
    "protocols", "tasks", "item", "item-no-section", "error-unknown-checkout", "error-unknown-task",
    "error-unknown-item", "error-protocol-unsupported", "error-usage",
)


@pytest.mark.parametrize("name", CASE_NAMES)
def test_every_golden_document_matches_and_validates(world, name):
    argv, expected_exit = _cases(world)[name]
    code, doc = document(argv, **inject())
    assert code == expected_exit, doc
    assert schema_errors(doc, load_schema(), load_schema()) == []
    check_golden(name, doc, world)


def test_every_golden_file_is_a_case():
    assert sorted(path.stem for path in GOLDEN.glob("*.json")) == sorted(CASE_NAMES)


def test_the_tasks_golden_covers_the_fixture(world):
    _code, doc = document(_cases(world)["tasks"][0], **inject())
    payload = normalize(doc, world)["payload"]
    [repository] = payload["repositories"]  # two checkouts of one origin are one repository
    assert (repository["name"], repository["checkouts"]) == ("alpha", [CHECKOUT, "<checkout:alpha-two>"])
    by_path = {t["path"]: t for t in payload["tasks"]}
    assert {t["classification"] for t in payload["tasks"]} == {"active", "blocked", "closed", "deferred"}
    assert by_path["dev/tasks/later-idea"]["checkout"] == "<checkout:alpha-two>"
    states = [i["state"] for i in by_path["dev/tasks/build-export"]["items"]]
    assert sorted(states) == ["available", "blocked-user", "deferred", "in-flight"]
    [session] = by_path["dev/tasks/build-export"]["sessions"]
    assert (session["host"], session["state"], session["local"]) == ("othermac", "unverified", False)
    assert by_path["dev/tasks/build-export"]["updates_total"] == 3
    assert payload["errors"] == []


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda t: t.pop("updates_total"), id="removed"),
        pytest.param(lambda t: t.__setitem__("updates_total", "3"), id="retyped"),
        pytest.param(lambda t: t.__setitem__("has_items_block", None), id="nulled"),
        pytest.param(lambda t: t.__setitem__("added_without_schema", 1), id="undocumented"),
    ],
)
def test_the_schema_refuses_a_changed_protocol_1_field(world, mutate):
    _code, doc = document(_cases(world)["tasks"][0], **inject())
    changed = copy.deepcopy(doc)
    mutate(changed["payload"]["tasks"][0])
    assert schema_errors(changed, load_schema(), load_schema()) != []
