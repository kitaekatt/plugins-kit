"""Differential parity: llm_kit.frontdoor vs legacy llm_scripting_kit.frontdoor.

The legacy side is imported from plugins/llm-scripting-kit/lib with a sys.path
insert scoped to the import. Both apps are built from the same registry file
and driven through httpx's ASGI transport against an in-process fake upstream;
the acceptance runner is driven against stdlib fake front doors on localhost.
No call leaves the machine.

Covered: pure helpers (normalize_body, cost policy, budget, group verdict,
check text, usage parse), routes (/v1/chat/completions, /v1/models, /health,
/health/backends, /who), routing (fill-first, spill, retry, 502), user
stripping, effort placement, billing.mode cost gate, access-log lines, the CLI
main (--check, --print-config, errors) and the acceptance verdicts. Cases that
need fastapi/httpx skip loudly (reason names the missing module) when the
frontdoor extra is absent.

The one deliberate difference is the argparse prog name, which names the new
module (llm_kit.frontdoor); the persisted access-log path keeps the legacy
plugin-name namespace and is asserted equal.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import re
import subprocess
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]
_LEGACY_LIB = _REPO / "plugins" / "llm-scripting-kit" / "lib"
_FINAL_LIB = _REPO / "packages" / "katsoft-llm-kit" / "src"

try:
    import fastapi  # noqa: F401
    import httpx

    _HAVE_WEB = True
    _WEB_MISSING = ""
except ImportError as _exc:  # pragma: no cover - environment dependent
    httpx = None  # type: ignore[assignment]
    _HAVE_WEB = False
    _WEB_MISSING = f"frontdoor extra not installed ({_exc}); web parity cases SKIPPED"

web = pytest.mark.skipif(not _HAVE_WEB, reason=_WEB_MISSING)


@pytest.fixture(scope="module")
def L():
    sys.path.insert(0, str(_LEGACY_LIB))
    try:
        return types.SimpleNamespace(
            frontdoor=importlib.import_module("llm_scripting_kit.frontdoor"),
            server=importlib.import_module("llm_scripting_kit.frontdoor.server"),
            acceptance=importlib.import_module("llm_scripting_kit.frontdoor.acceptance"),
            endpoints=importlib.import_module("llm_scripting_kit.model_endpoints"),
            account=importlib.import_module("llm_scripting_kit.account"),
        )
    finally:
        sys.path.remove(str(_LEGACY_LIB))


@pytest.fixture(scope="module")
def F():
    return types.SimpleNamespace(
        frontdoor=importlib.import_module("llm_kit.frontdoor"),
        server=importlib.import_module("llm_kit.frontdoor.server"),
        acceptance=importlib.import_module("llm_kit.frontdoor.acceptance"),
        endpoints=importlib.import_module("llm_kit.registry.model_endpoints"),
        account=importlib.import_module("llm_kit.adapters.account"),
    )


_REAL_ACCESS_LOG = (Path.home() / ".claude" / "plugins" / "data" / "plugins-kit"
                    / "llm-scripting-kit" / "frontdoor-access.jsonl")


def _stat_sig(path: Path):
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_size, st.st_mtime_ns)


_REAL_LOG_AT_START = _stat_sig(_REAL_ACCESS_LOG)


_WATCHED_LOG = _REAL_ACCESS_LOG  # the guard's watched path
_TMP_LOG = []  # per-test tmp access log, the explicit default for apps built without one


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Both front doors resolve their default log from Path.home() inside create_app (call
    time, not import time), so HOME/USERPROFILE reach it; apps built here ALSO get an explicit
    tmp access_log, so no single mechanism is load-bearing."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    _TMP_LOG[:] = [tmp_path / "default-access.jsonl"]
    yield
    _TMP_LOG[:] = []


@pytest.fixture(scope="session", autouse=True)
def _real_log_guard_session():
    """Session-wide guard: the watched log must be unchanged when the session ends."""
    before = _stat_sig(_WATCHED_LOG)
    yield
    assert _stat_sig(_WATCHED_LOG) == before, "real front-door access log was written"


def _write_registry(tmp_path: Path, text: str, name: str = "models.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _load(mod, path: Path):
    return mod.endpoints.load_endpoint_registry({"MODEL_ENDPOINTS_REGISTRY": str(path)})


def _first_entry(mod, path: Path):
    return list(_load(mod, path).entries.values())[0]


def _outcome(fn, *a, **k):
    try:
        return ("ok", fn(*a, **k))
    except BaseException as exc:  # noqa: BLE001 -- compared, never swallowed
        return ("err", [c.__name__ for c in type(exc).__mro__], str(exc))


# --------------------------------------------------------------------------
# Surface
# --------------------------------------------------------------------------


def _own_names(module) -> set[str]:
    return {
        n for n, v in vars(module).items()
        if not n.startswith("__")
        and getattr(v, "__module__", None) == module.__name__
    }


def test_public_surface_matches(L, F):
    assert F.frontdoor.__all__ == L.frontdoor.__all__
    for name in ("server", "acceptance"):
        assert _own_names(getattr(F, name)) == _own_names(getattr(L, name)), name
    for const in ("DEPLOYMENT_HEADER", "PAID_SPILL_EXTRA", "_CHECKPOINTS"):
        assert getattr(F.acceptance, const) == getattr(L.acceptance, const)


def test_importing_package_needs_no_web_extra():
    code = (
        f"import sys; sys.path.insert(0, {str(_FINAL_LIB)!r}); import llm_kit.frontdoor;"
        "print([m for m in ('fastapi','uvicorn','httpx') if m in sys.modules])"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip() == "[]"


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

_EFFORT_STYLES = ["", "effort_style: ninfer", "effort_style: openai",
                  "effort_style: unsupported", "effort_style: bogus"]
_INBOUND = [
    {},
    {"reasoning_effort": "high"},
    {"reasoning_effort": "low"},
    {"reasoning": {"effort": "medium"}},
    {"reasoning_effort": "xhigh", "user": "u"},
    {"chat_template_kwargs": {"enable_thinking": False}},
    {"user": "someone", "messages": [{"role": "user", "content": "x"}]},
]


@pytest.mark.parametrize("style", _EFFORT_STYLES)
@pytest.mark.parametrize("inbound", _INBOUND)
def test_normalize_body_parity(L, F, tmp_path, style, inbound):
    extra = f", {style}" if style else ""
    path = _write_registry(
        tmp_path,
        "models:\n  qwen:\n    base_url: http://up/v1\n    model: qwen-real\n"
        f"    routing: {{group: qwen, order: 1{extra}}}\n",
    )
    results = []
    for mod in (L, F):
        body = json.loads(json.dumps(inbound))
        results.append(_outcome(mod.server._normalize_body, body, _first_entry(mod, path)) + (body,))
    assert results[0] == results[1]


_USAGES = [
    None, {}, {"prompt_tokens": 1}, {"cost": 0.5}, {"cost": 0.5, "cost_source": "x"},
    {"cost": None}, {"cost": "bad"}, {"cost": -1}, {"cost_source": "y", "prompt_tokens": 3},
]


@pytest.mark.parametrize("mode", ["", "unmetered", "provider-reported", "metered"])
@pytest.mark.parametrize("usage", _USAGES)
def test_apply_cost_policy_parity(L, F, tmp_path, mode, usage):
    line = f"    billing: {{mode: {mode}}}\n" if mode else ""
    path = _write_registry(
        tmp_path,
        "models:\n  qwen:\n    base_url: http://up/v1\n    model: q\n"
        "    routing: {group: qwen, order: 1}\n" + line,
    )
    bodies = [b"not json", b"[1,2]", b"{}", json.dumps({"choices": []}).encode()]
    if usage is not None:
        bodies.append(json.dumps({"choices": [], "usage": usage}).encode())
    for raw in bodies:
        res = [_outcome(mod.server._apply_cost_policy, raw, _first_entry(mod, path))
               for mod in (L, F)]
        assert res[0] == res[1]


@pytest.mark.parametrize("raw", [None, "", "abc", "0", "-5", "100", "1500", "999999", "12.5", "250"])
def test_parse_budget_ms_parity(L, F, raw):
    assert _outcome(L.server._parse_budget_ms, raw) == _outcome(F.server._parse_budget_ms, raw)


@pytest.mark.parametrize("statuses", [[], ["reachable"], ["unreachable"], ["unknown"],
                                      ["unreachable", "reachable"], ["unreachable", "unknown"]])
def test_group_verdict_parity(L, F, statuses):
    assert _outcome(L.server._group_verdict, statuses) == _outcome(F.server._group_verdict, statuses)


@pytest.mark.parametrize("body", [b"", b"{", b"[]", b'{"usage": null}',
                                  b'{"usage": {"prompt_tokens": 2, "completion_tokens": 3}}'])
def test_usage_from_json_parity(L, F, body):
    assert _outcome(L.server._usage_from_json, body) == _outcome(F.server._usage_from_json, body)


def test_check_text_and_main_cli_parity(L, F, tmp_path, capsys):
    path = _write_registry(
        tmp_path,
        "models:\n  a:\n    base_url: http://a/v1\n    model: ma\n    routing: {group: g, order: 1, max_parallel: 2}\n"
        "  b:\n    base_url: http://b/v1\n    model: mb\n  c:\n    base_url: http://c/v1\n    model: mc\n"
        "    routing: {group: g, order: 2}\n",
    )
    assert L.server._check_text(_load(L, path)) == F.server._check_text(_load(F, path))
    for argv in (["--check", "--registry", str(path)],
                 ["--print-config", "--registry", str(path), "--port", "9"],
                 ["--registry", str(tmp_path / "missing.yaml")]):
        got = []
        for mod in (L, F):
            capsys.readouterr()
            rc = _outcome(mod.server.main, list(argv))
            cap = capsys.readouterr()
            got.append((rc, cap.out, cap.err))
        assert got[0] == got[1], argv


def test_argparse_help_differs_only_in_prog(L, F, capsys):
    texts = []
    for mod in (L, F):
        with pytest.raises(SystemExit):
            mod.server.main(["--help"])
        texts.append(capsys.readouterr().out)
    # the usage wrap indent follows the prog length, so compare whitespace-normalized
    norm = [" ".join(t.split()) for t in texts]
    assert norm[0].replace("llm_scripting_kit.frontdoor", "llm_kit.frontdoor") == norm[1]


# --------------------------------------------------------------------------
# App parity (ASGI)
# --------------------------------------------------------------------------


class _Upstream:
    """In-process stand-in for httpx.AsyncClient used by the front door."""

    def __init__(self, release=None, failures=None, body=None, status=200):
        self.release = release
        self.failures = dict(failures or {})
        self.body = body
        self.status = status
        self.calls: list[str] = []
        self.sent: list[dict[str, Any]] = []

    async def post(self, url, *, json, headers):  # noqa: A002
        dep = url.split("//", 1)[1].split("/", 1)[0]
        self.calls.append(dep)
        self.sent.append(dict(json))
        if self.failures.get(dep, 0):
            self.failures[dep] -= 1
            raise httpx.ConnectError("connection failed")
        if self.release is not None:
            await self.release.wait()
        payload = self.body if self.body is not None else {
            "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
        return httpx.Response(self.status, json=payload)

    async def aclose(self):
        pass


_REAL_CLIENT = httpx.AsyncClient if _HAVE_WEB else None


def _scrub_log(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        rec = json.loads(line)
        rec["ts"] = "<ts>"
        rec["latency_ms"] = "<ms>"
        rows.append(rec)
    return rows


def _hdrs(r):
    return {k: v for k, v in r.headers.items() if k.startswith("x-") or k == "content-type"}


async def _drive(mod, registry, upstream, monkeypatch, steps, *, access_log=None, **app_kw):
    """Run steps sequentially; each step is (method, path, json|None)."""
    app = mod.server.create_app(registry, access_log=access_log, **app_kw)
    out = []
    async with _REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://frontdoor") as c:
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: upstream)
        for method, path, payload in steps:
            r = await (c.get(path) if method == "get" else c.post(path, json=payload))
            out.append((r.status_code, _hdrs(r), r.json() if r.content else None))
    return out


def _both(L, F, tmp_path, monkeypatch, text, make_upstream, steps, **kw):
    path = _write_registry(tmp_path, text)
    results, logs = [], []
    for i, mod in enumerate((L, F)):
        log = tmp_path / f"access{i}.jsonl"
        upstream = make_upstream()
        out = asyncio.run(_drive(mod, _load(mod, path), upstream, monkeypatch, steps,
                                 access_log=log, **kw))
        results.append((out, upstream.calls, upstream.sent))
        logs.append(_scrub_log(log.read_text(encoding="utf-8")) if log.exists() else [])
    return results, logs


_QWEN = ("models:\n  qwen:\n    base_url: http://up/v1\n    model: qwen-real\n"
         "    routing: {group: qwen, order: 1, effort_style: ninfer}\n")


@web
def test_default_access_log_path_unchanged(L, F, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path = _write_registry(tmp_path, "models:\n  q:\n    base_url: http://up/v1\n    model: m\n"
                                     "    routing: {group: q, order: 1}\n")
    expected = tmp_path / ".claude/plugins/data/plugins-kit/llm-scripting-kit/frontdoor-access.jsonl"
    lines = []
    for mod in (L, F):
        expected.parent.mkdir(parents=True, exist_ok=True)
        if expected.exists():
            expected.unlink()
        asyncio.run(_drive(mod, _load(mod, path), _Upstream(), monkeypatch,
                           [("post", "/v1/chat/completions", {"model": "q"})]))
        lines.append(expected.read_text(encoding="utf-8").count("\n"))
    assert lines == [1, 1]


@web
def test_user_stripped_effort_normalized_header_and_log(L, F, tmp_path, monkeypatch):
    steps = [
        ("post", "/v1/chat/completions",
         {"model": "qwen", "messages": [], "user": "u1", "reasoning_effort": "high"}),
        ("post", "/v1/chat/completions",
         {"model": "qwen", "user": "u2", "reasoning": {"effort": "low"}}),
        ("post", "/v1/chat/completions", {"model": "nope"}),
        ("post", "/v1/chat/completions", {"messages": []}),
        ("get", "/v1/models", None),
        ("get", "/health", None),
        ("get", "/who", None),
    ]
    results, logs = _both(L, F, tmp_path, monkeypatch, _QWEN, lambda: _Upstream(), steps)
    assert results[0] == results[1]
    assert logs[0] == logs[1]
    assert results[1][0][0][1]["x-frontdoor-deployment"] == "qwen"
    assert "user" not in results[1][2][0] and results[1][2][0]["reasoning_effort"] == "xhigh"
    assert len(logs[1]) >= 2


@web
@pytest.mark.parametrize("billing", ["", "unmetered", "provider-reported", "metered"])
@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 1}, {"cost": 0.25, "cost_source": "z"},
                                   {"cost": None}, {"cost": "x"}])
def test_billing_cost_gate_parity(L, F, tmp_path, monkeypatch, billing, usage):
    line = f"    billing: {{mode: {billing}}}\n" if billing else ""
    text = ("models:\n  qwen:\n    base_url: http://up/v1\n    model: q\n"
            "    routing: {group: qwen, order: 1}\n" + line)
    body: dict[str, Any] = {"choices": []}
    if usage is not None:
        body["usage"] = usage
    steps = [("post", "/v1/chat/completions", {"model": "qwen", "user": "u"})]
    results, logs = _both(L, F, tmp_path, monkeypatch, text, lambda: _Upstream(body=body), steps)
    assert results[0] == results[1]
    assert logs[0] == logs[1]


_TIERS = (
    "models:\n"
    "  first:\n    base_url: http://first/v1\n    model: m1\n    routing: {group: g, order: 1, max_parallel: 2}\n"
    "  second:\n    base_url: http://second/v1\n    model: m2\n    routing: {group: g, order: 2, max_parallel: 1}\n"
    "  third:\n    base_url: http://third/v1\n    model: m3\n    routing: {group: g, order: 3}\n"
)


async def _concurrent(mod, registry, monkeypatch, n, **app_kw):
    release = asyncio.Event()
    upstream = _Upstream(release=release)
    app_kw.setdefault("access_log", _TMP_LOG[0])
    app = mod.server.create_app(registry, **app_kw)
    async with _REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://frontdoor") as c:
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: upstream)
        tasks = [asyncio.create_task(c.post("/v1/chat/completions", json={"model": "g", "user": f"u{i}"}))
                 for i in range(n)]
        who = None
        for _ in range(300):
            who = (await c.get("/who")).json()
            if len(upstream.calls) >= n:
                break
            await asyncio.sleep(0.01)
        who = (await c.get("/who")).json()
        release.set()
        rs = await asyncio.gather(*tasks)
    return (who, sorted(r.headers["x-frontdoor-deployment"] for r in rs), sorted(upstream.calls))


@web
@pytest.mark.parametrize("n", [1, 3, 4])
def test_fill_first_routing_parity(L, F, tmp_path, monkeypatch, n):
    path = _write_registry(tmp_path, _TIERS)
    got = [asyncio.run(_concurrent(mod, _load(mod, path), monkeypatch, n)) for mod in (L, F)]
    assert got[0] == got[1]


@web
def test_spill_after_parity(L, F, tmp_path, monkeypatch):
    path = _write_registry(tmp_path, _TIERS)
    got = [asyncio.run(_concurrent(mod, _load(mod, path), monkeypatch, 3, spill_after=0.05))
           for mod in (L, F)]
    assert got[0] == got[1]


@web
def test_retry_and_all_failed_parity(L, F, tmp_path, monkeypatch):
    steps = [("post", "/v1/chat/completions", {"model": "g", "user": "u"})]
    for failures in ({"first": 1}, {"first": 5, "second": 5, "third": 5}):
        results, logs = _both(L, F, tmp_path, monkeypatch, _TIERS,
                              lambda f=failures: _Upstream(failures=f), steps)
        assert results[0] == results[1]
        assert logs[0] == logs[1]


@web
def test_upstream_error_status_passthrough_parity(L, F, tmp_path, monkeypatch):
    steps = [("post", "/v1/chat/completions", {"model": "g"})]
    results, logs = _both(L, F, tmp_path, monkeypatch, _TIERS,
                          lambda: _Upstream(status=500, body={"error": "boom"}), steps)
    assert results[0] == results[1]
    assert logs[0] == logs[1]


def _probe_factory(account, statuses, calls):
    def fake(entry, *, timeout, key_resolver=None, project_root=None):
        calls.append((entry.id, entry.base_url, timeout))
        status = statuses[entry.id]
        if status == "reachable":
            return account.EndpointProbe(True, entry.id, entry.base_url, "ok")
        if status == "unreachable":
            return account.EndpointProbe(False, entry.id, entry.base_url, "unreachable: refused")
        return account.EndpointProbe(False, entry.id, entry.base_url,
                                     "no API key resolved", decisive=False)
    return fake


@web
@pytest.mark.parametrize("query", ["", "?budget_ms=900", "?budget_ms=1", "?budget_ms=zzz",
                                   "?budget_ms=999999"])
def test_health_backends_parity(L, F, tmp_path, monkeypatch, query):
    text = (_TIERS
            + "  other:\n    base_url: http://other/v1\n    model: o\n    routing: {group: second, order: 1}\n")
    path = _write_registry(tmp_path, text)
    statuses = {"first": "unreachable", "second": "reachable", "third": "unknown", "other": "unknown"}
    got = []
    for mod in (L, F):
        calls: list = []
        monkeypatch.setattr(mod.server, "probe_entry", _probe_factory(mod.account, statuses, calls))
        out = asyncio.run(_drive(mod, _load(mod, path), _Upstream(), monkeypatch,
                                 [("get", "/health/backends" + query, None)]))
        status, hdrs, body = out[0]
        body = dict(body)
        body["checked_at"] = "<ts>"
        got.append((status, hdrs, body, sorted(calls)))
    assert got[0] == got[1]


# --------------------------------------------------------------------------
# Acceptance
# --------------------------------------------------------------------------

SPILL = [("loc-a", 1, 4), ("loc-b", 2, 1), ("cloud-c", 3, None)]
QUEUE = [("q-a", 1, 1), ("q-b", 2, 1)]


_GUARD = 15.0  # hang guard only; never a sequencing delay

# Burst sizes each group receives, in order (spill: fill 4+1, paid spill 4+1+2; queue:
# fill 1+1, overfull 1+1+1). A case that skips a leg only leaves a trailing entry unused.
_BURSTS = {"spill": [5, 7], "queue": [2, 3]}


class _FakeFrontdoor:
    """Fake front door whose completion barrier is causal, not timed.

    A burst is the next entry of _BURSTS for the request's group. No request of a burst
    completes until every request of that burst has ARRIVED (so tier choice depends only
    on caps, never on how the host spread the arrivals), and the held requests then
    finish in tier order (so a request queued behind a full tier lands deterministically).
    """

    def __init__(self, reject_when_full=False, wrong_answers=False,
                 unreachable=(), omit=()):
        self.groups = {"spill": SPILL, "queue": QUEUE}
        self.reject, self.wrong = reject_when_full, wrong_answers
        self.unreachable, self.omit = set(unreachable), set(omit)
        self.cond = threading.Condition()
        self.errors = []
        self.bursts = {g: [{"size": n, "arrived": 0, "holders": []} for n in sizes]
                       for g, sizes in _BURSTS.items()}
        self.cursor = {g: 0 for g in _BURSTS}
        self.in_flight = {d[0]: 0 for g in self.groups.values() for d in g}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload, headers=()):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in headers:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.startswith("/health/backends"):
                    groups = {}
                    for name, tiers in owner.groups.items():
                        deps = [{"id": t[0], "checked": "models-endpoint", "detail": "",
                                 "status": "unreachable" if t[0] in owner.unreachable else "reachable"}
                                for t in tiers if t[0] not in owner.omit]
                        groups[name] = {"status": "reachable", "deployments": deps}
                    return self._send(200, {"protocol": 1, "frontdoor_status": "ok",
                                            "checked_at": "x", "groups": groups})
                self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                tiers = sorted(owner.groups[body["model"]], key=lambda t: t[1])
                text = body["messages"][0]["content"]
                x, y = map(int, re.search(r"product of (\d+) and (\d+)", text).groups())
                with owner.cond:
                    burst = owner.bursts[body["model"]][owner.cursor[body["model"]]]
                    burst["arrived"] += 1
                    if burst["arrived"] == burst["size"]:
                        owner.cursor[body["model"]] += 1
                    owner.cond.notify_all()

                    def free():
                        return next((t for t in tiers
                                     if t[2] is None or owner.in_flight[t[0]] < t[2]), None)

                    if not owner.cond.wait_for(lambda: free() or owner.reject, _GUARD):
                        owner.errors.append("slot wait timed out")
                    pick = free()
                    if pick is None:
                        return self._send(503, {"error": "full"})
                    owner.in_flight[pick[0]] += 1
                    mine = (pick[1], pick[0])
                    burst["holders"].append(mine)
                    if not owner.cond.wait_for(
                            lambda: burst["arrived"] == burst["size"]
                            and not any(h < mine for h in burst["holders"]), _GUARD):
                        owner.errors.append("completion barrier timed out")
                answer = x * y + (1 if owner.wrong else 0)
                content = " ".join(str(i) for i in range(1, 201)) + "\nANSWER=%d" % answer
                with owner.cond:
                    owner.in_flight[pick[0]] -= 1
                    burst["holders"].remove(mine)
                    owner.cond.notify_all()
                self._send(200, {"choices": [{"message": {"content": content},
                                              "finish_reason": "stop"}]},
                           [("x-frontdoor-deployment", pick[0])])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _acc_registry(tmp_path, *, unmark=()):
    lines = ["models:"]
    for group, tiers in {"spill": SPILL, "queue": QUEUE}.items():
        for tid, order, cap in tiers:
            lines.append("  %s:" % tid)
            lines.append("    base_url: http://127.0.0.1:1/v1")
            lines.append("    model: %s-model" % tid)
            if not tid.startswith("cloud") and tid not in unmark:
                lines.append("    billing: {mode: unmetered}")
            routing = "group: %s, order: %d" % (group, order)
            if cap is not None:
                routing += ", max_parallel: %d" % cap
            lines.append("    routing: {%s}" % routing)
    path = tmp_path / "registry.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _verdict(mod, fake_kw, registry, run_kw):
    fake = _FakeFrontdoor(**fake_kw)
    try:
        def go():
            report = mod.acceptance.run_frontdoor_acceptance(
                fake.url, registry, spill_group="spill", queue_group="queue",
                request_timeout=20.0, **run_kw)
            data = json.loads(json.dumps(report.to_json()).replace(fake.url, "<url>"))
            # The tally dict is printed in response-arrival order, which is client-thread
            # scheduling and not a fake or library property; its COUNTS are pinned by the fake.
            for check in data["checks"]:
                check["detail"] = re.sub(
                    r"tally (\{.*\})",
                    lambda m: "tally %s" % sorted(ast.literal_eval(m.group(1)).items()),
                    check["detail"])
            return data, report.exit_code
        result = _outcome(go)
        assert not fake.errors, fake.errors
        return result
    finally:
        fake.close()


_ACC_CASES = [
    ("quick", {}, {}, {"quick": True}),
    ("full_no_paid", {}, {}, {}),
    ("paid", {}, {}, {"paid": True}),
    ("wrong_cap", {}, {}, {"quick": True, "expect_tier1_cap": 2}),
    ("negative_cap", {}, {}, {"expect_tier1_cap": -1}),
    ("unmarked_never_free", {}, {"unmark": ("loc-a",)}, {"quick": True}),
    ("unreachable_local", {"unreachable": ("loc-a",)}, {}, {"quick": True}),
    ("health_mismatch", {"omit": ("loc-b",)}, {}, {"quick": True}),
    ("queue_rejects", {"reject_when_full": True}, {}, {}),
    ("wrong_answer", {"wrong_answers": True}, {}, {"quick": True}),
]


@pytest.mark.parametrize("label,fake_kw,reg_kw,run_kw", _ACC_CASES, ids=[c[0] for c in _ACC_CASES])
def test_acceptance_verdict_parity(L, F, tmp_path, label, fake_kw, reg_kw, run_kw):
    registry = _acc_registry(tmp_path, **reg_kw)
    with ThreadPoolExecutor(max_workers=2) as pool:  # independent fakes; halves wall time
        fl = pool.submit(_verdict, L, fake_kw, registry, run_kw)
        ff = pool.submit(_verdict, F, fake_kw, registry, run_kw)
        legacy, final = fl.result(), ff.result()
    assert legacy == final
    assert (legacy[0] == "err") == (label == "negative_cap")


def test_acceptance_config_errors_parity(L, F, tmp_path):
    registry = _acc_registry(tmp_path)
    cases = [
        ("http://127.0.0.1:1", str(tmp_path / "nope.yaml"), "spill", "queue"),
        ("http://127.0.0.1:1", registry, "nogroup", "queue"),
        ("http://127.0.0.1:1", registry, "spill", "spill"),  # uncapped tier in the queue group
        ("not a url", registry, "spill", "queue"),
    ]
    for url, reg, sg, qg in cases:
        res = [
            _outcome(mod.acceptance.run_frontdoor_acceptance, url, reg,
                     spill_group=sg, queue_group=qg, request_timeout=1.0)
            for mod in (L, F)
        ]
        assert res[0][0] == "err" and res[0] == res[1], (url, reg, sg, qg)


def test_acceptance_unreachable_frontdoor_parity(L, F, tmp_path):
    registry = _acc_registry(tmp_path)
    res = []
    for mod in (L, F):
        rep = mod.acceptance.run_frontdoor_acceptance(
            "http://127.0.0.1:1", registry, spill_group="spill", queue_group="queue",
            quick=True, request_timeout=2.0)
        res.append((rep.to_json(), rep.exit_code))
    assert res[0] == res[1] and res[0][1] != 0


def test_acceptance_planning_helpers_parity(L, F, tmp_path):
    registry = _acc_registry(tmp_path, unmark=("loc-b",))
    for group in ("spill", "queue"):
        lv = [mod.acceptance.load_levels(registry, group) for mod in (L, F)]
        assert repr(lv[0]) == repr(lv[1])
        for fn in ("plan_fill", "plan_paid_spill"):
            r = [getattr(mod.acceptance, fn)(mod.acceptance.load_levels(registry, group)[0])
                 for mod in (L, F)]
            assert repr(r[0]) == repr(r[1]), (fn, group)


def test_real_access_log_untouched():
    """Runs last in this module: the real front-door access log must not change."""
    assert _stat_sig(_WATCHED_LOG) == _REAL_LOG_AT_START
