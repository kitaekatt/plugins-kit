"""Pure tests for the root conftest's real-user PATH diagnostic."""

import importlib.util
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "root_conftest", Path(__file__).with_name("conftest.py")
)
_ROOT_CONFTEST = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_ROOT_CONFTEST)
_describe_path_change = _ROOT_CONFTEST._describe_path_change


def test_describes_added_entries_in_after_order():
    description = _describe_path_change(("before;keep", 1), ("keep;added-a;added-b", 1))

    assert description == (
        "ADDED:\n  added-a\n  added-b\nREMOVED:\n  before"
    )


def test_describes_removed_entries_in_before_order():
    description = _describe_path_change(("gone-a;keep;gone-b", 1), ("keep", 1))

    assert description == "ADDED:\n  (none)\nREMOVED:\n  gone-a\n  gone-b"


def test_describes_created_value():
    description = _describe_path_change(None, ("created", 2))

    assert description == "Path value was created.\nADDED:\n  created\nREMOVED:\n  (none)"


def test_describes_deleted_value():
    description = _describe_path_change(("deleted", 2), None)

    assert description == "Path value was deleted.\nADDED:\n  (none)\nREMOVED:\n  deleted"


def test_describes_type_change():
    description = _describe_path_change(("same", 1), ("same", 2))

    assert description == (
        "Path value type changed from 1 to 2.\n"
        "ADDED:\n  (none)\nREMOVED:\n  (none)"
    )
