"""presence-kit session state (scripts/presence_state.py): contracts.md sections 1-3.

The module is loaded by file path. Config is never imported: state_set takes the
resolved `behaviors` mapping, so these tests inject a fake config object that
matches the contracts.md section 1 interface (`.behaviors`, `.enabled(id)`).
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "presence-kit" / "scripts" / "presence_state.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("presence_state_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ps = _load_module()

BEHAVIOR_IDS = (
    "afk-on-departure", "afk-on-quiet", "autonomous-on-handover", "recap-on-return",
    "waiting-guard", "ending-recap", "terse-while-away", "park-questions",
    "notify-for-input",
)
SID = "sess-1"
T0 = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=30)
T2 = T0 + timedelta(hours=2)
S0, S1, S2 = (ps.format_timestamp(t) for t in (T0, T1, T2))


class FakeConfig:
    """The contracts.md section 1 Config surface the state layer needs."""

    def __init__(self, **overrides):
        self.behaviors = {i: True for i in BEHAVIOR_IDS}
        for key, value in overrides.items():
            self.behaviors[key.replace("_", "-")] = value

    def enabled(self, behavior_id):
        return self.behaviors[behavior_id]


@pytest.fixture(params=["data-root-env", "home-default"])
def base(request, tmp_path, monkeypatch):
    """Both bases: (env mapping, expected `<base>` directory)."""
    if request.param == "data-root-env":
        root = tmp_path / "data-root"
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(root))
        return {"CLAUDE_BOOTSTRAP_DATA_ROOT": str(root)}, root
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    return {}, home / ".claude" / "plugins" / "data"


@pytest.fixture
def env(base):
    return base[0]


def sessions_dir(base):
    return base[1] / "plugins-kit" / "presence-kit" / "sessions"


def write_doc(base, sid=SID, **fields):
    doc = ps.default_state()
    doc.update(fields)
    path = sessions_dir(base) / f"{sid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def count_writes(monkeypatch):
    calls = []
    real = ps._write_atomic

    def spy(path, doc):
        calls.append(path)
        real(path, doc)

    monkeypatch.setattr(ps, "_write_atomic", spy)
    return calls


# ------------------------------------------------------------------ paths and ids

def test_state_path_under_both_bases(base, env):
    expected = sessions_dir(base) / f"{SID}.json"
    assert ps.state_path(SID, env) == expected
    assert ps.data_root(env) == base[1]


def test_empty_data_root_override_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    got = ps.state_path(SID, {"CLAUDE_BOOTSTRAP_DATA_ROOT": ""})
    assert got == (
        tmp_path / ".claude" / "plugins" / "data" / "plugins-kit" / "presence-kit"
        / "sessions" / f"{SID}.json"
    )


@pytest.mark.parametrize("sid", ["../x", "a/b", "a\\b", "a b", "a.b", "x" * 129, "a\n"])
def test_bad_session_id_refused(sid, env):
    with pytest.raises(ps.StateError) as err:
        ps.state_path(sid, env)
    assert err.value.kind == "bad-session-id"
    assert err.value.text == f"session id '{sid}' must match [A-Za-z0-9_-]{{1,128}}"


@pytest.mark.parametrize("sid", [None, ""])
def test_missing_session_id(sid, env):
    with pytest.raises(ps.StateError) as err:
        ps.state_path(sid, env)
    assert err.value.kind == "no-session"
    assert err.value.text == "no session id: pass --session or set CLAUDE_CODE_SESSION_ID"


def test_max_length_session_id_accepted(env):
    assert ps.state_path("a" * 128, env).name == "a" * 128 + ".json"


@pytest.mark.parametrize("pid", ["", "a b", "a\tb", "x" * 129])
def test_bad_prompt_id_refused(pid):
    doc = ps.default_state()
    for call in (lambda: ps.mark_recap(doc, "return", pid), lambda: ps.bump_nudge(doc, pid)):
        with pytest.raises(ps.StateError) as err:
            call()
        assert err.value.kind == "bad-prompt-id"
        assert err.value.text == (
            f"prompt id '{pid}' must be 1-128 characters without whitespace"
        )
    assert doc == ps.default_state()


# --------------------------------------------------------------------- timestamps

def test_format_timestamp_is_utc_seconds():
    assert S0 == "2026-10-17T09:00:00Z"
    plus2 = timezone(timedelta(hours=2))
    assert ps.format_timestamp(datetime(2026, 10, 17, 11, 0, 5, 999999, tzinfo=plus2)) == (
        "2026-10-17T09:00:05Z"
    )


def test_format_timestamp_refuses_naive():
    with pytest.raises(ValueError):
        ps.format_timestamp(datetime(2026, 10, 17, 9, 0, 0))


@pytest.mark.parametrize("text", [
    "2026-10-17T09:00:00Z",
    "2026-10-17T09:00:00.123Z",
    "2026-10-17T09:00:00.123456789Z",
    "2026-10-17T11:00:00+02:00",
])
def test_parse_timestamp_aware(text):
    parsed = ps.parse_timestamp(text)
    assert parsed is not None and parsed.utcoffset() == timedelta(0)
    assert parsed.replace(microsecond=0) == T0


@pytest.mark.parametrize("value", [
    "2026-10-17T09:00:00", "2026-10-17", "", "yesterday", "2026-13-01T09:00:00Z",
    "2026-10-17 09:00:00Z", None, 5, ["2026-10-17T09:00:00Z"],
])
def test_parse_timestamp_none_for_naive_or_unparsable(value):
    assert ps.parse_timestamp(value) is None


def test_parse_inverts_format():
    assert ps.parse_timestamp(ps.format_timestamp(T1)) == T1


def test_now_utc_is_aware_utc():
    now = ps.now_utc()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


# ----------------------------------------------------------------- default / load

def test_default_state_shape_and_key_order():
    doc = ps.default_state()
    assert list(doc) == [
        "version", "presence", "afk_since", "afk_prompt", "goal", "goal_since", "recaps",
        "nudges", "greeted", "unresolved", "pending_return", "notices", "cron_prompts",
    ]
    assert doc == {
        "version": 3, "presence": "present", "afk_since": None, "afk_prompt": None,
        "goal": None, "goal_since": None, "recaps": {}, "nudges": {}, "greeted": False,
        "unresolved": [], "pending_return": None, "notices": [], "cron_prompts": [],
    }
    assert ps.SCHEMA_VERSION == 3 and ps.KEEP_PROMPTS == 20
    assert ps.default_state() is not ps.default_state()


def test_load_absent_gives_defaults_and_creates_nothing(base, env):
    assert ps.load(SID, env) == ps.default_state()
    assert ps.state_get(SID, env) == {"exists": False, "state": ps.default_state()}
    assert not base[1].exists()


def test_load_validates_bad_session(env):
    with pytest.raises(ps.StateError) as err:
        ps.load("../x", env)
    assert err.value.kind == "bad-session-id"


# ------------------------------------------------------------ update: RMW + rename

def test_update_creates_directories_and_writes_ascii_json(base, env):
    def fn(doc):
        doc["goal"] = "caf\u00e9 \u2713"

    new = ps.update(SID, fn, env)
    path = ps.state_path(SID, env)
    raw = path.read_bytes()
    raw.decode("ascii")
    assert raw.endswith(b"\n")
    on_disk = json.loads(raw)
    assert on_disk == new and on_disk["goal"] == "caf\u00e9 \u2713"
    assert list(on_disk) == list(ps.default_state())
    assert sorted(p.name for p in path.parent.iterdir()) == [f"{SID}.json", f"{SID}.lock"]
    assert ps.load(SID, env) == new
    assert ps.state_get(SID, env) == {"exists": True, "state": new}


def test_update_returns_the_new_document_and_sees_the_previous_one(env):
    ps.update(SID, lambda d: d.update(greeted=True), env)
    seen = []
    new = ps.update(SID, lambda d: seen.append(dict(d)), env)
    assert seen[0]["greeted"] is True and new["greeted"] is True


def test_update_writes_only_when_changed(env, monkeypatch):
    writes = count_writes(monkeypatch)
    ps.update(SID, lambda d: None, env)
    assert writes == [] and not ps.state_path(SID, env).exists()
    ps.update(SID, lambda d: d.update(greeted=True), env)
    assert len(writes) == 1
    ps.update(SID, lambda d: d.update(greeted=True), env)  # same value: unchanged
    ps.update(SID, lambda d: None, env)
    assert len(writes) == 1


def test_write_lands_by_atomic_rename_from_a_pid_named_temp(env, monkeypatch):
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append((Path(src).name, Path(dst).name, Path(src).read_text(encoding="utf-8")))
        real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    new = ps.update(SID, lambda d: d.update(greeted=True), env)
    assert len(seen) == 1
    src, dst, content = seen[0]
    assert src == f"{SID}.json.tmp{os.getpid()}" and dst == f"{SID}.json"
    assert json.loads(content) == new  # the temp was complete before the rename
    assert not any(".tmp" in p.name for p in ps.state_path(SID, env).parent.iterdir())


def test_failed_rename_keeps_the_old_file_and_removes_the_temp(env, monkeypatch):
    ps.update(SID, lambda d: d.update(greeted=True), env)
    path = ps.state_path(SID, env)
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk says no")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk says no"):
        ps.update(SID, lambda d: d.update(greeted=False), env)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == [f"{SID}.json", f"{SID}.lock"]


def test_exception_in_fn_writes_nothing_and_releases_the_lock(env):
    ps.update(SID, lambda d: d.update(greeted=True), env)
    path = ps.state_path(SID, env)
    before = path.read_bytes()

    def bad(doc):
        doc["greeted"] = False
        raise RuntimeError("fn failed")

    with pytest.raises(RuntimeError, match="fn failed"):
        ps.update(SID, bad, env)
    assert path.read_bytes() == before
    assert ps.update(SID, lambda d: d.update(notices=["x"]), env)["notices"] == ["x"]


def test_update_refuses_to_write_an_invalid_document(env):
    ps.update(SID, lambda d: d.update(greeted=True), env)
    path = ps.state_path(SID, env)
    before = path.read_bytes()
    with pytest.raises(ps.StateError) as err:
        ps.update(SID, lambda d: d.update(presence="asleep"), env)
    assert err.value.kind == "malformed"
    assert path.read_bytes() == before


def test_sessions_are_independent_files(env):
    ps.update("a", lambda d: d.update(greeted=True), env)
    assert ps.load("b", env)["greeted"] is False
    assert ps.state_path("a", env) != ps.state_path("b", env)


# ------------------------------------------------------------------------ malformed

def _valid():
    return ps.default_state()


def _mut(**fields):
    doc = _valid()
    doc.update(fields)
    return json.dumps(doc)


def _without(key):
    doc = _valid()
    del doc[key]
    return json.dumps(doc)


BAD_FILES = {
    "empty": "",
    "not-json": "{nope",
    "root-list": "[]",
    "root-null": "null",
    "missing-key": _without("notices"),
    "extra-key": _mut(color="red"),
    "version-4": _mut(version=4),
    "version-2-with-the-v3-keys": _mut(version=2),
    "v3-missing-cron-prompts": _without("cron_prompts"),
    "cron-prompts-string": _mut(cron_prompts="a"),
    "cron-prompts-number-entry": _mut(cron_prompts=[5]),
    "cron-prompts-empty-entry": _mut(cron_prompts=[""]),
    "cron-prompts-unstripped-entry": _mut(cron_prompts=[" a "]),
    "version-0": _mut(version=0),
    "version-1-with-the-v2-keys": _mut(version=1),
    "v2-missing-afk-prompt": _without("afk_prompt"),
    "afk-prompt-number": _mut(afk_prompt=5),
    "afk-prompt-empty": _mut(afk_prompt=""),
    "afk-prompt-whitespace": _mut(afk_prompt="a b"),
    "version-bool": _mut(version=True),
    "version-string": _mut(version="1"),
    "presence-bad": _mut(presence="asleep"),
    "afk-since-bad": _mut(afk_since="soon"),
    "afk-since-naive": _mut(afk_since="2026-10-17T09:00:00"),
    "afk-since-number": _mut(afk_since=5),
    "goal-number": _mut(goal=5),
    "goal-since-bad": _mut(goal_since="x"),
    "recaps-list": _mut(recaps=[]),
    "recaps-bad-trigger": _mut(recaps={"p1": "other"}),
    "nudges-string": _mut(nudges={"p1": "2"}),
    "nudges-bool": _mut(nudges={"p1": True}),
    "nudges-negative": _mut(nudges={"p1": -1}),
    "greeted-string": _mut(greeted="no"),
    "unresolved-str": _mut(unresolved="x"),
    "unresolved-nonstr": _mut(unresolved=[1]),
    "pending-number": _mut(pending_return=3),
    "notices-nonstr": _mut(notices=[None]),
}


@pytest.mark.parametrize("name", sorted(BAD_FILES))
def test_malformed_state_file_is_a_loud_error(name, base, env):
    path = sessions_dir(base) / f"{SID}.json"
    path.parent.mkdir(parents=True)
    path.write_text(BAD_FILES[name], encoding="utf-8")
    for call in (
        lambda: ps.load(SID, env),
        lambda: ps.state_get(SID, env),
        lambda: ps.update(SID, lambda d: d.update(greeted=True), env),
    ):
        with pytest.raises(ps.StateError) as err:
            call()
        assert err.value.kind == "malformed"
        assert err.value.text.startswith(f"{path}: malformed state file -- ")
        assert str(err.value) == err.value.text
    assert path.read_text(encoding="utf-8") == BAD_FILES[name]  # nothing repaired


def test_invalid_utf8_is_malformed(base, env):
    path = sessions_dir(base) / f"{SID}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ps.StateError) as err:
        ps.load(SID, env)
    assert err.value.kind == "malformed"


def test_valid_document_with_timestamps_and_markers_loads(base, env):
    write_doc(
        base, presence="afk", afk_since=S0, goal="g", goal_since=S1,
        recaps={"p1": "return"}, nudges={"p1": 2}, greeted=True,
        unresolved=["w"], pending_return="p1", notices=["entry-missing"],
    )
    doc = ps.load(SID, env)
    assert doc["presence"] == "afk" and doc["nudges"] == {"p1": 2}


# --------------------------------------- v1 / v2 -> v3 upgrade (0.2.x and 0.3.0-0.3.1 files)

V1_DOC = {
    "version": 1, "presence": "afk", "afk_since": S0, "goal": "ship", "goal_since": S1,
    "recaps": {"p1": "return"}, "nudges": {"p1": 2}, "greeted": True,
    "unresolved": ["w: x (prompt p1)"], "pending_return": "p1", "notices": ["entry-missing"],
}


def write_raw(base, text, sid=SID):
    path = sessions_dir(base) / f"{sid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_v1_file_is_upgraded_in_memory_and_written_as_v3_on_disk(base, env, monkeypatch):
    path = write_raw(base, json.dumps(V1_DOC))
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append((Path(src).name, Path(dst).name, json.loads(Path(src).read_text(encoding="utf-8"))))
        real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    doc = ps.load(SID, env)
    expected = dict(V1_DOC, version=3, afk_prompt=None, cron_prompts=[])
    assert doc == expected
    assert list(doc) == list(ps.default_state())
    # the v3 file landed by atomic rename of a complete temp, not by an in-place edit
    assert [(src, dst) for src, dst, _ in seen] == [(f"{SID}.json.tmp{os.getpid()}", f"{SID}.json")]
    assert seen[0][2] == expected
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == expected and list(on_disk) == list(ps.default_state())
    assert sorted(p.name for p in path.parent.iterdir()) == [f"{SID}.json", f"{SID}.lock"]


def test_v1_upgrade_happens_once_and_every_reader_sees_v3(base, env, monkeypatch):
    path = write_raw(base, json.dumps(V1_DOC))
    assert ps.state_get(SID, env)["state"]["version"] == 3
    after_first = path.read_bytes()
    writes = count_writes(monkeypatch)
    assert ps.load(SID, env)["afk_prompt"] is None
    assert ps.update(SID, lambda d: None, env)["version"] == 3
    assert writes == [] and path.read_bytes() == after_first


def test_v1_file_through_update_applies_the_change_on_top_of_the_upgrade(base, env):
    path = write_raw(base, json.dumps(V1_DOC))
    seen = []
    new = ps.update(SID, lambda d: (seen.append(d["version"]), d.update(greeted=False))[0], env)
    assert seen == [3] and new["greeted"] is False and new["afk_prompt"] is None
    assert json.loads(path.read_text(encoding="utf-8")) == new


V1_BAD = {
    "v1-bad-presence": dict(V1_DOC, presence="asleep"),
    "v1-bad-afk-since": dict(V1_DOC, afk_since="soon"),
    "v1-bad-nudges": dict(V1_DOC, nudges={"p1": "2"}),
    "v1-bool-version": dict(V1_DOC, version=True),
    "v1-version-0": dict(V1_DOC, version=0),
    "v1-version-3": dict(V1_DOC, version=3),
    "v1-extra-key": dict(V1_DOC, color="red"),
    "v1-missing-key": {k: v for k, v in V1_DOC.items() if k != "notices"},
    "v2-keys-with-v1-version": dict(V1_DOC, afk_prompt=None),
    "v2-missing-afk-prompt": dict(V1_DOC, version=2),
    "v2-extra-key": dict(V1_DOC, version=2, afk_prompt=None, color="red"),
    "v3-keys-with-v1-version": dict(V1_DOC, afk_prompt=None, cron_prompts=[]),
}


@pytest.mark.parametrize("name", sorted(V1_BAD))
def test_only_an_exact_valid_v1_file_is_upgraded(name, base, env):
    """Everything else stays malformed and untouched on disk (no rewrite)."""
    text = json.dumps(V1_BAD[name])
    path = write_raw(base, text)
    for call in (
        lambda: ps.load(SID, env),
        lambda: ps.state_get(SID, env),
        lambda: ps.update(SID, lambda d: d.update(greeted=False), env),
    ):
        with pytest.raises(ps.StateError) as err:
            call()
        assert err.value.kind == "malformed"
    assert path.read_text(encoding="utf-8") == text
    assert sorted(p.name for p in path.parent.iterdir()) == [f"{SID}.json", f"{SID}.lock"]


def test_a_v3_file_is_read_as_is_and_not_rewritten(base, env, monkeypatch):
    path = write_raw(base, json.dumps(dict(V2_DOC, version=3, cron_prompts=["tick"])))
    before = path.read_bytes()
    writes = count_writes(monkeypatch)
    doc = ps.load(SID, env)
    assert doc["afk_prompt"] == "p9" and doc["cron_prompts"] == ["tick"]
    assert writes == [] and path.read_bytes() == before


V2_DOC = dict(V1_DOC, version=2, afk_prompt="p9")


def test_v2_file_is_upgraded_to_v3_with_empty_cron_prompts(base, env):
    path = write_raw(base, json.dumps(V2_DOC))
    doc = ps.load(SID, env)
    expected = dict(V2_DOC, version=3, cron_prompts=[])
    assert doc == expected and list(doc) == list(ps.default_state())
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert sorted(p.name for p in path.parent.iterdir()) == [f"{SID}.json", f"{SID}.lock"]


def test_v2_upgrade_happens_once(base, env, monkeypatch):
    path = write_raw(base, json.dumps(V2_DOC))
    ps.load(SID, env)
    after_first = path.read_bytes()
    writes = count_writes(monkeypatch)
    assert ps.update(SID, lambda d: None, env)["version"] == 3
    assert writes == [] and path.read_bytes() == after_first


V2_BAD = {
    "v2-bad-presence": dict(V2_DOC, presence="asleep"),
    "v2-bad-afk-prompt": dict(V2_DOC, afk_prompt="a b"),
    "v2-bool-version": dict(V2_DOC, version=True),
    "v2-extra-key": dict(V2_DOC, color="red"),
    "v2-missing-key": {k: v for k, v in V2_DOC.items() if k != "notices"},
    "v3-keys-with-v2-version": dict(V2_DOC, cron_prompts=[]),
}


@pytest.mark.parametrize("name", sorted(V2_BAD))
def test_only_an_exact_valid_v2_file_is_upgraded(name, base, env):
    text = json.dumps(V2_BAD[name])
    path = write_raw(base, text)
    with pytest.raises(ps.StateError) as err:
        ps.load(SID, env)
    assert err.value.kind == "malformed"
    assert path.read_text(encoding="utf-8") == text


def test_set_cron_prompts_strips_drops_blanks_and_dedups_and_replaces():
    doc = ps.default_state()
    ps.set_cron_prompts(doc, ["  a ", "", "b", "a", "   "])
    assert doc["cron_prompts"] == ["a", "b"]
    ps.set_cron_prompts(doc, ["c"])
    assert doc["cron_prompts"] == ["c"]
    ps.set_cron_prompts(doc, [])
    assert doc["cron_prompts"] == []


# -------------------------------------------------------------------------- locking

def test_lock_timeout_when_another_holder_has_the_lock(env, monkeypatch):
    monkeypatch.setattr(ps, "LOCK_TIMEOUT_SECONDS", 0.2)
    path = ps.state_path(SID, env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(ps.default_state()), encoding="utf-8")
    with ps._locked(path):
        for call in (
            lambda: ps.update(SID, lambda d: d.update(greeted=True), env),
            lambda: ps.load(SID, env),
        ):
            with pytest.raises(ps.StateError) as err:
                call()
            assert err.value.kind == "lock-timeout"
            assert err.value.text == f"{path}: could not lock within 5 s"
    assert ps.update(SID, lambda d: d.update(greeted=True), env)["greeted"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["greeted"] is True


def test_default_lock_timeout_is_five_seconds():
    assert ps.LOCK_TIMEOUT_SECONDS == 5.0


def test_threads_racing_updates_lose_nothing(env):
    threads_n, each = 4, 10
    start = threading.Barrier(threads_n)
    errors = []

    def worker(who):
        try:
            start.wait()
            for i in range(each):
                def fn(doc, i=i):
                    ps.bump_nudge(doc, "p")
                    ps.add_notice(doc, f"t{who}-{i}")

                ps.update(SID, fn, env)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors
    doc = ps.load(SID, env)
    assert doc["nudges"]["p"] == threads_n * each
    assert len(doc["notices"]) == threads_n * each


_WORKER = '''
import importlib.util, os, sys
mod_path, who, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
spec = importlib.util.spec_from_file_location("ps_worker", mod_path)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)
print("ready", flush=True)
sys.stdin.readline()
for i in range(count):
    def fn(doc, i=i):
        ps.bump_nudge(doc, "p")
        ps.add_notice(doc, "%s-%d" % (who, i))
    ps.update("race", fn, os.environ)
print("done", flush=True)
'''


def test_two_processes_racing_updates_lose_nothing(base, tmp_path):
    worker = tmp_path / "worker_scripts" / "race_worker.py"
    worker.parent.mkdir()
    worker.write_text(_WORKER, encoding="utf-8")
    child_env = dict(os.environ)
    child_env.pop("CLAUDE_BOOTSTRAP_DATA_ROOT", None)
    if base[0]:
        child_env.update(base[0])
    else:
        home = str(base[1].parents[2])  # <home>/.claude/plugins/data
        child_env.update(HOME=home, USERPROFILE=home)
    each = 15
    procs = [
        subprocess.Popen(
            [sys.executable, "-I", str(worker), str(_SCRIPT), who, str(each)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=child_env,
        )
        for who in ("a", "b")
    ]
    try:
        for proc in procs:
            assert proc.stdout.readline().strip() == "ready", proc.stderr.read()
        for proc in procs:  # release both together so the loops overlap
            proc.stdin.write("go\n")
            proc.stdin.flush()
        results = [proc.communicate(timeout=120) for proc in procs]
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
    for proc, (out, err) in zip(procs, results):
        assert proc.returncode == 0, err
        assert out.strip().endswith("done")
    doc = ps.load("race", base[0])
    assert doc["nudges"]["p"] == 2 * each
    assert sorted(doc["notices"]) == sorted(f"{w}-{i}" for w in "ab" for i in range(each))
    leftovers = [p.name for p in sessions_dir(base).iterdir()]
    assert sorted(leftovers) == ["race.json", "race.lock"]


# ----------------------------------------------------------------------- mutators

def test_enter_afk_fresh_sets_afk_since_and_clears_greeted():
    doc = ps.default_state()
    doc["greeted"] = True
    ps.enter_afk(doc, S0)
    assert (doc["presence"], doc["afk_since"], doc["greeted"]) == ("afk", S0, False)


def test_enter_afk_when_already_afk_changes_nothing():
    doc = ps.default_state()
    ps.enter_afk(doc, S0)
    doc["greeted"] = True
    before = json.dumps(doc)
    ps.enter_afk(doc, S1)
    assert json.dumps(doc) == before


def test_enter_afk_without_a_prompt_leaves_afk_prompt_null():
    doc = ps.default_state()
    ps.enter_afk(doc, S0)
    assert doc["afk_prompt"] is None


def test_enter_afk_with_a_prompt_records_it_on_a_fresh_afk():
    doc = ps.default_state()
    doc["greeted"] = True
    ps.enter_afk(doc, S0, prompt="p1")
    assert (doc["presence"], doc["afk_since"], doc["afk_prompt"], doc["greeted"]) == ("afk", S0, "p1", False)


def test_a_fresh_afk_without_a_prompt_overwrites_a_stale_afk_prompt():
    doc = ps.default_state()
    doc["afk_prompt"] = "stale"
    ps.enter_afk(doc, S0)
    assert doc["afk_prompt"] is None


def test_enter_afk_already_afk_with_a_prompt_records_only_the_prompt():
    doc = ps.default_state()
    ps.enter_afk(doc, S0, prompt="p1")
    doc["greeted"] = True
    ps.enter_afk(doc, S1, prompt="p2")
    assert (doc["afk_since"], doc["afk_prompt"], doc["greeted"]) == (S0, "p2", True)


def test_enter_afk_already_afk_without_a_prompt_keeps_the_hooks_afk_prompt():
    """The afk skill's own `state set afk` on the hook's prompt must not clear the record."""
    doc = ps.default_state()
    ps.enter_afk(doc, S0, prompt="p1")
    before = json.dumps(doc)
    ps.enter_afk(doc, S1)
    assert json.dumps(doc) == before


def test_enter_afk_refuses_a_bad_prompt_id():
    doc = ps.default_state()
    with pytest.raises(ps.StateError) as err:
        ps.enter_afk(doc, S0, prompt="a b")
    assert err.value.kind == "bad-prompt-id"
    assert doc == ps.default_state()


def test_enter_afk_after_return_starts_a_new_afk_period():
    doc = ps.default_state()
    ps.enter_afk(doc, S0)
    ps.set_present(doc)
    assert doc["afk_since"] == S0  # kept for the recap window
    ps.enter_afk(doc, S2)
    assert (doc["presence"], doc["afk_since"]) == ("afk", S2)


def test_enter_autonomous_and_set_normal_mutators():
    doc = ps.default_state()
    ps.enter_autonomous(doc, "ship", S0)
    assert (doc["goal"], doc["goal_since"]) == ("ship", S0)
    ps.enter_autonomous(doc, "ship", S1)
    assert doc["goal_since"] == S0
    ps.enter_autonomous(doc, "ship v2", S1)
    assert (doc["goal"], doc["goal_since"]) == ("ship v2", S1)
    ps.enter_afk(doc, S1)
    ps.set_normal(doc)
    assert (doc["goal"], doc["goal_since"], doc["presence"]) == (None, None, "afk")


def test_set_present_only_flips_presence():
    doc = ps.default_state()
    ps.enter_afk(doc, S0)
    doc["greeted"] = True
    doc["pending_return"] = "p9"
    doc["afk_prompt"] = "p1"
    ps.set_present(doc)
    assert doc["presence"] == "present" and doc["afk_since"] == S0
    assert doc["greeted"] is True and doc["pending_return"] == "p9"
    assert doc["afk_prompt"] is None   # the hook's record ends with the afk period


def test_mark_recap_last_write_wins_and_moves_key_to_end():
    doc = ps.default_state()
    ps.mark_recap(doc, "return", "p1")
    ps.mark_recap(doc, "return", "p2")
    ps.mark_recap(doc, "afk-ending", "p1")
    assert doc["recaps"] == {"p2": "return", "p1": "afk-ending"}
    assert list(doc["recaps"]) == ["p2", "p1"]
    with pytest.raises(ValueError):
        ps.mark_recap(doc, "bogus", "p3")


def test_bump_nudge_counts_and_moves_key_to_end():
    doc = ps.default_state()
    assert ps.bump_nudge(doc, "p1") == 1
    assert ps.bump_nudge(doc, "p2") == 1
    assert ps.bump_nudge(doc, "p1") == 2
    assert doc["nudges"] == {"p2": 1, "p1": 2} and list(doc["nudges"]) == ["p2", "p1"]


def test_add_notice_once_per_cause():
    doc = ps.default_state()
    assert ps.add_notice(doc, "entry-missing") is True
    assert ps.add_notice(doc, "entry-missing") is False
    assert ps.add_notice(doc, "session-crons-absent") is True
    assert doc["notices"] == ["entry-missing", "session-crons-absent"]


# ----------------------------------------------------------------------- pruning

def test_prune_keeps_the_last_twenty_of_recaps_and_nudges():
    doc = ps.default_state()
    for n in range(25):
        ps.mark_recap(doc, "return", f"p{n}")
        ps.bump_nudge(doc, f"p{n}")
    ps.prune(doc)
    expected = [f"p{n}" for n in range(5, 25)]
    assert list(doc["recaps"]) == expected and list(doc["nudges"]) == expected


def test_prune_keeps_a_recently_touched_old_key():
    doc = ps.default_state()
    for n in range(20):
        ps.bump_nudge(doc, f"p{n}")
        ps.mark_recap(doc, "afk-ending", f"p{n}")
    ps.bump_nudge(doc, "p0")  # touch the oldest...
    ps.mark_recap(doc, "return", "p0")
    ps.bump_nudge(doc, "new")  # ...then push one more key in
    ps.mark_recap(doc, "return", "new")
    ps.prune(doc)
    for table in ("recaps", "nudges"):
        assert len(doc[table]) == 20
        assert "p0" in doc[table] and "new" in doc[table] and "p1" not in doc[table]
    assert doc["nudges"]["p0"] == 2


def test_prune_leaves_small_tables_and_other_fields_alone():
    doc = ps.default_state()
    for n in range(20):
        ps.bump_nudge(doc, f"p{n}")
    doc["notices"] = ["a"]
    doc["unresolved"] = ["u"]
    before = json.dumps(doc)
    ps.prune(doc)
    assert json.dumps(doc) == before


def test_recap_done_prunes_to_twenty(env):
    for n in range(25):
        ps.recap_done(SID, "afk-ending", f"p{n}", env, T0)
    doc = ps.load(SID, env)
    assert list(doc["recaps"]) == [f"p{n}" for n in range(5, 25)]


def test_update_does_not_prune_on_its_own(env):
    def fn(doc):
        for n in range(25):
            ps.bump_nudge(doc, f"p{n}")

    assert len(ps.update(SID, fn, env)["nudges"]) == 25


def test_prune_before_the_write_persists_twenty(env):
    def fn(doc):
        for n in range(25):
            ps.bump_nudge(doc, f"p{n}")
        ps.prune(doc)

    ps.update(SID, fn, env)
    assert len(json.loads(ps.state_path(SID, env).read_text(encoding="utf-8"))["nudges"]) == 20


# ---------------------------------------------------------------- recap-done effects

def afk_doc(base, **fields):
    fields.setdefault("presence", "afk")
    fields.setdefault("afk_since", S0)
    return write_doc(base, **fields)


def test_recap_done_return_transition(base, env):
    afk_doc(
        base, goal="ship", goal_since=S1, greeted=True, unresolved=["w: x (prompt p1)"],
        pending_return="p7", nudges={"p7": 2}, notices=["entry-missing"],
        recaps={"p0": "afk-ending"},
    )
    out = ps.recap_done(SID, "return", "p7", env, T2)
    assert list(out) == ["recorded", "prompt_id", "state"]
    assert out["recorded"] == "return" and out["prompt_id"] == "p7"
    st = out["state"]
    assert st["presence"] == "present"
    assert st["afk_since"] == S0  # kept
    assert st["greeted"] is False
    assert st["unresolved"] == []
    assert st["pending_return"] is None
    assert st["recaps"] == {"p0": "afk-ending", "p7": "return"}
    # untouched
    assert (st["goal"], st["goal_since"]) == ("ship", S1)
    assert st["nudges"] == {"p7": 2} and st["notices"] == ["entry-missing"]
    assert ps.load(SID, env) == st


def test_recap_done_return_when_already_present_still_records(env):
    st = ps.recap_done(SID, "return", "p1", env, T0)["state"]
    assert st["presence"] == "present" and st["recaps"] == {"p1": "return"}
    assert ps.state_get(SID, env)["exists"] is True


def test_recap_done_afk_ending_is_marker_only(base, env):
    path = afk_doc(
        base, goal="ship", goal_since=S1, greeted=True, unresolved=["u"],
        pending_return="p7",
    )
    expected = json.loads(path.read_text(encoding="utf-8"))
    expected["recaps"] = {"p3": "afk-ending"}
    out = ps.recap_done(SID, "afk-ending", "p3", env, T2)
    assert out["recorded"] == "afk-ending" and out["prompt_id"] == "p3"
    assert out["state"] == expected
    assert out["state"]["presence"] == "afk"


def test_recap_done_autonomous_done_clears_goal_and_stays_afk(base, env):
    afk_doc(
        base, goal="ship", goal_since=S1, greeted=True, unresolved=["u"],
        pending_return="p7",
    )
    st = ps.recap_done(SID, "autonomous-done", "p4", env, T2)["state"]
    assert st["recaps"] == {"p4": "autonomous-done"}
    assert (st["goal"], st["goal_since"]) == (None, None)
    assert st["presence"] == "afk" and st["afk_since"] == S0
    assert st["greeted"] is True and st["unresolved"] == ["u"]
    assert st["pending_return"] == "p7"


def test_recap_done_return_then_afk_ending_same_pid_last_write_wins(base, env):
    afk_doc(base)
    ps.recap_done(SID, "return", "p1", env, T1)
    st = ps.recap_done(SID, "afk-ending", "p1", env, T2)["state"]
    assert st["recaps"] == {"p1": "afk-ending"}
    assert st["presence"] == "present"  # the afk-ending marker does not touch presence


def test_recap_done_afk_ending_then_return_same_pid_last_write_wins(base, env):
    afk_doc(base)
    ps.recap_done(SID, "afk-ending", "p1", env, T1)
    st = ps.recap_done(SID, "return", "p1", env, T2)["state"]
    assert st["recaps"] == {"p1": "return"} and st["presence"] == "present"


def test_recap_done_rejects_bad_input_and_writes_nothing(base, env):
    with pytest.raises(ValueError):
        ps.recap_done(SID, "bogus", "p1", env, T0)
    with pytest.raises(ps.StateError) as err:
        ps.recap_done(SID, "return", "has space", env, T0)
    assert err.value.kind == "bad-prompt-id"
    with pytest.raises(ps.StateError) as err:
        ps.recap_done("../x", "return", "p1", env, T0)
    assert err.value.kind == "bad-session-id"
    assert not base[1].exists()


def test_recap_done_on_malformed_file_is_loud(base, env):
    path = sessions_dir(base) / f"{SID}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ps.StateError) as err:
        ps.recap_done(SID, "return", "p1", env, T0)
    assert err.value.kind == "malformed"


# ------------------------------------------------------------------ state set: afk

def test_state_set_afk_by_user_writes_even_when_the_id_is_off(env):
    cfg = FakeConfig(afk_on_departure=False)
    out = ps.state_set(SID, "afk", "user", None, cfg.behaviors, env, T0)
    assert list(out) == ["written", "by", "state"]
    assert out["written"] is True and out["by"] == "user"
    assert out["state"]["presence"] == "afk" and out["state"]["afk_since"] == S0
    assert ps.load(SID, env) == out["state"]


def test_state_set_by_user_never_needs_config(env):
    out = ps.state_set(SID, "afk", "user", None, None, env, T0)
    assert out["written"] is True
    out = ps.state_set(SID, "autonomous", "user", "ship", None, env, T0)
    assert out["written"] is True


def test_state_set_afk_by_model_with_the_id_off_is_skipped_and_writes_nothing(base, env):
    cfg = FakeConfig(afk_on_departure=False)
    out = ps.state_set(SID, "afk", "model", None, cfg.behaviors, env, T0)
    assert out == {"skipped": "afk-on-departure", "reason": "off"}
    assert not base[1].exists()  # no state file, lock file or directory
    assert ps.load(SID, env) == ps.default_state()


def test_state_set_afk_by_model_skip_leaves_an_existing_file_untouched(base, env):
    path = afk_doc(base, presence="present", afk_since=None)
    before = path.read_bytes()
    out = ps.state_set(SID, "afk", "model", None, FakeConfig(afk_on_departure=False).behaviors, env, T0)
    assert out["skipped"] == "afk-on-departure"
    assert path.read_bytes() == before


def test_state_set_afk_by_model_with_the_id_on_writes(env):
    cfg = FakeConfig()
    out = ps.state_set(SID, "afk", "model", None, cfg.behaviors, env, T0)
    assert out["written"] is True and out["by"] == "model"
    assert out["state"]["presence"] == "afk"


def test_state_set_afk_is_not_gated_by_the_autonomy_id(env):
    cfg = FakeConfig(autonomous_on_handover=False)
    assert ps.state_set(SID, "afk", "model", None, cfg.behaviors, env, T0)["written"] is True


def test_state_set_afk_when_already_afk_is_a_noop_that_still_reports_written(base, env, monkeypatch):
    afk_doc(base, afk_since=S0, greeted=True)
    writes = count_writes(monkeypatch)
    out = ps.state_set(SID, "afk", "user", None, None, env, T2)
    assert out["written"] is True
    assert out["state"]["afk_since"] == S0 and out["state"]["greeted"] is True
    assert writes == []


def test_state_set_fresh_afk_resets_a_stale_greeted_flag(base, env):
    write_doc(base, presence="present", afk_since=S0, greeted=True)
    st = ps.state_set(SID, "afk", "user", None, None, env, T2)["state"]
    assert st["presence"] == "afk" and st["afk_since"] == S2 and st["greeted"] is False


# ---------------------------------------------------------------- state set: autonomous

def test_state_set_autonomous_enters_afk_and_sets_a_stripped_goal(env):
    out = ps.state_set(SID, "autonomous", "user", "  ship the thing \n", None, env, T0)
    st = out["state"]
    assert out["written"] is True and out["by"] == "user"
    assert (st["presence"], st["afk_since"]) == ("afk", S0)
    assert (st["goal"], st["goal_since"]) == ("ship the thing", S0)


def test_state_set_autonomous_by_model_with_the_id_off_is_skipped(base, env):
    cfg = FakeConfig(autonomous_on_handover=False)
    out = ps.state_set(SID, "autonomous", "model", "ship", cfg.behaviors, env, T0)
    assert out == {"skipped": "autonomous-on-handover", "reason": "off"}
    assert not base[1].exists()


def test_state_set_autonomous_by_user_writes_while_the_id_is_off(env):
    cfg = FakeConfig(autonomous_on_handover=False)
    out = ps.state_set(SID, "autonomous", "user", "ship", cfg.behaviors, env, T0)
    assert out["written"] is True and out["state"]["goal"] == "ship"


def test_state_set_autonomous_by_model_with_the_id_on_writes(env):
    cfg = FakeConfig(afk_on_departure=False)  # the afk id does not gate autonomy
    out = ps.state_set(SID, "autonomous", "model", "ship", cfg.behaviors, env, T0)
    assert out["written"] is True and out["by"] == "model"


@pytest.mark.parametrize("goal", [None, "", "   ", "\n\t"])
def test_state_set_autonomous_needs_a_nonblank_goal(goal, base, env):
    with pytest.raises(ValueError):
        ps.state_set(SID, "autonomous", "user", goal, None, env, T0)
    assert not base[1].exists()


def test_autonomous_same_goal_keeps_goal_since(env):
    ps.state_set(SID, "autonomous", "user", "ship the thing", None, env, T0)
    st = ps.state_set(SID, "autonomous", "user", "  ship the thing  ", None, env, T1)["state"]
    assert st["goal"] == "ship the thing"
    assert st["goal_since"] == S0
    assert st["afk_since"] == S0
    assert ps.load(SID, env)["goal_since"] == S0


def test_autonomous_different_goal_resets_goal_since(env):
    ps.state_set(SID, "autonomous", "user", "ship the thing", None, env, T0)
    st = ps.state_set(SID, "autonomous", "user", "ship another thing", None, env, T1)["state"]
    assert st["goal"] == "ship another thing"
    assert st["goal_since"] == S1
    assert st["afk_since"] == S0  # still the same afk period
    assert ps.load(SID, env)["goal_since"] == S1


def test_autonomous_after_normal_resets_goal_since(env):
    ps.state_set(SID, "autonomous", "user", "ship the thing", None, env, T0)
    ps.state_set(SID, "normal", "user", None, None, env, T1)
    st = ps.state_set(SID, "autonomous", "user", "ship the thing", None, env, T2)["state"]
    assert st["goal"] == "ship the thing"
    assert st["goal_since"] == S2
    assert ps.load(SID, env)["goal_since"] == S2


# --------------------------------------------------------------------- state set: present

def test_state_set_present_undoes_a_hook_set_afk(env):
    """Cross-check fix 1: the undo path P3 hands to Claude."""
    def hook_sets_afk(doc):
        ps.enter_afk(doc, S0, prompt="p1")
        ps.enter_autonomous(doc, "ship", S0)
        doc["pending_return"] = "p1"

    ps.update(SID, hook_sets_afk, env)
    out = ps.state_set(SID, "present", "user", None, None, env, T2)
    assert list(out) == ["written", "by", "state"]
    assert out["written"] is True and out["by"] == "user"
    st = out["state"]
    assert st["presence"] == "present" and st["afk_prompt"] is None and st["pending_return"] is None
    assert st["afk_since"] == S0                       # kept for the recap window
    assert (st["goal"], st["goal_since"]) == ("ship", S0)   # authority untouched
    assert ps.load(SID, env) == st


def test_state_set_present_while_present_changes_nothing(env, monkeypatch):
    writes = count_writes(monkeypatch)
    out = ps.state_set(SID, "present", "user", None, None, env, T0)
    assert out["state"] == ps.default_state() and out["by"] == "user"
    assert writes == []


def test_state_set_present_ignores_by_and_the_gates(env):
    cfg = FakeConfig(afk_on_departure=False, autonomous_on_handover=False)
    ps.state_set(SID, "afk", "user", None, None, env, T0)
    out = ps.state_set(SID, "present", "model", None, cfg.behaviors, env, T1)
    assert out["by"] == "user" and out["state"]["presence"] == "present"


def test_state_set_present_is_a_set_target():
    assert ps.SET_TARGETS == ("afk", "autonomous", "normal", "present")


def test_recap_done_return_clears_the_hooks_afk_prompt(env):
    ps.update(SID, lambda d: ps.enter_afk(d, S0, prompt="p1"), env)
    st = ps.recap_done(SID, "return", "p2", env, T1)["state"]
    assert st["presence"] == "present" and st["afk_prompt"] is None


# ---------------------------------------------------------------------- state set: normal

def test_state_set_normal_clears_authority_and_leaves_presence(env):
    ps.state_set(SID, "autonomous", "user", "ship", None, env, T0)
    out = ps.state_set(SID, "normal", "user", None, None, env, T1)
    assert list(out) == ["written", "by", "state"]
    assert out["written"] is True and out["by"] == "user"
    st = out["state"]
    assert (st["goal"], st["goal_since"]) == (None, None)
    assert (st["presence"], st["afk_since"]) == ("afk", S0)


def test_state_set_normal_while_present_changes_nothing_and_never_loads_config(env):
    out = ps.state_set(SID, "normal", "user", None, None, env, T0)
    assert out["state"] == ps.default_state() and out["by"] == "user"


def test_state_set_normal_ignores_the_by_gate(env):
    cfg = FakeConfig(afk_on_departure=False, autonomous_on_handover=False)
    out = ps.state_set(SID, "normal", "model", None, cfg.behaviors, env, T0)
    assert out["written"] is True and out["by"] == "user"


def test_state_set_by_model_needs_behaviors(env):
    with pytest.raises(ValueError):
        ps.state_set(SID, "afk", "model", None, None, env, T0)


def test_state_set_rejects_unknown_target_or_by(env):
    with pytest.raises(ValueError):
        ps.state_set(SID, "sleep", "user", None, None, env, T0)
    with pytest.raises(ValueError):
        ps.state_set(SID, "afk", "robot", None, None, env, T0)


def test_state_set_validates_the_session_id(env):
    with pytest.raises(ps.StateError) as err:
        ps.state_set("../x", "afk", "user", None, None, env, T0)
    assert err.value.kind == "bad-session-id"


def test_state_get_reports_exists_after_a_set(env):
    assert ps.state_get(SID, env)["exists"] is False
    ps.state_set(SID, "afk", "user", None, None, env, T0)
    got = ps.state_get(SID, env)
    assert got["exists"] is True and got["state"]["presence"] == "afk"
    assert list(got) == ["exists", "state"]


def test_fake_config_matches_the_section_1_surface():
    cfg = FakeConfig(afk_on_departure=False)
    assert tuple(cfg.behaviors) == BEHAVIOR_IDS
    assert cfg.enabled("afk-on-departure") is False and cfg.enabled("waiting-guard") is True
