"""Tests for the front-door acceptance library and CLI verb.

A stdlib fake front door serves ``/health/backends`` and a fill-first, queueing
``/v1/chat/completions`` with the ``x-frontdoor-deployment`` header. Registry
files use neutral ids. A tier is paid unless the registry declares
``billing.mode: unmetered``.
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llm_scripting_kit import cli
from llm_scripting_kit.frontdoor.acceptance import AcceptanceConfigError, run_frontdoor_acceptance

SPILL = [("loc-a", 1, 4), ("loc-b", 2, 1), ("cloud-c", 3, None)]
QUEUE = [("q-a", 1, 1), ("q-b", 2, 1)]


class FakeFrontdoor:
    def __init__(self, groups=None, hold=0.4, reject_when_full=False, wrong_answers=False,
                 unreachable=(), omit=()):
        self.groups = groups or {"spill": SPILL, "queue": QUEUE}
        self.hold, self.reject, self.wrong = hold, reject_when_full, wrong_answers
        self.unreachable, self.omit = set(unreachable), set(omit)
        self.cond = threading.Condition()
        self.last_arrival = time.monotonic()
        self.in_flight = {d[0]: 0 for g in self.groups.values() for d in g}
        self.calls = {d[0]: 0 for g in self.groups.values() for d in g}
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
                    owner.last_arrival = time.monotonic()
                    while True:
                        pick = next((t for t in tiers
                                     if t[2] is None or owner.in_flight[t[0]] < t[2]), None)
                        if pick or owner.reject:
                            break
                        owner.cond.wait(0.02)
                    if pick is None:
                        return self._send(503, {"error": "full"})
                    owner.in_flight[pick[0]] += 1
                    owner.calls[pick[0]] += 1
                # Keep the slot until arrivals have been quiet for 3x `hold`: a fixed
                # sleep lets a slow-to-connect request in a loaded run find an
                # earlier one already released, changing which tier serves it.
                time.sleep(owner.hold)
                while time.monotonic() - owner.last_arrival < 3 * owner.hold:
                    time.sleep(0.02)
                answer = x * y + (1 if owner.wrong else 0)
                content = " ".join(str(i) for i in range(1, 201)) + "\nANSWER=%d" % answer
                with owner.cond:
                    owner.in_flight[pick[0]] -= 1
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


def _registry(tmp_path, url, *, unmark=()):
    lines = ["models:"]
    for group, tiers in {"spill": SPILL, "queue": QUEUE}.items():
        for tid, order, cap in tiers:
            lines.append("  %s:" % tid)
            lines.append("    base_url: %s/v1" % url)
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


@pytest.fixture
def frontdoor():
    made = []

    def make(**kw):
        fake = FakeFrontdoor(**kw)
        made.append(fake)
        return fake

    yield make
    for fake in made:
        fake.close()


def _run(fake, registry, **kw):
    return run_frontdoor_acceptance(
        fake.url, registry, spill_group="spill", queue_group="queue",
        request_timeout=20.0, **kw)


def _failed(report):
    return [c.name for c in report.checks if not c.passed and not c.skipped]


def test_frontdoor_acceptance_quick_passes_without_paid_spend(frontdoor, tmp_path):
    fake = frontdoor()
    report = _run(fake, _registry(tmp_path, fake.url), quick=True)
    assert _failed(report) == [] and report.exit_code == 0
    assert fake.calls["loc-a"] == 4 and fake.calls["loc-b"] == 1
    assert fake.calls["cloud-c"] == 0
    assert fake.calls["q-a"] == 1 and fake.calls["q-b"] == 1


def test_frontdoor_acceptance_wrong_tier1_cap_exits_one(frontdoor, tmp_path, capsys):
    fake = frontdoor()
    registry = _registry(tmp_path, fake.url)
    base = ["acceptance", "frontdoor", "--url", fake.url, "--registry", registry,
            "--spill-group", "spill", "--queue-group", "queue", "--quick"]
    assert cli.main(base + ["--expect-tier1-cap", "4"]) == 0
    assert cli.main(base + ["--expect-tier1-cap", "3"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_frontdoor_paid_spill_requires_paid_flag(frontdoor, tmp_path):
    fake = frontdoor()
    report = _run(fake, _registry(tmp_path, fake.url))
    assert _failed(report) == [] and report.exit_code == 0
    assert fake.calls["cloud-c"] == 0, "default execution reached a paid tier"
    assert any(c.skipped and "paid" in c.name for c in report.checks)
    # the queue-not-reject leg ran without --paid: 2 fill + 3 overfull requests
    assert fake.calls["q-a"] + fake.calls["q-b"] == 2 + 3

    paid = frontdoor()
    report = _run(paid, _registry(tmp_path, paid.url), paid=True)
    assert _failed(report) == [] and report.exit_code == 0
    assert paid.calls["cloud-c"] == 2
    assert paid.calls["loc-a"] == 4 + 4 and paid.calls["loc-b"] == 1 + 1


def test_frontdoor_acceptance_default_never_treats_unmarked_tier_as_free(frontdoor, tmp_path):
    fake = frontdoor()
    report = _run(fake, _registry(tmp_path, fake.url, unmark=("loc-a",)), quick=True)
    assert report.exit_code == 1
    assert fake.calls["loc-a"] == 0 and fake.calls["cloud-c"] == 0


def test_frontdoor_acceptance_refuses_unreachable_local_tier(frontdoor, tmp_path):
    fake = frontdoor(unreachable=("loc-b",))
    report = _run(fake, _registry(tmp_path, fake.url), quick=True)
    assert report.exit_code == 1
    assert fake.calls["cloud-c"] == 0 and fake.calls["loc-a"] == 0


def test_frontdoor_acceptance_registry_must_match_health(frontdoor, tmp_path):
    fake = frontdoor(omit=("loc-b",))
    report = _run(fake, _registry(tmp_path, fake.url), quick=True)
    assert report.exit_code == 1
    assert any("registry" in n for n in _failed(report))


def test_frontdoor_acceptance_queue_must_not_reject(frontdoor, tmp_path):
    fake = frontdoor(reject_when_full=True)
    report = _run(fake, _registry(tmp_path, fake.url))
    assert report.exit_code == 1


def test_frontdoor_acceptance_wrong_answer_fails(frontdoor, tmp_path):
    fake = frontdoor(wrong_answers=True)
    report = _run(fake, _registry(tmp_path, fake.url), quick=True)
    assert any("correct" in n for n in _failed(report))


def test_frontdoor_acceptance_config_errors(frontdoor, tmp_path):
    fake = frontdoor()
    registry = _registry(tmp_path, fake.url)
    with pytest.raises(AcceptanceConfigError):
        run_frontdoor_acceptance(fake.url, str(tmp_path / "absent.yaml"),
                                 spill_group="spill", queue_group="queue")
    with pytest.raises(AcceptanceConfigError):
        run_frontdoor_acceptance(fake.url, registry, spill_group="nope", queue_group="queue")
    with pytest.raises(AcceptanceConfigError):  # queue group with an uncapped tier
        run_frontdoor_acceptance(fake.url, registry, spill_group="queue", queue_group="spill")
    with pytest.raises(AcceptanceConfigError):
        run_frontdoor_acceptance(fake.url, registry, spill_group="spill",
                                 queue_group="queue", expect_tier1_cap=-1)


def test_frontdoor_acceptance_unreachable_frontdoor_is_failure(tmp_path):
    registry = _registry(tmp_path, "http://127.0.0.1:9")
    report = run_frontdoor_acceptance("http://127.0.0.1:9", registry, spill_group="spill",
                                      queue_group="queue", request_timeout=2.0)
    assert report.exit_code == 1
