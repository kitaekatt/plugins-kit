"""Differential parity: llm_kit.scaffold vs legacy content_pipeline.cli.scaffold."""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
LEGACY_FILE = (
    ROOT / "plugins" / "content-pipeline-kit" / "lib" / "content_pipeline" / "cli" / "scaffold.py"
)
LIB = ROOT / "packages" / "katsoft-llm-kit" / "src"


def _load_legacy() -> Any:
    spec = importlib.util.spec_from_file_location("_legacy_cp_scaffold", LEGACY_FILE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_legacy_cp_scaffold"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_final() -> Any:
    added = str(LIB) not in sys.path
    if added:
        sys.path.insert(0, str(LIB))
    try:
        import llm_kit.scaffold as mod
    finally:
        if added:
            sys.path.remove(str(LIB))
    return mod


legacy = _load_legacy()
final = _load_final()


def test_public_names_identical() -> None:
    assert list(final.__all__) == list(legacy.__all__)
    for name in legacy.__all__:
        assert hasattr(final, name)
    for const in ("EXIT_OK", "EXIT_ERROR", "EXIT_USAGE"):
        assert getattr(final, const) == getattr(legacy, const)


def _run(mod: Any, argv: list[str], make_cmds: Any, **kw: Any) -> tuple[Any, str, str]:
    out, err = io.StringIO(), io.StringIO()
    try:
        rc: Any = mod.dispatch(argv, make_cmds(mod), out=out, err=err, **kw)
    except BaseException as exc:  # noqa: BLE001 -- compare exceptions too
        rc = (type(exc).__name__, str(exc))
    return rc, out.getvalue(), err.getvalue()


def _cmds(mod: Any) -> dict[str, Any]:
    def boom(_a: list[str]) -> Any:
        raise ValueError("kaboom")

    def exit_str(_a: list[str]) -> Any:
        raise SystemExit("msg")

    def exit_none(_a: list[str]) -> Any:
        raise SystemExit()

    def exit_int(_a: list[str]) -> Any:
        raise SystemExit(7)

    return {
        "status": mod.Command("status", lambda a: {"args": a, "z": 1, "a": 2}, "h"),
        "stats": lambda a: [1, 2, "x"],
        "none": lambda a: None,
        "boom": boom,
        "exit_str": exit_str,
        "exit_none": exit_none,
        "exit_int": exit_int,
    }


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["status"],
        ["status", "a", "b"],
        ["statu"],
        ["zzzzzz"],
        ["stats"],
        ["none"],
        ["boom"],
        ["exit_str"],
        ["exit_none"],
        ["exit_int"],
    ],
)
def test_dispatch_parity(argv: list[str]) -> None:
    assert _run(final, argv, _cmds) == _run(legacy, argv, _cmds)


def test_dispatch_cutoff_and_render_parity() -> None:
    kw = {"cutoff": 0.9, "render": lambda r: "R" if r is not None else ""}
    for argv in (["statu"], ["stats"], ["none"]):
        assert _run(final, argv, _cmds, **kw) == _run(legacy, argv, _cmds, **kw)


def test_did_you_mean_and_filter_scope_parity() -> None:
    cands = ["alpha", "beta", "gamma", "alphabet"]
    for name in ("alpah", "bet", "zzz", ""):
        for kw in ({}, {"n": 1}, {"cutoff": 0.9}):
            assert final.did_you_mean(name, cands, **kw) == legacy.did_you_mean(name, cands, **kw)
    items = ["alpha", "beta", "alphabet"]
    match = lambda i, v: v in i  # noqa: E731
    for value in (None, "", "alp", "alpah", "zzz"):
        for universe in (None, cands):
            assert final.filter_scope(
                items, value, match=match, universe=universe
            ) == legacy.filter_scope(items, value, match=match, universe=universe)


def test_emit_yaml_parity() -> None:
    for value in (None, {"b": 1, "a": [1, 2]}, [], "s", {"u": "caf\u00e9"}, 3):
        assert final.emit_yaml(value) == legacy.emit_yaml(value)


def test_command_dataclass_parity() -> None:
    f, l = final.Command("n", len, "h"), legacy.Command("n", len, "h")
    assert (f.name, f.handler, f.help) == (l.name, l.handler, l.help)
    assert final.Command("n", len).help == legacy.Command("n", len).help == ""
    with pytest.raises(Exception) as fe:
        f.name = "x"  # type: ignore[misc]
    with pytest.raises(Exception) as le:
        l.name = "x"  # type: ignore[misc]
    assert type(fe.value).__name__ == type(le.value).__name__
