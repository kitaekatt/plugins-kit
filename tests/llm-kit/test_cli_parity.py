"""Differential parity: ``llm-kit`` against the frozen legacy CLIs, verb by verb.

Every legacy verb that llm-kit carries is run on BOTH sides over the same fake
environment (temp HOME, a registry file, fake backends, replaced reachability
checks) and the outcomes are compared: exit code, stdout and stderr, with only
two normalizations -- temp paths, and the executable token where usage text
necessarily names ``llm-kit`` instead of ``llm-scripting-kit`` / ``job-kit``.
A message the final package words for llm-kit (``final_wording.RENAMES``, each
proved against the live sources by ``test_final_messages.py``) is compared in
its final wording.

Legacy side: ``llm_scripting_kit.cli`` (account, completion, models, local),
``job_kit.cli`` (jobs) and ``content_pipeline.cli.run`` (pipeline). Final side:
``llm_kit.cli`` addressed by group.

Deliberate differences, each pinned by a named test in ``test_cli_groups.py``:

* ``models`` (list the models of an endpoint) is ``models list`` because the
  group is already called ``models`` (``TestVerbMap``);
* the argparse ``prog`` and ``--version`` text name ``llm-kit <group>``
  (``TestUsageTokens``);
* ``acceptance`` lives under ``local`` (``TestVerbMap``);
* the pipeline group is a thin mount of the store-only command set on
  ``--store`` (``TestPipelineMount``).
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import pytest

from final_wording import to_final
from test_cli_harness import (  # noqa: F401 -- fixtures
    FakeBackend,
    Outcome,
    final_mods,
    home,
    legacy_mods,
    normalize,
    normalize_json_text,
    run,
    scrub_json,
    shipped_openrouter_without_style,
    write,
)

_REGISTRY = """\
default: local
models:
  local: {base_url: http://h.invalid/v1, model: m-local, effort_style: ninfer, key_env: LOCAL_KEY}
  other: {base_url: http://h.invalid/v1, model: m-other}
  cl: {harness: claude, model: opus, tier: 2, family: alpha}
  cdx: {harness: codex, model: gpt-x, effort: high, tier: 3, family: beta}
"""


class Side:
    """One implementation under test, addressed by legacy verb names."""

    def __init__(self, name: str, mods: Any) -> None:
        self.name = name
        self.m = mods

    # -- invocation ------------------------------------------------------------------

    def scripting(self, verb: str, *rest: str) -> list[str]:
        if self.name == "legacy":
            return [verb, *rest]
        group = {
            "status": "account", "which": "account", "set-key": "account",
            "complete": "completion", "request-schema": "completion", "repair-json": "completion",
            "endpoints": "models", "probe": "models", "usage": "models", "record-halt": "models",
            "describe": "models", "seats": "models", "models": "models", "resolve": "models",
            "swapper": "local", "acceptance": "local",
        }[verb]
        # `models` (list an endpoint's models) is `models list` because the group is already `models`.
        return [group, "list" if verb == "models" else verb, *rest]

    def main(self) -> Callable[..., int]:
        return self.m.cli.main

    # -- seams -----------------------------------------------------------------------

    def patch_backend(self, monkeypatch: pytest.MonkeyPatch, factory: Callable[..., Any]) -> None:
        if self.name == "legacy":
            monkeypatch.setattr(self.m.cli, "create_backend", factory)
        else:
            monkeypatch.setattr(self.m.factory, "create_backend", factory)

    def patch_reachability(self, monkeypatch: pytest.MonkeyPatch, check_entry: Callable[..., Any]) -> None:
        owner = self.m.cli if self.name == "legacy" else self.m.models_cli
        monkeypatch.setattr(owner, "check_entry", check_entry)

    def patch_validate(self, monkeypatch: pytest.MonkeyPatch, validate: Callable[..., Any]) -> None:
        owner = self.m.cli if self.name == "legacy" else self.m.account
        monkeypatch.setattr(owner, "validate_endpoint", validate)

    def patch_declaration_reachability(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every declared entry answers "reachable"; nothing is probed."""
        owner = self.m.declaration

        def reachable(entries: Any, **_: Any) -> dict:
            return {n: self.Reachability("reachable", "models-endpoint", "ok") for n in entries}

        monkeypatch.setattr(owner, "check_many", reachable)

    def point_env_file(self, monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
        """Re-point every module that bound USER_ENV_FILE at import time."""
        owner = self.m.cli if self.name == "legacy" else self.m.account
        monkeypatch.setattr(owner, "USER_ENV_FILE", path)
        monkeypatch.setattr(self.m.api_key, "USER_ENV_FILE", path)
        if self.name == "final":
            # llm-kit WRITES the user key file at $LLM_KIT_ENV_FILE (else the neutral
            # path) and reads the legacy constant above only as a fallback.
            monkeypatch.setenv("LLM_KIT_ENV_FILE", str(path))
        else:
            monkeypatch.delenv("LLM_KIT_ENV_FILE", raising=False)

    @property
    def BackendSelection(self) -> Any:
        return self.m.factory.BackendSelection

    @property
    def LLMResponse(self) -> Any:
        return self.m.types.LLMResponse

    @property
    def Reachability(self) -> Any:
        return self.m.reach.Reachability


@pytest.fixture()
def sides(legacy_mods, final_mods):
    return Side("legacy", legacy_mods), Side("final", final_mods)


def _swap_token(side: Side, group: str) -> dict[str, str]:
    """Where usage text necessarily names the executable, normalize it."""
    if side.name == "legacy":
        return {"llm-scripting-kit": "<exe>", "job-kit": "<exe>"}
    return {f"llm-kit {group}": "<exe>", "llm-kit": "<exe>"}


def both(sides, group: str, verb: str, *rest: str, capsys, monkeypatch, stdin: Optional[str] = None,
         stdin_bytes: Optional[bytes] = None, setup: Optional[Callable[[Side], None]] = None,
         tmp: Optional[Path] = None) -> tuple[Outcome, Outcome]:
    """Run one scripting verb on both sides and return the two normalized outcomes."""
    results = []
    for side in sides:
        if setup is not None:
            setup(side)
        argv = side.scripting(verb, *rest)
        outcome = run(side.main(), argv, capsys, stdin=stdin, stdin_bytes=stdin_bytes, monkeypatch=monkeypatch)
        repl = _swap_token(side, group)
        if tmp is not None:
            repl = {str(tmp): "<T>", tmp.as_posix(): "<T>", **repl}
        out, err = normalize(outcome.out, repl), normalize(outcome.err, repl)
        if side.name == "legacy":
            out, err = to_final(out), to_final(err)
        # The final side loses its group word after the executable above ("llm-kit models" -> "<exe>"), but
        # to_final re-adds the group to a legacy message the final CLI words with it (the halt write-back
        # names "models record-halt"). Strip the group after the executable on both sides, so the comparison
        # is symmetric; the final group wording itself is asserted by tests/llm-kit/test_final_messages.py.
        out, err = _strip_group_after_exe(out, group), _strip_group_after_exe(err, group)
        results.append(Outcome(outcome.code, out, err))
    return results[0], results[1]


def _strip_group_after_exe(text: str, group: str) -> str:
    return text.replace(f"<exe> {group} ", "<exe> ")


def same(left: Outcome, right: Outcome) -> Outcome:
    assert left.code == right.code, (left, right)
    assert left.out == right.out, (left.out, right.out)
    assert left.err == right.err, (left.err, right.err)
    return left


def same_usage_error(left: Outcome, right: Outcome) -> Outcome:
    """An argparse refusal: equal code, empty stdout, equal ``error:`` line.

    The usage banner above that line wraps at a width set by the program name,
    and the two names differ in length, so the banner is not compared.
    """
    assert left.code == right.code == 2, (left, right)
    assert left.out == right.out == ""
    assert left.err.splitlines()[-1] == right.err.splitlines()[-1], (left.err, right.err)
    return left


def same_json(left: Outcome, right: Outcome) -> Outcome:
    """Equal exit code and stderr; stdout equal after parsing and clock scrubbing."""
    assert left.code == right.code, (left, right)
    assert left.err == right.err, (left.err, right.err)
    a, b = normalize_json_text(left.out, {}), normalize_json_text(right.out, {})
    assert a == b, _first_difference(a, b)
    return left


def _first_difference(a: Any, b: Any, path: str = "$") -> str:
    """The path and the two values of the first place two parsed JSON documents differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{path}.{key}: present on one side only"
            where = _first_difference(a[key], b[key], f"{path}.{key}")
            if where:
                return where
        return ""
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for index, (x, y) in enumerate(zip(a, b)):
            where = _first_difference(x, y, f"{path}[{index}]")
            if where:
                return where
        return ""
    return "" if a == b else f"{path}: {a!r} != {b!r}"


@pytest.fixture()
def registry(home, tmp_path, monkeypatch):
    path = write(tmp_path / "registry.yaml", _REGISTRY)
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))
    return path




# --------------------------------------------------------------------------
# account
# --------------------------------------------------------------------------


def _ok_status(side: Side, **over: Any) -> Any:
    cls = side.m.account.AccountStatus if side.name == "legacy" else side.m.account_adapter.AccountStatus
    base = dict(ok=True, label="lbl", usage=1.5, limit=10.0, is_free_tier=False,
                rate_limit={"requests": 5}, failure_reason=None, raw=None)
    base.update(over)
    return cls(**base)


def _account_error(side: Side) -> type:
    return (side.m.account if side.name == "legacy" else side.m.account_adapter).AccountCheckError


class TestAccount:
    @pytest.mark.parametrize("scenario", ["ok", "free-tier-none", "auth", "no-credit", "unvalidated"])
    def test_status_with_a_key(self, sides, registry, capsys, monkeypatch, scenario):
        monkeypatch.setenv("OPENROUTER_API_KEY", "k-env")

        def setup(side: Side) -> None:
            def validate(ep: Any, key: str) -> Any:
                assert key == "k-env"
                if scenario == "ok":
                    return _ok_status(side)
                if scenario == "free-tier-none":
                    return _ok_status(side, is_free_tier=None, usage=None, limit=None, rate_limit=None, label=None)
                if scenario == "auth":
                    return _ok_status(side, ok=False, failure_reason="auth")
                if scenario == "no-credit":
                    return _ok_status(side, ok=False, failure_reason="no_credit")
                return None

            side.patch_validate(monkeypatch, validate)

        left, right = both(sides, "account", "status", capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        out = same(left, right)
        assert out.code == (0 if scenario in ("ok", "free-tier-none", "unvalidated") else 1)

    def test_status_validation_error_is_a_usage_error(self, sides, registry, capsys, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "k-env")

        def setup(side: Side) -> None:
            def validate(ep: Any, key: str) -> Any:
                raise _account_error(side)("network down")

            side.patch_validate(monkeypatch, validate)

        left, right = both(sides, "account", "status", capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        assert same(left, right).code == 2

    def test_status_without_a_key_fails(self, sides, registry, home, capsys, monkeypatch, tmp_path):
        def setup(side: Side) -> None:
            side.point_env_file(monkeypatch, tmp_path / "absent.env")

        left, right = both(sides, "account", "status", capsys=capsys, monkeypatch=monkeypatch, setup=setup,
                           tmp=tmp_path)
        out = same(left, right)
        assert out.code == 1 and "No API key found" in out.out

    def test_unknown_endpoint_is_a_usage_error(self, sides, registry, capsys, monkeypatch):
        left, right = both(sides, "account", "status", "--endpoint", "nope", capsys=capsys, monkeypatch=monkeypatch)
        out = same(left, right)
        assert out.code == 2 and "Endpoint error" in out.err

    def test_which_names_the_source_or_reports_missing(self, sides, registry, home, capsys, monkeypatch, tmp_path):
        def setup(side: Side) -> None:
            side.point_env_file(monkeypatch, tmp_path / "absent.env")

        missing = same(*both(sides, "account", "which", capsys=capsys, monkeypatch=monkeypatch, setup=setup,
                             tmp=tmp_path))
        assert (missing.code, missing.out) == (1, "missing\n")
        monkeypatch.setenv("OPENROUTER_API_KEY", "k-env")
        present = same(*both(sides, "account", "which", capsys=capsys, monkeypatch=monkeypatch, setup=setup,
                             tmp=tmp_path))
        assert present.code == 0 and present.out.startswith("env")

    def test_set_key_writes_the_legacy_env_file(self, sides, registry, home, capsys, monkeypatch, tmp_path):
        written = {}
        for side in sides:
            target = tmp_path / side.name / ".env"
            side.point_env_file(monkeypatch, target)
            outcome = run(side.main(), side.scripting("set-key", "--key", "k-new", "--no-validate"), capsys,
                          monkeypatch=monkeypatch)
            repl = {str(target): "<ENV>", target.as_posix(): "<ENV>", **_swap_token(side, "account")}
            written[side.name] = (outcome.code, normalize(outcome.out, repl), normalize(outcome.err, repl),
                                  target.read_text(encoding="utf-8"))
        assert written["legacy"] == written["final"]
        assert written["final"][0] == 0 and written["final"][3] == "OPENROUTER_API_KEY=k-new\n"

    def test_set_key_validation_rejection_writes_nothing(self, sides, registry, home, capsys, monkeypatch, tmp_path):
        for side in sides:
            target = tmp_path / side.name / ".env"
            side.point_env_file(monkeypatch, target)
            side.patch_validate(monkeypatch, lambda ep, key, _s=side: _ok_status(_s, ok=False, failure_reason="auth"))
        left = run(sides[0].main(), sides[0].scripting("set-key", "--key", "bad"), capsys, monkeypatch=monkeypatch)
        right = run(sides[1].main(), sides[1].scripting("set-key", "--key", "bad"), capsys, monkeypatch=monkeypatch)
        assert (left.code, left.out, left.err) == (right.code, right.out, right.err)
        assert left.code == 1 and "Validation failed: auth" in left.err
        assert not (tmp_path / "legacy" / ".env").exists() and not (tmp_path / "final" / ".env").exists()

    def test_set_key_prompts_when_no_key_is_given(self, sides, registry, home, capsys, monkeypatch, tmp_path):
        import getpass

        monkeypatch.setattr(getpass, "getpass", lambda prompt: "")
        left, right = both(sides, "account", "set-key", capsys=capsys, monkeypatch=monkeypatch)
        out = same(left, right)
        assert out.code == 1 and "Empty key, nothing written." in out.err

    def test_set_key_aborted_prompt_exits_130(self, sides, registry, home, capsys, monkeypatch):
        import getpass

        def abort(prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr(getpass, "getpass", abort)
        left, right = both(sides, "account", "set-key", capsys=capsys, monkeypatch=monkeypatch)
        assert same(left, right).code == 130


# --------------------------------------------------------------------------
# completion
# --------------------------------------------------------------------------


def _selection(side: Side, backend: Any) -> Any:
    return side.BackendSelection("chosen", "harness", backend, "model-id", "high")


def _factory(side: Side, monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    side.patch_backend(monkeypatch, lambda *_a, **_k: _selection(side, backend))


def _timeout_error(side: Side) -> BaseException:
    import importlib

    cls = (
        importlib.import_module("llm_scripting_kit.completion").AgentTimeoutError if side.name == "legacy"
        else importlib.import_module("llm_kit.completion.halt").AgentTimeoutError
    )
    return cls("timed out", cmd=["claude"], elapsed_s=1.0, stdout="", stderr="")


class TestCompletion:
    def test_complete_flag_surface(self, sides, registry, capsys, monkeypatch):
        calls = {}

        def setup(side: Side) -> None:
            backend = FakeBackend(side.LLMResponse(text="answer", model="model-id", input_tokens=4))
            calls[side.name] = backend
            _factory(side, monkeypatch, backend)

        left, right = both(sides, "completion", "complete", "--system", "instructions",
                           capsys=capsys, monkeypatch=monkeypatch, stdin="question", setup=setup)
        out = same_json(left, right)
        assert out.code == 0
        payload = json.loads(out.out)
        assert payload["protocol"] == 1 and payload["response"]["text"] == "answer"
        for side_name, backend in calls.items():
            assert backend.call[:3] == ("instructions", "question", "model-id"), side_name
            assert backend.call[3].effort == "high"

    @pytest.mark.parametrize("error,halt,code", [
        (RuntimeError("limited"), "rate_limit", 3),
        (RuntimeError("boom"), None, 1),
    ])
    def test_complete_failure_is_data_with_a_distinct_halt_exit(self, sides, registry, capsys, monkeypatch,
                                                                error, halt, code):
        def setup(side: Side) -> None:
            _factory(side, monkeypatch, FakeBackend(error=error, halt=halt))

        left, right = both(sides, "completion", "complete", "--prompt", "hello", capsys=capsys,
                           monkeypatch=monkeypatch, setup=setup)
        out = same_json(left, right)
        assert out.code == code
        assert json.loads(out.out)["response"]["status"] == "error"

    def test_complete_text_format_states_a_failure_on_stderr(self, sides, registry, capsys, monkeypatch):
        def setup(side: Side) -> None:
            _factory(side, monkeypatch, FakeBackend(error=RuntimeError("limited"), halt="rate_limit"))

        left, right = both(sides, "completion", "complete", "--prompt", "hello", "--format", "text",
                           capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        out = same(left, right)
        assert (out.code, out.out) == (3, "") and "rate_limit: limited" in out.err

    def test_complete_text_format_prints_the_answer(self, sides, registry, capsys, monkeypatch):
        def setup(side: Side) -> None:
            _factory(side, monkeypatch, FakeBackend(side.LLMResponse(text="plain answer", model="m")))

        left, right = both(sides, "completion", "complete", "--prompt", "hello", "--format", "text",
                           capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        assert same(left, right).out == "plain answer\n"

    def test_complete_timeout_is_a_result(self, sides, registry, capsys, monkeypatch):
        def setup(side: Side) -> None:
            _factory(side, monkeypatch, FakeBackend(error=_timeout_error(side)))

        left, right = both(sides, "completion", "complete", "--prompt", "hello", capsys=capsys,
                           monkeypatch=monkeypatch, setup=setup)
        out = same_json(left, right)
        assert out.code == 1 and json.loads(out.out)["response"]["status"] == "timeout"

    def test_request_file_protocol_and_its_errors(self, sides, registry, capsys, monkeypatch, tmp_path):
        good = write(tmp_path / "good.json", json.dumps(
            {"protocol": 1, "endpoint": "chosen", "system": "s", "prompt": "p"}))
        bad_json = write(tmp_path / "bad.json", "{not json")

        def setup(side: Side) -> None:
            _factory(side, monkeypatch, FakeBackend(side.LLMResponse(text="via request", model="model-id")))

        ok = same_json(*both(sides, "completion", "complete", "--request-file", str(good), capsys=capsys,
                             monkeypatch=monkeypatch, setup=setup, tmp=tmp_path))
        assert ok.code == 0 and json.loads(ok.out)["response"]["text"] == "via request"
        stdin_ok = same_json(*both(sides, "completion", "complete", "--request-file", "-", capsys=capsys,
                                   monkeypatch=monkeypatch, setup=setup, tmp=tmp_path,
                                   stdin=good.read_text(encoding="utf-8")))
        assert stdin_ok.code == 0
        broken = same(*both(sides, "completion", "complete", "--request-file", str(bad_json), capsys=capsys,
                            monkeypatch=monkeypatch, setup=setup, tmp=tmp_path))
        assert broken.code == 4 and broken.out == "" and "request is not valid JSON" in broken.err
        conflict = same(*both(sides, "completion", "complete", "--request-file", str(good), "--prompt", "x",
                              capsys=capsys, monkeypatch=monkeypatch, setup=setup, tmp=tmp_path))
        assert conflict.code == 4 and "cannot be combined with --prompt" in conflict.err

    def test_max_attempts_without_a_routed_declaration_is_refused(self, sides, registry, capsys, monkeypatch):
        def setup(side: Side) -> None:
            _factory(side, monkeypatch, FakeBackend(side.LLMResponse(text="x", model="m")))

        left, right = both(sides, "completion", "complete", "--prompt", "p", "--max-attempts", "2",
                           capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        out = same(left, right)
        assert out.code == 2 and "--max-attempts needs a multi-entry" in out.err

    def test_routed_declaration_falls_back_on_a_classified_halt(self, sides, registry, capsys, monkeypatch):
        def setup(side: Side) -> None:
            side.patch_declaration_reachability(monkeypatch)
            backends = {"cl": FakeBackend(error=RuntimeError("limited"), halt="rate_limit"),
                        "cdx": FakeBackend(side.LLMResponse(text="from cdx", model="gpt-x"))}

            def make(name: str, **kwargs: Any) -> Any:
                return side.BackendSelection(name, "harness", backends[name], kwargs.get("model") or name, "high")

            side.patch_backend(monkeypatch, make)

        left, right = both(sides, "completion", "complete", "--models", "cl,cdx", "--prompt", "hi",
                           capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        out = same_json(left, right)
        payload = json.loads(out.out)
        assert out.code == 0 and payload["entry"] == "cdx" and payload["run_status"] == "completed"
        assert [a["entry"] for a in payload["attempts"]] == ["cl", "cdx"]

    def test_routed_declaration_without_a_usable_entry_is_a_floor(self, sides, registry, capsys, monkeypatch):
        def setup(side: Side) -> None:
            def nothing(entries: Any, **_: Any) -> dict:
                return {n: side.Reachability("unreachable", "cli-version", "gone") for n in entries}

            monkeypatch.setattr(side.m.declaration, "check_many", nothing)

        left, right = both(sides, "completion", "complete", "--models", "cl,cdx", "--prompt", "hi",
                           capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        out = same(left, right)
        assert out.code == 1 and "error" in json.loads(out.err)

    def test_request_schema_prints_the_accepted_shape(self, sides, capsys, monkeypatch, home):
        left, right = both(sides, "completion", "request-schema", capsys=capsys, monkeypatch=monkeypatch)
        out = same(left, right)
        assert out.code == 0 and json.loads(out.out)

    def test_repair_json_repairs_one_answer_on_stdin(self, sides, capsys, monkeypatch, home, tmp_path):
        schema = write(tmp_path / "schema.json", json.dumps(
            {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}))
        left, right = both(sides, "completion", "repair-json", "--schema-file", str(schema), capsys=capsys,
                           monkeypatch=monkeypatch, stdin_bytes=b'```json\n{"n": 1}\n```', tmp=tmp_path)
        out = same(left, right)
        assert out.code == 0 and json.loads(out.out)["protocol"] == 1

    def test_repair_json_refuses_a_relative_schema_path(self, sides, capsys, monkeypatch, home):
        left, right = both(sides, "completion", "repair-json", "--schema-file", "schema.json", capsys=capsys,
                           monkeypatch=monkeypatch, stdin_bytes=b"{}")
        out = same(left, right)
        assert out.code == 2 and "must be an absolute path" in out.err


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("shipped_openrouter_without_style")  # declared difference, see the fixture
class TestModels:
    def test_endpoints_lists_the_registry_identically(self, sides, registry, capsys, monkeypatch, tmp_path):
        left, right = both(sides, "models", "endpoints", capsys=capsys, monkeypatch=monkeypatch, tmp=tmp_path)
        out = same_json(left, right)
        assert out.code == 0
        assert {"local", "other", "cl", "cdx"} <= set(json.loads(out.out)["endpoints"])

    def test_endpoints_verify_adds_reachability(self, sides, registry, capsys, monkeypatch):
        def setup(side: Side) -> None:
            def many(entries: Any, **_: Any) -> dict:
                return {n: side.Reachability("reachable", "models-endpoint", "ok") for n in entries}

            monkeypatch.setattr(side.m.cli if side.name == "legacy" else side.m.models_cli, "check_many", many)

        left, right = both(sides, "models", "endpoints", "--verify", capsys=capsys, monkeypatch=monkeypatch,
                           setup=setup)
        out = same_json(left, right)
        assert all(e["reachability"]["status"] == "reachable" for e in json.loads(out.out)["endpoints"].values())

    @pytest.mark.parametrize("status,code", [("reachable", 0), ("unreachable", 1), ("unknown", 5)])
    def test_probe_has_three_distinguishable_exit_codes(self, sides, registry, capsys, monkeypatch, status, code):
        def setup(side: Side) -> None:
            side.patch_reachability(monkeypatch, lambda *a, **k: side.Reachability(status, "cli-version", f"{status}!"))

        left, right = both(sides, "models", "probe", "--endpoint", "cl", capsys=capsys, monkeypatch=monkeypatch,
                           setup=setup)
        out = same_json(left, right)
        assert out.code == code
        assert json.loads(out.out)["reachability"]["status"] == status

    def test_probe_of_an_unconfigured_endpoint_is_a_usage_error(self, sides, registry, capsys, monkeypatch):
        left, right = both(sides, "models", "probe", "--endpoint", "nope", capsys=capsys, monkeypatch=monkeypatch)
        out = same(left, right)
        assert out.code == 2 and "no configured endpoint named 'nope'" in out.err

    def test_usage_with_and_without_an_opted_in_endpoint(self, sides, registry, capsys, monkeypatch, home):
        none = same(*both(sides, "models", "usage", capsys=capsys, monkeypatch=monkeypatch))
        assert none.out == "no endpoint declares conserve_usage\n"
        as_json = same(*both(sides, "models", "usage", "--json", "--no-pin", capsys=capsys, monkeypatch=monkeypatch))
        assert json.loads(as_json.out)["verdicts"] == {}

    def test_record_halt_for_an_unknown_and_a_plain_entry(self, sides, registry, capsys, monkeypatch, home):
        unknown = same(*both(sides, "models", "record-halt", "nope", capsys=capsys, monkeypatch=monkeypatch))
        assert unknown.code == 2 and "unknown entry 'nope'" in unknown.err
        plain = same(*both(sides, "models", "record-halt", "cl", capsys=capsys, monkeypatch=monkeypatch))
        assert plain.code == 0 and json.loads(plain.out)["recorded"] is False

    def test_describe_renders_a_declaration(self, sides, registry, capsys, monkeypatch, home):
        def setup(side: Side) -> None:
            side.patch_declaration_reachability(monkeypatch)
            if side.name != "legacy":
                from llm_kit.adapters import harness_auth
                from llm_kit.registry import cost

                monkeypatch.setattr(
                    harness_auth, "probe_harness_auth",
                    lambda harnesses, env=None, *, cwd: {
                        h.lower(): cost.HarnessAuth(cost.SUBSCRIPTION, "fixture subscription") for h in harnesses
                    },
                )

        # The final describe always reports each entry's cost class and the optional-consent note; the legacy
        # one reports neither. That is the one declared divergence: strip exactly those additions from the final
        # output, assert they are there, and compare everything else byte for byte.
        left, right = both(sides, "models", "describe", "cl", "cdx", "--caller", "session", capsys=capsys,
                           monkeypatch=monkeypatch, setup=setup)
        consent_lines = [line for line in right.out.splitlines() if line.startswith("Consent layer: optional")]
        assert len(consent_lines) == 1
        right = Outcome(right.code, right.out.replace(consent_lines[0] + "\n", ""), right.err)
        text = same(left, right)
        assert text.code == 0 and text.out
        left, right = both(sides, "models", "describe", "cl", "cdx", "--json", capsys=capsys,
                           monkeypatch=monkeypatch, setup=setup)
        document = json.loads(right.out)
        assert document.pop("consent")["optional"] is True
        for entry in document["rendered_entries"]:
            assert entry.pop("cost")["class"] == "subscription"
        right = Outcome(right.code, json.dumps(document), right.err)
        as_json = same_json(left, right)
        assert json.loads(as_json.out)

    def test_describe_with_nothing_usable_reports_the_floor(self, sides, registry, capsys, monkeypatch, home):
        left, right = both(sides, "models", "describe", "nope", capsys=capsys, monkeypatch=monkeypatch)
        out = same(left, right)
        assert out.code == 1 and "error" in json.loads(out.err)

    def test_seats_of_an_unknown_self_is_a_usage_error(self, sides, registry, capsys, monkeypatch, home):
        left, right = both(sides, "models", "seats", "--self", "nope", "--json", capsys=capsys,
                           monkeypatch=monkeypatch)
        out = same_json(left, right)
        assert out.code == 2 and "unknown self endpoint or model 'nope'" in out.err

    @pytest.mark.parametrize("status,code", [("reachable", 0), ("unknown", 5)])
    def test_seats_preserves_an_indeterminate_probe(self, sides, registry, capsys, monkeypatch, home,
                                                    status, code):
        def setup(side: Side) -> None:
            def many(entries: Any, **_: Any) -> dict:
                return {n: side.Reachability(status, "cli-version", f"{status}!") for n in entries}

            monkeypatch.setattr(side.m.seats, "check_many", many)

        left, right = both(sides, "models", "seats", "--self", "cl", "--json", capsys=capsys,
                           monkeypatch=monkeypatch, setup=setup)
        out = same_json(left, right)
        assert out.code == code

    def test_models_list_for_a_harness_and_a_transport(self, sides, registry, capsys, monkeypatch, home):
        harness = same_json(*both(sides, "models", "models", "--endpoint", "cl", capsys=capsys,
                                  monkeypatch=monkeypatch))
        assert json.loads(harness.out) == {"endpoint": "cl", "kind": "harness", "models": {"cl": "opus"}}
        transport = same_json(*both(sides, "models", "models", "--endpoint", "local", capsys=capsys,
                                    monkeypatch=monkeypatch))
        assert json.loads(transport.out)["kind"] == "transport"

    def test_resolve_reports_the_delivered_effort(self, sides, registry, capsys, monkeypatch, home):
        def setup(side: Side) -> None:
            side.patch_declaration_reachability(monkeypatch)

        harness = same_json(*both(sides, "models", "resolve", "--models", "cdx", capsys=capsys,
                                  monkeypatch=monkeypatch, setup=setup))
        assert json.loads(harness.out)["kind"] == "harness" and json.loads(harness.out)["effort"] == "high"
        transport = same_json(*both(sides, "models", "resolve", "--models", "local", capsys=capsys,
                                    monkeypatch=monkeypatch, setup=setup))
        assert json.loads(transport.out)["effort_delivery"]["style"] == "ninfer"

    def test_resolve_requirements_without_models_is_a_usage_error(self, sides, registry, capsys, monkeypatch, home):
        left, right = both(sides, "models", "resolve", "--requirements", "{}", capsys=capsys,
                           monkeypatch=monkeypatch)
        out = same(left, right)
        assert out.code == 2 and "--requirements needs --models" in out.err


# --------------------------------------------------------------------------
# local: swapper and acceptance
# --------------------------------------------------------------------------


class FakeSwapperClient:
    def __init__(self, base_url: str, running: Any = (), unload: Any = None, unload_error: Any = None) -> None:
        self.base_url = base_url
        self._running = tuple(running)
        self._unload = tuple(unload) if unload is not None else self._running
        self._unload_error = unload_error

    def running(self) -> Any:
        return self._running

    def unload_all(self) -> Any:
        if self._unload_error is not None:
            raise self._unload_error
        return self._unload


def _local_owner(side: Side) -> Any:
    return side.m.cli if side.name == "legacy" else side.m.local


def _running_model(side: Side, model: str = "qwen38") -> Any:
    return side.m.swapper.RunningModel(model=model, state="ready", proxy="http://127.0.0.1:5800", cmd="serve")


def _swapper_target(side: Side, monkeypatch: pytest.MonkeyPatch, client: FakeSwapperClient) -> None:
    owner = _local_owner(side)
    monkeypatch.setattr(owner, "resolve_swapper_url", lambda **_kw: client.base_url)
    monkeypatch.setattr(owner, "SwapperClient", lambda url: client)


class TestLocalSwapper:
    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_running(self, sides, home, capsys, monkeypatch, fmt):
        def setup(side: Side) -> None:
            client = FakeSwapperClient("http://127.0.0.1:8081", running=[_running_model(side), _running_model(side, "o")])
            _swapper_target(side, monkeypatch, client)

        left, right = both(sides, "local", "swapper", "running", "--url", "http://127.0.0.1:8081", "--format", fmt,
                           capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        out = same(left, right)
        assert out.code == 0 and "qwen38" in out.out

    def test_running_requires_exactly_one_target(self, sides, home, capsys, monkeypatch):
        for extra in ([], ["--endpoint", "e", "--url", "http://x"]):
            left, right = both(sides, "local", "swapper", "running", *extra, capsys=capsys, monkeypatch=monkeypatch)
            same_usage_error(left, right)

    @pytest.mark.parametrize("escalated", [False, True])
    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_terminate(self, sides, home, capsys, monkeypatch, escalated, fmt):
        def setup(side: Side) -> None:
            client = FakeSwapperClient("http://127.0.0.1:8081")
            _swapper_target(side, monkeypatch, client)
            result = types_ns(signal="SIGTERM", escalated=escalated,
                              process=types_ns(pid=4242, swapper_pid=99))
            monkeypatch.setattr(_local_owner(side), "terminate_model", lambda c, model, grace_s: result)

        left, right = both(sides, "local", "swapper", "terminate", "qwen38", "--url", "http://x", "--format", fmt,
                           capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        assert same(left, right).code == 0

    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_unload_requires_both_confirmations_and_reports(self, sides, home, capsys, monkeypatch, fmt):
        refused = same_usage_error(*both(sides, "local", "swapper", "unload", "--url", "http://x", capsys=capsys,
                                         monkeypatch=monkeypatch))
        assert "--all, --accept-no-drain" in refused.err

        def setup(side: Side) -> None:
            client = FakeSwapperClient("http://127.0.0.1:8081", running=[_running_model(side)])
            _swapper_target(side, monkeypatch, client)

        left, right = both(sides, "local", "swapper", "unload", "--url", "http://x", "--all", "--accept-no-drain",
                           "--format", fmt, capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        assert same(left, right).code == 0

    @pytest.mark.parametrize("found,code", [(True, 1), (False, 0)])
    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_strays_exit_one_when_found(self, sides, home, capsys, monkeypatch, found, code, fmt):
        def setup(side: Side) -> None:
            records = []
            if found:
                records = [side.m.process.ProcessRecord(pid=7, ppid=1, name="mlx", owner="me", create_time=1.0)]
            monkeypatch.setattr(_local_owner(side), "find_strays", lambda: records)

        left, right = both(sides, "local", "swapper", "strays", "--format", fmt, capsys=capsys,
                           monkeypatch=monkeypatch, setup=setup)
        assert same(left, right).code == code

    @pytest.mark.parametrize("action", ["none", "terminated"])
    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_terminate_listener(self, sides, home, capsys, monkeypatch, action, fmt):
        def setup(side: Side) -> None:
            result = types_ns(port=5800, action=action, pid=1 if action != "none" else None,
                              create_time=2.5, signal="SIGTERM", escalated=False)
            monkeypatch.setattr(_local_owner(side), "terminate_listener", lambda port, grace_s: result)

        left, right = both(sides, "local", "swapper", "terminate-listener", "--port", "5800", "--accept-replace",
                           "--format", fmt, capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        assert same(left, right).code == 0

    def test_guard_launch_is_quiet_on_success_and_exits_three_on_refusal(self, sides, home, capsys, monkeypatch):
        def allow(side: Side) -> None:
            monkeypatch.setattr(_local_owner(side), "check_launch_allowed", lambda caller_pid: None)

        quiet = same(*both(sides, "local", "swapper", "guard-launch", "--caller-pid", "1", capsys=capsys,
                           monkeypatch=monkeypatch, setup=allow))
        assert (quiet.code, quiet.out, quiet.err) == (0, "", "")

        def refuse(side: Side) -> None:
            def check(caller_pid: int) -> None:
                raise side.m.swapper.LaunchRefused("a swapper is active")

            monkeypatch.setattr(_local_owner(side), "check_launch_allowed", check)

        refused = same(*both(sides, "local", "swapper", "guard-launch", "--caller-pid", "1", capsys=capsys,
                             monkeypatch=monkeypatch, setup=refuse))
        assert refused.code == 3 and json.loads(refused.err)["error"]["kind"] == "launch-refused"

    @pytest.mark.parametrize("name,expected", [
        ("NonLocalTargetError", 2), ("ModelNotRunningError", 1), ("SwapperUsageError", 2),
        ("InspectionIndeterminate", 5), ("SafetyRefusal", 1), ("LaunchRefused", 3),
    ])
    def test_every_swapper_error_maps_to_its_own_exit_code(self, sides, home, capsys, monkeypatch, name, expected):
        def setup(side: Side) -> None:
            cls = getattr(side.m.swapper, name)
            client = FakeSwapperClient("http://x")
            _swapper_target(side, monkeypatch, client)

            def boom(*_a: Any, **_k: Any) -> Any:
                raise cls("nope")

            monkeypatch.setattr(_local_owner(side), "terminate_model", boom)

        left, right = both(sides, "local", "swapper", "terminate", "m", "--url", "http://x", capsys=capsys,
                           monkeypatch=monkeypatch, setup=setup)
        out = same(left, right)
        assert out.code == expected
        assert json.loads(out.err)["error"]["message"] == "nope"


def types_ns(**kw: Any) -> Any:
    import types

    return types.SimpleNamespace(**kw)


class TestLocalAcceptance:
    @pytest.mark.parametrize("fmt,exit_code", [("text", 0), ("json", 1)])
    def test_swapper_acceptance_reports_and_exits_with_the_report(self, sides, home, capsys, monkeypatch, fmt,
                                                                   exit_code):
        seen = {}

        def setup(side: Side) -> None:
            def run_acceptance(url: str, rounds: int, settle_s: float, poll_s: float) -> Any:
                seen[side.name] = (url, rounds, settle_s, poll_s)
                return types_ns(to_json=lambda: {"passed": exit_code == 0}, to_text=lambda: "PASSED",
                                exit_code=exit_code)

            monkeypatch.setattr(side.m.swapper_acceptance, "run_swapper_acceptance", run_acceptance)

        left, right = both(sides, "local", "acceptance", "swapper", "--url", "http://x", "--rounds", "2",
                           "--format", fmt, capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        assert same(left, right).code == exit_code
        assert seen["legacy"] == seen["final"] == ("http://x", 2, 3.0, 1.0)

    def test_frontdoor_acceptance_passes_its_arguments_and_maps_a_config_error_to_two(self, sides, home, capsys,
                                                                                      monkeypatch):
        seen = {}

        def setup(side: Side) -> None:
            def run_acceptance(url: str, registry: str, **kw: Any) -> Any:
                seen[side.name] = (url, registry, kw)
                return types_ns(to_json=lambda: {"ok": True}, to_text=lambda: "OK", exit_code=0)

            monkeypatch.setattr(side.m.frontdoor_acceptance, "run_frontdoor_acceptance", run_acceptance)

        args = ("local", "acceptance", "frontdoor", "--url", "http://f", "--registry", "r.yaml", "--spill-group",
                "g", "--queue-group", "q", "--quick", "--paid", "--expect-tier1-cap", "3")
        left, right = both(sides, *args, capsys=capsys, monkeypatch=monkeypatch, setup=setup)
        assert same(left, right).code == 0
        assert seen["legacy"] == seen["final"]
        assert seen["final"][2] == {"spill_group": "g", "queue_group": "q", "quick": True, "paid": True,
                                    "expect_tier1_cap": 3}

        def misconfigured(side: Side) -> None:
            def run_acceptance(url: str, rounds: int, **kw: Any) -> Any:
                raise side.m.swapper_acceptance.AcceptanceConfigError("rounds must be at least 1")

            monkeypatch.setattr(side.m.swapper_acceptance, "run_swapper_acceptance", run_acceptance)

        left, right = both(sides, "local", "acceptance", "swapper", "--url", "http://x", "--rounds", "0",
                           capsys=capsys, monkeypatch=monkeypatch, setup=misconfigured)
        out = same(left, right)
        assert out.code == 2 and json.loads(out.err)["error"]["kind"] == "configuration"


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

_JOBS_REGISTRY = """\
default: fake-endpoint
models:
  fake-endpoint: {harness: claude, model: fake-model}
"""


def _fake_claude(side: Side) -> type:
    """A claude-cli stand-in: real advertised capabilities, one fixed answer."""
    response = side.LLMResponse

    class FakeClaude:
        name = "claude-cli"
        calls: list = []

        def __init__(self, **_kw: Any) -> None:
            pass

        @property
        def capabilities(self) -> Any:
            from importlib import import_module

            mod = (
                import_module("llm_scripting_kit.completion.adapter_capabilities") if side.name == "legacy"
                else import_module("llm_kit.adapters.capabilities")
            )
            return mod.adapter_capabilities()["claude-cli"]

        def complete(self, system: str, user: str, *, model: str, options: Any = None) -> Any:
            FakeClaude.calls.append(user)
            return response(text="answer", model="fake-model")

        def classify_halt(self, exc: BaseException) -> None:
            return None

    return FakeClaude


def _install_jobs_transport(side: Side, monkeypatch: pytest.MonkeyPatch) -> type:
    """Route the side's job runner to the fake claude with no process started."""
    fake = _fake_claude(side)
    side.patch_declaration_reachability(monkeypatch)
    if side.name == "legacy":
        monkeypatch.setattr(side.m.factory, "ClaudeCliBackend", fake)
    else:
        from llm_kit.dispatch.pipeline_backends import ConfiguredBackendFactory

        monkeypatch.setattr(
            side.m.jobs_cli, "configured_factory",
            lambda: ConfiguredBackendFactory(classes={"claude-cli": fake}),
        )
    return fake


class JobsSide:
    """Addressing and path normalization for the jobs group of one side."""

    def __init__(self, side: Side, root: Path) -> None:
        self.side = side
        self.root = root / side.name
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def store(self) -> Path:
        return self.root / "ledger.sqlite3"

    def main(self) -> Callable[..., int]:
        return self.side.m.jobs_cli.main if self.side.name == "legacy" else self.side.m.cli.main

    def argv(self, verb: str, *rest: str) -> list[str]:
        return [verb, *rest] if self.side.name == "legacy" else ["jobs", verb, *rest]

    def jobs_file(self, job_ids: Sequence[str] = ("first",), contract: Optional[str] = None,
                  max_parallel: int = 1) -> Path:
        command = contract or f'[{json.dumps(sys.executable)}, -c, "pass"]'
        entries = "".join(
            f"  - id: {job_id}\n    prompt:\n      user: {job_id}\n    models: [fake-endpoint]\n"
            f"    directory: {self.root.as_posix()}\n    contract:\n      command: {command}\n"
            f"      directory: {self.root.as_posix()}\n"
            for job_id in job_ids
        )
        return write(self.root / "jobs.yaml",
                     f"max_parallel: {max_parallel}\nworkspace_root: {(self.root / 'ws').as_posix()}\njobs:\n{entries}")

    def norm(self, text: str) -> str:
        """Temp paths replaced (plain, posix, and repr-escaped forms); the program name is NOT touched.

        Error messages keep the legacy ``job-kit:`` prefix on both sides, so only the
        argparse banner differs, and tests compare that part by its ``error:`` text.
        """
        roots = {self.root, self.root.resolve()}
        repl: dict[str, str] = {}
        for root in sorted(roots, key=lambda r: -len(str(r))):
            for form in (str(root), root.as_posix(), str(root).replace("\\", "\\\\")):
                repl[form] = "<S>"
        repl[self.side.name] = "<side>"
        return normalize(text, repl)

    def invoke(self, capsys: Any, monkeypatch: pytest.MonkeyPatch, verb: str, *rest: str,
               stdin: Optional[str] = None) -> Outcome:
        outcome = run(self.main(), self.argv(verb, *rest), capsys, stdin=stdin, monkeypatch=monkeypatch)
        return Outcome(outcome.code, self.norm(outcome.out), self.norm(outcome.err))


@pytest.fixture()
def jobs_pair(sides, home, tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "jobs-registry.yaml", _JOBS_REGISTRY)))
    for side in sides:
        _install_jobs_transport(side, monkeypatch)
    return JobsSide(sides[0], tmp_path), JobsSide(sides[1], tmp_path)


def _jobs_both(pair, capsys, monkeypatch, verb: str, *rest: str, stdin: Optional[str] = None,
               with_store: bool = True, args_for: Optional[Callable[[JobsSide], Sequence[str]]] = None) -> tuple:
    outcomes = []
    for js in pair:
        extra = list(args_for(js)) if args_for is not None else []
        store = ["--store", str(js.store)] if with_store else []
        outcomes.append(js.invoke(capsys, monkeypatch, verb, *rest, *extra, *store, stdin=stdin))
    return outcomes[0], outcomes[1]


def _without_zero_cancelled_count(text: str) -> str:
    """Drop ``counts.cancelled`` from a status payload while it is zero.

    Deliberate difference (ledger schema 13): llm-kit's ``counts`` carries a
    ``cancelled`` key legacy job-kit lacks. A nonzero count is left in place so
    a run that cancelled a job would still differ.
    """
    stripped = []
    for line in text.splitlines(keepends=True):
        try:
            payload = json.loads(line)
        except ValueError:
            stripped.append(line)
            continue
        counts = payload.get("counts") if isinstance(payload, dict) else None
        if isinstance(counts, dict) and counts.get("cancelled") == 0:
            del counts["cancelled"]
            line = json.dumps(payload, sort_keys=True) + ("\n" if line.endswith("\n") else "")
        stripped.append(line)
    return "".join(stripped)


def _same_jobs_json(left: Outcome, right: Outcome) -> Outcome:
    assert left.code == right.code, (left, right)
    assert left.err == right.err, (left.err, right.err)
    assert normalize_json_text(_without_zero_cancelled_count(left.out), {}) == normalize_json_text(
        _without_zero_cancelled_count(right.out), {}
    ), (left.out, right.out)
    return left


class TestJobs:
    def test_run_status_events_and_gc_over_one_accepted_run(self, jobs_pair, capsys, monkeypatch):
        outcomes = {}
        for js in jobs_pair:
            # The file asks for 2 workers; the override to 1 must win, be recorded, and keep
            # the event order deterministic.
            jobs_file = js.jobs_file(("first", "second"), max_parallel=2)
            outcomes[js.side.name] = {
                "run": js.invoke(capsys, monkeypatch, "run", str(jobs_file), "--store", str(js.store),
                                 "--run-id", "run-1", "--max-parallel", "1"),
                "status": js.invoke(capsys, monkeypatch, "status", "run-1", "--store", str(js.store)),
                "events": js.invoke(capsys, monkeypatch, "events", "run-1", "--store", str(js.store)),
                "gc": js.invoke(capsys, monkeypatch, "gc", "run-1", "--store", str(js.store)),
            }
        for step in ("run", "status", "gc"):
            _same_jobs_json(outcomes["legacy"][step], outcomes["final"][step])
        assert outcomes["final"]["run"].code == 0
        payload = json.loads(outcomes["final"]["run"].out)
        assert payload["run"]["id"] == "run-1" and payload["run"]["max_parallel"] == 1
        assert {j["state"] for j in payload["jobs"]} == {"accepted"}
        # The event stream: same events in the same order, byte for byte after the clock is removed.
        legacy_events = [scrub_json(json.loads(l)) for l in outcomes["legacy"]["events"].out.splitlines()]
        final_events = [scrub_json(json.loads(l)) for l in outcomes["final"]["events"].out.splitlines()]
        assert legacy_events == final_events and legacy_events
        assert outcomes["legacy"]["events"].code == outcomes["final"]["events"].code == 0

    def test_a_failing_contract_exits_one(self, jobs_pair, capsys, monkeypatch):
        outcomes = []
        for js in jobs_pair:
            jobs_file = js.jobs_file(contract=f'[{json.dumps(sys.executable)}, -c, "import sys; sys.exit(3)"]')
            outcomes.append(js.invoke(capsys, monkeypatch, "run", str(jobs_file), "--store", str(js.store),
                                      "--run-id", "run-f"))
        out = _same_jobs_json(*outcomes)
        assert out.code == 1
        assert {j["state"] for j in json.loads(out.out)["jobs"]} <= {"rejected", "failed"}

    def test_resume_a_created_run(self, jobs_pair, capsys, monkeypatch):
        outcomes = []
        for js in jobs_pair:
            jobs_file = js.jobs_file()
            api = js.side.m
            model = api.jobs_model
            job_file = model.load_job_file(jobs_file)
            api.jobs_store.JobStore(js.store).create_run(
                "run-r", job_file.jobs, jobs_path=jobs_file, max_parallel=1, workspace_root=js.root / "ws")
            outcomes.append(js.invoke(capsys, monkeypatch, "resume", "run-r", "--store", str(js.store),
                                      "--max-parallel", "2"))
        out = _same_jobs_json(*outcomes)
        assert out.code == 0 and {j["state"] for j in json.loads(out.out)["jobs"]} == {"accepted"}

    def test_events_out_writes_a_new_file_and_refuses_an_existing_one(self, jobs_pair, capsys, monkeypatch):
        written, refused = [], []
        for js in jobs_pair:
            jobs_file = js.jobs_file()
            js.invoke(capsys, monkeypatch, "run", str(jobs_file), "--store", str(js.store), "--run-id", "run-e")
            target = js.root / "events.jsonl"
            written.append(js.invoke(capsys, monkeypatch, "events", "run-e", "--store", str(js.store),
                                     "--out", str(target)))
            written[-1] = Outcome(written[-1].code, written[-1].out, written[-1].err)
            refused.append(js.invoke(capsys, monkeypatch, "events", "run-e", "--store", str(js.store),
                                     "--out", str(target)))
            assert target.read_text(encoding="utf-8")
        _same_jobs_json(*written)
        assert written[0].code == 0
        left, right = refused
        assert left.code == right.code == 3 and left.err == right.err
        assert "--out file already exists" in left.err

    def test_status_of_a_missing_store_is_a_runner_failure(self, jobs_pair, capsys, monkeypatch):
        left, right = _jobs_both(jobs_pair, capsys, monkeypatch, "status", "run-1")
        assert (left.code, left.out, left.err) == (right.code, right.out, right.err)
        assert left.code == 3 and "store does not exist" in left.err and left.err.startswith("job-kit: ")

    def test_an_unreadable_jobs_file_is_a_runner_failure(self, jobs_pair, capsys, monkeypatch):
        outcomes = []
        for js in jobs_pair:
            outcomes.append(js.invoke(capsys, monkeypatch, "run", str(js.root / "absent.yaml"),
                                      "--store", str(js.store)))
        left, right = outcomes
        assert left.code == right.code == 3 and left.out == right.out == ""
        assert left.err.startswith("job-kit: ")

    def test_resolve_refusals_and_usage(self, jobs_pair, capsys, monkeypatch):
        left, right = _jobs_both(jobs_pair, capsys, monkeypatch, "resolve", "run-1", "int-1", "--reject")
        assert (left.code, left.out, left.err) == (right.code, right.out, right.err)
        assert left.code == 3
        usage = _jobs_both(jobs_pair, capsys, monkeypatch, "resolve", "run-1", "int-1", "--input", "{}",
                           "--reason", "why")
        assert usage[0].code == usage[1].code == 2
        assert usage[0].err.splitlines()[-1].split(": ", 1)[1] == usage[1].err.splitlines()[-1].split(": ", 1)[1]
        assert "--reason is only valid with --reject" in usage[0].err
        neither = _jobs_both(jobs_pair, capsys, monkeypatch, "resolve", "run-1", "int-1")
        assert neither[0].code == neither[1].code == 2

    def test_max_parallel_and_run_id_are_validated_by_argparse(self, jobs_pair, capsys, monkeypatch):
        for bad in (("--max-parallel", "0"), ("--run-id", "has space")):
            outcomes = []
            for js in jobs_pair:
                jobs_file = js.jobs_file()
                outcomes.append(js.invoke(capsys, monkeypatch, "run", str(jobs_file), *bad))
            assert outcomes[0].code == outcomes[1].code == 2
            assert outcomes[0].err.splitlines()[-1].split(": ", 1)[1] == outcomes[1].err.splitlines()[-1].split(": ", 1)[1]

    def test_the_exit_code_for_a_snapshot_is_identical(self, sides):
        """failure dominates a wait; a wait is 4; all accepted is 0; anything else is 1."""
        outcomes = {}
        for side in sides:
            js_mod = side.m.jobs_cli
            states = side.m.jobs_model.JobState
            exit_for = js_mod._exit_for_snapshot
            ns = lambda *s: types_ns(jobs=[types_ns(state=x) for x in s])  # noqa: E731
            outcomes[side.name] = [
                exit_for(ns(states.ACCEPTED)),
                exit_for(ns(states.ACCEPTED, states.WAITING)),
                exit_for(ns(states.WAITING, states.REJECTED)),
                exit_for(ns(states.ACCEPTED, states.RUNNING)),
            ]
        assert outcomes["legacy"] == outcomes["final"] == [0, 4, 1, 1]


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------

_YAML_CLOCK = re.compile(r"(?m)^(\s*[\w-]*(?:_at|_s|_ms|elapsed|per_min|rate)[\w-]*: ).*$")


def _scrub_yaml(text: str) -> str:
    return _YAML_CLOCK.sub(r"\1<t>", text)


class PipelineSide:
    """Addressing for the leased-run command set: legacy library mount vs ``llm-kit pipeline``."""

    def __init__(self, side: Side, root: Path) -> None:
        self.side = side
        self.root = root / side.name
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "run.db"

    def invoke(self, capsys: Any, monkeypatch: pytest.MonkeyPatch, *argv: str,
               stdin: Optional[str] = None) -> Outcome:
        if self.side.name == "legacy":
            m = self.side.m

            def main(args: list[str]) -> int:
                store = m.cpk_store.ExecutionStore(self.db)
                return m.cpk_scaffold.dispatch(args, m.cpk_run.build_commands(store))

            outcome = run(main, list(argv), capsys, stdin=stdin, monkeypatch=monkeypatch)
        else:
            outcome = run(self.side.m.cli.main, ["pipeline", "--store", str(self.db), *argv], capsys,
                          stdin=stdin, monkeypatch=monkeypatch)
        repl = {str(self.root): "<S>", self.root.as_posix(): "<S>"}
        return Outcome(outcome.code, _scrub_yaml(normalize(outcome.out, repl)), normalize(outcome.err, repl))


@pytest.fixture()
def pipeline_pair(sides, home, tmp_path):
    return PipelineSide(sides[0], tmp_path), PipelineSide(sides[1], tmp_path)


def _pipe(pair, capsys, monkeypatch, *argv: str, stdin: Optional[str] = None) -> Outcome:
    left, right = (ps.invoke(capsys, monkeypatch, *argv, stdin=stdin) for ps in pair)
    return same(left, right)


class TestPipeline:
    def test_a_full_unit_lifecycle_renders_identically(self, pipeline_pair, capsys, monkeypatch):
        steps = [
            ("create-run", "r1", "inline", "mock", "m", "v1"),
            ("register-units", "r1", "u0", "u1", "u2"),
            ("claim", "r1", "u0", "worker-a", "--lease-seconds=60"),
            ("renew", "r1", "u0", "1", "--lease-seconds=90"),
            ("accept", "r1", "u0", "1", "--input-tokens=5", "--output-tokens=7"),
            ("claim", "r1", "u1", "worker-b"),
            ("fail", "r1", "u1", "1", "--error=boom"),
            ("claim", "r1", "u1", "worker-b"),
            ("fail", "r1", "u1", "2", "--error=final", "--terminal"),
            ("halt", "r1", "quota", "--detail=out"),
            ("clear-halt", "r1"),
            ("status", "r1", "--window=30", "--max-failure-groups=2"),
        ]
        for step in steps:
            out = _pipe(pipeline_pair, capsys, monkeypatch, *step)
            assert out.code == 0, (step, out)
        status = _pipe(pipeline_pair, capsys, monkeypatch, "status", "r1")
        assert "r1" in status.out

    def test_create_run_resolves_a_declaration_for_empty_backend_and_model(self, pipeline_pair, capsys, monkeypatch):
        from importlib import import_module

        for module in ("content_pipeline.llm.backends", "llm_kit.pipeline.runtime.backends"):
            try:
                mod = import_module(module)
            except ImportError:
                continue
            monkeypatch.setattr(mod, "declared_backend_and_model", lambda names, **_: ("claude-cli", f"model-{names[0]}"))
        out = _pipe(pipeline_pair, capsys, monkeypatch, "create-run", "r2", "inline", "", "", "v1", "--models=a,b")
        assert out.code == 0 and "backend: claude-cli" in out.out and "model: model-a" in out.out

    def test_usage_errors_and_command_errors(self, pipeline_pair, capsys, monkeypatch):
        unknown = _pipe(pipeline_pair, capsys, monkeypatch, "clam")
        assert unknown.code == 2 and "Did you mean: claim" in unknown.err
        bare = _pipe(pipeline_pair, capsys, monkeypatch)
        assert bare.code == 2 and "usage: <command> [args]; commands:" in bare.err
        missing = _pipe(pipeline_pair, capsys, monkeypatch, "claim", "r1")
        assert missing.code == 1 and "missing required argument: unit_id" in missing.err
        unknown_run = _pipe(pipeline_pair, capsys, monkeypatch, "status", "nope")
        assert unknown_run.code == 1


# --------------------------------------------------------------------------
# frontdoor
# --------------------------------------------------------------------------


class TestFrontdoor:
    """``frontdoor`` hands every argument to the front door's own parser, on both sides."""

    def _both(self, sides, capsys, monkeypatch, *argv: str) -> tuple[Outcome, Outcome]:
        return tuple(  # type: ignore[return-value]
            run(side.main(), ["frontdoor", *argv], capsys, monkeypatch=monkeypatch) for side in sides
        )

    def test_check_and_print_config(self, sides, registry, capsys, monkeypatch, tmp_path):
        left, right = self._both(sides, capsys, monkeypatch, "--check", "--registry", str(registry))
        repl = {str(registry): "<R>", registry.as_posix(): "<R>"}
        left = Outcome(left.code, normalize(left.out, repl), normalize(left.err, repl))
        right = Outcome(right.code, normalize(right.out, repl), normalize(right.err, repl))
        assert left.code == right.code and left.out == right.out and left.err == right.err
        left, right = self._both(sides, capsys, monkeypatch, "--print-config", "--registry", str(registry),
                                 "--port", "4999", "--spill-after", "1.5")
        assert left.code == right.code == 0
        left_cfg, right_cfg = json.loads(left.out), json.loads(right.out)
        assert left_cfg == right_cfg and left_cfg["port"] == 4999 and left_cfg["spill_after_s"] == 1.5

    def test_a_missing_registry_is_a_usage_error(self, sides, home, capsys, monkeypatch, tmp_path):
        gone = tmp_path / "gone.yaml"
        left, right = self._both(sides, capsys, monkeypatch, "--check", "--registry", str(gone))
        assert left.code == right.code == 2 and left.out == right.out == ""
        assert left.err.replace(str(gone), "<R>") == right.err.replace(str(gone), "<R>")

    def test_a_leading_option_reaches_the_front_door_parser(self, sides, registry, capsys, monkeypatch):
        """The reason the group bypasses argparse subparsers: ``frontdoor --check`` must parse."""
        left, right = self._both(sides, capsys, monkeypatch, "--check", "--registry", str(registry))
        assert left.code == right.code == 0 and left.out
