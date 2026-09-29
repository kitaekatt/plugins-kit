"""Host-neutral acceptance run for a llama-swap model swapper.

Everything is asserted through the swapper's own HTTP surface (``/v1/models``,
``/running``, ``/v1/chat/completions``); no host process access is needed, so the
same run works against a local or a remote swapper.

Assertions:

1. Correctness: every response is HTTP 200 with an exact arithmetic answer. Each
   request carries its own operand pair, so a cached or truncated reply fails.
2. Residency: while a round is in flight, ``/running`` shows the round's model and
   nothing else.
3. Greedy never-evict, on a CONSTRUCTED overlap. Alternating rounds cannot prove
   it: each waits for the previous one, so no request is ever in flight when the
   next model is demanded. The overlap check starts a long request on one model,
   demands the other model while it runs, and fails if the second model becomes
   resident before the first request finishes. It also fails when the overlap
   could not be constructed, so a run never passes vacuously.

Exit codes for a caller: 0 all passed, 1 an assertion failed, 2 usage or
configuration error (:class:`AcceptanceConfigError`).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .swapper import SwapperUsageError, normalize_swapper_url

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2


class AcceptanceConfigError(Exception):
    """Bad arguments or configuration; the run could not start (exit 2)."""


@dataclass
class Check:
    """One assertion result. ``skipped`` marks a leg that was deliberately not run."""

    name: str
    passed: bool
    detail: str = ""
    skipped: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "skipped": self.skipped, "detail": self.detail}


@dataclass
class AcceptanceReport:
    kind: str
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "", *, skipped: bool = False) -> bool:
        self.checks.append(Check(name, passed, detail, skipped))
        return passed

    @property
    def passed(self) -> bool:
        return all(c.passed or c.skipped for c in self.checks)

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.passed else EXIT_FAILED

    def to_json(self) -> dict[str, Any]:
        return {
            "protocol": 1,
            "kind": self.kind,
            "passed": self.passed,
            "checks": [c.to_json() for c in self.checks],
            "notes": list(self.notes),
        }

    def to_text(self) -> str:
        lines = list(self.notes)
        if lines:
            lines.append("")
        for c in self.checks:
            tag = "SKIP" if c.skipped else ("PASS" if c.passed else "FAIL")
            lines.append(f"  {tag}: {c.name}" + (f" ({c.detail})" if c.detail else ""))
        failed = sum(1 for c in self.checks if not c.passed and not c.skipped)
        lines.append("")
        lines.append(
            f"PASS -- {self.kind} acceptance held." if not failed
            else f"FAIL -- {failed} assertion(s) failed."
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class HttpResult:
    status: int  # 0 = no HTTP response (connection failure or timeout)
    headers: dict[str, str]
    body: bytes
    error: str = ""

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (ValueError, TypeError):
            return None


def http_request(
    method: str, url: str, payload: Optional[dict[str, Any]] = None, *, timeout: float = 30.0
) -> HttpResult:
    """One HTTP call that never raises; a transport failure has ``status`` 0."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return HttpResult(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read())
    except urllib.error.HTTPError as exc:
        return HttpResult(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read() or b"")
    except Exception as exc:  # noqa: BLE001 -- reported as a failed check
        return HttpResult(0, {}, b"", f"{type(exc).__name__}: {exc}".rstrip(": "))


def service_root(url: str) -> str:
    """The service root of ``url`` (trailing ``/`` and ``/v1`` removed)."""
    try:
        return normalize_swapper_url(url)
    except SwapperUsageError as exc:
        raise AcceptanceConfigError(str(exc)) from exc


def operands(n: int) -> tuple[int, int]:
    """A deterministic two-digit operand pair unique to request number ``n``."""
    return (n * 7 + 13) % 90 + 10, (n * 11 + 29) % 90 + 10


def chat_body(model: str, prompt: str, max_tokens: int) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }


def message_content(result: HttpResult) -> Optional[str]:
    body = result.json()
    try:
        return (body["choices"][0]["message"].get("content") or "")
    except (TypeError, KeyError, IndexError, AttributeError):
        return None


def running_models(root: str, timeout: float = 5.0) -> Optional[list[str]]:
    """Model ids resident per ``/running``, or None when it did not answer."""
    result = http_request("GET", root + "/running", timeout=timeout)
    body = result.json() if result.status == 200 else None
    if not isinstance(body, dict):
        return None
    return sorted(str(m.get("model", "?")) for m in body.get("running", []) if isinstance(m, dict))


def _spawn(fn: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    box: list[Any] = []
    thread = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    thread.start()
    return thread, box


def _ask_sum(root: str, model: str, n: int, timeout: float) -> tuple[bool, str]:
    x, y = operands(n)
    prompt = f"What is {x} plus {y}? Reply with only the number, nothing else."
    result = http_request("POST", root + "/v1/chat/completions", chat_body(model, prompt, 2048), timeout=timeout)
    if result.status != 200:
        return False, f"HTTP {result.status} {result.error or result.body[:80]!r}"
    content = (message_content(result) or "").strip()
    return content == str(x + y), f"expected {x + y!r} got {content[:60]!r}"


def _resident_text(resident: Optional[list[str]]) -> str:
    return "?" if resident is None else (",".join(resident) or "-")


def run_swapper_acceptance(
    url: str,
    rounds: int,
    *,
    settle_s: float = 3.0,
    poll_s: float = 1.0,
    busy_setup_s: float = 60.0,
    request_timeout: float = 900.0,
) -> AcceptanceReport:
    """Run the swapper acceptance sequence against ``url``.

    Raises :class:`AcceptanceConfigError` for bad arguments; every runtime
    problem is a failed check in the returned report.
    """
    if rounds < 1:
        raise AcceptanceConfigError("--rounds must be at least 1")
    root = service_root(url)
    report = AcceptanceReport("swapper")

    listing = http_request("GET", root + "/v1/models", timeout=10.0)
    body = listing.json() if listing.status == 200 else None
    if not isinstance(body, dict):
        report.add("discovery: /v1/models answers", False, listing.error or f"HTTP {listing.status}")
        return report
    models = sorted(str(m["id"]) for m in body.get("data", []) if isinstance(m, dict) and "id" in m)
    if running_models(root) is None:
        report.add(
            "discovery: /running answers", False,
            "not fronted by llama-swap, so there is no swapper to accept; nothing was tested",
        )
        return report
    if len(models) < 2:
        report.add("discovery: two models to alternate", False, f"the swapper serves {len(models)}")
        return report
    model_a, model_b = models[0], models[1]
    report.notes += [f"swapper   : {root}", f"model A   : {model_a}", f"model B   : {model_b}", f"rounds    : {rounds}"]
    report.add("discovery: /v1/models, /running, two models", True, f"{model_a}, {model_b}")

    for r in range(1, rounds + 1):
        model = model_a if r % 2 == 1 else model_b
        count = (r * 5 + 3) % 6 + 1  # 1..6 concurrent, varying per round
        threads = [
            _spawn(lambda i=i: _ask_sum(root, model, r * 10 + i, request_timeout))
            for i in range(1, count + 1)
        ]
        started = time.monotonic()
        samples: list[Optional[list[str]]] = []
        while any(t.is_alive() for t, _ in threads):
            time.sleep(poll_s)
            if time.monotonic() - started >= settle_s and any(t.is_alive() for t, _ in threads):
                samples.append(running_models(root))
        for t, _ in threads:
            t.join()
        results = [box[0] if box else (False, "no result") for _, box in threads]
        good = sum(1 for ok, _ in results if ok)
        detail = "; ".join(d for ok, d in results if not ok)[:200]
        report.add(f"round {r} ({count} request(s) -> {model}): correctness", good == count,
                   f"{good}/{count} exact" + (f"; {detail}" if detail else ""))
        wrong = [s for s in samples if s not in (None, [], [model])]
        report.add(
            f"round {r}: residency", not wrong,
            (f"saw {_resident_text(wrong[0])} resident while requesting {model}" if wrong
             else f"{len(samples)} sample(s), only {model} or draining"),
        )
    _run_overlap(report, root, model_a, model_b, poll_s, busy_setup_s, request_timeout)
    return report


def _run_overlap(
    report: AcceptanceReport, root: str, busy: str, cold: str,
    poll_s: float, busy_setup_s: float, request_timeout: float,
) -> None:
    """Constructed never-evict overlap: demand ``cold`` while ``busy`` is mid-request."""
    url = root + "/v1/chat/completions"
    http_request("POST", url, chat_body(busy, "hi", 8), timeout=request_timeout)  # start warm
    long_prompt = "Count from 1 to 300, one number per line, nothing else."
    busy_thread, busy_box = _spawn(
        lambda: http_request("POST", url, chat_body(busy, long_prompt, 1400), timeout=request_timeout)
    )
    deadline = time.monotonic() + busy_setup_s
    busy_resident = False
    while busy_thread.is_alive() and time.monotonic() < deadline:
        if running_models(root) == [busy]:
            busy_resident = True
            break
        time.sleep(poll_s)
    in_flight_at_demand = busy_thread.is_alive()
    x, y = 61, 47
    cold_thread, cold_box = _spawn(lambda: http_request(
        "POST", url,
        chat_body(cold, f"What is {x} plus {y}? Reply with only the number, nothing else.", 2048),
        timeout=request_timeout,
    ))
    evicted_seen: Optional[list[str]] = None
    overlap_samples = 0
    while busy_thread.is_alive():
        resident = running_models(root)
        if busy_thread.is_alive():
            overlap_samples += 1
            if resident is not None and cold in resident and evicted_seen is None:
                evicted_seen = resident
        time.sleep(poll_s)
    busy_thread.join()
    constructed = busy_resident and in_flight_at_demand and overlap_samples > 0
    report.add(
        "overlap: constructed", constructed,
        f"{busy} resident={busy_resident}, in flight when {cold} demanded={in_flight_at_demand}, "
        f"samples during overlap={overlap_samples}",
    )
    report.add(
        "overlap: greedy never-evict", evicted_seen is None,
        (f"EVICTED: {cold} became resident while {busy} had a request in flight "
         f"(saw {_resident_text(evicted_seen)})" if evicted_seen is not None
         else f"{busy} stayed resident until its request finished"),
    )
    busy_result: HttpResult = busy_box[0]
    length = len(message_content(busy_result) or "") if busy_result.status == 200 else -1
    report.add("overlap: busy request survived intact", busy_result.status == 200 and length >= 200,
               f"HTTP {busy_result.status}, {length} chars")
    cold_thread.join()
    cold_result: HttpResult = cold_box[0]
    answer = (message_content(cold_result) or "").strip() if cold_result.status == 200 else ""
    report.add("overlap: queued cold-model request served after the drain",
               cold_result.status == 200 and answer == str(x + y),
               f"HTTP {cold_result.status}, answer {answer[:40]!r}")
