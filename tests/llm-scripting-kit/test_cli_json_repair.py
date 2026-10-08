"""Model-free ``repair-json`` CLI contract."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from llm_scripting_kit import cli


_SCHEMA = {
    "type": "object",
    "required": ["a"],
    "additionalProperties": False,
    "properties": {"a": {"type": "array", "items": {"type": "string"}}},
}
_AMBIGUOUS_SCHEMA = {
    "type": "object",
    "required": ["a"],
    "additionalProperties": False,
    "properties": {
        "a": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
        }
    },
}
_OUTPUT_KEYS = {"protocol", "status", "text", "edits", "reason"}


def _write_schema(path: Path, schema: Any) -> Path:
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path.resolve()


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    schema_file: Path,
    raw: bytes,
) -> tuple[int, str, str]:
    stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    code = cli.main(["repair-json", "--schema-file", str(schema_file)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize(
    ("schema", "raw", "expected"),
    [
        (
            _SCHEMA,
            '{"a":["x"]}',
            {
                "protocol": 1,
                "status": "valid",
                "text": '{"a":["x"]}',
                "edits": [],
                "reason": "",
            },
        ),
        (
            _SCHEMA,
            'prefix {"a":["x"} suffix',
            {
                "protocol": 1,
                "status": "repaired",
                "text": '{"a":["x"]}',
                "edits": [
                    {"op": "strip", "token": "leading", "offset": 0},
                    {"op": "strip", "token": "trailing", "offset": 17},
                    {"op": "insert", "token": "]", "offset": 16},
                ],
                "reason": "",
            },
        ),
        (
            _AMBIGUOUS_SCHEMA,
            '{"a":[["x"]"y"]]}',
            {
                "protocol": 1,
                "status": "ambiguous",
                "text": '{"a":[["x"]"y"]]}',
                "edits": [],
                "reason": "2 different structures fit the schema",
            },
        ),
        (
            _SCHEMA,
            '{"a":["x"',
            {
                "protocol": 1,
                "status": "unrecoverable",
                "text": '{"a":["x"',
                "edits": [],
                "reason": "no structural edit yields a document of the schema's shape",
            },
        ),
    ],
    ids=("valid", "repaired", "ambiguous", "unrecoverable"),
)
def test_statuses_round_trip_with_exact_output_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    schema: Any,
    raw: str,
    expected: dict[str, Any],
) -> None:
    schema_file = _write_schema(tmp_path / "schema.json", schema)
    code, stdout, stderr = _run(monkeypatch, capsys, schema_file, raw.encode("utf-8"))

    assert code == cli.EXIT_OK
    assert stderr == ""
    assert stdout.count("\n") == 1
    payload = json.loads(stdout)
    assert set(payload) == _OUTPUT_KEYS
    assert payload == expected


@pytest.mark.parametrize("kind", ("missing", "directory", "not-json"))
def test_invalid_schema_file_exits_two_without_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    schema_file = (tmp_path / "schema.json").resolve()
    if kind == "directory":
        schema_file.mkdir()
    elif kind == "not-json":
        schema_file.write_text("{not json", encoding="utf-8")

    code, stdout, stderr = _run(monkeypatch, capsys, schema_file, b"{}")

    assert code == cli.EXIT_USAGE
    assert stdout == ""
    assert stderr.count("\n") == 1
    assert json.loads(stderr)["error"]["kind"] == "configuration"


def test_unsupported_schema_exits_two_without_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schema_file = _write_schema(tmp_path / "schema.json", {"oneOf": [{"type": "string"}]})

    code, stdout, stderr = _run(monkeypatch, capsys, schema_file, b'"value"')

    assert code == cli.EXIT_USAGE
    assert stdout == ""
    assert stderr.count("\n") == 1
    assert "oneOf" in stderr


def test_non_utf8_stdin_exits_two_without_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schema_file = _write_schema(tmp_path / "schema.json", _SCHEMA)

    code, stdout, stderr = _run(monkeypatch, capsys, schema_file, b"\xff")

    assert code == cli.EXIT_USAGE
    assert stdout == ""
    assert stderr.count("\n") == 1
    assert json.loads(stderr)["error"]["kind"] == "configuration"


def test_command_never_resolves_an_endpoint_or_touches_the_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schema_file = _write_schema(tmp_path / "schema.json", _SCHEMA)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("repair-json crossed into endpoint or network code")

    for name in (
        "_collect_endpoint_entries",
        "_first_usable",
        "create_backend",
        "get_api_key",
        "load_model_config",
        "resolve_endpoint",
        "resolve_model",
        "validate_endpoint",
    ):
        monkeypatch.setattr(cli, name, forbidden)

    code, stdout, stderr = _run(monkeypatch, capsys, schema_file, b'{"a":[]}')

    assert code == cli.EXIT_OK
    assert stderr == ""
    assert json.loads(stdout)["status"] == "valid"
