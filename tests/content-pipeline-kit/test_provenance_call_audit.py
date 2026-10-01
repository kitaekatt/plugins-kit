"""PR3: provenance.call_audit (per-call audit files from attempt observers)."""

import json
import threading
from types import SimpleNamespace

from content_pipeline.llm.platform import CallAttempt, LLMResponse
from content_pipeline.provenance.call_audit import CallAuditor


def _attempt(user="U", v=1, t=1, text="ok", error=None, from_cache=False,
             salt=0, rejections=(), system="S", cost=None):
    response = None
    if error is None:
        response = SimpleNamespace(
            text=text, model="m", input_tokens=10, output_tokens=5,
            from_cache=from_cache, wall_ms=7, reported_cost_usd=cost,
            reported_cost_source="src" if cost is not None else None,
        )
    return SimpleNamespace(
        identifier="id", validation_attempt=v, transport_attempt=t,
        system=system, user=user, model="m", temperature=0.2, max_tokens=99,
        cache_salt=salt, effort="low", response=response, error=error,
        rejections=tuple(rejections),
    )


def _meta(path):
    return json.loads(path.read_text(encoding="ascii"))


def test_call_auditor_layout(tmp_path):
    aud = CallAuditor(tmp_path / "audit")
    obs = aud.observer("unit/1")
    obs(_attempt(cost=0.5))
    aud.finalize()
    d = tmp_path / "audit" / "unit_1"
    assert (d / "system_prompt.txt").read_text() == "S"
    assert (d / "user_prompt.txt").read_text() == "U"
    assert (d / "attempt_1_response.txt").read_text() == "ok"
    meta = _meta(d / "meta.json")
    assert meta["model"] == "m" and meta["provider"] == "src"
    assert meta["input_tokens"] == 10 and meta["cost_usd"] == 0.5
    assert meta["from_cache"] is False and meta["wall_ms"] == 7
    a = meta["attempts"][0]
    assert (a["temperature"], a["max_tokens"], a["cache_salt"], a["effort"]) == (0.2, 99, 0, "low")
    assert _meta(tmp_path / "audit" / "summary.json")["totals"]["attempts"] == 1
    for p in (tmp_path / "audit").rglob("*.json"):
        assert str(tmp_path) not in p.read_text(encoding="ascii")


def test_retry_prompt_written_only_when_different(tmp_path):
    aud = CallAuditor(tmp_path)
    obs = aud.observer("c")
    obs(_attempt(user="U", v=1, rejections=("bad",)))
    obs(_attempt(user="U", v=2, salt=1))             # same prompt
    obs(_attempt(user="U+feedback", v=3, salt=2))    # retry prompt
    d = tmp_path / "c"
    assert not (d / "attempt_2_user_prompt.txt").exists()
    assert (d / "attempt_3_user_prompt.txt").read_text() == "U+feedback"
    assert not (d / "attempt_1_user_prompt.txt").exists()
    meta = _meta(d / "meta.json")
    assert meta["attempts"][0]["rejections"] == ["bad"]
    assert [a["cache_salt"] for a in meta["attempts"]] == [0, 1, 2]


def test_error_attempt_writes_error_file(tmp_path):
    obs = CallAuditor(tmp_path).observer("c")
    obs(_attempt(error="TimeoutError: slow"))
    obs(_attempt(t=2, text="fine"))
    d = tmp_path / "c"
    assert (d / "attempt_1_error.txt").read_text() == "TimeoutError: slow"
    assert not (d / "attempt_1_response.txt").exists()
    assert (d / "attempt_2_response.txt").read_text() == "fine"


def test_chunk_ids_unique_under_threads(tmp_path):
    aud = CallAuditor(tmp_path)
    errors = []

    def work(i):
        try:
            obs = aud.observer("same")
            for v in range(3):
                obs(_attempt(user="u%d" % i, v=v + 1, text="r%d" % i))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    dirs = sorted(p.name for p in tmp_path.iterdir())
    assert len(dirs) == 8 and len(set(dirs)) == 8
    for d in dirs:
        # each directory holds exactly one thread's coherent attempts
        users = {(tmp_path / d / "user_prompt.txt").read_text()}
        assert len(users) == 1
        assert len(_meta(tmp_path / d / "meta.json")["attempts"]) == 3


def test_none_audit_dir_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    aud = CallAuditor(None)
    aud.observer("c")(_attempt())
    aud.finalize()
    assert list(tmp_path.iterdir()) == []


def test_chunk_name_cannot_escape_audit_dir(tmp_path):
    CallAuditor(tmp_path / "a").observer("../../evil")(_attempt())
    assert not (tmp_path / "evil").exists()
    assert [p.name for p in (tmp_path / "a").iterdir()] == ["evil"]


def test_accepts_real_call_attempt(tmp_path):
    resp = LLMResponse(text="hi", model="m", from_cache=True)
    att = CallAttempt(
        identifier="i", validation_attempt=1, transport_attempt=1, system="s",
        user="u", model="m", temperature=None, max_tokens=None, cache_salt=0,
        effort=None, response=resp, error=None,
    )
    CallAuditor(tmp_path).observer("real")(att)
    assert _meta(tmp_path / "real" / "meta.json")["from_cache"] is True
