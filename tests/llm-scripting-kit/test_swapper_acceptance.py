"""Tests for the host-neutral swapper acceptance library and CLI verb.

A stdlib fake llama-swap (``/v1/models``, ``/running``, ``/v1/chat/completions``)
runs in a thread. ``faithful`` mode queues a request for a second model until the
first model's in-flight requests drain; ``evicting`` mode swaps immediately.
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llm_scripting_kit import cli
from llm_scripting_kit.swapper_acceptance import run_swapper_acceptance


class FakeSwapper:
    def __init__(self, mode="faithful", models=("alpha", "beta"), delay=0.2,
                 busy_delay=1.2, wrong_answers=False, running_route=True):
        self.mode, self.models, self.delay = mode, list(models), delay
        self.busy_delay, self.wrong_answers = busy_delay, wrong_answers
        self.running_route = running_route
        self.cond = threading.Condition()
        self.resident = []
        self.inflight = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/v1/models":
                    return self._send(200, {"data": [{"id": m} for m in owner.models]})
                if self.path == "/running" and owner.running_route:
                    with owner.cond:
                        running = [{"model": m, "state": "ready"} for m in owner.resident]
                    return self._send(200, {"running": running})
                self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model = body["model"]
                prompt = body["messages"][0]["content"]
                busy = prompt.startswith("Count from 1 to 300")
                with owner.cond:
                    if owner.mode == "faithful":
                        waited = False
                        while any(n for m, n in owner.inflight.items() if m != model):
                            waited = True
                            owner.cond.wait(0.05)
                        if waited:
                            time.sleep(0.15)  # model load time after the drain
                    owner.resident = [model]
                    owner.inflight[model] = owner.inflight.get(model, 0) + 1
                time.sleep(owner.busy_delay if busy else owner.delay)
                if busy:
                    text = "\n".join(str(i) for i in range(1, 301))
                else:
                    match = re.match(r"What is (\d+) plus (\d+)\?", prompt)
                    if match is None:
                        text = "hello"
                    else:
                        total = int(match.group(1)) + int(match.group(2))
                        text = str(total + 1 if owner.wrong_answers else total)
                # The request counts as in flight until its response is written,
                # so a drain-then-swap cannot become visible before the client
                # has its answer.
                self._send(200, {"choices": [{"message": {"content": text}}]})
                with owner.cond:
                    owner.inflight[model] -= 1
                    owner.cond.notify_all()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_swapper():
    made = []

    def make(**kw):
        fake = FakeSwapper(**kw)
        made.append(fake)
        return fake

    yield make
    for fake in made:
        fake.close()


FAST = dict(settle_s=0.05, poll_s=0.02, busy_setup_s=5.0, request_timeout=20.0)


def _failed(report):
    return [c.name for c in report.checks if not c.passed and not c.skipped]


def test_swapper_acceptance_passes_against_faithful_swapper(fake_swapper):
    fake = fake_swapper()
    report = run_swapper_acceptance(fake.url, 3, **FAST)
    assert _failed(report) == []
    assert report.exit_code == 0
    assert any("greedy never-evict" in c.name for c in report.checks)


def test_swapper_acceptance_constructed_overlap_detects_eviction(fake_swapper):
    fake = fake_swapper(mode="evicting")
    report = run_swapper_acceptance(fake.url, 2, **FAST)
    assert "overlap: greedy never-evict" in _failed(report)
    assert report.exit_code == 1


def test_swapper_acceptance_overlap_must_actually_be_constructed(fake_swapper):
    # A busy request that finishes instantly cannot prove greediness; the run
    # must say so instead of passing vacuously.
    fake = fake_swapper(busy_delay=0.0)
    report = run_swapper_acceptance(fake.url, 2, **FAST)
    assert "overlap: constructed" in _failed(report)
    assert report.exit_code == 1


def test_swapper_acceptance_wrong_answer_fails(fake_swapper):
    fake = fake_swapper(wrong_answers=True)
    report = run_swapper_acceptance(fake.url, 2, **FAST)
    assert any(n.endswith("correctness") for n in _failed(report))


def test_swapper_acceptance_needs_running_route_and_two_models(fake_swapper):
    no_running = fake_swapper(running_route=False)
    assert run_swapper_acceptance(no_running.url, 1, **FAST).exit_code == 1
    one_model = fake_swapper(models=("only",))
    assert run_swapper_acceptance(one_model.url, 1, **FAST).exit_code == 1


def test_swapper_acceptance_cli_json_and_exit_codes(fake_swapper, capsys):
    fake = fake_swapper()
    code = cli.main(["acceptance", "swapper", "--url", fake.url, "--rounds", "2",
                     "--format", "json", "--settle-seconds", "0.05", "--poll-seconds", "0.02"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "swapper" and payload["passed"] is True
    evicting = fake_swapper(mode="evicting")
    assert cli.main(["acceptance", "swapper", "--url", evicting.url, "--rounds", "2",
                     "--settle-seconds", "0.05", "--poll-seconds", "0.02"]) == 1
    assert "FAIL" in capsys.readouterr().out
