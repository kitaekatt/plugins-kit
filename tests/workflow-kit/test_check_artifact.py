"""Tests for workflow-kit's artifact checker (scripts/check_artifact.py).

The checker is loaded by path and run in-process through ``main``. Kind
``schema`` uses the REAL llm_scripting_kit (its lib/ is put on sys.path,
standing in for the bootstrap shared-lib .pth): ``OutputContract`` for the
subset check and the digest, ``completion.json_schema.validate`` for the
judgment. Kind ``opaque-file`` needs no llm_scripting_kit at all.
"""

import builtins
import hashlib
import importlib.util
import json
import os
import sys
import threading
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "plugins" / "workflow-kit" / "scripts" / "check_artifact.py"
_LSK_LIB = _REPO / "plugins" / "llm-scripting-kit" / "lib"


def _load():
    spec = importlib.util.spec_from_file_location("workflow_kit_check_artifact_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ca = _load()

SCHEMA = {
    "type": "object",
    "required": ["lines", "words"],
    "additionalProperties": False,
    "properties": {
        "lines": {"type": "integer", "minimum": 0},
        "words": {"type": "integer", "minimum": 0},
    },
}


@pytest.fixture
def lsk(monkeypatch):
    monkeypatch.syspath_prepend(str(_LSK_LIB))


def _digest(schema):
    sys.path.insert(0, str(_LSK_LIB))
    try:
        from llm_scripting_kit.completion import OutputContract
    finally:
        sys.path.remove(str(_LSK_LIB))
    return OutputContract(id="x", policy="validated-result", schema=schema).schema_digest


def _paths(tmp_path):
    return tmp_path / "count.out", tmp_path / ".workflow-kit" / "r1" / "count.contract.json"


def _argv(out, verdict, *, kind="schema", schema=SCHEMA, digest=None, command_exit=0,
          artifact="doc_stats"):
    argv = ["--artifact", artifact, "--kind", kind, "--in", str(out),
            "--verdict", str(verdict), "--command-exit", str(command_exit)]
    if kind == "schema":
        argv += ["--schema", json.dumps(schema),
                 "--schema-digest", digest if digest is not None else _digest(schema)]
    return argv


def _check(tmp_path, payload, **kw):
    """Write ``payload`` (bytes or str; None = no file) to $OUT and run the checker."""
    out, verdict = _paths(tmp_path)
    if payload is not None:
        out.write_bytes(payload if isinstance(payload, bytes) else payload.encode("utf-8"))
    rc = ca.main(_argv(out, verdict, **kw))
    record = json.loads(verdict.read_text(encoding="ascii")) if verdict.exists() else None
    return rc, record


def _seed(verdict):
    verdict.parent.mkdir(parents=True, exist_ok=True)
    verdict.write_text(json.dumps({"schema": ca.VERDICT_SCHEMA, "verdict": "satisfied"}),
                       encoding="ascii")


def _block_lsk(monkeypatch):
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)


# --------------------------------------------------------------------------- #
# judgments
# --------------------------------------------------------------------------- #
def test_checker_schema_satisfied_writes_the_verdict(lsk, tmp_path, capsys):
    payload = '{"lines": 3, "words": 5}\n'
    rc, record = _check(tmp_path, payload)
    assert rc == 0
    assert record == {
        "schema": "workflow-kit.artifact-verdict/v1",
        "artifact": "doc_stats",
        "kind": "schema",
        "verdict": "satisfied",
        "path": str(_paths(tmp_path)[0]),
        "bytes": len(payload.encode("utf-8")),
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "schema_digest": _digest(SCHEMA),
        "errors": [],
        "errors_truncated": False,
    }
    assert capsys.readouterr().err == ""


def test_checker_schema_violation_exits_1_with_errors(lsk, tmp_path):
    rc, record = _check(tmp_path, '{"lines": "three", "words": -1, "extra": 1}')
    assert rc == 1
    assert record["verdict"] == "violated"
    assert record["errors"] == [["/extra", "additionalProperties"], ["/lines", "type"],
                                ["/words", "minimum"]]
    assert record["errors_truncated"] is False


def test_checker_unparseable_payload_is_violated(lsk, tmp_path):
    rc, record = _check(tmp_path, '{"lines": 3, "words": ')
    assert rc == 1
    assert record["verdict"] == "violated"
    assert record["errors"] == [["", "unparseable"]]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_checker_nan_payload_is_violated(lsk, tmp_path, constant):
    schema = {"type": "object", "properties": {"x": {"type": "number"}}}
    rc, record = _check(tmp_path, '{"x": %s}' % constant, schema=schema)
    assert rc == 1
    assert record["verdict"] == "violated"
    assert record["errors"] == [["", "unparseable"]]


def test_checker_non_utf8_payload_is_violated(lsk, tmp_path):
    # With a lenient decode this payload would become {"name": "U+FFFD"} and
    # satisfy the schema; strict UTF-8 makes it unparseable.
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    rc, record = _check(tmp_path, b'{"name": "\xff"}', schema=schema)
    assert rc == 1
    assert record["verdict"] == "violated"
    assert record["errors"] == [["", "unparseable"]]


def test_checker_opaque_satisfied(tmp_path, capsys):
    rc, record = _check(tmp_path, "any bytes at all", kind="opaque-file")
    assert rc == 0
    assert record["verdict"] == "satisfied"
    assert record["kind"] == "opaque-file"
    assert "schema_digest" not in record
    assert record["bytes"] == len("any bytes at all")
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("kind", ["opaque-file", "schema"])
def test_checker_opaque_missing_file_is_missing(lsk, tmp_path, capsys, kind):
    rc, record = _check(tmp_path, None, kind=kind)
    assert rc == 1
    assert record["verdict"] == "missing"
    assert record["bytes"] is None and record["sha256"] is None
    # an absent artifact is missing, not a read error
    assert "cannot read" not in capsys.readouterr().err


@pytest.mark.parametrize("which", ["directory", "device"])
def test_checker_directory_is_missing(tmp_path, capsys, which):
    out, verdict = _paths(tmp_path)
    target = tmp_path / "adir" if which == "directory" else Path(os.devnull)
    if which == "directory":
        target.mkdir()
    rc = ca.main(_argv(target, verdict, kind="opaque-file"))
    assert rc == 1
    record = json.loads(verdict.read_text(encoding="ascii"))
    assert record["verdict"] == "missing"
    assert record["bytes"] is None
    assert "cannot read" not in capsys.readouterr().err


def test_checker_command_failure_records_missing_and_keeps_exit_code(lsk, tmp_path):
    rc, record = _check(tmp_path, '{"lines": 3, "words": 5}', command_exit=7)
    assert rc == 7
    assert record["verdict"] == "missing"
    assert record["bytes"] is None and record["errors"] == []


@pytest.mark.parametrize("code,expected", [(300, 255), (-4, 1)])
def test_checker_command_exit_is_clamped(tmp_path, code, expected):
    rc, record = _check(tmp_path, "x", kind="opaque-file", command_exit=code)
    assert rc == expected
    assert record["verdict"] == "missing"


def test_checker_rerun_after_failure_replaces_satisfied_verdict(lsk, tmp_path):
    rc, record = _check(tmp_path, '{"lines": 3, "words": 5}')
    assert (rc, record["verdict"]) == (0, "satisfied")
    rc, record = _check(tmp_path, '{"lines": 3, "words": 5}', command_exit=3)
    assert rc == 3
    assert record is not None and record["verdict"] == "missing"


def test_checker_read_oserror_records_missing(lsk, tmp_path, monkeypatch, capsys):
    out, _verdict = _paths(tmp_path)
    real = Path.read_bytes

    def denied(self):
        if self == out:
            raise PermissionError(13, "denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", denied)
    rc, record = _check(tmp_path, '{"lines": 3, "words": 5}')
    assert rc == 1
    assert record["verdict"] == "missing"
    assert record["bytes"] is None
    assert "cannot read" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# the verdict file's content
# --------------------------------------------------------------------------- #
def test_checker_errors_capped_at_100(lsk, tmp_path):
    schema = {"type": "array", "items": {"type": "integer"}}
    rc, record = _check(tmp_path, json.dumps(["x"] * 150), schema=schema)
    assert rc == 1
    assert len(record["errors"]) == 100
    assert record["errors_truncated"] is True


def test_checker_verdict_carries_no_payload_values(lsk, tmp_path):
    sentinel = "SENTINEL-payload-value-4f1c"
    rc, _record = _check(tmp_path, json.dumps({"lines": sentinel, "words": 1}))
    assert rc == 1
    text = _paths(tmp_path)[1].read_text(encoding="ascii")
    assert sentinel not in text
    assert json.loads(text)["errors"] == [["/lines", "type"]]


def test_checker_verdict_hash_matches_judged_file(lsk, tmp_path):
    payload = b'{"lines": 1,\r\n "words": 2}\r\n'
    rc, record = _check(tmp_path, payload)
    assert rc == 0
    on_disk = _paths(tmp_path)[0].read_bytes()
    assert record["bytes"] == len(on_disk) == len(payload)
    assert record["sha256"] == hashlib.sha256(on_disk).hexdigest()


# --------------------------------------------------------------------------- #
# usage and refusals (exit 2)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("drop", ["--schema", "--schema-digest"])
def test_checker_schema_kind_requires_schema_and_digest(tmp_path, capsys, drop):
    out, verdict = _paths(tmp_path)
    argv = _argv(out, verdict, digest="0" * 64)
    i = argv.index(drop)
    del argv[i:i + 2]
    with pytest.raises(SystemExit) as caught:
        ca.main(argv)
    assert caught.value.code == 2
    assert "--kind schema requires --schema and --schema-digest" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--schema", "--schema-digest"])
def test_checker_opaque_kind_refuses_schema_flags(tmp_path, capsys, flag):
    out, verdict = _paths(tmp_path)
    argv = _argv(out, verdict, kind="opaque-file") + [flag, "{}"]
    with pytest.raises(SystemExit) as caught:
        ca.main(argv)
    assert caught.value.code == 2
    assert "--kind opaque-file takes no --schema or --schema-digest" in capsys.readouterr().err


def test_checker_usage_error_leaves_files_untouched(tmp_path, capsys):
    # Documented narrowing: argparse errors precede invalidation, so a usage
    # error leaves a previous verdict in place (compiled commands always parse).
    out, verdict = _paths(tmp_path)
    _seed(verdict)
    before = verdict.read_bytes()
    argv = _argv(out, verdict, kind="opaque-file") + ["--schema", "{}"]
    with pytest.raises(SystemExit):
        ca.main(argv)
    assert verdict.read_bytes() == before


def test_checker_schema_digest_mismatch_exits_2_writes_nothing(lsk, tmp_path, capsys):
    rc, record = _check(tmp_path, '{"lines": 3, "words": 5}', digest="0" * 64)
    assert rc == 2
    assert record is None
    assert not _paths(tmp_path)[1].parent.exists()
    assert "changed in transit" in capsys.readouterr().err


def test_checker_schema_outside_subset_exits_2_writes_nothing(lsk, tmp_path, capsys):
    schema = {"type": "object", "properties": {"name": {"type": "string", "pattern": "^a"}}}
    rc, record = _check(tmp_path, '{"name": "b"}', schema=schema, digest="0" * 64)
    assert rc == 2
    assert record is None
    assert not _paths(tmp_path)[1].parent.exists()
    assert "outside what llm-scripting-kit's validated-result contract accepts" in (
        capsys.readouterr().err
    )


def test_checker_lsk_absent_exits_2_writes_nothing(tmp_path, capsys, monkeypatch):
    digest = _digest(SCHEMA)
    _block_lsk(monkeypatch)
    rc, record = _check(tmp_path, '{"lines": 3, "words": 5}', digest=digest)
    assert rc == 2
    assert record is None
    err = capsys.readouterr().err
    assert "llm_scripting_kit not importable" in err
    assert "claude plugin update" not in err


def test_checker_lsk_too_old_exits_2_distinct_message(tmp_path, capsys, monkeypatch):
    digest = _digest(SCHEMA)
    pkg = types.ModuleType("llm_scripting_kit")
    pkg.__path__ = []
    completion = types.ModuleType("llm_scripting_kit.completion")
    completion.BackendOptions = object  # OutputContract deliberately absent
    pkg.completion = completion
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", pkg)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", completion)
    rc, record = _check(tmp_path, '{"lines": 3, "words": 5}', digest=digest)
    assert rc == 2
    assert record is None
    err = capsys.readouterr().err
    assert "llm-scripting-kit >= 0.56.0" in err
    assert "claude plugin update llm-scripting-kit@plugins-kit" in err
    assert "not importable" not in err


def test_checker_refused_probe_creates_no_directory(tmp_path, monkeypatch):
    digest = _digest(SCHEMA)
    _block_lsk(monkeypatch)
    out, verdict = _paths(tmp_path)
    out.write_text('{"lines": 3, "words": 5}', encoding="utf-8")
    assert ca.main(_argv(out, verdict, digest=digest)) == 2
    assert not verdict.parent.exists()
    assert not verdict.parent.parent.exists()


# --------------------------------------------------------------------------- #
# invalidation before the probes
# --------------------------------------------------------------------------- #
def test_checker_probe_failure_removes_seeded_verdict(tmp_path, monkeypatch):
    digest = _digest(SCHEMA)
    out, verdict = _paths(tmp_path)
    _seed(verdict)
    _block_lsk(monkeypatch)
    assert ca.main(_argv(out, verdict, digest=digest)) == 2
    assert not verdict.exists()


def test_checker_digest_mismatch_removes_seeded_verdict(lsk, tmp_path):
    out, verdict = _paths(tmp_path)
    _seed(verdict)
    assert ca.main(_argv(out, verdict, digest="0" * 64)) == 2
    assert not verdict.exists()


def test_checker_invalidation_creates_no_directory(lsk, tmp_path):
    out, verdict = _paths(tmp_path)
    assert ca.main(_argv(out, verdict, digest="f" * 64)) == 2
    assert not verdict.parent.exists()
    assert not verdict.parent.parent.exists()


def test_checker_invalidation_of_absent_verdict_succeeds(tmp_path):
    rc, record = _check(tmp_path, "payload", kind="opaque-file")
    assert rc == 0
    assert record["verdict"] == "satisfied"


def test_checker_undeletable_verdict_exits_2_before_probes(tmp_path, capsys, monkeypatch):
    out, verdict = _paths(tmp_path)
    out.write_text('{"lines": 3, "words": 5}', encoding="utf-8")
    verdict.mkdir(parents=True)  # a directory where the verdict file goes
    imported = []
    real_import = builtins.__import__

    def spy(name, *args, **kwargs):
        if name.startswith("llm_scripting_kit"):
            imported.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", spy)
    rc = ca.main(_argv(out, verdict, digest="0" * 64))
    assert rc == 2
    assert imported == []
    assert verdict.is_dir() and list(verdict.iterdir()) == []
    assert sorted(p.name for p in verdict.parent.iterdir()) == [verdict.name]
    assert "cannot invalidate the previous verdict at" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# the atomic write
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("failure", ["serializer", "replace"])
def test_checker_verdict_write_leaves_no_partial_file(lsk, tmp_path, monkeypatch, capsys, failure):
    out, verdict = _paths(tmp_path)
    _seed(verdict)  # invalidation removes it first
    if failure == "serializer":
        def partial(record, fh):
            fh.write('{"schema": "workflow-kit.artifact-')
            raise OSError("disk full")

        monkeypatch.setattr(ca, "_serialize", partial)
    else:
        def refuse(src, dst):
            raise OSError("replace refused")

        monkeypatch.setattr(ca.os, "replace", refuse)
    rc, record = _check(tmp_path, '{"lines": 3, "words": 5}')
    assert rc == 1  # no body error: the first cleanup error surfaces as exit 1
    assert record is None
    assert not verdict.exists()
    assert list(verdict.parent.iterdir()) == []  # no temp sibling either
    assert "the verdict cleanup layer failed" in capsys.readouterr().err


def test_verdict_temp_name_is_unique_per_process(tmp_path, monkeypatch):
    final = tmp_path / "v" / "n.contract.json"
    temps = []
    lock = threading.Lock()
    real_replace = os.replace

    def recording(src, dst):
        with lock:
            temps.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(ca.os, "replace", recording)
    errors = []

    def writer(n):
        try:
            for i in range(20):
                ca.write_verdict(final, ca.verdict_record(
                    artifact=f"a{n}", kind="opaque-file", verdict="satisfied",
                    path="p", data=b"x" * i,
                ))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(temps) == len(set(temps))
    assert all(f".{os.getpid()}." in Path(t).name for t in temps)
    assert json.loads(final.read_text(encoding="ascii"))["verdict"] == "satisfied"
    assert sorted(p.name for p in final.parent.iterdir()) == [final.name]
    # PermissionError on Windows when two writers race os.replace is a
    # platform effect of replacing an open target, not a shared temp path.
    assert all(isinstance(e, PermissionError) for e in errors)
