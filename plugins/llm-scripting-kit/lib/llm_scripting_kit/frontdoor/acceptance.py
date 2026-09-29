"""Host-neutral acceptance run for a front door's group routing.

Discovery: ``GET /health/backends`` names each group's deployments and their
reachability; the explicitly supplied registry supplies order, caps and billing.
The two must agree on each group's deployment ids. Nothing else about the fleet
is assumed.

Legs (each is a burst of concurrent, exactly-checkable completions):

* fill -- N equals the capped capacity of the tiers that precede the first paid
  tier; every such tier must serve exactly its cap, in order, and no paid tier
  may serve anything (runs without ``paid``).
* queue-fill -- N equals the queue group's total cap; every tier serves its cap.
* paid spill -- unpaid capacity plus two more; the excess must land on the first
  paid tier. Runs only with ``paid=True``.
* queue overfull -- one more request than the queue group's total cap; every
  request must return 200 (queued, never a 429/503) and one tier must serve more
  than its cap, which is only possible when a queued request waited for a slot.

Paid-tier rule: a tier is paid unless the registry declares
``billing.mode: unmetered`` for it. An undeclared tier is treated as paid (fail
closed), because neither ``key_env`` nor a hostname reliably tells owner-funded
from metered. Without ``paid=True`` a burst never exceeds the capped capacity of
unpaid tiers that precede the first paid tier and every such tier must be
reachable per ``/health/backends``, so the front door has no reason to reach a
paid tier.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Optional

from ..model_endpoints import (
    TRANSPORT_KIND,
    EndpointEntry,
    EndpointRegistryError,
    load_endpoint_registry,
)
from ..swapper_acceptance import (
    AcceptanceConfigError,
    AcceptanceReport,
    http_request,
    message_content,
    operands,
    running_models,
    service_root,
)

DEPLOYMENT_HEADER = "x-frontdoor-deployment"
PAID_SPILL_EXTRA = 2
_CHECKPOINTS = ("1", "50", "100", "150", "200")


@dataclass(frozen=True)
class Tier:
    id: str
    order: int
    cap: Optional[int]
    base_url: str
    paid: bool


@dataclass(frozen=True)
class Level:
    """Tiers sharing one routing order; the front door treats them as one step."""

    order: int
    tiers: tuple[Tier, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(t.id for t in self.tiers)

    @property
    def cap(self) -> Optional[int]:
        caps = [t.cap for t in self.tiers]
        return None if any(c is None for c in caps) else sum(c for c in caps if c is not None)

    @property
    def paid(self) -> bool:
        return any(t.paid for t in self.tiers)

    @property
    def label(self) -> str:
        return "+".join(self.ids)


def is_paid(entry: EndpointEntry) -> bool:
    """Paid unless the registry declares ``billing.mode: unmetered``."""
    return entry.billing_mode != "unmetered"


def load_levels(registry_path: str, group: str) -> tuple[list[Level], list[str]]:
    """The group's tiers from the registry, grouped by order, plus registry notes."""
    try:
        registry = load_endpoint_registry({"MODEL_ENDPOINTS_REGISTRY": registry_path})
    except EndpointRegistryError as exc:
        raise AcceptanceConfigError(str(exc)) from exc
    tiers = [
        Tier(e.id, e.routing.order, e.routing.max_parallel, e.base_url or "", is_paid(e))
        for e in registry.entries.values()
        if e.kind == TRANSPORT_KIND and e.routing is not None and e.routing.group == group
    ]
    if not tiers:
        raise AcceptanceConfigError(f"registry {registry_path!r} declares no routing group {group!r}")
    by_order: dict[int, list[Tier]] = {}
    for tier in sorted(tiers, key=lambda t: (t.order, t.id)):
        by_order.setdefault(tier.order, []).append(tier)
    return [Level(o, tuple(ts)) for o, ts in sorted(by_order.items())], list(registry.notes)


Expected = list[tuple[Level, int]]


def plan_fill(levels: list[Level]) -> tuple[int, Expected]:
    """N and the per-level counts that fill the unpaid levels before the first paid one."""
    expected: Expected = []
    for level in levels:
        if level.paid:
            break
        if level.cap is None:
            expected.append((level, 1))  # an uncapped unpaid level takes the remainder
            break
        expected.append((level, level.cap))
    return sum(n for _, n in expected), expected


def plan_paid_spill(levels: list[Level]) -> Optional[tuple[int, Expected]]:
    """N and counts that push PAID_SPILL_EXTRA requests onto the first paid level."""
    n, expected = plan_fill(levels)
    paid = next((lv for lv in levels if lv.paid), None)
    if paid is None or not expected or any(lv.cap is None for lv, _ in expected):
        return None
    extra = PAID_SPILL_EXTRA if paid.cap is None else min(PAID_SPILL_EXTRA, paid.cap)
    return n + extra, expected + [(paid, extra)]


@dataclass
class _Outcome:
    code: int
    deployment: str
    correct: bool
    detail: str


def _check_content(content: Optional[str], expect: int, finish: str) -> tuple[bool, str]:
    if content is None:
        return False, "no choices"
    if finish == "stop" and not content.strip():
        return False, "finish_reason stop with empty content"
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    answer = next((ln for ln in lines if ln.startswith("ANSWER=")), None)
    if answer is None:
        return False, "no ANSWER= line"
    try:
        got: Optional[int] = int(answer.split("=", 1)[1].strip())
    except ValueError:
        got = None
    if got != expect:
        return False, f"expected ANSWER={expect} got {answer[:40]!r}"
    flat = " " + content.replace("\n", " ").replace(",", " ") + " "
    for mark in _CHECKPOINTS:
        if f" {mark} " not in flat:
            return False, f"count checkpoint {mark} missing"
    return True, ""


def _ask(root: str, group: str, n: int, timeout: float) -> _Outcome:
    x, y = operands(n)
    prompt = (
        "Count from 1 to 200, separated by spaces. Then on a final line write "
        f"exactly: ANSWER=<the product of {x} and {y}>, replacing the "
        "placeholder with the computed number and nothing else on that line."
    )
    body = {"model": group, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096, "temperature": 0}
    result = http_request("POST", root + "/v1/chat/completions", body, timeout=timeout)
    deployment = result.headers.get(DEPLOYMENT_HEADER, "?")
    if result.status != 200:
        return _Outcome(result.status, deployment, False, result.error or f"HTTP {result.status}")
    parsed = result.json()
    try:
        finish = parsed["choices"][0].get("finish_reason") or "?"
    except (TypeError, KeyError, IndexError, AttributeError):
        finish = "?"
    ok, detail = _check_content(message_content(result), x * y, finish)
    return _Outcome(200, deployment, ok, detail)


def _burst(root: str, group: str, n: int, seed: int, timeout: float) -> list[_Outcome]:
    outcomes: list[Optional[_Outcome]] = [None] * n
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        outcomes[i] = _ask(root, group, seed * 100 + i + 1, timeout)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [o or _Outcome(0, "?", False, "no result") for o in outcomes]


def _tally(outcomes: list[_Outcome]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.deployment] = counts.get(o.deployment, 0) + 1
    return counts


def _resident_note(levels: list[Level], when: str) -> list[str]:
    """Best-effort ``/running`` observation of each unpaid tier's swapper (informational)."""
    notes = []
    for level in levels:
        for tier in level.tiers:
            if tier.paid or not tier.base_url:
                continue
            resident = running_models(service_root(tier.base_url), timeout=3.0)
            notes.append(f"/running [{when}] {tier.id}: {'n/a' if resident is None else (','.join(resident) or '-')}")
    return notes


class _Run:
    def __init__(self, root: str, spill: list[Level], queue: list[Level], queue_group: str,
                 health: dict[str, dict[str, str]], report: AcceptanceReport, timeout: float,
                 override: Optional[int], paid: bool) -> None:
        self.root, self.spill, self.queue, self.health = root, spill, queue, health
        self.queue_group = queue_group
        self.report, self.timeout, self.override, self.paid = report, timeout, override, paid
        self.seed = 0
        self.executed = 0

    def _reachable(self, group: str, levels: list[Level]) -> Optional[str]:
        for level in levels:
            for tier in level.tiers:
                status = self.health[group].get(tier.id)
                if status != "reachable":
                    return f"{tier.id} is {status} per /health/backends"
        return None

    def burst(self, label: str, group: str, levels: list[Level], n: int, expected: Expected,
              *, override_first: bool, forbid_paid: bool) -> None:
        involved = [lv for lv in levels if any(lv is e[0] for e in expected)]
        if forbid_paid and not self.paid and any(lv.paid for lv in involved):
            self.report.add(f"{label}: paid tier in this leg", False,
                            "the registry marks a needed tier as paid; rerun with --paid to spend on it")
            return
        blocked = self._reachable(group, involved)
        if blocked:
            self.report.add(f"{label}: local tiers reachable", False, blocked)
            return
        self.seed += 1
        self.report.notes += _resident_note(levels, f"{label} before")
        outcomes = _burst(self.root, group, n, self.seed, self.timeout)
        self.executed += 1
        self.report.notes += _resident_note(levels, f"{label} after")
        counts = _tally(outcomes)
        known = {tid for lv in levels for tid in lv.ids}
        self.report.notes.append(f"{label} tally: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        bad_http = [o for o in outcomes if o.code != 200]
        bad_ok = [o for o in outcomes if o.code == 200 and not o.correct]
        self.report.add(f"{label}: every request HTTP 200", not bad_http,
                        f"{len(bad_http)} non-200" + (f" (first: {bad_http[0].detail})" if bad_http else ""))
        self.report.add(f"{label}: every response is a correct completion", not bad_ok,
                        f"{len(bad_ok)} incorrect" + (f" (first: {bad_ok[0].detail})" if bad_ok else ""))
        unexpected = {k: v for k, v in counts.items() if k not in known}
        self.report.add(f"{label}: no unexpected deployment served this group", not unexpected,
                        f"unexpected: {unexpected}" if unexpected else "")
        for index, (level, want) in enumerate(expected):
            if override_first and index == 0 and self.override is not None:
                want = self.override
            got = sum(counts.get(tid, 0) for tid in level.ids)
            self.report.add(f"{label}: {level.label} count", got == want, f"expected {want}, got {got}")
        if not self.paid:
            paid_served = sum(counts.get(t.id, 0) for lv in levels for t in lv.tiers if t.paid)
            self.report.add(f"{label}: no paid tier served", paid_served == 0, f"{paid_served} served")

    def queue_overfull(self) -> None:
        label = "queue overfull"
        levels = self.queue
        total = sum(lv.cap or 0 for lv in levels)
        n = total + 1
        if any(lv.paid for lv in levels) and not self.paid:
            self.report.add(f"{label}: paid tier in this leg", False,
                            "the registry marks a queue tier as paid; rerun with --paid to spend on it")
            return
        blocked = self._reachable(self.queue_group, levels)
        if blocked:
            self.report.add(f"{label}: tiers reachable", False, blocked)
            return
        self.seed += 1
        outcomes = _burst(self.root, self.queue_group, n, self.seed, self.timeout)
        self.executed += 1
        counts = _tally(outcomes)
        self.report.notes.append(f"{label} tally: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        bad_http = [o for o in outcomes if o.code != 200]
        rejected = [o for o in outcomes if o.code in (429, 503)]
        self.report.add(f"{label}: every request HTTP 200 (queued and served, not rejected)", not bad_http,
                        f"{len(bad_http)} non-200")
        self.report.add(f"{label}: no HTTP 429/503", not rejected, f"{len(rejected)} rejected")
        self.report.add(f"{label}: every response is a correct completion",
                        not [o for o in outcomes if o.code == 200 and not o.correct], "")
        known = {tid for lv in levels for tid in lv.ids}
        self.report.add(f"{label}: all {n} requests landed on the group's deployments",
                        sum(counts.get(k, 0) for k in known) == n, f"tally {counts}")
        caps = {t.id: t.cap for lv in levels for t in lv.tiers}
        reused = [k for k, v in counts.items() if k in caps and caps[k] is not None and v > caps[k]]
        self.report.add(f"{label}: a capped deployment served past its cap (the extra request queued for a slot)",
                        bool(reused), f"over-cap: {reused}")


def run_frontdoor_acceptance(
    url: str,
    registry_path: str,
    *,
    spill_group: str,
    queue_group: str,
    quick: bool = False,
    paid: bool = False,
    expect_tier1_cap: Optional[int] = None,
    request_timeout: float = 900.0,
) -> AcceptanceReport:
    """Run the front-door acceptance legs; see the module docstring.

    Raises :class:`AcceptanceConfigError` for bad arguments or an unusable
    registry/group. Every runtime problem is a failed check.
    """
    if expect_tier1_cap is not None and expect_tier1_cap < 0:
        raise AcceptanceConfigError("--expect-tier1-cap must not be negative")
    root = service_root(url)
    if not os.path.isfile(registry_path):
        raise AcceptanceConfigError(f"registry {registry_path!r} is not a readable file")
    spill, notes = load_levels(registry_path, spill_group)
    queue, _ = load_levels(registry_path, queue_group)
    if any(lv.cap is None for lv in queue):
        raise AcceptanceConfigError(
            f"queue group {queue_group!r} has an uncapped tier, so 'every tier full' cannot be constructed"
        )
    report = AcceptanceReport("frontdoor")
    report.notes += [f"front door: {root}", f"registry  : {registry_path}"] + [f"registry note: {n}" for n in notes]
    if expect_tier1_cap is not None:
        report.notes.append(f"--expect-tier1-cap {expect_tier1_cap}: counterfactual run for group {spill_group}")

    reply = http_request("GET", root + "/health/backends", timeout=15.0)
    body = reply.json() if reply.status == 200 else None
    if not isinstance(body, dict) or body.get("protocol") != 1 or not isinstance(body.get("groups"), dict):
        report.add("discovery: /health/backends protocol 1", False,
                   reply.error or f"HTTP {reply.status}, unsupported or malformed body")
        return report
    health: dict[str, dict[str, str]] = {}
    ok = True
    for group, levels in ((spill_group, spill), (queue_group, queue)):
        block = body["groups"].get(group)
        if not isinstance(block, dict):
            ok = report.add(f"discovery: group {group!r} present in /health/backends", False) and ok
            continue
        health[group] = {str(d.get("id")): str(d.get("status")) for d in block.get("deployments", [])
                         if isinstance(d, dict)}
        registry_ids = {tid for lv in levels for tid in lv.ids}
        match = set(health[group]) == registry_ids
        ok = report.add(
            f"discovery: registry matches /health/backends for group {group!r}", match,
            f"health={sorted(health[group])} registry={sorted(registry_ids)}" if not match else "",
        ) and ok
    if not ok:
        return report
    for group, levels in ((spill_group, spill), (queue_group, queue)):
        report.notes.append(f"{group} tiers: " + " -> ".join(
            f"{lv.label}(cap {'uncapped' if lv.cap is None else lv.cap}{', paid' if lv.paid else ''})"
            for lv in levels))

    run = _Run(root, spill, queue, queue_group, health, report, request_timeout, expect_tier1_cap, paid)
    n, expected = plan_fill(spill)
    if n:
        run.burst(f"{spill_group} fill N={n}", spill_group, spill, n, expected,
                  override_first=True, forbid_paid=True)
    else:
        report.add(f"{spill_group} fill", False, "no unpaid capped tier precedes the first paid tier; nothing runnable without --paid")
    q_total = sum(lv.cap or 0 for lv in queue)
    run.burst(f"{queue_group} fill N={q_total}", queue_group, queue, q_total,
              [(lv, lv.cap or 0) for lv in queue], override_first=False, forbid_paid=True)
    if not quick:
        plan = plan_paid_spill(spill)
        if plan is None:
            report.add("paid spill: the spill group has a paid tier after capped unpaid tiers", True,
                       "no such tier; leg not applicable", skipped=True)
        elif not paid:
            report.add("paid spill: leg needs --paid (would spend on a paid tier)", True,
                       "not run", skipped=True)
        else:
            pn, pexpected = plan
            run.burst(f"{spill_group} paid spill N={pn}", spill_group, spill, pn, pexpected,
                      override_first=True, forbid_paid=False)
        run.queue_overfull()
    if not run.executed:
        report.add("at least one burst ran", False, "every leg was refused before sending work")
    return report
