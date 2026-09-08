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
