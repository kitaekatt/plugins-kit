"""Raw Claude and Codex usage readers.

The file-reading half of the legacy ``usage_budget`` module: it reads the two
usage sources a harness already wrote for its own reasons and returns a
:class:`~llm_kit.registry.conserve.Budget` verdict for one pool. Nothing here
authenticates and nothing reads a credential.

- **claude** -- ``~/.claude/plugins/data/plugins-kit/claude-ui-kit/rate-limits.json``,
  the snapshot claude-ui-kit's statusline drops on every render. A FILE
  CONTRACT, not an import edge: the file is optional and its absence is a
  supported state.
- **codex** -- the newest session rollout under ``~/.codex/sessions/``, whose
  ``token_count`` events carry a ``rate_limits`` object with ``used_percent``,
  ``window_minutes`` and ``resets_at``.

FAIL OPEN: a pool that is missing, malformed, or from a window that has already
reset yields ``STATUS_NO_DATA``. The pacing policy (harness dispatch, session
pinning, verdict-cache writes, observed-halt recording) is NOT here; it lives in
``llm_kit.dispatch.usage_budget`` above this layer, which imports these readers.
Budget, ConserveSpec and the STATUS_*/POOL_* constants are registry values
(``llm_kit.registry.conserve``).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from llm_kit.registry.conserve import (
    POOL_MODEL_SCOPED,
    POOL_PRIMARY,
    POOL_SEVEN_DAY,
    STATUS_AVAILABLE,
    STATUS_NO_DATA,
    STATUS_OUT_OF_QUOTA,
    STATUS_UNDER_QUOTA,
    Budget,
    ConserveSpec,
)

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

_CODEX_EXHAUSTION_LATCH_SECONDS = 5 * _HOUR
"""How long an exhausted reading with no parseable reset time is trusted.

Codex's own principal window (`primary`) is five hours, so this is not an
arbitrary guess: it is the longest a real exhaustion could plausibly persist
without codex itself emitting a fresh, informative rate_limits event.
Exhaustion dropping a codex seat from routing means no new codex session gets
launched from here, so without a bound a stale exhausted reading -- one whose
`usage_limit_exceeded` text was missing or unparseable -- would latch the seat
out of selection forever, past any real reset. See the `usage_limit_exceeded`
reset-text parse in `read_codex_pool`, which this bound only backstops."""

_RESET_TEXT_RE = re.compile(
    r"try again at\s+([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,\s*(\d{4})\s+"
    r"(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)
"""Matches the `usage_limit_exceeded` error text's reset clause, e.g.
``"...or try again at Sep 19th, 2026 3:34 PM."`` codex's own local time,
un-zoned -- see :func:`_parse_reset_text`."""


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


@dataclass(frozen=True)
class _CodexRateLimitReading:
    """The most recent ``rate_limits`` event in a rollout's tail, plus any
    ``usage_limit_exceeded`` error text that followed it (before any LATER
    ``rate_limits`` event superseded it), and the reading event's own
    timestamp -- used only when that error text carries no parseable reset
    time, to bound how long the reading is trusted (see
    :data:`_CODEX_EXHAUSTION_LATCH_SECONDS`)."""

    limits: Mapping[str, Any]
    event_epoch: Optional[int]
    reset_message: Optional[str]


def _last_codex_rate_limits(path: Path) -> Optional[_CodexRateLimitReading]:
    """Scan a rollout's tail for the most recent ``rate_limits`` object.

    Events are read strictly in file order, so a ``usage_limit_exceeded``
    error is attributed to the reading it follows only while no later
    ``rate_limits`` event has superseded that reading in the meantime.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _CODEX_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    found: Optional[Tuple[Mapping[str, Any], Optional[int]]] = None
    reset_message: Optional[str] = None
    for line in tail.splitlines():
        if '"rate_limits"' not in line and "usage_limit_exceeded" not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        limits = _find_rate_limits(event)
        if limits is not None:
            found = (limits, _epoch(event.get("timestamp")))
            reset_message = None
            continue
        if found is not None:
            message = _find_usage_limit_message(event)
            if message is not None:
                reset_message = message
    if found is None:
        return None
    return _CodexRateLimitReading(
        limits=found[0], event_epoch=found[1], reset_message=reset_message
    )


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


def _find_usage_limit_message(node: Any, depth: int = 0) -> Optional[str]:
    """Locate a ``codex_error_info: "usage_limit_exceeded"`` error's message.

    Searched the same defensively-nested way as :func:`_find_rate_limits`,
    including through lists -- the ``task_complete`` event nests its ``error``
    one level under ``payload``, and a future codex release is free to nest it
    differently or wrap it in a list.
    """
    if depth > 6:
        return None
    if isinstance(node, Mapping):
        if node.get("codex_error_info") == "usage_limit_exceeded":
            message = node.get("message")
            return message if isinstance(message, str) else ""
        for value in node.values():
            found = _find_usage_limit_message(value, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(node, list):
        for item in node:
            found = _find_usage_limit_message(item, depth + 1)
            if found is not None:
                return found
    return None


def _parse_reset_text(message: str) -> Optional[int]:
    """Parse a ``usage_limit_exceeded`` message's ``"try again at ..."``
    clause into an epoch, interpreted as codex's LOCAL time (the message
    carries no timezone, so this reads it the same way a human would: as the
    machine's own local clock).
    """
    if not isinstance(message, str):
        return None
    match = _RESET_TEXT_RE.search(message)
    if not match:
        return None
    month_name, day, year, hour, minute, ampm = match.groups()
    try:
        month_num = time.strptime(month_name[:3], "%b").tm_mon
    except ValueError:
        return None
    hour_i = int(hour) % 12
    if ampm.upper() == "PM":
        hour_i += 12
    try:
        moment = datetime(int(year), month_num, int(day), hour_i, int(minute))
    except ValueError:
        return None
    return int(time.mktime(moment.timetuple()))


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
    reading = _last_codex_rate_limits(rollout)
    if reading is None:
        return Budget(
            status=STATUS_NO_DATA, pool=spec.pool,
            detail=f"no 'rate_limits' event in the tail of {rollout.name}",
        )
    limits = reading.limits
    # A recognised exhaustion shape: BOTH windows are null (no `primary` and
    # no `secondary` mapping -- a payload carrying a `primary` window is never
    # treated as exhausted here, whatever `credits` says) AND the account's
    # own `credits` block reports none left. `credits.has_credits: false`
    # ALONE is not exhaustion -- it also appears on every HEALTHY reading from
    # this same plan (39 of 60 sampled rollouts, 2026-09-13..09-15, `primary`
    # present, `has_credits: false`): that field describes purchased EXTRA
    # credits, which this plan never has whether or not it is spent. Checked
    # BEFORE the window lookup below, because a genuinely exhausted account
    # reports no window at all -- the ordinary lookup would read that as
    # no-data forever, never as spent. Fails CLOSED: only an explicit
    # `has_credits: false` paired with `unlimited` not True, on a reading with
    # no window at all, is treated as exhaustion.
    credits = limits.get("credits")
    primary_present = isinstance(limits.get("primary"), Mapping)
    secondary_present = isinstance(limits.get("secondary"), Mapping)
    if not primary_present and not secondary_present and isinstance(credits, Mapping):
        has_credits = credits.get("has_credits")
        unlimited = credits.get("unlimited")
        if has_credits is False and unlimited is not True:
            reset_at = _parse_reset_text(reading.reset_message)
            if reset_at is not None:
                if moment >= reset_at:
                    return Budget(
                        status=STATUS_NO_DATA, pool=spec.pool,
                        detail=(
                            f"codex usage-limit window already reset at {reset_at} "
                            "(parsed from the 'try again at' error text)"
                        ),
                    )
                return Budget(
                    status=STATUS_OUT_OF_QUOTA, pool=spec.pool,
                    detail=(
                        "codex account reports no credits remaining; resets at "
                        f"{reset_at} (parsed from the 'try again at' error text)"
                    ),
                    remaining=0.0,
                    resets_at=reset_at,
                )
            # No parseable reset time -- latch out-of-quota only while the
            # exhausted reading is recent (see
            # _CODEX_EXHAUSTION_LATCH_SECONDS), falling back to the rollout
            # file's mtime when the event itself carries no usable timestamp.
            event_epoch = reading.event_epoch
            if event_epoch is None:
                try:
                    event_epoch = int(rollout.stat().st_mtime)
                except OSError:
                    event_epoch = None
            if event_epoch is not None and moment - event_epoch < _CODEX_EXHAUSTION_LATCH_SECONDS:
                return Budget(
                    status=STATUS_OUT_OF_QUOTA, pool=spec.pool,
                    detail=(
                        "codex account reports no credits remaining; no parseable "
                        "reset time, latched out-of-quota while the reading is "
                        f"under {_CODEX_EXHAUSTION_LATCH_SECONDS // _HOUR}h old"
                    ),
                    remaining=0.0,
                    # The latch end is the reset a pinned session honours;
                    # without it the verdict would hold for the whole session.
                    resets_at=int(event_epoch + _CODEX_EXHAUSTION_LATCH_SECONDS),
                )
            return Budget(
                status=STATUS_NO_DATA, pool=spec.pool,
                detail=(
                    "codex exhaustion reading carries no parseable reset time and "
                    f"is older than the {_CODEX_EXHAUSTION_LATCH_SECONDS // _HOUR}h "
                    "latch; treating as stale rather than assuming it still holds"
                ),
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


__all__ = [
    "CLAUDE_SNAPSHOT",
    "CODEX_SESSIONS_DIR",
    "read_claude_pool",
    "read_codex_pool",
]
