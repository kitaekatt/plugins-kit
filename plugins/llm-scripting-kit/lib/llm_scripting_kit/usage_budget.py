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
is the same honesty rule :mod:`llm_scripting_kit.reachability` applies one axis
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
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

STATUS_AVAILABLE = "available"
"""The pool was read and the burn-down is at or ahead of the clock."""

STATUS_UNDER_QUOTA = "under-quota"
"""The pool was read and quota is being spent faster than time passes. The
model stays USABLE and is merely de-prioritized -- see the module docstring on
why this is ordering rather than exclusion."""

STATUS_OUT_OF_QUOTA = "out-of-quota"
"""The pool was read and nothing is left in it. The model is DISABLED: unlike
:data:`STATUS_UNDER_QUOTA` this is not a preference, it is the absence of the
capability, and a call against it would fail."""

STATUS_NO_DATA = "no-data"
"""No usable pool reading. NEVER a reason to disable or de-prioritize the model -- see the
module docstring on failing open. Distinct from :data:`STATUS_AVAILABLE`
because "nothing gated this" and "the pacing check passed" are different
facts, and a caller reporting capacity must not state the second when it
observed the first."""

#: The all-model weekly window. The default pool for an entry that opts in
#: with a bare ``conserve_usage: true``, on either harness.
POOL_SEVEN_DAY = "seven_day"

#: The per-model weekly bucket carried in the claude payload's ``model_scoped``
#: array. Selected by ``display_name`` -- see :func:`_read_model_scoped`.
POOL_MODEL_SCOPED = "model_scoped"

#: Codex's own name for its principal window.
POOL_PRIMARY = "primary"

_HOUR = 3600
_DAY = 24 * _HOUR

# Window lengths for the claude pools, which -- unlike codex -- state the
# window only in the key name. `model_scoped` entries are rendered by Claude
# Code itself as "Current week (<model>)", so they are seven-day windows.
_CLAUDE_WINDOW_SECONDS = {
    "five_hour": 5 * _HOUR,
    "seven_day": 7 * _DAY,
    "seven_day_opus": 7 * _DAY,
    "seven_day_sonnet": 7 * _DAY,
    "seven_day_oauth_apps": 7 * _DAY,
    "seven_day_overage_included": 7 * _DAY,
    POOL_MODEL_SCOPED: 7 * _DAY,
}

CLAUDE_SNAPSHOT = (
    Path.home() / ".claude" / "plugins" / "data" / "plugins-kit"
    / "claude-ui-kit" / "rate-limits.json"
)
"""Where claude-ui-kit's statusline drops its ``rate_limits`` copy."""

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
"""Root of codex's session rollouts, whose newest file carries the latest
``rate_limits`` event."""

VERDICT_CACHE = (
    Path.home() / ".claude" / "plugins" / "data" / "plugins-kit"
    / "llm-scripting-kit" / "usage-verdicts.json"
)
"""Session-pinned verdicts. See the module docstring on pinning."""

_PACE_EPSILON = 1e-9
"""Tolerance on the pacing comparison, so exactly-on-pace reads as available.

The rule is "remaining is AT LEAST the window fraction", and both sides are
computed by division, so an exact boundary does not survive binary floating
point: 80% used leaves ``1 - 80/100 == 0.19999999999999996``, which is less
than a window fraction of exactly ``0.2`` and would conserve a model that is
precisely on pace. The epsilon is far below any real difference in a
percentage reported to whole numbers."""

_CODEX_TAIL_BYTES = 512 * 1024
"""How much of a codex rollout's tail to scan for the last ``rate_limits``.
A rollout grows without bound and the newest reading is at the end, so the
whole file is never read. Generous enough to span many events."""


@dataclass(frozen=True)
class Budget:
    """One pacing verdict. Constructors never raise.

    ``status`` is one of :data:`STATUS_AVAILABLE`, :data:`STATUS_UNDER_QUOTA`,
    :data:`STATUS_OUT_OF_QUOTA`, or :data:`STATUS_NO_DATA`. ``remaining`` and ``window_remaining`` are the
    two fractions the rule compares, both None when there was no reading.
    ``pool`` names the window consulted and ``detail`` says, in one line, why
    the verdict came out as it did.
    """

    status: str
    pool: str
    detail: str
    remaining: Optional[float] = None
    window_remaining: Optional[float] = None
    resets_at: Optional[int] = None

    @property
    def usable(self) -> bool:
        """False ONLY when the pool is spent. Fails open on every other state.

        The predicate a caller gates selection on. Deliberately not the
        negation of :attr:`deprioritized`: an under-quota model is usable and
        a caller that conflated the two would drop a capability it still has.
        """
        return self.status != STATUS_OUT_OF_QUOTA

    @property
    def deprioritized(self) -> bool:
        """True when the model is usable but should lose to an equal peer."""
        return self.status == STATUS_UNDER_QUOTA

    def to_json(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "pool": self.pool,
            "detail": self.detail,
            "remaining": self.remaining,
            "window_remaining": self.window_remaining,
            "resets_at": self.resets_at,
        }


class ConserveConfigError(ValueError):
    """A ``conserve_usage`` declaration that cannot be read as one."""


@dataclass(frozen=True)
class ConserveSpec:
    """A parsed ``conserve_usage`` declaration.

    ``display_name`` applies only to :data:`POOL_MODEL_SCOPED`, where the pool
    is an array and an entry has to be picked out of it by label.
    """

    pool: str
    display_name: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"pool": self.pool}
        if self.display_name is not None:
            result["display_name"] = self.display_name
        return result


def parse_conserve_usage(
    value: object, *, source: str, entry_id: str
) -> Optional[ConserveSpec]:
    """Read a ``conserve_usage`` value, or None when the entry does not opt in.

    Accepted forms, and nothing else::

        conserve_usage: false                       # or omitted -- not opted in
        conserve_usage: true                        # the all-model weekly pool
        conserve_usage: {pool: seven_day}
        conserve_usage: {pool: model_scoped, display_name: Fable}

    Raises:
        ConserveConfigError: any other shape. A misspelled pool is a silent
            no-op if tolerated -- the entry would read as opted-in-but-never-
            paced -- so it is refused at parse time instead.
    """
    if value is None or value is False:
        return None
    if value is True:
        return ConserveSpec(pool=POOL_SEVEN_DAY)
    if not isinstance(value, Mapping):
        raise ConserveConfigError(
            f"{source}: entry '{entry_id}' has an invalid 'conserve_usage' "
            f"({value!r}); expected a boolean or a mapping with 'pool'"
        )
    unknown = set(value) - {"pool", "display_name"}
    if unknown:
        raise ConserveConfigError(
            f"{source}: entry '{entry_id}' has unknown 'conserve_usage' keys "
            f"({', '.join(sorted(unknown))}); expected 'pool' and 'display_name'"
        )
    pool = value.get("pool")
    if not isinstance(pool, str) or not pool.strip():
        raise ConserveConfigError(
            f"{source}: entry '{entry_id}' has a 'conserve_usage' with no "
            "'pool' (a non-empty string is required)"
        )
    display_name = value.get("display_name")
    if display_name is not None and (
        not isinstance(display_name, str) or not display_name.strip()
    ):
        raise ConserveConfigError(
            f"{source}: entry '{entry_id}' has a non-string "
            f"'conserve_usage.display_name' ({display_name!r})"
        )
    spec = ConserveSpec(
        pool=pool.strip(),
        display_name=display_name.strip() if isinstance(display_name, str) else None,
    )
    if spec.pool == POOL_MODEL_SCOPED and spec.display_name is None:
        # The pool is an ARRAY; without a label there is no bucket to select, so
        # this opt-in could only ever return no-data. Refused for the same
        # reason as a misspelled pool: an entry that is opted in and can never
        # conserve is indistinguishable from a working one.
        raise ConserveConfigError(
            f"{source}: entry '{entry_id}' declares pool '{POOL_MODEL_SCOPED}' "
            "with no 'display_name'; that pool is an array and needs a label "
            "to select a bucket from it"
        )
    return spec


def _epoch(value: object) -> Optional[int]:
    """Read a reset timestamp in either shape the payloads use.

    The on-disk claude snapshot carries an integer epoch; ``model_scoped``
    entries carry an ISO 8601 string. Both are the same fact, and a reader
    that handles only one silently loses the pool that uses the other.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.isdigit():
            return int(text)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return int(parsed.timestamp())
    return None


def _used_percentage(window: Mapping[str, Any]) -> Optional[float]:
    """Read percent-used under either name the two payload shapes use.

    The statusline snapshot says ``used_percentage``; ``model_scoped`` entries
    and codex both say ``utilization`` / ``used_percent``. All three are a
    percentage on the same 0-100 scale.
    """
    for key in ("used_percentage", "utilization", "used_percent"):
        value = window.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _verdict(
    pool: str, used_pct: float, resets_at: int, window_seconds: float, *, now: float
) -> Budget:
    """Apply the pacing rule to one read window."""
    remaining = max(0.0, min(1.0, 1.0 - used_pct / 100.0))
    seconds_left = resets_at - now
    if seconds_left <= 0:
        return Budget(
            status=STATUS_NO_DATA, pool=pool,
            detail=f"window '{pool}' already reset at {int(resets_at)}; reading is from a dead window",
        )
    window_remaining = max(0.0, min(1.0, seconds_left / window_seconds))
    if remaining <= 0.0:
        # Checked BEFORE the pacing test: an empty pool is behind pace by
        # definition, and reporting it as merely under-quota would leave a
        # model in selection that cannot answer a call.
        status = STATUS_OUT_OF_QUOTA
    elif remaining + _PACE_EPSILON < window_remaining:
        status = STATUS_UNDER_QUOTA
    else:
        status = STATUS_AVAILABLE
    return Budget(
        status=status,
        pool=pool,
        detail=(
            f"{remaining * 100:.0f}% of '{pool}' remaining with "
            f"{window_remaining * 100:.0f}% of the window left"
        ),
        remaining=remaining,
        window_remaining=window_remaining,
        resets_at=int(resets_at),
    )


def _load_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_model_scoped(
    rate_limits: Mapping[str, Any], display_name: Optional[str]
) -> Tuple[Optional[Mapping[str, Any]], Optional[str]]:
    """Pick one entry out of the ``model_scoped`` array by its label.

    Returns ``(window, error)``. The array is additive and present only when
    the server emits it, so its absence is an ordinary no-data outcome rather
    than a defect.
    """
    buckets = rate_limits.get(POOL_MODEL_SCOPED)
    if not isinstance(buckets, list) or not buckets:
        return None, "no 'model_scoped' buckets in the snapshot (the server emits them for some accounts only)"
    if display_name is None:
        # Unreachable through parse_conserve_usage, which refuses this pairing;
        # kept because read_claude_pool accepts a hand-built ConserveSpec too.
        return None, "pool 'model_scoped' needs a 'display_name' to select a bucket"
    wanted = display_name.strip().lower()
    for bucket in buckets:
        if not isinstance(bucket, Mapping):
            continue
        label = bucket.get("display_name")
        if isinstance(label, str) and label.strip().lower() == wanted:
            return bucket, None
    labels = ", ".join(
        sorted(
            str(b.get("display_name"))
            for b in buckets
            if isinstance(b, Mapping) and b.get("display_name")
        )
    ) or "<none>"
    return None, f"no 'model_scoped' bucket named {display_name!r} (present: {labels})"


def read_claude_pool(
    spec: ConserveSpec, *, now: Optional[float] = None, snapshot: Optional[Path] = None
) -> Budget:
    """Verdict for a claude entry, read from the statusline snapshot."""
    moment = time.time() if now is None else now
    path = CLAUDE_SNAPSHOT if snapshot is None else snapshot
    data = _load_json(path)
    if not isinstance(data, Mapping):
        return Budget(
            status=STATUS_NO_DATA, pool=spec.pool,
            detail=f"no readable claude usage snapshot at {path}",
        )
    rate_limits = data.get("rate_limits")
    if not isinstance(rate_limits, Mapping):
        return Budget(
            status=STATUS_NO_DATA, pool=spec.pool,
            detail=f"claude usage snapshot at {path} carries no 'rate_limits'",
        )

    if spec.pool == POOL_MODEL_SCOPED:
        window, error = _read_model_scoped(rate_limits, spec.display_name)
        if window is None:
            return Budget(status=STATUS_NO_DATA, pool=spec.pool, detail=error or "no bucket")
    else:
        candidate = rate_limits.get(spec.pool)
        if not isinstance(candidate, Mapping):
            known = ", ".join(sorted(k for k, v in rate_limits.items() if isinstance(v, Mapping))) or "<none>"
            return Budget(
                status=STATUS_NO_DATA, pool=spec.pool,
                detail=f"no '{spec.pool}' window in the claude snapshot (present: {known})",
            )
        window = candidate

    used = _used_percentage(window)
    resets_at = _epoch(window.get("resets_at"))
    if used is None or resets_at is None:
        return Budget(
            status=STATUS_NO_DATA, pool=spec.pool,
            detail=f"claude window '{spec.pool}' is missing a usage percentage or a reset time",
        )
    window_seconds = _CLAUDE_WINDOW_SECONDS.get(spec.pool)
    if window_seconds is None:
        return Budget(
            status=STATUS_NO_DATA, pool=spec.pool,
            detail=f"no known window length for claude pool '{spec.pool}'",
        )
    return _verdict(spec.pool, used, resets_at, window_seconds, now=moment)


def _newest_codex_rollout(sessions_dir: Path) -> Optional[Path]:
    try:
        rollouts = [p for p in sessions_dir.rglob("*.jsonl") if p.is_file()]
    except OSError:
        return None
    if not rollouts:
        return None
    return max(rollouts, key=lambda p: p.stat().st_mtime)


def _last_codex_rate_limits(path: Path) -> Optional[Mapping[str, Any]]:
    """Scan a rollout's tail for the most recent ``rate_limits`` object."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _CODEX_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    found: Optional[Mapping[str, Any]] = None
    for line in tail.splitlines():
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        limits = _find_rate_limits(event)
        if limits is not None:
            found = limits
    return found


def _find_rate_limits(node: Any, depth: int = 0) -> Optional[Mapping[str, Any]]:
    """Locate the ``rate_limits`` object wherever the event nests it.

    Codex's event envelope has changed shape across releases, so the key is
    searched for rather than addressed by a fixed path -- a path that is right
    for one codex version and silently wrong for the next is the failure this
    avoids.
    """
    if depth > 6 or not isinstance(node, Mapping):
        return None
    limits = node.get("rate_limits")
    if isinstance(limits, Mapping):
        return limits
    for value in node.values():
        if isinstance(value, Mapping):
            found = _find_rate_limits(value, depth + 1)
            if found is not None:
                return found
    return None


def read_codex_pool(
    spec: ConserveSpec, *, now: Optional[float] = None, sessions_dir: Optional[Path] = None
) -> Budget:
    """Verdict for a codex entry, read from the newest session rollout.

    Codex states its window length in the data (``window_minutes``), so unlike
    claude no window length has to be inferred from the pool's name.
    """
    moment = time.time() if now is None else now
    root = CODEX_SESSIONS_DIR if sessions_dir is None else sessions_dir
    rollout = _newest_codex_rollout(root)
    if rollout is None:
        return Budget(
            status=STATUS_NO_DATA, pool=spec.pool,
            detail=f"no codex session rollout under {root}",
        )
    limits = _last_codex_rate_limits(rollout)
    if limits is None:
        return Budget(
            status=STATUS_NO_DATA, pool=spec.pool,
            detail=f"no 'rate_limits' event in the tail of {rollout.name}",
        )
    # Codex names its windows `primary` and `secondary`; `seven_day` is this
    # module's harness-neutral default, so it resolves to the principal window
    # rather than failing on a name codex has never emitted.
    key = POOL_PRIMARY if spec.pool == POOL_SEVEN_DAY else spec.pool
    window = limits.get(key)
    if not isinstance(window, Mapping):
        known = ", ".join(sorted(k for k, v in limits.items() if isinstance(v, Mapping))) or "<none>"
        return Budget(
            status=STATUS_NO_DATA, pool=spec.pool,
            detail=f"no '{key}' window in the codex rate_limits (present: {known})",
        )
    used = _used_percentage(window)
    resets_at = _epoch(window.get("resets_at"))
    minutes = window.get("window_minutes")
    if used is None or resets_at is None:
        return Budget(
            status=STATUS_NO_DATA, pool=spec.pool,
            detail=f"codex window '{key}' is missing a usage percentage or a reset time",
        )
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
        return Budget(
            status=STATUS_NO_DATA, pool=spec.pool,
            detail=f"codex window '{key}' has no usable 'window_minutes'",
        )
    return _verdict(spec.pool, used, resets_at, float(minutes) * 60.0, now=moment)


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
        return read_claude_pool(spec, now=now)
    if key == "codex":
        return read_codex_pool(spec, now=now)
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
    data = _load_json(path)
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
    "session_key",
]
