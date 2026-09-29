"""Focused front-door contract tests."""
from __future__ import annotations

import json
import asyncio
from pathlib import Path
from typing import Any

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from llm_scripting_kit.frontdoor import create_app, main
from llm_scripting_kit.model_endpoints import load_endpoint_registry


def _registry(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(
        "models:\n  qwen:\n    base_url: http://up/v1\n    model: qwen-real\n"
        "    routing: {group: qwen, order: 1, effort_style: ninfer}\n",
        encoding="utf-8",
    )
    return load_endpoint_registry({"MODEL_ENDPOINTS_REGISTRY": str(path)})


def _registry_from_text(tmp_path: Path, text: str):
    path = tmp_path / "models.yaml"
    path.write_text(text, encoding="utf-8")
    return load_endpoint_registry({"MODEL_ENDPOINTS_REGISTRY": str(path)})


def _group_registry(tmp_path: Path, deployments: str):
    return _registry_from_text(
        tmp_path,
        "models:\n" + deployments,
    )


def _deployment_yaml(
    name: str, order: int, max_parallel: str, *, group: str = "qwen"
) -> str:
    return (
        f"  {name}:\n"
        f"    base_url: http://{name}/v1\n"
        f"    model: {name}-model\n"
        f"    routing: {{group: {group}, order: {order}, "
        f"max_parallel: {max_parallel}}}\n"
    )


class _FakeUpstream:
    def __init__(self, release: asyncio.Event, *, failures: dict[str, int] | None = None):
        self.release = release
        self.failures = failures or {}
        self.calls: list[str] = []

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]):
        deployment = url.split("//", 1)[1].split("/", 1)[0]
        self.calls.append(deployment)
        if self.failures.get(deployment, 0):
            self.failures[deployment] -= 1
            raise httpx.ConnectError("connection failed")
        await self.release.wait()
        return httpx.Response(
            200,
            json={"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
        )

    async def aclose(self) -> None:
        pass


async def _wait_for_in_flight(client, expected: dict[str, int]) -> dict[str, Any]:
    for _ in range(100):
        who = (await client.get("/who")).json()
        observed = {item["id"]: item["in_flight"] for item in who["deployments"]}
        if all(observed.get(name) == count for name, count in expected.items()):
            return who
        await asyncio.sleep(0.01)
    pytest.fail(f"requests did not reach expected in-flight counts: {expected}")


def test_frontdoor_strips_user_normalizes_effort_and_sets_header(tmp_path, monkeypatch):
    asyncio.run(_test_frontdoor_strips_user_normalizes_effort_and_sets_header(tmp_path, monkeypatch))


async def _test_frontdoor_strips_user_normalizes_effort_and_sets_header(tmp_path, monkeypatch):
    seen = {}

    class FakeClient:
        async def post(self, url, *, json, headers):
            seen.update(json)
            return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 1}})

        async def aclose(self):
            pass

    asgi_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(_registry(tmp_path))), base_url="http://frontdoor")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    response = await asgi_client.post(
        "/v1/chat/completions",
        json={"model": "qwen", "messages": [], "user": "u1", "reasoning_effort": "high"},
    )
    await asgi_client.aclose()
    assert response.status_code == 200
    assert response.headers["x-frontdoor-deployment"] == "qwen"
    assert "user" not in seen
    assert seen["model"] == "qwen-real"
    assert seen["reasoning_effort"] == "xhigh"


def test_frontdoor_reads_registry_key_file_and_sets_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _test_frontdoor_reads_registry_key_file_and_sets_authorization(
            tmp_path, monkeypatch
        )
    )


async def _test_frontdoor_reads_registry_key_file_and_sets_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = tmp_path / "frontdoor-key.txt"
    key_file.write_text("test-frontdoor-key", encoding="utf-8")
    registry_path = tmp_path / "models.yaml"
    registry_path.write_text(
        "models:\n"
        "  openrouter-tier:\n"
        "    base_url: https://vendor.invalid/v1\n"
        "    model: vendor/model\n"
        "    key_env: FRONTDOOR_TEST_API_KEY\n"
        "    key_file: ~/frontdoor-key.txt\n"
        "    routing: {group: routed, order: 1}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(registry_path))
    monkeypatch.delenv("FRONTDOOR_TEST_API_KEY", raising=False)
    monkeypatch.setattr(
        "llm_scripting_kit.api_key.USER_ENV_FILE", tmp_path / "missing.env"
    )
    monkeypatch.setattr("llm_scripting_kit.frontdoor.server._KEY_CACHE", {})
    registry = load_endpoint_registry()
    seen_headers: dict[str, str] = {}

    class FakeClient:
        async def post(
            self, url: str, *, json: dict[str, Any], headers: dict[str, str]
        ) -> httpx.Response:
            seen_headers.update(headers)
            return httpx.Response(200, json={"choices": [], "usage": {}})

        async def aclose(self) -> None:
            pass

    app = create_app(registry)
    asgi_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://frontdoor"
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    response = await asgi_client.post(
        "/v1/chat/completions", json={"model": "routed", "messages": []}
    )
    await asgi_client.aclose()

    assert response.status_code == 200
    assert seen_headers["Authorization"] == "Bearer test-frontdoor-key"


def test_frontdoor_health_and_who_shapes(tmp_path):
    asyncio.run(_test_frontdoor_health_and_who_shapes(tmp_path))


async def _test_frontdoor_health_and_who_shapes(tmp_path):
    app = create_app(_registry(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://frontdoor") as client:
        health = (await client.get("/health")).json()
        who = (await client.get("/who")).json()
    assert health["status"] == "ok"
    assert health["deployments"][0]["in_flight"] == 0
    assert "users" in who["deployments"][0]


def test_fill_first(tmp_path, monkeypatch):
    asyncio.run(_test_fill_first(tmp_path, monkeypatch))


async def _test_fill_first(tmp_path, monkeypatch):
    registry = _group_registry(
        tmp_path,
        _deployment_yaml("tier1", 1, "2")
        + _deployment_yaml("tier2", 2, "1")
        + _deployment_yaml("tier3", 3, "null"),
    )
    release = asyncio.Event()
    fake = _FakeUpstream(release)
    app = create_app(registry)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://frontdoor") as client:
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
        tasks = [asyncio.create_task(client.post("/v1/chat/completions", json={"model": "qwen", "user": f"u{i}"})) for i in range(5)]
        who = await _wait_for_in_flight(client, {"tier1": 2, "tier2": 1, "tier3": 2})
        assert sorted(user for item in who["deployments"] for user in item["users"]) == [f"u{i}" for i in range(5)]
        release.set()
        responses = await asyncio.gather(*tasks)
    assert sorted(response.headers["x-frontdoor-deployment"] for response in responses) == ["tier1", "tier1", "tier2", "tier3", "tier3"]


def test_release_and_refill(tmp_path, monkeypatch):
    asyncio.run(_test_release_and_refill(tmp_path, monkeypatch))


async def _test_release_and_refill(tmp_path, monkeypatch):
    registry = _group_registry(tmp_path, _deployment_yaml("tier1", 1, "1") + _deployment_yaml("tier2", 2, "null"))
    first_release = asyncio.Event()
    fake = _FakeUpstream(first_release)
    app = create_app(registry)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://frontdoor") as client:
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
        first = asyncio.create_task(client.post("/v1/chat/completions", json={"model": "qwen", "user": "first"}))
        await _wait_for_in_flight(client, {"tier1": 1, "tier2": 0})
        second = asyncio.create_task(client.post("/v1/chat/completions", json={"model": "qwen", "user": "second"}))
        await _wait_for_in_flight(client, {"tier1": 1, "tier2": 1})
        first_release.set()
        first_response = await first
        second_response = await second
        third = await client.post("/v1/chat/completions", json={"model": "qwen", "user": "third"})
    assert first_response.headers["x-frontdoor-deployment"] == "tier1"
    assert second_response.headers["x-frontdoor-deployment"] == "tier2"
    assert third.headers["x-frontdoor-deployment"] == "tier1"


def test_waits_when_all_capped(tmp_path, monkeypatch):
    asyncio.run(_test_waits_when_all_capped(tmp_path, monkeypatch))


async def _test_waits_when_all_capped(tmp_path, monkeypatch):
    registry = _group_registry(tmp_path, _deployment_yaml("tier1", 1, "1"))
    release = asyncio.Event()
    fake = _FakeUpstream(release)
    app = create_app(registry)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://frontdoor") as client:
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
        first = asyncio.create_task(client.post("/v1/chat/completions", json={"model": "qwen", "user": "first"}))
        await _wait_for_in_flight(client, {"tier1": 1})
        second = asyncio.create_task(client.post("/v1/chat/completions", json={"model": "qwen", "user": "second"}))
        await asyncio.sleep(0.05)
        assert not second.done()
        release.set()
        await first
        response = await second
    assert response.headers["x-frontdoor-deployment"] == "tier1"


def test_retry_on_connection_error(tmp_path, monkeypatch):
    asyncio.run(_test_retry_on_connection_error(tmp_path, monkeypatch))


async def _test_retry_on_connection_error(tmp_path, monkeypatch):
    registry = _group_registry(tmp_path, _deployment_yaml("tier1", 1, "1") + _deployment_yaml("tier2", 2, "null"))
    release = asyncio.Event()
    release.set()
    fake = _FakeUpstream(release, failures={"tier1": 1})
    app = create_app(registry)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://frontdoor") as client:
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
        response = await client.post("/v1/chat/completions", json={"model": "qwen", "user": "u1"})
        health = (await client.get("/health")).json()
    tier1 = next(item for item in health["deployments"] if item["id"] == "tier1")
    assert response.headers["x-frontdoor-deployment"] == "tier2"
    assert tier1["last_error"]
    assert tier1["in_flight"] == 0


def test_all_failed_502(tmp_path, monkeypatch):
    asyncio.run(_test_all_failed_502(tmp_path, monkeypatch))


async def _test_all_failed_502(tmp_path, monkeypatch):
    registry = _group_registry(tmp_path, _deployment_yaml("tier1", 1, "1") + _deployment_yaml("tier2", 2, "null"))
    fake = _FakeUpstream(asyncio.Event(), failures={"tier1": 1, "tier2": 1})
    app = create_app(registry)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://frontdoor") as client:
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
        response = await client.post("/v1/chat/completions", json={"model": "qwen"})
    assert response.status_code == 502
    assert response.json()["error"]["message"]


def test_access_log(tmp_path, monkeypatch):
    asyncio.run(_test_access_log(tmp_path, monkeypatch))


async def _test_access_log(tmp_path, monkeypatch):
    release = asyncio.Event()
    release.set()
    fake = _FakeUpstream(release)
    app = create_app(_registry(tmp_path), access_log=tmp_path / "a.jsonl")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://frontdoor") as client:
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
        await client.post("/v1/chat/completions", json={"model": "qwen", "user": "u1"})
    record = json.loads((tmp_path / "a.jsonl").read_text(encoding="utf-8"))
    assert {"ts", "user", "group", "deployment", "status", "latency_ms", "prompt_tokens", "completion_tokens", "stream", "error"} <= record.keys()
    assert record["user"] == "u1"


def test_check_lists_untagged(tmp_path, capsys):
    path = tmp_path / "models.yaml"
    path.write_text("models:\n" + _deployment_yaml("routed", 1, "null") + "  untagged:\n    base_url: http://untagged/v1\n    model: untagged-model\n", encoding="utf-8")
    assert main(["--check", "--registry", str(path)]) == 0
    output = capsys.readouterr().out
    assert "untagged transport entries:\n  untagged" in output
    assert "qwen:" in output and "routed" in output


def test_spill_after_moves_on_to_the_next_tier(tmp_path, monkeypatch):
    asyncio.run(_test_spill_after_moves_on_to_the_next_tier(tmp_path, monkeypatch))


async def _test_spill_after_moves_on_to_the_next_tier(tmp_path, monkeypatch):
    """With spill_after > 0 and tier 1 held at its cap, a second request waits
    the bounded interval and then lands on tier 2 -- it must not re-wait on
    tier 1 forever."""
    path = tmp_path / "models.yaml"
    path.write_text(
        "models:\n"
        "  t1:\n    base_url: http://t1/v1\n    model: m1\n"
        "    routing: {group: g, order: 1, max_parallel: 1}\n"
        "  t2:\n    base_url: http://t2/v1\n    model: m2\n"
        "    routing: {group: g, order: 2}\n",
        encoding="utf-8",
    )
    registry = load_endpoint_registry({"MODEL_ENDPOINTS_REGISTRY": str(path)})
    hold = asyncio.Event()

    class FakeClient:
        async def post(self, url, *, json, headers):
            if url.startswith("http://t1/"):
                await hold.wait()
            return httpx.Response(200, json={"choices": [], "usage": {}})

        async def aclose(self):
            pass

    app = create_app(registry, spill_after=0.05, access_log=tmp_path / "a.jsonl")
    asgi = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fd")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    async with asgi as client:
        first = asyncio.create_task(client.post("/v1/chat/completions", json={"model": "g", "messages": []}))
        await asyncio.sleep(0.01)
        second = await asyncio.wait_for(
            client.post("/v1/chat/completions", json={"model": "g", "messages": []}), timeout=2
        )
        assert second.headers["x-frontdoor-deployment"] == "t2"
        hold.set()
        assert (await first).headers["x-frontdoor-deployment"] == "t1"


# ---------------------------------------------------------------------------
# Migration step 3 (L6, O4): a front-door group is ONE declaration id
# ---------------------------------------------------------------------------


def test_a_front_door_group_is_one_declaration_id(tmp_path):
    from llm_scripting_kit.declaration import describe
    from llm_scripting_kit.reachability import Reachability

    registry = _registry_from_text(
        tmp_path,
        "models:\n"
        "  qwen38:\n"
        "    base_url: http://frontdoor.invalid/v1\n"
        "    model: qwen38\n"
        + _deployment_yaml("box-a", 1, "2", group="qwen38")
        + _deployment_yaml("box-b", 2, "null", group="qwen38"),
    )
    ranking = describe(
        ["qwen38"], caller="process", entries=registry.entries,
        reachability_cache={"qwen38": Reachability("reachable", "models-endpoint", "ok")},
    )
    # The group is not expanded into its deployments: in-group failover stays
    # inside the front door, and selection sees one entry.
    assert [e.id for e in ranking.rendered_entries] == ["qwen38"]
    assert [d.id for d in ranking.dispositions] == ["qwen38"]


# ---------------------------------------------------------------------------
# Reported cost: the front door decides trust (U2, amendment R1)
# ---------------------------------------------------------------------------


_REAL_ASYNC_CLIENT = httpx.AsyncClient  # before any test patches it


def _billing_registry(tmp_path: Path, billing: str):
    line = f"    billing: {{mode: {billing}}}\n" if billing else ""
    return _registry_from_text(
        tmp_path,
        "models:\n  qwen:\n    base_url: http://up/v1\n    model: qwen-real\n"
        "    routing: {group: qwen, order: 1}\n" + line,
    )


def _run_cost(tmp_path, monkeypatch, billing, upstream_usage):
    tmp_path.mkdir(parents=True, exist_ok=True)
    real = _REAL_ASYNC_CLIENT

    async def go():
        class FakeClient:
            async def post(self, url, *, json, headers):
                body = {"choices": []}
                if upstream_usage is not None:
                    body["usage"] = dict(upstream_usage)
                return httpx.Response(200, json=body)

            async def aclose(self):
                pass

        log = tmp_path / "access.jsonl"
        app = create_app(_billing_registry(tmp_path, billing), access_log=log)
        async with real(
            transport=httpx.ASGITransport(app=app), base_url="http://frontdoor"
        ) as client:
            monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
            response = await client.post(
                "/v1/chat/completions", json={"model": "qwen", "user": "u"}
            )
        record = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        return response.json(), record

    return asyncio.run(go())


def test_frontdoor_injects_explicit_unmetered_zero_with_source(tmp_path, monkeypatch):
    body, record = _run_cost(
        tmp_path, monkeypatch, "unmetered", {"prompt_tokens": 3, "completion_tokens": 2}
    )
    assert body["usage"]["cost"] == 0.0
    assert body["usage"]["cost_source"] == "registry-unmetered"
    assert body["usage"]["prompt_tokens"] == 3  # existing usage untouched
    assert record["reported_cost_usd"] == 0.0
    assert record["reported_cost_source"] == "registry-unmetered"


def test_frontdoor_strips_upstream_cost_unless_provider_reported(tmp_path, monkeypatch):
    spoof = {"prompt_tokens": 3, "cost": 9.5, "cost_source": "registry-unmetered"}
    # no billing declared: the upstream claim is stripped, nothing injected
    body, record = _run_cost(tmp_path, monkeypatch, "", spoof)
    assert "cost" not in body["usage"] and "cost_source" not in body["usage"]
    assert "reported_cost_usd" not in record
    # unmetered: the upstream claim is replaced by the registry's explicit zero
    body, record = _run_cost(tmp_path / "b", monkeypatch, "unmetered", spoof)
    assert body["usage"]["cost"] == 0.0
    assert body["usage"]["cost_source"] == "registry-unmetered"
    # provider-reported: the upstream cost passes through unchanged
    body, record = _run_cost(tmp_path / "c", monkeypatch, "provider-reported", spoof)
    assert body["usage"]["cost"] == 9.5
    assert "cost_source" not in body["usage"]  # upstream cannot pick the source label
    assert record["reported_cost_usd"] == 9.5
    assert record["reported_cost_source"] == "provider"


def test_frontdoor_ignores_invalid_provider_cost_for_the_log(tmp_path, monkeypatch):
    body, record = _run_cost(
        tmp_path, monkeypatch, "provider-reported", {"prompt_tokens": 3, "cost": -1}
    )
    assert "reported_cost_usd" not in record


# ---------------------------------------------------------------------------
# /health/backends (U3)
# ---------------------------------------------------------------------------


def _fake_probe_factory(statuses: dict, calls: list):
    from llm_scripting_kit.account import EndpointProbe

    def fake(entry, *, timeout, key_resolver=None, project_root=None):
        calls.append((entry.id, entry.base_url, timeout))
        status = statuses[entry.id]
        if status == "reachable":
            return EndpointProbe(True, entry.id, entry.base_url, "ok")
        if status == "unreachable":
            return EndpointProbe(False, entry.id, entry.base_url, "unreachable: refused")
        return EndpointProbe(False, entry.id, entry.base_url, "no API key resolved", decisive=False)

    return fake


def _health_registry(tmp_path: Path):
    return _group_registry(
        tmp_path,
        _deployment_yaml("local", 1, "2")
        + _deployment_yaml("paid", 2, "null")
        + _deployment_yaml("other", 1, "null", group="second"),
    )


def _get_health(app, query: str = ""):
    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://frontdoor"
        ) as client:
            return (await client.get("/health/backends" + query)).json()

    return asyncio.run(go())


def test_health_backends_uses_supplied_registry_and_reports_each_deployment(
    tmp_path, monkeypatch
):
    from llm_scripting_kit.frontdoor import server as server_mod

    # A DIFFERENT registry on disk: the route must not reload from it.
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    decoy_path = decoy / "models.yaml"
    decoy_path.write_text(
        "models:\n  decoy:\n    base_url: http://decoy/v1\n    model: d\n"
        "    routing: {group: qwen, order: 1}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(decoy_path))
    registry = _health_registry(tmp_path)
    calls: list = []
    monkeypatch.setattr(
        server_mod, "probe_entry",
        _fake_probe_factory({"local": "unreachable", "paid": "reachable", "other": "unknown"}, calls),
    )
    body = _get_health(create_app(registry), "?budget_ms=900")
    assert body["protocol"] == 1 and body["frontdoor_status"] == "ok"
    assert body["checked_at"]
    qwen = body["groups"]["qwen"]
    assert qwen["status"] == "reachable"  # any reachable deployment
    by_id = {d["id"]: d for d in qwen["deployments"]}
    assert by_id["local"]["status"] == "unreachable"
    assert by_id["paid"]["status"] == "reachable"
    assert all(d["checked"] == "models-endpoint" for d in qwen["deployments"])
    assert body["groups"]["second"]["status"] == "unknown"
    # entries came from the supplied registry, never the decoy, with an
    # explicit timeout on every probe (never the 2 s default implicitly)
    assert sorted(c[0] for c in calls) == ["local", "other", "paid"]
    assert {c[2] for c in calls} == {0.9}
    # no credential or key text leaks into the report
    assert "Bearer" not in json.dumps(body)


def test_health_backends_group_aggregation(tmp_path, monkeypatch):
    from llm_scripting_kit.frontdoor import server as server_mod

    registry = _health_registry(tmp_path)
    for statuses, qwen in (
        ({"local": "unreachable", "paid": "unreachable", "other": "unreachable"}, "unreachable"),
        ({"local": "unreachable", "paid": "unknown", "other": "reachable"}, "unknown"),
        ({"local": "reachable", "paid": "unknown", "other": "reachable"}, "reachable"),
    ):
        monkeypatch.setattr(server_mod, "probe_entry", _fake_probe_factory(statuses, []))
        assert _get_health(create_app(registry))["groups"]["qwen"]["status"] == qwen


def test_health_endpoint_is_unchanged_liveness(tmp_path):
    body = asyncio.run(_liveness(tmp_path))
    assert body["status"] == "ok" and "groups" not in body


async def _liveness(tmp_path):
    app = create_app(_health_registry(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://frontdoor") as client:
        return (await client.get("/health")).json()


def test_health_backends_budget_ms_is_clamped_and_defaulted(tmp_path, monkeypatch):
    from llm_scripting_kit.frontdoor import server as server_mod

    registry = _health_registry(tmp_path)
    seen: list = []
    monkeypatch.setattr(
        server_mod, "probe_entry",
        _fake_probe_factory({"local": "reachable", "paid": "reachable", "other": "reachable"}, seen),
    )
    app = create_app(registry)
    cases = {
        "?budget_ms=750": 0.75,
        "?budget_ms=4000": 4.0,
        "?budget_ms=999999": 4.0,  # clamped to the documented maximum
        "": 1.5,  # missing -> server default
        "?budget_ms=abc": 1.5,
        "?budget_ms=1.5": 1.5,
        "?budget_ms=0": 1.5,
        "?budget_ms=-20": 1.5,
    }
    for query, expected in cases.items():
        seen.clear()
        _get_health(app, query)
        assert {c[2] for c in seen} == {expected}, query
    assert server_mod.BACKEND_BUDGET_MAX_MS == 4000
    assert server_mod.BACKEND_BUDGET_DEFAULT_MS == 1500


def test_frontdoor_strips_null_upstream_cost_keys(tmp_path, monkeypatch):
    usage = {"prompt_tokens": 3, "cost": None, "cost_source": None}
    body, _record = _run_cost(tmp_path, monkeypatch, "", usage)
    assert "cost" not in body["usage"] and "cost_source" not in body["usage"]
    assert body["usage"]["prompt_tokens"] == 3


# ---------------------------------------------------------------------------
# _normalize_body characterization: the effort translation is byte-identical
# across the shared-vocabulary refactor. Each expected body is the literal
# output of the pre-refactor implementation, KEY ORDER INCLUDED (the upstream
# request is ``json.dumps`` of this dict, so order is part of the wire).
# ---------------------------------------------------------------------------

_NB_BODIES = {
    "none": {"model": "g", "messages": [], "user": "u1"},
    "top_high": {"model": "g", "messages": [], "reasoning_effort": "high"},
    "top_medium": {"model": "g", "messages": [], "reasoning_effort": "medium"},
    "top_null": {"model": "g", "messages": [], "reasoning_effort": None},
    "nested_high_extra": {
        "model": "g", "messages": [],
        "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "high"},
    },
    "nested_only": {"model": "g", "messages": [], "chat_template_kwargs": {"reasoning_effort": "low"}},
    "both": {
        "model": "g", "messages": [], "reasoning_effort": "low",
        "chat_template_kwargs": {"reasoning_effort": "high", "x": 1},
    },
    "top_null_nested_high": {
        "model": "g", "messages": [], "reasoning_effort": None,
        "chat_template_kwargs": {"reasoning_effort": "high"},
    },
    "ctk_not_dict": {"model": "g", "messages": [], "reasoning_effort": "high", "chat_template_kwargs": "raw"},
    "ctk_other_only": {
        "model": "g", "messages": [], "reasoning_effort": "high",
        "chat_template_kwargs": {"enable_thinking": False},
    },
}

_NB_GOLDEN = [
    ("top-level", "none", {"model": "real", "messages": []}, "u1"),
    ("top-level", "top_high", {"model": "real", "messages": [], "reasoning_effort": "high"}, None),
    ("top-level", "top_medium", {"model": "real", "messages": [], "reasoning_effort": "medium"}, None),
    ("top-level", "top_null", {"model": "real", "messages": []}, None),
    ("top-level", "nested_high_extra", {"model": "real", "messages": [], "chat_template_kwargs": {"enable_thinking": True}, "reasoning_effort": "high"}, None),
    ("top-level", "nested_only", {"model": "real", "messages": [], "chat_template_kwargs": {}, "reasoning_effort": "low"}, None),
    ("top-level", "both", {"model": "real", "messages": [], "chat_template_kwargs": {"reasoning_effort": "high", "x": 1}, "reasoning_effort": "low"}, None),
    ("top-level", "top_null_nested_high", {"model": "real", "messages": [], "chat_template_kwargs": {}, "reasoning_effort": "high"}, None),
    ("top-level", "ctk_not_dict", {"model": "real", "messages": [], "chat_template_kwargs": "raw", "reasoning_effort": "high"}, None),
    ("top-level", "ctk_other_only", {"model": "real", "messages": [], "chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "high"}, None),
    ("ninfer", "none", {"model": "real", "messages": []}, "u1"),
    ("ninfer", "top_high", {"model": "real", "messages": [], "reasoning_effort": "xhigh"}, None),
    ("ninfer", "top_medium", {"model": "real", "messages": [], "reasoning_effort": "medium"}, None),
    ("ninfer", "top_null", {"model": "real", "messages": []}, None),
    ("ninfer", "nested_high_extra", {"model": "real", "messages": [], "chat_template_kwargs": {"enable_thinking": True}, "reasoning_effort": "xhigh"}, None),
    ("ninfer", "nested_only", {"model": "real", "messages": [], "chat_template_kwargs": {}, "reasoning_effort": "low"}, None),
    ("ninfer", "both", {"model": "real", "messages": [], "chat_template_kwargs": {"reasoning_effort": "high", "x": 1}, "reasoning_effort": "low"}, None),
    ("ninfer", "top_null_nested_high", {"model": "real", "messages": [], "chat_template_kwargs": {}, "reasoning_effort": "xhigh"}, None),
    ("ninfer", "ctk_not_dict", {"model": "real", "messages": [], "chat_template_kwargs": "raw", "reasoning_effort": "xhigh"}, None),
    ("ninfer", "ctk_other_only", {"model": "real", "messages": [], "chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "xhigh"}, None),
    ("chat_template_kwargs", "none", {"model": "real", "messages": []}, "u1"),
    ("chat_template_kwargs", "top_high", {"model": "real", "messages": [], "chat_template_kwargs": {"reasoning_effort": "high"}}, None),
    ("chat_template_kwargs", "top_medium", {"model": "real", "messages": [], "chat_template_kwargs": {"reasoning_effort": "medium"}}, None),
    ("chat_template_kwargs", "top_null", {"model": "real", "messages": []}, None),
    ("chat_template_kwargs", "nested_high_extra", {"model": "real", "messages": [], "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "high"}}, None),
    ("chat_template_kwargs", "nested_only", {"model": "real", "messages": [], "chat_template_kwargs": {"reasoning_effort": "low"}}, None),
    ("chat_template_kwargs", "both", {"model": "real", "messages": [], "chat_template_kwargs": {"reasoning_effort": "low", "x": 1}}, None),
    ("chat_template_kwargs", "top_null_nested_high", {"model": "real", "messages": [], "chat_template_kwargs": {"reasoning_effort": "high"}}, None),
    ("chat_template_kwargs", "ctk_not_dict", {"model": "real", "messages": [], "chat_template_kwargs": {"reasoning_effort": "high"}}, None),
    ("chat_template_kwargs", "ctk_other_only", {"model": "real", "messages": [], "chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": "high"}}, None),
    (None, "none", {"model": "real", "messages": []}, "u1"),
    (None, "top_high", {"model": "real", "messages": []}, None),
    (None, "top_medium", {"model": "real", "messages": []}, None),
    (None, "top_null", {"model": "real", "messages": []}, None),
    (None, "nested_high_extra", {"model": "real", "messages": [], "chat_template_kwargs": {"enable_thinking": True}}, None),
    (None, "nested_only", {"model": "real", "messages": [], "chat_template_kwargs": {}}, None),
    (None, "both", {"model": "real", "messages": [], "chat_template_kwargs": {"reasoning_effort": "high", "x": 1}}, None),
    (None, "top_null_nested_high", {"model": "real", "messages": [], "chat_template_kwargs": {}}, None),
    (None, "ctk_not_dict", {"model": "real", "messages": [], "chat_template_kwargs": "raw"}, None),
    (None, "ctk_other_only", {"model": "real", "messages": [], "chat_template_kwargs": {"enable_thinking": False}}, None),
]


@pytest.mark.parametrize(
    "style,body_name,expected,expected_user",
    _NB_GOLDEN,
    ids=[f"{row[0]}-{row[1]}" for row in _NB_GOLDEN],
)
def test_normalize_body_effort_translation_is_byte_identical(
    style, body_name, expected, expected_user
) -> None:
    """``style`` None is a transport entry with no ``routing:`` block."""
    import copy

    from llm_scripting_kit.frontdoor.server import _normalize_body
    from llm_scripting_kit.model_endpoints import EndpointEntry, RoutingConfig

    routing = RoutingConfig(group="g", effort_style=style) if style else None
    entry = EndpointEntry(id="d", base_url="http://d/v1", model="real", routing=routing)
    body = copy.deepcopy(_NB_BODIES[body_name])
    before = copy.deepcopy(body)
    forwarded, user = _normalize_body(body, entry)
    assert json.dumps(forwarded) == json.dumps(expected)
    assert user == expected_user
    assert body == before  # the inbound body is never mutated


def _deployment(tmp_path: Path, extra: str):
    registry = _registry_from_text(
        tmp_path,
        "models:\n  d:\n    base_url: http://d/v1\n    model: real\n" + extra,
    )
    return registry.entries["d"]


@pytest.mark.parametrize(
    "extra,inbound,expected",
    [
        # Spillover to a routing deployment that declares no style: top-level,
        # exactly as before the entry-level style existed.
        ("    routing: {group: g, order: 3}\n", {"reasoning_effort": "high"}, {"reasoning_effort": "high"}),
        # The entry-level style overrides the routing style.
        (
            "    effort_style: chat_template_kwargs\n    routing: {group: g, effort_style: ninfer}\n",
            {"reasoning_effort": "high"},
            {"chat_template_kwargs": {"reasoning_effort": "high"}},
        ),
        # `unsupported` strips the effort from either inbound channel.
        ("    routing: {group: g, effort_style: unsupported}\n", {"reasoning_effort": "high"}, {}),
        (
            "    effort_style: unsupported\n    routing: {group: g}\n",
            {"chat_template_kwargs": {"reasoning_effort": "low", "x": 1}},
            {"chat_template_kwargs": {"x": 1}},
        ),
        # A declared-invalid routing style guesses no wire format.
        ("    routing: {group: g, effort_style: nope}\n", {"reasoning_effort": "high"}, {}),
        # A nested inbound effort is relocated to the deployment's style.
        (
            "    routing: {group: g, effort_style: ninfer}\n",
            {"chat_template_kwargs": {"reasoning_effort": "high"}},
            {"chat_template_kwargs": {}, "reasoning_effort": "xhigh"},
        ),
    ],
    ids=["routing-no-style", "endpoint-overrides-routing", "unsupported-top", "unsupported-nested",
         "invalid-routing", "nested-relocated"],
)
def test_normalize_body_uses_the_deployment_effort_style(tmp_path, extra, inbound, expected):
    from llm_scripting_kit.frontdoor.server import _normalize_body

    entry = _deployment(tmp_path, extra)
    forwarded, _user = _normalize_body({"model": "g", **inbound}, entry)
    assert forwarded == {"model": "real", **expected}
