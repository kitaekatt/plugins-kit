"""Tests for content_pipeline.cli.run's "protocol" command and create-run's
adapter_version handling.

Pins two fixes:

- Defect 1: the envelope is read from stdin (preferred), from ``@<path>``, or
  from the legacy positional-argv form (back-compat). stdin and ``@<path>``
  are decoded as UTF-8 explicitly, and a payload containing both escaped
  quotes and a `|` (a YAML block scalar) round-trips correctly -- the argv
  path is exactly what breaks on Windows under a `.bat` wrapper, since
  `cmd.exe` re-parses the command line and misreads the `|` as a pipe
  operator once its quote-tracking is confused by the escaped inner quotes.
- Defect 2: create-run echoes ``adapter_version`` in its result, and refuses
  a supplied ``adapter_version`` that disagrees with a mounted adapter's own
  reported identity.

Every content-carrying assertion below reads the value back out of the
STORE (via ``_last_attempt_error``) rather than only checking ``result["ok"]``.
That distinction is load-bearing: a test that only checks ``ok is True``
cannot tell "the content round-tripped intact" from "the content was
silently corrupted but still happened to parse as valid JSON and hit a
verb that does not inspect it closely" -- e.g. decoding UTF-8 bytes as
cp1252 does not raise (cp1252 maps almost every byte), so a wrong decode
still produces syntactically valid JSON and a call that still returns
``ok: true``. Only comparing the round-tripped string against the exact
input catches that.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from content_pipeline.cli.run import build_commands
from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.store import ExecutionStore


@pytest.fixture
def store(tmp_path) -> ExecutionStore:
    return ExecutionStore(tmp_path / "run.db")


def _adapter(*, adapter_version="v1"):
    return RunAdapter(
        user_for=lambda u: f"user:{u.id}",
        parse_fn=lambda t: t,
        apply=lambda uid, payload: None,
        adapter_version=adapter_version,
    )


def _set_stdin(monkeypatch, text: str) -> None:
    """Install a fake stdin whose ``.buffer.read()`` yields ``text`` encoded
    as UTF-8 -- mirrors how a real console/pipe stdin is consumed by the
    handler (``sys.stdin.buffer.read()``, never the text-mode default)."""
    fake = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", fake)


def _seed_claimed_unit(store: ExecutionStore, run_id="r1", unit_id="u0", adapter_version="v1") -> None:
    """Set up a run with one claimed unit (fencing_token=1) so a 'fail'
    envelope can be dispatched and its ``error`` text read back afterwards."""
    store.create_run(run_id, driver="inline", backend="mock", model="m", adapter_version=adapter_version)
    store.register_units(run_id, [unit_id])
    store.claim_unit(run_id, unit_id, "worker-a")


def _last_attempt_error(store: ExecutionStore, run_id: str, unit_id: str) -> str:
    """Read back the ``error`` text of the most recent attempt row -- the
    durable record the 'fail' verb writes -- so a test can assert the exact
    string that made it through the envelope's transport, not merely that
    the call reported success."""
    attempts = store.list_attempts(run_id, unit_id)
    assert attempts, f"expected at least one attempt for {run_id}/{unit_id}, found none"
    return attempts[-1].error


def _fail_envelope(*, run_id="r1", unit_id="u0", fencing_token=1, error_text: str) -> dict:
    return {
        "protocol_version": "1",
        "verb": "fail",
        "payload": {
            "run_id": run_id,
            "unit_id": unit_id,
            "fencing_token": fencing_token,
            "error": error_text,
        },
    }


# -- Defect 1: stdin / @file / argv envelope sourcing --------------------------


def test_protocol_reads_envelope_from_stdin_with_quotes_and_pipe(store, monkeypatch):
    """The actual regression: a payload with escaped quotes AND a literal
    `|` (as produced by a YAML block scalar like `reasoning: |`) is exactly
    what corrupts cmd.exe's argv quote-tracking. Built here so it WOULD have
    broken the old argv-only path; stdin has no such re-parsing step.

    Reads the text back out of the store rather than only checking `ok`:
    a structural-JSON-only check would pass even if the string that reached
    the handler was truncated or re-escaped differently than what was sent,
    as long as it still happened to parse.
    """
    adapter = _adapter()
    commands = build_commands(store, adapter=adapter)
    _seed_claimed_unit(store)

    tricky_text = 'He said "hello" | pipe-looking text\nline two'
    _set_stdin(monkeypatch, json.dumps(_fail_envelope(error_text=tricky_text)))

    result = commands["protocol"].handler([])
    assert result["ok"] is True
    assert result["result"]["state"] == "pending"
    assert _last_attempt_error(store, "r1", "u0") == tricky_text


def test_protocol_stdin_preserves_non_ascii(store, monkeypatch):
    """zh-Hans (or any non-ASCII) content in the envelope must survive --
    stdin is decoded as UTF-8 explicitly, never the platform default (cp1252
    on Windows, which would corrupt this rather than raise: cp1252 maps
    almost every byte, so a wrong decode still parses as structurally valid
    JSON and the 'fail' verb still succeeds -- only comparing the
    round-tripped text against the exact input catches the corruption)."""
    adapter = _adapter()
    commands = build_commands(store, adapter=adapter)
    _seed_claimed_unit(store)

    zh_text = "你好世界"  # "hello world" in zh-Hans
    _set_stdin(monkeypatch, json.dumps(_fail_envelope(error_text=zh_text), ensure_ascii=False))

    result = commands["protocol"].handler([])
    assert result["ok"] is True
    assert result["result"]["state"] == "pending"
    assert _last_attempt_error(store, "r1", "u0") == zh_text


def test_protocol_reads_envelope_from_file(store, monkeypatch, tmp_path):
    """Same round-trip discipline as the stdin tests, and specifically with
    non-ASCII content: the `@<path>` read also specifies `encoding="utf-8"`
    explicitly, and that claim needs the same proof stdin's does -- a wrong
    decode of non-ASCII bytes would still often parse as valid JSON."""
    adapter = _adapter()
    commands = build_commands(store, adapter=adapter)
    _seed_claimed_unit(store)

    zh_text = "你好世界"
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text(
        json.dumps(_fail_envelope(error_text=zh_text), ensure_ascii=False), encoding="utf-8"
    )

    result = commands["protocol"].handler([f"@{envelope_path}"])
    assert result["ok"] is True
    assert result["result"]["state"] == "pending"
    assert _last_attempt_error(store, "r1", "u0") == zh_text


def test_protocol_argv_form_still_works(store, monkeypatch):
    """Back-compat: the original 0.9.0 positional-argv form must keep
    working, even though it is now discouraged. Content-checked the same
    way as the stdin/@file forms so a subtle corruption specific to this
    branch (e.g. accidentally routing it through the stdin/file decode path)
    would be caught."""
    adapter = _adapter()
    commands = build_commands(store, adapter=adapter)
    _seed_claimed_unit(store)

    marker_text = "argv-form-marker: distinct from stdin/@file paths"
    envelope_text = json.dumps(_fail_envelope(error_text=marker_text))
    result = commands["protocol"].handler([envelope_text])
    assert result["ok"] is True
    assert result["result"]["state"] == "pending"
    assert _last_attempt_error(store, "r1", "u0") == marker_text


def test_protocol_empty_stdin_gives_clear_error(store, monkeypatch):
    adapter = _adapter()
    commands = build_commands(store, adapter=adapter)
    _set_stdin(monkeypatch, "")

    result = commands["protocol"].handler([])
    assert result["ok"] is False
    assert result["error"]["type"] == "EmptyEnvelopeError"


def test_protocol_malformed_json_gives_clear_error(store, monkeypatch):
    adapter = _adapter()
    commands = build_commands(store, adapter=adapter)
    _set_stdin(monkeypatch, "{not valid json")

    result = commands["protocol"].handler([])
    assert result["ok"] is False
    assert result["error"]["type"] == "MalformedEnvelopeError"


def test_protocol_missing_file_gives_clear_error(store, monkeypatch, tmp_path):
    adapter = _adapter()
    commands = build_commands(store, adapter=adapter)

    missing = tmp_path / "does-not-exist.json"
    result = commands["protocol"].handler([f"@{missing}"])
    assert result["ok"] is False
    assert result["error"]["type"] == "MissingEnvelopeFileError"
    assert "does-not-exist.json" in result["error"]["message"]


def test_protocol_dash_triggers_stdin_explicitly(store, monkeypatch):
    """Confirms `-` is treated as "read stdin", not as the literal envelope
    text `-` (which would fail to parse as JSON and never reach the store).
    A content round-trip check is what makes that distinction observable:
    if `-` fell through to the literal-argv branch, `json.loads("-")` would
    raise and no attempt would ever be written, which `_last_attempt_error`
    would catch as a hard failure rather than silently passing."""
    adapter = _adapter()
    commands = build_commands(store, adapter=adapter)
    _seed_claimed_unit(store)

    marker_text = "dash-stdin-marker"
    _set_stdin(monkeypatch, json.dumps(_fail_envelope(error_text=marker_text)))

    result = commands["protocol"].handler(["-"])
    assert result["ok"] is True
    assert _last_attempt_error(store, "r1", "u0") == marker_text


# -- Defect 2: create-run adapter_version validation ----------------------------


def test_create_run_echoes_adapter_version(store):
    commands = build_commands(store)
    result = commands["create-run"].handler(["r1", "inline", "mock", "m", "v1"])
    assert result["adapter_version"] == "v1"
    assert store.get_run("r1").adapter_version == "v1"


def test_omitting_protocol_handlers_preserves_store_only_commands(store):
    """The additive keyword does not change the store-only command registry."""
    implicit = build_commands(store)
    explicit = build_commands(store, protocol_handlers=None)
    assert tuple(implicit) == tuple(explicit)
    assert {name: command.help for name, command in implicit.items()} == {
        name: command.help for name, command in explicit.items()
    }
    assert "protocol" not in implicit


def test_protocol_handlers_mapping_is_used_without_building_default_handlers(store, monkeypatch):
    seen = []

    def handler(payload):
        seen.append(dict(payload))
        return {"handled": True}

    commands = build_commands(store, protocol_handlers={"custom": handler})
    _set_stdin(monkeypatch, json.dumps({
        "protocol_version": "1",
        "verb": "custom",
        "payload": {"value": "ok"},
    }))

    result = commands["protocol"].handler([])
    assert result["ok"] is True
    assert result["result"] == {"handled": True}
    assert seen == [{"value": "ok"}]


def test_create_run_refuses_mismatched_adapter_version(store):
    adapter = _adapter(adapter_version="some.module/1")
    commands = build_commands(store, adapter=adapter)

    with pytest.raises(ValueError, match="some.module/1"):
        commands["create-run"].handler(["r1", "inline", "mock", "m", "1"])

    assert store.get_run("r1") is None


def test_create_run_defaults_adapter_version_from_adapter(store):
    adapter = _adapter(adapter_version="some.module/1")
    commands = build_commands(store, adapter=adapter)

    result = commands["create-run"].handler(["r1", "inline", "mock", "m"])
    assert result["adapter_version"] == "some.module/1"
    assert store.get_run("r1").adapter_version == "some.module/1"


def test_create_run_without_adapter_still_requires_adapter_version(store):
    commands = build_commands(store)
    with pytest.raises(ValueError, match="missing required argument"):
        commands["create-run"].handler(["r1", "inline", "mock", "m"])
    assert store.get_run("r1") is None


def test_create_run_with_matching_adapter_version_succeeds(store):
    adapter = _adapter(adapter_version="v1")
    commands = build_commands(store, adapter=adapter)
    result = commands["create-run"].handler(["r1", "inline", "mock", "m", "v1"])
    assert result["adapter_version"] == "v1"
    run = store.get_run("r1")
    assert run is not None
    assert run.adapter_version == "v1"


# -- item 5 (A-min.4): create-run snapshots and anchors the environment -------


def test_create_run_without_adapter_records_no_environment(store):
    commands = build_commands(store)
    result = commands["create-run"].handler(["r1", "inline", "mock", "m", "v1"])
    assert result["environment"] is None
    assert store.get_run("r1").environment is None


def test_create_run_with_adapter_declaring_nothing_records_an_empty_snapshot(store):
    adapter = _adapter(adapter_version="v1")  # default WorkerEnvironment(): nothing declared
    commands = build_commands(store, adapter=adapter)
    result = commands["create-run"].handler(["r1", "inline", "mock", "m", "v1"])
    assert result["environment"] == {}
    assert store.get_run("r1").environment == {}


def test_create_run_snapshots_a_required_var_matching_the_live_environment(store, monkeypatch):
    import os as os_module

    from content_pipeline.execution.adapter import WorkerEnvironment

    # APP_ROOT is not a path-looking anchor check target unless it also
    # matches os.getcwd() (require_creatable_environment); pin cwd to the
    # same value so this test exercises the SNAPSHOT round-trip, not the
    # create-time refusal (covered separately below).
    monkeypatch.setattr(os_module, "getcwd", lambda: "D:\\dev\\proj")
    monkeypatch.setenv("APP_ROOT", "D:\\dev\\proj")
    adapter = RunAdapter(
        user_for=lambda u: f"user:{u.id}",
        parse_fn=lambda t: t,
        apply=lambda uid, payload: None,
        adapter_version="v1",
        environment=WorkerEnvironment(required_vars=("APP_ROOT",)),
    )
    commands = build_commands(store, adapter=adapter)
    result = commands["create-run"].handler(["r1", "inline", "mock", "m", "v1"])
    assert result["environment"] == {"APP_ROOT": "D:\\dev\\proj"}
    assert store.get_run("r1").environment == {"APP_ROOT": "D:\\dev\\proj"}


def test_create_run_refuses_on_git_bash_pwd_flavour_mismatch(store, monkeypatch):
    """DECIDED point 3: under Git Bash, PWD snapshots as a POSIX-style path
    while os.getcwd() in the SAME process is native -- they never match as
    strings, so create-run refuses in the human's own shell before any
    worker ever runs, rather than let a worker resolve against the wrong
    root."""
    import os as os_module

    from content_pipeline.execution.adapter import (
        WorkerEnvironment,
        WorkerEnvironmentMismatchError,
    )

    monkeypatch.setattr(os_module, "getcwd", lambda: "D:\\dev\\example-project\\main")
    monkeypatch.setenv("PWD", "/d/dev/example-project/main")
    adapter = RunAdapter(
        user_for=lambda u: f"user:{u.id}",
        parse_fn=lambda t: t,
        apply=lambda uid, payload: None,
        adapter_version="v1",
        environment=WorkerEnvironment(cwd_vars=("PWD",)),
    )
    commands = build_commands(store, adapter=adapter)
    with pytest.raises(WorkerEnvironmentMismatchError) as exc_info:
        commands["create-run"].handler(["r1", "inline", "mock", "m", "v1"])
    assert exc_info.value.likely_path_flavour_mismatch is True
    # The refused run must never have been created.
    assert store.get_run("r1") is None


def test_create_run_a_content_root_var_that_differs_from_cwd_is_not_refused(store, monkeypatch):
    """Companion to the git-bash-refusal test above: a declared required
    var whose value legitimately differs from cwd (a real content root, not
    a cwd_var) must NOT be refused at create-run time -- only cwd_vars are
    ever compared against os.getcwd()."""
    import os as os_module

    from content_pipeline.execution.adapter import WorkerEnvironment

    fake_cwd = "D:\\dev\\example-project\\main"
    monkeypatch.setattr(os_module, "getcwd", lambda: fake_cwd)
    monkeypatch.setenv("CONTENT_ROOT", fake_cwd + "\\plugins")
    adapter = RunAdapter(
        user_for=lambda u: f"user:{u.id}",
        parse_fn=lambda t: t,
        apply=lambda uid, payload: None,
        adapter_version="v1",
        environment=WorkerEnvironment(required_vars=("CONTENT_ROOT",)),  # not a cwd_var
    )
    commands = build_commands(store, adapter=adapter)
    result = commands["create-run"].handler(["r1", "inline", "mock", "m", "v1"])
    assert result["environment"] == {"CONTENT_ROOT": fake_cwd + "\\plugins"}
