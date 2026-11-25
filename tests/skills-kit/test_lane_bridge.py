"""skills_kit_lib.lane_bridge and scripts/run_lane.py: one md-domain lane on codex.

The digest and canonical form are pinned against independently known values
(FNV-1a 32-bit test vectors). The JS twins in the lane route chunk are pinned
against these functions in test_lane_route_dispatch.py. Dispatch is driven
through run_lane with a fake codex backend and a fake Reselector, so the sandbox,
network and schema handling are observed on the options the adapter receives;
the real codex run is the live proof recorded with the change.
"""

from __future__ import annotations

import dataclasses
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from skills_kit_lib import lane_bridge
from skills_kit_lib.lane_bridge import (
    BridgeInputError,
    BridgeRunError,
    canonical,
    digest,
    parse_payload,
    strict_schema,
    strip_optional_nulls,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LANE = REPO_ROOT / "plugins" / "skills-kit" / "scripts" / "run_lane.py"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["COMPLIANT", "NON-COMPLIANT"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"line": {"type": "integer"}, "note": {"type": "string"}},
                "required": ["line"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["verdict", "findings"],
}


# -- digest and canonical ----------------------------------------------------


def test_digest_is_fnv1a_32_with_the_code_point_count():
    # Published FNV-1a 32-bit vectors: "" -> 811c9dc5, "a" -> e40c292c,
    # "foobar" -> bf9cf968.
    assert digest("") == "fnv1a32:0:811c9dc5"
    assert digest("a") == "fnv1a32:1:e40c292c"
    assert digest("foobar") == "fnv1a32:6:bf9cf968"
    # One code point, not its UTF-8 or UTF-16 length.
    assert digest("\u00e9").startswith("fnv1a32:1:")


def test_canonical_sorts_keys_compacts_and_prints_integral_floats_as_integers():
    assert canonical({"b": [1.0, 2.5, True, None], "a": "caf\u00e9"}) == \
        '{"a":"caf\u00e9","b":[1,2.5,true,null]}'


# -- payload -----------------------------------------------------------------


def _payload(**over: Any) -> dict:
    base = {"v": 1, "entry": {"id": "luna", "effort": "high"}, "access": "read-only",
            "prompt": "Audit /w/doc.md.", "schema": SCHEMA}
    base.update(over)
    return base


def test_parse_payload_accepts_a_matching_digest():
    text = json.dumps(_payload())
    assert parse_payload(text, digest(text))["entry"]["id"] == "luna"


def test_parse_payload_refuses_a_changed_copy():
    # Revert that turns this RED: skip the digest comparison.
    text = json.dumps(_payload())
    with pytest.raises(BridgeInputError, match="input digest mismatch"):
        parse_payload(text.replace("Audit", "Audlt"), digest(text))


@pytest.mark.parametrize("over, needle", [
    ({"access": "admin"}, "access must be one of"),
    ({"cwd": "relative/dir"}, "absolute path"),
    ({"entry": {"id": "luna"}}, "entry must be"),
    ({"prompt": " "}, "prompt must be"),
    ({"extra": 1}, "unknown field"),
    ({"v": 2}, "v must be 1"),
])
def test_parse_payload_refuses_a_malformed_payload(over, needle):
    with pytest.raises(BridgeInputError, match=needle):
        parse_payload(json.dumps(_payload(**over)), None)


# -- schema normalization ----------------------------------------------------


def test_strict_schema_requires_every_property_and_makes_the_optional_ones_nullable():
    from llm_kit.completion.contract_types import strict_schema_violations

    assert strict_schema_violations(SCHEMA)  # the lane schema itself breaks strict mode
    strict = strict_schema(SCHEMA)
    assert strict_schema_violations(strict) == ()
    assert strict["required"] == ["verdict", "findings", "summary"]
    assert strict["properties"]["summary"]["type"] == ["string", "null"]
    assert strict["properties"]["verdict"] == SCHEMA["properties"]["verdict"]
    item = strict["properties"]["findings"]["items"]
    assert item["required"] == ["line", "note"]
    assert item["properties"]["note"]["type"] == ["string", "null"]
    assert SCHEMA["required"] == ["verdict", "findings"]  # the input is not mutated


def test_strict_schema_adds_null_to_an_optional_enum():
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"k": {"type": "string", "enum": ["a", "b"]}}, "required": []}
    assert strict_schema(schema)["properties"]["k"]["enum"] == ["a", "b", None]


def test_strict_schema_refuses_a_free_form_object():
    schema = {"type": "object", "properties": {"m": {"type": "object"}}, "required": ["m"]}
    with pytest.raises(BridgeInputError, match="/properties/m is not expressible"):
        strict_schema(schema)


def test_strip_optional_nulls_removes_only_optional_nulls():
    value = {"verdict": "COMPLIANT", "summary": None,
             "findings": [{"line": 3, "note": None}, {"line": 4, "note": "x"}]}
    assert strip_optional_nulls(value, SCHEMA) == {
        "verdict": "COMPLIANT", "findings": [{"line": 3}, {"line": 4, "note": "x"}]}


# -- run_lane against a fake codex backend ------------------------------------


@dataclasses.dataclass
class FakeCodex:
    answer: Any = None
    raise_exc: BaseException | None = None
    dropped: tuple = ()
    runner: Any = None
    seen: list = dataclasses.field(default_factory=list)
    name: str = dataclasses.field(default="codex-cli", init=False)

    def complete(self, system, user, *, model, options):
        self.runner(["codex", "exec", "-s", options.extras["sandbox"], "-C", str(options.cwd)], user, options.cwd)
        self.seen.append({"system": system, "user": user, "model": model, "options": options})
        if self.raise_exc is not None:
            raise self.raise_exc
        return type("Resp", (), {"structured": self.answer, "model": model, "dropped_params": self.dropped,
                                 "execution_controls_applied": ("sandbox-mode", "network-disable")})()

    def classify_halt(self, exc):
        return None


def _lib(state_id: str = "luna", floor: bool = False) -> dict:
    lib = lane_bridge._llm_kit()

    class FakeReselector:
        def __init__(self, names, **kwargs):
            self.names, self.kwargs = names, kwargs

        def select(self):
            if floor:
                raise lib["NoUsableRoutingTarget"](self.names, (), "process")
            return type("State", (), {"id": state_id})()

    lib = dict(lib, Reselector=FakeReselector,
               call_with_fallthrough=lambda reselector, call, first: call(first))
    return lib


def _factory(backend: Any, model: str = "gpt-6-luna"):
    calls: list = []

    def factory(name, **kwargs):
        calls.append(name)
        return type("Sel", (), {"backend": backend, "model": model, "endpoint": name})()

    factory.calls = calls
    return factory


def _run(payload: dict, backend: Any, **lib_kwargs: Any) -> dict:
    seen_argv: list = []
    backend.runner = lambda argv, prompt, root, **kw: seen_argv.append(argv)
    status = lane_bridge.run_lane(payload, lib=_lib(**lib_kwargs), backend_factory=_factory(backend))
    return status


@pytest.mark.parametrize("access, sandbox", [("read-only", "read-only"), ("write", "workspace-write")])
def test_run_lane_sets_the_sandbox_from_access_and_turns_the_network_off(tmp_path, access, sandbox):
    # Revert that turns this RED: send no extras, or network on.
    backend = FakeCodex(answer={"verdict": "COMPLIANT", "findings": [], "summary": None})
    status = _run(_payload(access=access, cwd=str(tmp_path)), backend)
    (call,) = backend.seen
    assert call["options"].extras == {"sandbox": sandbox, "network": False}
    assert call["options"].effort == "high"
    assert call["options"].cwd == tmp_path.resolve()
    assert call["options"].output_contract.strict_violations == ()
    assert lane_bridge.ACCESS_NOTES[access] in call["system"]
    assert call["user"] == "Audit /w/doc.md."
    assert status["argv"][:4] == ["codex", "exec", "-s", sandbox]
    assert status["result"] == {"verdict": "COMPLIANT", "findings": []}
    assert status["digest"] == digest(canonical(status["result"]))
    assert status["ok"] is True and status["entry"] == {"id": "luna", "effort": "high"}


def test_run_lane_refuses_an_entry_that_is_not_codex():
    backend = FakeCodex(answer={})
    backend.name = "claude-cli"
    with pytest.raises(BridgeInputError, match="not a codex harness entry"):
        _run(_payload(), backend)
    assert backend.seen == []


def test_run_lane_reports_an_entry_routing_refuses():
    backend = FakeCodex(answer={})
    with pytest.raises(BridgeRunError, match="is not serving this run"):
        _run(_payload(), backend, floor=True)
    assert backend.seen == []


@pytest.mark.parametrize("backend, needle", [
    (FakeCodex(answer={"verdict": "MAYBE", "findings": []}), "fails the lane schema at /verdict"),
    (FakeCodex(answer=None), "no structured result"),
    (FakeCodex(answer={"verdict": "COMPLIANT", "findings": []}, dropped=("effort",)), "dropped effort"),
    (FakeCodex(raise_exc=RuntimeError("codex exec failed (exit 1)")), "failed: RuntimeError"),
])
def test_run_lane_fails_loudly_without_a_valid_result(backend, needle):
    with pytest.raises(BridgeRunError, match=needle):
        _run(_payload(), backend)


def test_run_lane_refuses_a_schema_outside_the_llm_kit_subset():
    schema = dict(SCHEMA, properties={**SCHEMA["properties"], "x": {"type": "string", "pattern": "a"}})
    with pytest.raises(BridgeInputError, match="outside llm-kit's schema subset"):
        _run(_payload(schema=schema), FakeCodex(answer={}))


# -- CLI ---------------------------------------------------------------------


def test_cli_prints_one_failure_line_and_exits_2_on_a_digest_mismatch():
    text = json.dumps(_payload())
    out = io.StringIO()
    code = lane_bridge.main(["--input-digest", digest(text + "x")], stdin=io.StringIO(text + "\n"), stdout=out)
    assert code == 2
    (line,) = out.getvalue().splitlines()
    status = json.loads(line)
    assert status["ok"] is False and status["exit"] == 2 and "digest mismatch" in status["error"]


def test_cli_input_file_takes_the_entry_from_the_flags(tmp_path, monkeypatch):
    seen = {}

    def fake_run(payload, timeout_s):
        seen.update(payload)
        return {"ok": True, "entry": payload["entry"], "result": {}}

    monkeypatch.setattr(lane_bridge, "run_lane", fake_run)
    lane_file = tmp_path / "0.lane.json"
    lane_file.write_text(json.dumps({"v": 1, "access": "read-only", "prompt": "p", "schema": SCHEMA}),
                         encoding="utf-8")
    out = io.StringIO()
    assert lane_bridge.main(["--input-file", str(lane_file), "--entry", "luna", "--effort", "low"],
                            stdout=out) == 0
    assert seen["entry"] == {"id": "luna", "effort": "low"}
    assert json.loads(out.getvalue())["ok"] is True


def test_run_lane_script_runs_from_any_directory_and_reports_bad_input(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(RUN_LANE), "--input-digest", "fnv1a32:0:811c9dc5"],
        input="{}\n", capture_output=True, text=True, cwd=tmp_path, timeout=60,
        env={**__import__("os").environ, "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1"},
    )
    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stdout)["error"].startswith("input digest mismatch")
