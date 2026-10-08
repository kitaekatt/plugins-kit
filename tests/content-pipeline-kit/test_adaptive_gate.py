"""AdaptiveGate: backpressure halts wait and shrink concurrency; others pass."""
import threading

import pytest

from content_pipeline.llm import call_llm_gated
from content_pipeline.llm.backends import MockBackend
from content_pipeline.llm.gate import AdaptiveGate, EndpointBusyError
from content_pipeline.llm.platform import (
    HALT_BACKPRESSURE, HALT_QUOTA, PipelineHaltError,
)


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def make(jobs=4, **kw):
    c = Clock()
    notes = []
    kw.setdefault("jitter", lambda: 0.0)
    g = AdaptiveGate(jobs, notes.append, clock=c, sleep=c.sleep, **kw)
    return g, c, notes


def bp(retry_after=None):
    return PipelineHaltError(HALT_BACKPRESSURE, "503 queue full", retry_after_s=retry_after)


def flaky(failures):
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] <= len(failures):
            raise failures[state["n"] - 1]
        return "ok"

    return fn, state


def test_success_passes_through():
    g, c, _ = make()
    assert g.run(lambda: 7) == 7
    assert c.slept == [] and g.limit == 4 and g.in_flight == 0


def test_backpressure_waits_retry_after_then_succeeds():
    g, c, notes = make()
    fn, st = flaky([bp(12.0)])
    assert g.run(fn) == "ok"
    assert c.slept == [12.0] and st["n"] == 2
    assert g.limit == 2 and g.refusals == 1 and "endpoint busy" in notes[0]


def test_backoff_without_hint_is_exponential_and_capped():
    g, c, _ = make(base_s=10, cap_s=25, give_up_s=1e9)
    fn, _ = flaky([bp(), bp(), bp()])
    g.run(fn)
    assert c.slept == [5.0, 10.0, 12.5]  # equal jitter at jitter()==0: step/2


def test_limit_halves_to_floor_and_recovers():
    g, c, _ = make(jobs=4, recover_after=2)
    g.run(flaky([bp()])[0])
    assert g.limit == 2
    g.run(lambda: 1); g.run(lambda: 1)
    assert g.limit == 3
    g.run(lambda: 1); g.run(lambda: 1)
    assert g.limit == 4
    g.run(lambda: 1); g.run(lambda: 1)
    assert g.limit == 4  # never above jobs
    g1, _, _ = make(jobs=1)
    g1.run(flaky([bp()])[0])
    assert g1.limit == 1


def test_gives_up_loudly_and_stays_given_up():
    g, c, _ = make(give_up_s=100, base_s=60, cap_s=60)
    calls = []

    def always():
        calls.append(1)
        raise bp()

    with pytest.raises(EndpointBusyError):
        g.run(always)
    assert c.now >= 100 - 1e-9 or g.gave_up
    n = len(calls)
    with pytest.raises(EndpointBusyError):
        g.run(lambda: calls.append(1))
    assert len(calls) == n  # later calls fail at once, without dispatch
    assert g.in_flight == 0


def test_quota_halt_and_other_errors_unchanged():
    g, c, _ = make()
    with pytest.raises(PipelineHaltError) as ei:
        g.run(flaky([PipelineHaltError(HALT_QUOTA, "spent")])[0])
    assert ei.value.kind == HALT_QUOTA and c.slept == [] and g.limit == 4
    with pytest.raises(KeyError):
        g.run(flaky([KeyError("x")])[0])
    assert g.in_flight == 0 and g.refusals == 0


def test_concurrency_never_exceeds_limit():
    g, _, _ = make(jobs=3)
    g.limit = 2
    cur = {"n": 0, "max": 0}
    lock = threading.Lock()
    gate_open = threading.Event()

    def work():
        with lock:
            cur["n"] += 1
            cur["max"] = max(cur["max"], cur["n"])
        gate_open.wait(0.05)
        with lock:
            cur["n"] -= 1

    ts = [threading.Thread(target=g.run, args=(work,)) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert cur["max"] <= 2


def test_call_llm_surfaces_kit_backpressure_halt_with_retry_after():
    class KitHalt(Exception):
        kind = "backpressure"
        retry_after_s = 3.5

    backend = MockBackend(responses=[KitHalt("queue full"), "answer"])
    g, c, _ = make()
    resp = call_llm_gated(g, backend, "sys", "user", model="m", retries=2)
    assert resp.text == "answer"
    assert c.slept == [3.5] and len(backend.calls) == 2
    # the backpressure halt is not consumed by call_llm's own retry budget
    backend = MockBackend(responses=[KitHalt("x")])
    with pytest.raises(PipelineHaltError) as ei:
        from content_pipeline.llm import call_llm
        call_llm(backend, "s", "u", model="m", retries=3)
    assert ei.value.kind == HALT_BACKPRESSURE and ei.value.retry_after_s == 3.5


def test_rate_limit_is_waited_out_like_backpressure():
    from content_pipeline.llm.platform import HALT_RATE_LIMIT, HALT_INSUFFICIENT_CREDIT
    g, c, _ = make()
    g.run(flaky([PipelineHaltError(HALT_RATE_LIMIT, "429", retry_after_s=2.0)])[0])
    assert c.slept == [2.0] and g.limit == 2
    with pytest.raises(PipelineHaltError):
        g.run(flaky([PipelineHaltError(HALT_INSUFFICIENT_CREDIT, "402")])[0])
    assert c.slept == [2.0]


def test_kit_halt_error_shape_is_read(monkeypatch):
    import pathlib
    lib = pathlib.Path(__file__).resolve().parents[2] / "plugins" / "llm-scripting-kit" / "lib"
    monkeypatch.syspath_prepend(str(lib))
    before = set(__import__("sys").modules)
    from llm_scripting_kit.completion.halt import HaltError
    g, c, _ = make()
    backend = MockBackend(responses=[HaltError("backpressure", "busy", retry_after_s=4.0), "ok"])
    assert call_llm_gated(g, backend, "s", "u", model="m").text == "ok"
    assert c.slept == [4.0]
    for name in set(__import__("sys").modules) - before:
        if name.startswith(("llm_scripting_kit", "bootstrap_lib")):
            del __import__("sys").modules[name]


def test_blocked_waiter_raises_when_another_thread_gives_up():
    g, c, _ = make(jobs=1, give_up_s=10)
    in_fn, release_fn = threading.Event(), threading.Event()
    ran = []

    def holder():
        in_fn.set()
        release_fn.wait(5)
        c.now += 20  # past give_up_s
        raise bp()

    def waiter_fn():
        ran.append(1)
        return "ran"

    errors = {}

    def run(name, fn):
        try:
            errors[name] = g.run(fn)
        except BaseException as exc:  # noqa: BLE001
            errors[name] = exc

    t1 = threading.Thread(target=run, args=("h", holder))
    t1.start()
    assert in_fn.wait(5)
    t2 = threading.Thread(target=run, args=("w", waiter_fn))
    t2.start()
    while True:  # deterministic: wait until the waiter is inside Condition.wait()
        with g._cond:
            if g.waiting == 1:
                break
    release_fn.set()
    t1.join(5)
    t2.join(5)
    assert isinstance(errors["h"], EndpointBusyError)
    assert isinstance(errors["w"], EndpointBusyError)
    assert ran == [] and g.in_flight == 0
