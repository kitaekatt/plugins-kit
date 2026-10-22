"""Golden fixtures for the llm-kit CLI verbs that external consumers (databench) call.

Each case runs ``llm_kit.cli.main(argv)`` in-process with a throwaway HOME, a pinned
model registry and no network, and compares exit code, stdout and stderr with a JSON
file under ``tests/llm-kit/goldens/cli/``. A change to a verb's output envelope, its
stderr, or its exit code fails here until the fixture is deliberately updated.

Offline: ``completion complete`` runs against a fake backend patched in at
``llm_kit.dispatch.factory.create_backend``; no real model and no claude/codex
binary is started. ``models describe`` reachability is replaced by "reachable".

Normalized (genuinely volatile) fields, and nothing else:
  * the pytest temp directory in any string -> ``<T>``
  * JSON keys ``*_at`` and ``started_at``/``ended_at`` -> ``<t>``; ``wall_ms`` -> ``<ms>``
  * the package version in ``llm-kit jobs X.Y.Z`` -> ``X.Y.Z`` (a release bump is not an
    output change; the shape is still checked)

Updating: after a deliberate output change, rerun with ``UPDATE_GOLDENS=1`` to rewrite
the fixtures, then review the diff:

    UPDATE_GOLDENS=1 uv run --extra dev pytest tests/llm-kit/test_cli_goldens.py -v
"""

from __future__ import annotations

import importlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from test_cli_harness import FakeBackend, home, write  # noqa: F401 -- home is a fixture

GOLDENS = Path(__file__).resolve().parent / "goldens" / "cli"
UPDATE = os.environ.get("UPDATE_GOLDENS") == "1"

_REGISTRY = """\
default: local
models:
  local: {base_url: http://h.invalid/v1, model: m-local, effort_style: ninfer, key_env: LOCAL_KEY}
  other: {base_url: http://h.invalid/v1, model: m-other}
  cl: {harness: claude, model: opus, tier: 2, family: alpha}
  cdx: {harness: codex, model: gpt-x, effort: high, tier: 3, family: beta}
"""

_TIME_KEYS = {"started_at", "ended_at"}


def _scrub(value: Any, tmp: Path) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _TIME_KEYS or k.endswith("_at"):
                out[k] = "<t>"
            elif k == "wall_ms":
                out[k] = "<ms>"
            else:
                out[k] = _scrub(v, tmp)
        return out
    if isinstance(value, list):
        return [_scrub(v, tmp) for v in value]
    if isinstance(value, str):
        return value.replace(tmp.as_posix(), "<T>").replace(str(tmp), "<T>")
    return value


def _form(text: str, tmp: Path) -> Any:
    """Parsed JSON (one object, or a list of JSON lines) when the text is JSON, else text."""
    text = text.replace(tmp.as_posix(), "<T>").replace(str(tmp), "<T>")
    stripped = text.strip()
    if not stripped:
        return text
    try:
        return {"json": _scrub(json.loads(stripped), tmp)}
    except json.JSONDecodeError:
        pass
    try:
        return {"json_lines": [_scrub(json.loads(l), tmp) for l in stripped.splitlines()]}
    except json.JSONDecodeError:
        return text


def _check(name: str, observed: dict) -> None:
    path = GOLDENS / f"{name}.json"
    rendered = json.dumps(observed, indent=2, sort_keys=True) + "\n"
    if UPDATE:
        path.write_text(rendered, encoding="utf-8", newline="\n")
        return
    assert path.exists(), f"missing golden {path}; create it with UPDATE_GOLDENS=1"
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(rendered), (
        f"CLI output for {name} differs from {path}. If deliberate, rerun with UPDATE_GOLDENS=1."
    )


@pytest.fixture()
def env(home, tmp_path, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _REGISTRY)))
    cli = importlib.import_module("llm_kit.cli")

    def invoke(name: str, argv: list[str], stdin: Optional[str] = None) -> dict:
        capsys.readouterr()
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin.encode()), encoding="utf-8"))
        try:
            code: Any = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
        cap = capsys.readouterr()
        shown = [a.replace(tmp_path.as_posix(), "<T>") for a in argv]
        observed = {"argv": shown, "exit": code, "stdout": _form(cap.out, tmp_path),
                    "stderr": _form(cap.err, tmp_path)}
        if name == "jobs_version":
            observed["stdout"] = re.sub(r"\d+\.\d+\.\d+", "X.Y.Z", cap.out)
            assert re.fullmatch(r"llm-kit jobs \d+\.\d+\.\d+\n", cap.out), cap.out
        _check(name, observed)
        return observed

    return invoke


@pytest.fixture()
def reachable(monkeypatch):
    decl = importlib.import_module("llm_kit.dispatch.declaration")
    reach = importlib.import_module("llm_kit.dispatch.reachability")
    monkeypatch.setattr(
        decl, "check_many",
        lambda entries, **_: {n: reach.Reachability("reachable", "models-endpoint", "ok") for n in entries},
    )


def _backend(monkeypatch, backend: FakeBackend) -> None:
    factory = importlib.import_module("llm_kit.dispatch.factory")
    types = importlib.import_module("llm_kit.completion.types")
    del types
    monkeypatch.setattr(
        factory, "create_backend",
        lambda *_a, **_k: factory.BackendSelection("chosen", "harness", backend, "model-id", "high"),
    )


_SCHEMA = json.dumps({"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]})


def test_repair_json_fenced(env, tmp_path):
    schema = write(tmp_path / "schema.json", _SCHEMA)
    got = env("repair_json_fenced", ["completion", "repair-json", "--schema-file", str(schema)],
              stdin='```json\n{"n": 1}\n```')
    assert got["exit"] == 0 and got["stderr"] == ""
    assert len(got["stdout"]["json"]) == 5  # protocol,status,text,edits,reason


def test_repair_json_unrepairable(env, tmp_path):
    schema = write(tmp_path / "schema.json", _SCHEMA)
    env("repair_json_unrepairable", ["completion", "repair-json", "--schema-file", str(schema)],
        stdin="no json here at all")


def test_repair_json_relative_schema_refused(env):
    env("repair_json_relative_path", ["completion", "repair-json", "--schema-file", "schema.json"], stdin="{}")


def test_request_schema(env):
    got = env("request_schema", ["completion", "request-schema"])
    assert got["exit"] == 0 and got["stderr"] == ""


def test_models_endpoints(env):
    env("models_endpoints", ["models", "endpoints"])


def test_models_describe_ok(env, reachable, tmp_path):
    req = write(tmp_path / "req.json", "{}")
    env("models_describe_ok", ["models", "describe", "--caller", "process", "--requirements", str(req),
                               "--json", "cl", "cdx"])


def test_models_describe_requirements_mismatch(env, reachable, tmp_path):
    req = write(tmp_path / "req.json", json.dumps({"params": ["no_such_param"]}))
    env("models_describe_mismatch", ["models", "describe", "--caller", "process", "--requirements", str(req),
                                     "--json", "cl", "cdx"])


def test_models_describe_exclude(env, reachable, tmp_path):
    req = write(tmp_path / "req.json", "{}")
    env("models_describe_exclude", ["models", "describe", "--caller", "process", "--requirements", str(req),
                                    "--json", "--exclude", "cl", "cl", "cdx"])


def test_models_resolve_harness(env, reachable):
    env("models_resolve_harness", ["models", "resolve", "--models", "cdx"])


def test_models_resolve_transport(env, reachable):
    env("models_resolve_transport", ["models", "resolve", "--models", "local"])


def test_complete_ok(env, monkeypatch):
    types = importlib.import_module("llm_kit.completion.types")
    _backend(monkeypatch, FakeBackend(types.LLMResponse(text="answer", model="model-id", input_tokens=4)))
    got = env("complete_ok", ["completion", "complete", "--system", "s", "--format", "json"], stdin="question")
    assert got["exit"] == 0


def test_complete_halt_exit_3(env, monkeypatch):
    _backend(monkeypatch, FakeBackend(error=RuntimeError("limited"), halt="rate_limit"))
    got = env("complete_halt", ["completion", "complete", "--prompt", "hello", "--format", "json"])
    assert got["exit"] == 3


def test_complete_failure_exit_1(env, monkeypatch):
    _backend(monkeypatch, FakeBackend(error=RuntimeError("boom")))
    got = env("complete_failure", ["completion", "complete", "--prompt", "hello", "--format", "json"])
    assert got["exit"] == 1


def test_complete_protocol_error_exit_4(env, monkeypatch, tmp_path):
    _backend(monkeypatch, FakeBackend())
    bad = write(tmp_path / "bad.json", "{not json")
    got = env("complete_protocol_error", ["completion", "complete", "--request-file", str(bad),
                                          "--format", "json"])
    assert got["exit"] == 4


def test_complete_request_file_conflict_exit_4(env, monkeypatch, tmp_path):
    _backend(monkeypatch, FakeBackend())
    good = write(tmp_path / "req.json", "{}")
    got = env("complete_request_file_conflict", ["completion", "complete", "--request-file", str(good),
                                                 "--prompt", "x", "--format", "json"])
    assert got["exit"] == 4


def test_complete_usage_error_exit_2(env, monkeypatch):
    _backend(monkeypatch, FakeBackend())
    got = env("complete_usage_error", ["completion", "complete", "--prompt", "x", "--max-attempts", "2",
                                       "--format", "json"])
    assert got["exit"] == 2


def test_jobs_version(env):
    got = env("jobs_version", ["jobs", "--version"])
    assert got["exit"] == 0
