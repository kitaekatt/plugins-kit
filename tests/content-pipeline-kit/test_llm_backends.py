"""Behavioral tests for content_pipeline.llm.backends.

Covers the hermetic surface: the MockBackend seam and process-level routing.
The two live transports (OpenRouterBackend, ClaudeCliBackend) are thin adapters
that delegate to ``llm_scripting_kit.completion`` -- the ported transport itself is
covered in tests/llm-scripting-kit (fake-runner subprocess seam, envelope parse,
retry, hard-stop, timeout, halt classification, OpenRouter fake client). Here we
only verify the adapters construct without the shared lib and raise a clear
ImportError when actually driven without it.
"""

import pytest

from content_pipeline.llm import backends
from content_pipeline.llm.backends import (
    ClaudeCliBackend,
    MockBackend,
    OpenRouterBackend,
    active_backend_name,
    route,
    routed_model,
    set_active_backend,
)
from content_pipeline.llm.platform import BackendOptions


# --- MockBackend -------------------------------------------------------------


def test_mock_serves_in_order():
    backend = MockBackend(responses=["a", "b"])
    assert backend.complete("s", "u", model="m").text == "a"
    assert backend.complete("s", "u", model="m").text == "b"


def test_mock_records_calls():
    backend = MockBackend(responses=["a"])
    backend.complete("sys", "usr", model="m1", options=BackendOptions(temperature=0.7))
    assert backend.calls[-1]["system"] == "sys"
    assert backend.calls[-1]["user"] == "usr"
    assert backend.calls[-1]["model"] == "m1"


def test_mock_raises_on_exhaustion():
    backend = MockBackend(responses=[])
    with pytest.raises(RuntimeError, match="exhausted"):
        backend.complete("s", "u", model="m")


def test_mock_dict_entry_carries_usage():
    backend = MockBackend(responses=[{"text": "x", "input_tokens": 12, "output_tokens": 3}])
    resp = backend.complete("s", "u", model="m")
    assert resp.input_tokens == 12
    assert resp.output_tokens == 3


def test_mock_keyed_responses_content_addressed():
    backend = MockBackend(keyed_responses={"alpha": "A", "beta": "B"})
    assert backend.complete("s", "please do beta now", model="m").text == "B"
    assert backend.complete("s", "and alpha too", model="m").text == "A"


def test_mock_keyed_no_match_raises():
    backend = MockBackend(keyed_responses={"alpha": "A"})
    with pytest.raises(RuntimeError, match="no key matched"):
        backend.complete("s", "nothing here", model="m")


def test_mock_exception_entry_is_raised():
    backend = MockBackend(responses=[ValueError("boom")])
    with pytest.raises(ValueError, match="boom"):
        backend.complete("s", "u", model="m")


def test_mock_default_model_when_blank():
    backend = MockBackend(responses=["x"], default_model="mm")
    assert backend.complete("s", "u", model="").model == "mm"


# --- live-backend delegation (adapter boundary) ------------------------------


def _has_llm_scripting_kit() -> bool:
    try:
        import llm_scripting_kit  # noqa: F401
        return True
    except ImportError:
        return False


def test_openrouter_backend_constructs_without_lib():
    """Constructing the adapter never needs the shared lib -- only driving it."""
    OpenRouterBackend()
    ClaudeCliBackend()


def test_openrouter_backend_requires_lib_when_driven():
    backend = OpenRouterBackend()
    if _has_llm_scripting_kit():  # pragma: no cover - env-dependent
        pytest.skip("llm_scripting_kit importable; delegation path exercised in llm-scripting-kit")
    with pytest.raises(ImportError, match="llm_scripting_kit"):
        backend.complete("s", "u", model="x")


def test_claude_cli_backend_requires_lib_when_driven():
    backend = ClaudeCliBackend()
    if _has_llm_scripting_kit():  # pragma: no cover - env-dependent
        pytest.skip("llm_scripting_kit importable; delegation path exercised in llm-scripting-kit")
    with pytest.raises(ImportError, match="llm_scripting_kit"):
        backend.complete("s", "u", model="x")


# --- routing -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_backend_env(monkeypatch):
    monkeypatch.delenv(backends.BACKEND_ENV, raising=False)
    monkeypatch.delenv(backends.MODEL_ENV, raising=False)
    yield


def test_routing_default_is_openrouter():
    assert active_backend_name() == "openrouter"
    assert isinstance(route(), OpenRouterBackend)


def test_routing_set_and_clear():
    set_active_backend("claude-cli")
    assert active_backend_name() == "claude-cli"
    set_active_backend(None)
    assert active_backend_name() == "openrouter"


def test_routing_returns_active_backend():
    set_active_backend("claude-cli")
    assert isinstance(route(), ClaudeCliBackend)
    set_active_backend("mock")
    assert isinstance(route(), MockBackend)


def test_routing_injected_instance_wins():
    set_active_backend("mock")
    mine = MockBackend(responses=["x"])
    assert route(mock=mine) is mine


def test_routed_model_substitutes_for_claude(monkeypatch):
    set_active_backend("claude-cli")
    monkeypatch.setenv(backends.MODEL_ENV, "claude-sonnet-4-6")
    # An OpenRouter-style id is substituted.
    assert routed_model("deepseek/deepseek-v4") == "claude-sonnet-4-6"
    # A caller-passed claude id wins (no substitution).
    assert routed_model("claude-opus") == "claude-opus"


def test_routed_model_no_substitution_for_openrouter():
    assert routed_model("deepseek/deepseek-v4") == "deepseek/deepseek-v4"


def test_mock_backend_exhaustion_race_raises_documented_error():
    """Threads racing the LAST scripted entry get RuntimeError, not IndexError.

    Regression: ``complete`` used to do an unguarded ``if not self._queue`` /
    ``self._queue.pop(0)``. Both threads could pass the emptiness check and
    the loser popped an empty list -- an IndexError leaking out of the mock
    instead of the documented "MockBackend exhausted" RuntimeError. Consumers
    share one backend across a ThreadPoolExecutor stage, so this is the shape
    that runs in real pipelines.

    It hammers the boundary directly (queue of one, many threads released
    together) across many rounds at a minimal switch interval.

    HONESTY NOTE: this is an INVARIANT test, not a proven regression test.
    It was run against the pre-fix code and PASSED there too -- the window is
    one bytecode wide and CPython's GIL makes the individual list ops
    effectively atomic, so the race is real by inspection but not practically
    reachable on this interpreter. It is kept because it pins the CONTRACT
    (the entry is served exactly once; exhaustion raises the documented
    RuntimeError, never IndexError), which WOULD catch a future change that
    genuinely breaks atomicity: a swap to a non-atomic container, any I/O
    added between the check and the pop, or a free-threaded (no-GIL) build.
    Do not read a pass here as evidence the lock is unnecessary.
    """
    import sys
    import threading

    n_threads = 16
    rounds = 300
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(rounds):
            backend = MockBackend(responses=["only"])
            served: list[str] = []
            exhausted = 0
            unexpected: list[BaseException] = []
            lock = threading.Lock()
            gate = threading.Barrier(n_threads)

            def worker() -> None:
                nonlocal exhausted
                gate.wait()
                try:
                    resp = backend.complete("sys", "user", model="m")
                except RuntimeError:
                    with lock:
                        exhausted += 1
                    return
                except BaseException as exc:  # noqa: BLE001 -- the defect
                    with lock:
                        unexpected.append(exc)
                    return
                with lock:
                    served.append(resp.text)

            threads = [threading.Thread(target=worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not unexpected, (
                f"unguarded check-then-pop leaked {unexpected[0]!r}; "
                "expected the documented RuntimeError"
            )
            assert served == ["only"], f"entry served {len(served)} times"
            assert exhausted == n_threads - 1
    finally:
        sys.setswitchinterval(old_interval)
