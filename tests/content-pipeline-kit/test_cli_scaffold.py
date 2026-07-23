"""Tests for content_pipeline.cli.scaffold.

Pins the CLI scaffold shape: subcommand dispatch to thin handlers, did-you-mean
on an unknown command (difflib), scope filtering with a did-you-mean on a miss,
uniform YAML output, and stable exit codes.
"""

import io

from content_pipeline.cli.scaffold import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    Command,
    did_you_mean,
    dispatch,
    emit_yaml,
    filter_scope,
)


# -- dispatch -----------------------------------------------------------------

def test_dispatch_calls_matching_handler():
    out = io.StringIO()
    code = dispatch(
        ["greet", "world"],
        {"greet": lambda args: {"said": args[0]}},
        out=out,
    )
    assert code == EXIT_OK
    assert "said: world" in out.getvalue()


def test_dispatch_unknown_command_suggests():
    err = io.StringIO()
    code = dispatch(
        ["generat"],  # typo for "generate"
        {"generate": lambda a: None, "apply": lambda a: None},
        err=err,
    )
    assert code == EXIT_USAGE
    assert "Did you mean: generate" in err.getvalue()


def test_dispatch_no_args_prints_usage():
    err = io.StringIO()
    code = dispatch([], {"generate": lambda a: None}, err=err)
    assert code == EXIT_USAGE
    assert "commands: generate" in err.getvalue()


def test_dispatch_handler_error_maps_to_exit_error():
    err = io.StringIO()

    def boom(args):
        raise RuntimeError("kaboom")

    code = dispatch(["cmd"], {"cmd": boom}, err=err)
    assert code == EXIT_ERROR
    assert "kaboom" in err.getvalue()


def test_dispatch_systemexit_propagates_code():
    def handler(args):
        raise SystemExit(3)

    assert dispatch(["cmd"], {"cmd": handler}, out=io.StringIO()) == 3


def test_dispatch_accepts_command_objects():
    out = io.StringIO()
    cmd = Command(name="x", handler=lambda a: {"ok": True}, help="does x")
    assert dispatch(["x"], {"x": cmd}, out=out) == EXIT_OK
    assert "ok: true" in out.getvalue()


# -- did-you-mean -------------------------------------------------------------

def test_did_you_mean_suggests_close():
    assert "generate" in did_you_mean("generat", ["generate", "apply", "revert"])


def test_did_you_mean_empty_on_no_close_match():
    assert did_you_mean("zzzzz", ["generate", "apply"]) == []


# -- scope filtering ----------------------------------------------------------

def test_filter_scope_keeps_matches():
    items = ["Bear_walk", "Bear_idle", "Fox_run"]
    kept, suggestions = filter_scope(
        items, "Bear", match=lambda item, v: v in item
    )
    assert kept == ["Bear_walk", "Bear_idle"]
    assert suggestions == []


def test_filter_scope_empty_value_keeps_all():
    items = ["a", "b"]
    kept, _ = filter_scope(items, "", match=lambda i, v: False)
    assert kept == items  # empty scope == whole corpus


def test_filter_scope_miss_suggests_from_universe():
    items = ["Bear_walk"]
    kept, suggestions = filter_scope(
        items,
        "Baer",  # typo
        match=lambda item, v: v in item,
        universe=["Bear", "Fox"],
    )
    assert kept == []
    assert "Bear" in suggestions


# -- emit_yaml ----------------------------------------------------------------

def test_emit_yaml_preserves_key_order():
    text = emit_yaml({"z": 1, "a": 2})
    assert text.splitlines() == ["z: 1", "a: 2"]  # not sorted


def test_emit_yaml_none_is_empty():
    assert emit_yaml(None) == ""
