"""Per-call audit files for LLM attempts.

A :class:`CallAuditor` turns the attempts reported to an ``on_attempt``
observer into files under a caller-supplied ``audit_dir``: the prompts sent,
each attempt's response or error, and a ``meta.json`` per chunk. It duck-types
``content_pipeline.llm.platform.CallAttempt`` (reads attributes only), so this
module imports nothing from ``content_pipeline``. Stdlib only.

Layout, per chunk directory ``<audit_dir>/<chunk>/``:

- ``system_prompt.txt`` and ``user_prompt.txt`` -- the first observed attempt.
- ``attempt_<K>_user_prompt.txt`` -- only when that attempt's user prompt
  differs from the base one (a retry that carried feedback).
- ``attempt_<K>_response.txt`` / ``attempt_<K>_error.txt`` -- the response
  text, or the ``"Type: message"`` of a raised call.
- ``meta.json`` -- model, provider label, tokens, cost, ``from_cache``,
  ``wall_ms`` and one entry per attempt (temperature, max_tokens, salt,
  effort, validation/transport attempt numbers, rejections).

``K`` is the 1-based order in which the chunk's attempts were observed. No
path is written into any file. ``audit_dir=None`` writes nothing.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from content_pipeline.provenance.record import json_dump

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_CHUNK = 80


def _clean_chunk(chunk: str) -> str:
    name = _SAFE.sub("_", str(chunk)).strip("._-")[:_MAX_CHUNK].strip("._-")
    return name or "chunk"


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="")


class _Chunk:
    """Mutable state of one chunk; guarded by the auditor's lock."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.count = 0
        self.base_user: Optional[str] = None
        self.attempts: List[Dict[str, Any]] = []
        self.last_response: Any = None
        self.last_rejections: List[str] = []


class CallAuditor:
    """Writes per-call audit files; thread-safe; one per call site."""

    def __init__(self, audit_dir: Optional[Path], *, dump: Callable = json_dump):
        self._dir = Path(audit_dir) if audit_dir is not None else None
        self._dump = dump
        self._lock = threading.Lock()
        self._chunks: Dict[str, _Chunk] = {}

    def _claim(self, chunk: str) -> _Chunk:
        base = _clean_chunk(chunk)
        with self._lock:
            name, n = base, 1
            while name in self._chunks:
                n += 1
                name = "%s-%d" % (base, n)
            state = _Chunk(name)
            self._chunks[name] = state
            return state

    def observer(self, chunk: str) -> Callable[[Any], None]:
        """Return an ``AttemptObserver`` that records ``chunk``'s attempts.

        Each call claims its own directory; a repeated ``chunk`` string gets a
        ``-2``, ``-3`` ... suffix, so concurrent call sites never collide.
        """
        if self._dir is None:
            return lambda attempt: None
        state = self._claim(chunk)

        def _observe(attempt: Any) -> None:
            self._record(state, attempt)

        return _observe

    def _record(self, state: _Chunk, attempt: Any) -> None:
        assert self._dir is not None
        response = getattr(attempt, "response", None)
        error = getattr(attempt, "error", None)
        user = getattr(attempt, "user", "") or ""
        with self._lock:
            out = self._dir / state.name
            out.mkdir(parents=True, exist_ok=True)
            state.count += 1
            k = state.count
            if state.base_user is None:
                state.base_user = user
                _write_text(out / "system_prompt.txt", getattr(attempt, "system", "") or "")
                _write_text(out / "user_prompt.txt", user)
            elif user != state.base_user:
                _write_text(out / ("attempt_%d_user_prompt.txt" % k), user)
            if response is not None:
                _write_text(out / ("attempt_%d_response.txt" % k), getattr(response, "text", "") or "")
            if error:
                _write_text(out / ("attempt_%d_error.txt" % k), str(error))
            rejections = [str(r) for r in (getattr(attempt, "rejections", ()) or ())]
            state.attempts.append({
                "attempt": k,
                "validation_attempt": getattr(attempt, "validation_attempt", None),
                "transport_attempt": getattr(attempt, "transport_attempt", None),
                "model": getattr(attempt, "model", None),
                "temperature": getattr(attempt, "temperature", None),
                "max_tokens": getattr(attempt, "max_tokens", None),
                "cache_salt": getattr(attempt, "cache_salt", None),
                "effort": getattr(attempt, "effort", None),
                "from_cache": bool(getattr(response, "from_cache", False)) if response is not None else False,
                "wall_ms": getattr(response, "wall_ms", None) if response is not None else None,
                "input_tokens": getattr(response, "input_tokens", None) if response is not None else None,
                "output_tokens": getattr(response, "output_tokens", None) if response is not None else None,
                "cost_usd": getattr(response, "reported_cost_usd", None) if response is not None else None,
                "error": str(error) if error else None,
                "rejections": rejections,
            })
            if response is not None:
                state.last_response = response
            state.last_rejections = rejections
            self._dump(self._meta(state), out / "meta.json")

    @staticmethod
    def _meta(state: _Chunk) -> Dict[str, Any]:
        response = state.last_response
        attempts = state.attempts
        costs = [a["cost_usd"] for a in attempts if a["cost_usd"] is not None]

        def _sum(key: str) -> int:
            return sum(a[key] or 0 for a in attempts)

        return {
            "chunk": state.name,
            "model": getattr(response, "model", None) if response is not None else None,
            "provider": getattr(response, "reported_cost_source", None) if response is not None else None,
            "input_tokens": _sum("input_tokens"),
            "output_tokens": _sum("output_tokens"),
            "cost_usd": sum(costs) if costs else None,
            "from_cache": bool(attempts[-1]["from_cache"]) if attempts else False,
            "wall_ms": _sum("wall_ms"),
            "rejections": list(state.last_rejections),
            "attempts": list(attempts),
        }

    def finalize(self) -> None:
        """Write ``summary.json`` listing every chunk (no-op without a dir)."""
        if self._dir is None:
            return
        with self._lock:
            chunks = [self._meta(s) for s in self._chunks.values() if s.attempts]
            if not chunks:
                return
            self._dir.mkdir(parents=True, exist_ok=True)
            costs = [c["cost_usd"] for c in chunks if c["cost_usd"] is not None]
            self._dump({
                "chunks": [
                    {k: c[k] for k in ("chunk", "model", "provider", "input_tokens",
                                       "output_tokens", "cost_usd", "from_cache",
                                       "wall_ms")} | {"attempts": len(c["attempts"])}
                    for c in chunks
                ],
                "totals": {
                    "chunks": len(chunks),
                    "attempts": sum(len(c["attempts"]) for c in chunks),
                    "input_tokens": sum(c["input_tokens"] for c in chunks),
                    "output_tokens": sum(c["output_tokens"] for c in chunks),
                    "cost_usd": sum(costs) if costs else None,
                },
            }, self._dir / "summary.json")

__all__ = [
    "CallAuditor",
]
