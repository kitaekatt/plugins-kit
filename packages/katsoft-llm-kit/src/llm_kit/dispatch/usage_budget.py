"""Subscription-usage pacing -- should an opted-in model be spent right now?

A third availability axis, deliberately separate from the two this package
already has. ``endpoints`` says what is CONFIGURED; ``reachability`` says
whether a configured endpoint ANSWERS. Neither can say that an endpoint which
is configured and answering should nonetheless be left alone, because the
subscription quota behind it is being burned faster than the clock -- and
that is the question a quota-paced model asks.

THE RULE, two thresholds and two different consequences. Let

    r = remaining_percentage / 100
    t = (resets_at - now) / window_seconds

    r <= 0   -> OUT OF QUOTA.   The pool is spent. The model is DISABLED: a
                                call would fail, so it is removed from
                                selection entirely.
    r < t    -> UNDER QUOTA.    Spending faster than the clock. The model is
                                DE-PRIORITIZED: still usable, but it loses to
                                any equally-suitable model that is not.
    else     -> AVAILABLE.      At or ahead of pace.

The gap between the two is the point. Withholding a model merely because it is
being spent quickly costs the caller a capability it still has; a model whose
pool is empty has no capability left to lose. Those are different facts and
they earn different treatment -- ordering for the first, exclusion for the
second.

Each opted-in entry names ITS OWN pool. That is not a detail: fable draws on a
per-model weekly bucket while opus draws on the all-model weekly window, so a
single shared window would gate one of them against a number that has nothing
to do with it. Pools are DECLARED per entry rather than derived from the model
id, because a plausible derivation ("use the model's own bucket when one
exists") would hand opus ``seven_day_opus`` -- the opposite of the all-model
pacing an opus entry is meant to follow.

WHERE THE NUMBERS COME FROM, and what is deliberately not used. Both sources
are files a harness already wrote for its own reasons; nothing here
authenticates, and nothing here reads a credential:

- **claude** -- ``~/.claude/plugins/data/plugins-kit/claude-ui-kit/rate-limits.json``,
  the snapshot claude-ui-kit's statusline drops on every render. The statusline
  hook payload is the only surface on which Claude Code emits ``rate_limits``
  at all, so that snapshot is the whole of what a script can see. This is a
  FILE CONTRACT, not an import edge: the file is optional, its absence is a
  supported state, and neither plugin depends on the other.
- **codex** -- the newest session rollout under ``~/.codex/sessions/``, whose
  ``token_count`` events carry a ``rate_limits`` object with ``used_percent``,
  ``window_minutes`` and ``resets_at``. Written by codex itself as a side
  effect of running; nothing writes it on demand.

Anthropic's ``/api/oauth/usage`` endpoint would answer more completely --
notably the per-model bucket that ``model_scoped`` mirrors -- and is
deliberately NOT called. It needs the user's OAuth access token, and putting a
subscription credential into a pacing check is a much larger step than reading
a file a harness already wrote. Usage visibility is something a CLI should
expose; where a CLI does not expose it, the honest outcome is a recorded gap,
not a token read. :data:`STATUS_NO_DATA` IS that gap, surfaced rather than
papered over.

FAIL OPEN, always. A pool that is missing, malformed, or from a window that has
already reset yields :data:`STATUS_NO_DATA` and the model stays AVAILABLE. This
is the same honesty rule :mod:`llm_kit.dispatch.reachability` applies one axis
over -- "I could not check" is never reported as "it is down" -- and here it has
a second, concrete justification: the per-model bucket is emitted by the server
only for some accounts, so a fail-closed reading would make fable permanently
unreachable on every machine whose payload omits it, for want of a number that
was never going to arrive.

PINNED FOR THE SESSION. A verdict is computed once per session key and reused,
so a model that was available when the session started does not become
unavailable halfway through -- a mid-session flip would strand work that was
planned against the earlier answer. The one exception runs in the safe
direction only: an UNDER-QUOTA or OUT-OF-QUOTA verdict is recomputed once its
window has reset, because a reset can only restore capacity. An AVAILABLE
verdict is never recomputed within the session.

The pin has a cost worth stating: a model that goes from under-quota to
genuinely spent DURING a session keeps its under-quota verdict, so a caller may
still choose it and the call may then fail on the provider's own rate limit.
That failure is already classified by this package's halt taxonomy
(``HALT_RATE_LIMIT``), which is the honest place for it -- an exhausted pool is
the provider's fact to report, and pretending to predict it mid-session would
reintroduce exactly the flip the pin exists to prevent.
"""
from __future__ import annotations

import json
import os
import sys
import time
import types
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from llm_kit.adapters import usage_sources as _sources
from llm_kit.registry.conserve import (
    POOL_MODEL_SCOPED,
    POOL_PRIMARY,
    POOL_SEVEN_DAY,
    STATUS_AVAILABLE,
    STATUS_NO_DATA,
    STATUS_OUT_OF_QUOTA,
    STATUS_UNDER_QUOTA,
    Budget,
    ConserveConfigError,
    ConserveSpec,
    parse_conserve_usage,
)

VERDICT_CACHE = (
    Path.home() / ".claude" / "plugins" / "data" / "plugins-kit"
    / "llm-scripting-kit" / "usage-verdicts.json"
)
"""Session-pinned verdicts. See the module docstring on pinning."""

# The raw readers (the claude snapshot and codex rollout readers, their helpers
# and their path constants) live in llm_kit.adapters.usage_sources; the pacing
# policy below reads them from there. Each reader name is still readable,
# settable and deletable on THIS module, and each access acts on the
# usage_sources attribute: a test that patches
# ``usage_budget.CODEX_SESSIONS_DIR`` or ``usage_budget.read_codex_pool``
# changes what the policy below reads and what the codex backend reads, as it
# did when the readers were defined here.
_READER_NAMES = frozenset({
    "_HOUR", "_DAY", "_CLAUDE_WINDOW_SECONDS", "CLAUDE_SNAPSHOT",
    "CODEX_SESSIONS_DIR", "_PACE_EPSILON", "_CODEX_TAIL_BYTES",
    "_CODEX_EXHAUSTION_LATCH_SECONDS", "_RESET_TEXT_RE", "_epoch",
    "_used_percentage", "_verdict", "_load_json", "_read_model_scoped",
    "read_claude_pool", "_newest_codex_rollout", "_CodexRateLimitReading",
    "_last_codex_rate_limits", "_find_rate_limits",
    "_find_usage_limit_message", "_parse_reset_text", "read_codex_pool",
})


class _UsageBudgetModule(types.ModuleType):
    """This module, with the moved reader names forwarded to usage_sources."""

    def __getattr__(self, name: str) -> Any:
        if name in _READER_NAMES:
            return getattr(_sources, name)
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _READER_NAMES:
            setattr(_sources, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _READER_NAMES:
            delattr(_sources, name)
        else:
            super().__delattr__(name)

    def __dir__(self) -> List[str]:
        return sorted(set(super().__dir__()) | _READER_NAMES)


sys.modules[__name__].__class__ = _UsageBudgetModule

def evaluate(
    spec: ConserveSpec,
    harness: Optional[str],
    *,
    now: Optional[float] = None,
) -> Budget:
    """Verdict for one opted-in entry, dispatched by harness.

    A harness with no usage source of its own -- opencode, or a transport
    endpoint -- yields :data:`STATUS_NO_DATA` rather than an error: opting a
    non-subscription endpoint in is pointless, not invalid, and failing open
    keeps it usable.
    """
    key = (harness or "").strip().lower()
    if key == "claude":
        return _sources.read_claude_pool(spec, now=now)
    if key == "codex":
        return _sources.read_codex_pool(spec, now=now)
    shown = harness if harness else "<none>"
    return Budget(
        status=STATUS_NO_DATA, pool=spec.pool,
        detail=f"no usage source for harness {shown!r} (claude and codex only)",
    )


def session_key(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The identity a pinned verdict is held against, or None when unpinnable.

    A Claude Code session -- the primary consumer, through ``seats`` -- always
    exports ``CLAUDE_CODE_SESSION_ID``. A bare script run outside one has no
    session to pin to, and gets a fresh verdict each call rather than a
    fabricated identity that would pin unrelated runs together.
    """
    env = os.environ if environ is None else environ
    for name in ("LLM_SCRIPTING_KIT_USAGE_SESSION", "CLAUDE_CODE_SESSION_ID"):
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def _load_cache(path: Path, key: str) -> Dict[str, Any]:
    data = _sources._load_json(path)
    if not isinstance(data, Mapping) or data.get("session_key") != key:
        return {}
    verdicts = data.get("verdicts")
    return dict(verdicts) if isinstance(verdicts, Mapping) else {}


def _store_cache(path: Path, key: str, verdicts: Mapping[str, Any]) -> None:
    """Write the pinned verdicts, or give up silently.

    A pacing cache that cannot be written costs a recomputation, which is a
    file read -- never a reason to fail the caller's actual work.
    """
    payload = {"session_key": key, "verdicts": dict(verdicts)}
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def pinned_evaluate(
    entry_id: str,
    spec: ConserveSpec,
    harness: Optional[str],
    *,
    now: Optional[float] = None,
    cache_path: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Budget:
    """:func:`evaluate`, pinned for the life of the session key.

    Reuses a stored verdict rather than re-reading the pool, so an endpoint
    that was available at the start of a session stays available through it.
    A stored UNDER-QUOTA or OUT-OF-QUOTA verdict is recomputed once its window
    has reset -- the only recomputation admitted, and it can only restore
    capacity. Without a
    session key nothing is pinned and every call evaluates afresh.
    """
    key = session_key(environ)
    if key is None:
        return evaluate(spec, harness, now=now)
    moment = time.time() if now is None else now
    path = VERDICT_CACHE if cache_path is None else cache_path
    verdicts = _load_cache(path, key)
    stored = verdicts.get(entry_id)
    if isinstance(stored, Mapping) and stored.get("spec") == spec.to_json():
        budget = stored.get("budget")
        if isinstance(budget, Mapping) and budget.get("status") in _STATUSES:
            raw_resets_at = budget.get("resets_at")
            # Normalize once: a cached resets_at can be a float epoch (a
            # harness snapshot may report sub-second timestamps), and the
            # expiry decision below and the rehydrated Budget must agree on
            # the same value -- keeping it only when isinstance(x, int) here
            # while accepting int OR float for the expiry test used the float
            # correctly to decide "not expired" and then reported it back as
            # resets_at=None ("no reset known").
            resets_at = (
                int(raw_resets_at) if isinstance(raw_resets_at, (int, float)) else None
            )
            expired = (
                budget["status"] in (STATUS_UNDER_QUOTA, STATUS_OUT_OF_QUOTA)
                and resets_at is not None
                and moment >= resets_at
            )
            if not expired:
                return Budget(
                    status=str(budget["status"]),
                    pool=str(budget.get("pool", spec.pool)),
                    detail=str(budget.get("detail", "")),
                    remaining=budget.get("remaining"),
                    window_remaining=budget.get("window_remaining"),
                    resets_at=resets_at,
                )
    fresh = evaluate(spec, harness, now=moment)
    verdicts[entry_id] = {"spec": spec.to_json(), "budget": fresh.to_json()}
    _store_cache(path, key, verdicts)
    return fresh


def quota_pool_key(harness: Optional[str], spec: Optional[ConserveSpec]) -> Optional[tuple]:
    """The identity of the quota an entry draws on, or None when it has none.

    Entries on the same harness account reading the same declared pool (and,
    for a model-scoped pool, the same labelled row) share one quota. This is
    the one definition: ``describe`` labels such entries "shares <pool> with
    <x>", and :func:`record_observed_halt` spends all of them on one observed
    halt. An entry with no ``conserve_usage`` or no harness draws on no known
    quota and shares with nothing.
    """
    key = (harness or "").strip().lower()
    if spec is None or not key:
        return None
    return (key, spec.pool, spec.display_name)


def _pool_siblings(
    entry_id: str, spec: ConserveSpec, entries: Mapping[str, Any]
) -> List[Tuple[str, ConserveSpec]]:
    """Every other entry in ``entries`` that shares ``entry_id``'s quota."""
    own = entries.get(entry_id)
    key = quota_pool_key(getattr(own, "harness", None), spec)
    if key is None:
        return []
    siblings = []
    for name, entry in entries.items():
        other = getattr(entry, "conserve_usage", None)
        if name != entry_id and other is not None and quota_pool_key(
            getattr(entry, "harness", None), other
        ) == key:
            siblings.append((name, other))
    return siblings


def record_observed_halt(
    entry_id: str,
    spec: ConserveSpec,
    *,
    entries: Optional[Mapping[str, Any]] = None,
    resets_at: Optional[int] = None,
    now: Optional[float] = None,
    cache_path: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[Budget]:
    """Force the pinned verdict to OUT_OF_QUOTA after an observed quota/credit halt.

    :func:`pinned_evaluate` only ever improves a stored verdict within a
    session -- an UNDER-QUOTA or OUT-OF-QUOTA reading is recomputed once its
    window resets, and an AVAILABLE reading is never recomputed at all (the
    ``conserve_usage`` register entry, root ``CLAUDE.md``). This is the one
    exception: a caller that OBSERVED a real dispatch failure -- codex's own
    :data:`~.completion.halt.HALT_QUOTA`, or an equivalent credit halt --
    knows the pool is spent right now, and writes that fact back so the rest
    of the session does not re-select the same exhausted entry on the
    strength of a stale AVAILABLE verdict. An AVAILABLE verdict still never
    flips from a mere re-READ; only an observed failure moves it.

    ``resets_at`` is the halt's own reset time when it carried one (a
    ``usage_limit_exceeded`` "try again at" clause, as
    ``usage_budget.read_codex_pool`` parses it). Absent that, the reset is
    latched to ``now + 5h`` -- the same
    :data:`_CODEX_EXHAUSTION_LATCH_SECONDS` bound ``read_codex_pool`` itself
    falls back to, so an unbounded verdict is never left to hold for the rest
    of the session.

    ``entries`` is the registry the caller resolved ``entry_id`` from. When
    it is given, the halt is written for every entry that shares the halted
    entry's quota (:func:`quota_pool_key`: same harness account, same pool),
    each under its own ``conserve_usage``: a spent pool is spent for all of
    them, and without this each sibling would cost one more failed dispatch
    before its own halt was observed. Entries without ``conserve_usage`` get
    no verdict. Every caller that has the registry passes it.

    Returns the halted entry's written :class:`Budget`, or ``None`` when there is no
    session key to pin against (nothing is written -- an unpinnable call gets
    a fresh :func:`evaluate` every time regardless, so writing to a keyless
    cache would leak the observation into an unrelated later session).
    """
    key = session_key(environ)
    if key is None:
        return None
    moment = time.time() if now is None else now
    reset_at = (
        int(resets_at)
        if resets_at is not None
        else int(moment + _sources._CODEX_EXHAUSTION_LATCH_SECONDS)
    )
    budget = Budget(
        status=STATUS_OUT_OF_QUOTA,
        pool=spec.pool,
        detail="observed quota/credit halt at dispatch",
        remaining=0.0,
        resets_at=reset_at,
    )
    path = VERDICT_CACHE if cache_path is None else cache_path
    verdicts = _load_cache(path, key)
    verdicts[entry_id] = {"spec": spec.to_json(), "budget": budget.to_json()}
    for name, sibling_spec in _pool_siblings(entry_id, spec, entries or {}):
        verdicts[name] = {
            "spec": sibling_spec.to_json(),
            "budget": Budget(
                status=STATUS_OUT_OF_QUOTA,
                pool=sibling_spec.pool,
                detail=f"observed quota/credit halt on {entry_id}, which shares this pool",
                remaining=0.0,
                resets_at=reset_at,
            ).to_json(),
        }
    _store_cache(path, key, verdicts)
    return budget


_STATUSES = (STATUS_AVAILABLE, STATUS_UNDER_QUOTA, STATUS_OUT_OF_QUOTA, STATUS_NO_DATA)


#: Package-level alias. ``evaluate`` is the right name inside this module and
#: far too generic at the package boundary, where it sits beside
#: ``resolve_model`` and ``check_entry``.
evaluate_usage_budget = evaluate


__all__ = [
    "STATUS_AVAILABLE",
    "STATUS_UNDER_QUOTA",
    "STATUS_OUT_OF_QUOTA",
    "STATUS_NO_DATA",
    "POOL_SEVEN_DAY",
    "POOL_MODEL_SCOPED",
    "POOL_PRIMARY",
    "CLAUDE_SNAPSHOT",
    "CODEX_SESSIONS_DIR",
    "VERDICT_CACHE",
    "Budget",
    "ConserveConfigError",
    "ConserveSpec",
    "parse_conserve_usage",
    "read_claude_pool",
    "read_codex_pool",
    "evaluate",
    "evaluate_usage_budget",
    "pinned_evaluate",
    "quota_pool_key",
    "record_observed_halt",
    "session_key",
]
