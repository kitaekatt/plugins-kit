"""``options.skill_context`` on the ``complete`` request protocol.

A request carries a skill SELECTION (JSON); the coercer materializes it with
the real ``bootstrap_lib.skill_material`` against the working directory, before
any endpoint is resolved. A refused selection or an unusable library is a
protocol error (exit 4); an adapter refusal after parsing is a failed envelope
(exit 1).
"""
from __future__ import annotations

import json
import sys
import typing

import pytest

import bootstrap_lib
from llm_scripting_kit import cli
from llm_scripting_kit import request_protocol
from llm_scripting_kit.completion.backends import ClaudeCliBackend, OpenRouterBackend
from llm_scripting_kit.completion.factory import BackendSelection
from llm_scripting_kit.completion.skill_context import materialize_skill_context
from llm_scripting_kit.completion.skill_context_types import SkillContext
from llm_scripting_kit.request_protocol import (
    ProtocolError,
    _WIRE_TYPE_OVERRIDES,
    _classify,
    _ensure_coercible,
    _option_fields,
    describe_request_schema,
    parse_request,
)

LIBRARY = "bootstrap_lib.skill_material"


def _write_skill(root, name="alpha"):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    text = f"---\nname: {name}\ndescription: The {name} skill.\n---\nFollow the {name} procedure.\n"
    (skill_dir / "SKILL.md").write_bytes(text.encode("utf-8"))
    return skill_dir


def _request(path, **extra):
    options = {"skill_context": {"skills": [{"path": path}], "token_budget": 10_000}}
    options.update(extra)
    return {"protocol": 1, "system": "sys", "prompt": "hi", "options": options}


def test_protocol_request_materializes_real_skill(tmp_path, monkeypatch):
    _write_skill(tmp_path / "skills")
    monkeypatch.chdir(tmp_path)
    request = parse_request(_request("skills/alpha"))
    context = request.options.skill_context
    assert isinstance(context, SkillContext)
    expected = materialize_skill_context(
        {"skills": [{"path": "skills/alpha"}], "token_budget": 10_000}, base_dir=tmp_path
    )
    assert context.report.digest == expected.report.digest
    assert context.text == expected.text
    assert context.report.provenance["skills"][0]["source"] == str(
        (tmp_path / "skills" / "alpha" / "SKILL.md").resolve()
    )


def test_request_schema_describes_skill_selection_object():
    described = describe_request_schema()["options"]["skill_context"]
    for word in ("token_budget", "skills", "declared_resources", "working directory"):
        assert word in described
    assert "SkillContext" not in described
    # The derived key set is unchanged: the override only renames a type.
    assert set(describe_request_schema()["options"]) == set(_option_fields())


def test_wire_type_overrides_are_settable_fields():
    assert _WIRE_TYPE_OVERRIDES
    assert set(_WIRE_TYPE_OVERRIDES) <= set(_option_fields())


def test_protocol_too_old_bootstrap_is_protocol_error(tmp_path, monkeypatch):
    skill = _write_skill(tmp_path)
    monkeypatch.setitem(sys.modules, LIBRARY, None)
    monkeypatch.delattr(bootstrap_lib, "skill_material", raising=False)
    with pytest.raises(ProtocolError) as info:
        parse_request(_request(str(skill)))
    assert "options.skill_context" in str(info.value)
    assert "claude plugin update bootstrap@plugins-kit" in str(info.value)


def test_protocol_library_refusal_is_protocol_error(tmp_path):
    with pytest.raises(ProtocolError) as info:
        parse_request(_request(str(tmp_path / "missing-skill")))
    assert "options.skill_context" in str(info.value)
    with pytest.raises(ProtocolError, match="options.skill_context"):
        parse_request(_request(str(_write_skill(tmp_path)))
                      | {"options": {"skill_context": {"skills": [], "token_budget": 1}}})


def test_request_protocol_imports():
    """The import-time coercibility assertion covers the new field."""
    _ensure_coercible()
    assert _classify(typing.Optional[SkillContext]) == "skill-context"


# -- the CLI ---------------------------------------------------------------------


class _Message:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = None


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = None


class _RecordingClient:
    def __init__(self):
        self.calls = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return _Response("answer")

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


class _RecordingRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(list(cmd))
        return json.dumps({"result": "ok", "usage": {}}), "", 0


def _request_file(tmp_path):
    skill = _write_skill(tmp_path)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(_request(str(skill))), encoding="utf-8")
    return str(path)


def test_cli_complete_envelope_includes_skill_context_report(tmp_path, monkeypatch, capsys):
    client = _RecordingClient()
    backend = OpenRouterBackend(client=client)
    monkeypatch.setattr(
        cli, "create_backend",
        lambda *_, **__: BackendSelection("chosen", "transport", backend, "test/slug", None),
    )
    assert cli.main(["complete", "--request-file", _request_file(tmp_path)]) == cli.EXIT_OK
    report = json.loads(capsys.readouterr().out)["response"]["skill_context"]
    assert report["adapter"] == "openrouter"
    assert report["delivery"] == "system-message"
    assert report["provenance"]["schema"] == "plugins-kit.skill-material-report/v1"
    system_text = client.calls[0]["messages"][0]["content"][0]["text"]
    assert system_text.startswith('<skill_context version="1">')
    assert system_text.endswith("</skill_context>\n\nsys")


def test_cli_complete_harness_skill_context_exits_1(tmp_path, monkeypatch, capsys):
    runner = _RecordingRunner()
    backend = ClaudeCliBackend(runner=runner, executable="claude")
    monkeypatch.setattr(
        cli, "create_backend",
        lambda *_, **__: BackendSelection("chosen", "harness", backend, "sonnet", None),
    )
    assert cli.main(["complete", "--request-file", _request_file(tmp_path)]) == cli.EXIT_FAILURE
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["response"]["status"] == "error"
    assert "does not deliver skill context" in envelope["response"]["error"]["message"]
    assert runner.calls == []


def test_cli_complete_too_old_bootstrap_exits_4(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, LIBRARY, None)
    monkeypatch.delattr(bootstrap_lib, "skill_material", raising=False)

    def _never(*args, **kwargs):
        raise AssertionError("no endpoint is resolved for a protocol error")

    monkeypatch.setattr(cli, "create_backend", _never)
    assert cli.main(["complete", "--request-file", _request_file(tmp_path)]) == cli.EXIT_PROTOCOL
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["kind"] == "protocol"
    assert "claude plugin update bootstrap@plugins-kit" in error["message"]
