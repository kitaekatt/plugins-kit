"""Per-stage store snapshots and a liveness event trail for the convergence loop.

Both classes are :data:`~content_pipeline.pipeline.convergence_loop.LoopObserver`
callables: pass them in ``observers=``. Stdlib only. Everything is written
under a caller-supplied ``bundle_dir`` / path, never one derived from this
file's location.

Layout, per cycle ``N``::

    cycle-N/store.before-grade<suffix>   store at cycle start
    cycle-N/store.after-grade<suffix>
    cycle-N/store.after-select<suffix>
    cycle-N/store.after-apply<suffix>
    cycle-N/store.after-fill<suffix>

A later stage's ``before`` equals the previous stage's ``after``, so only five
files are written; :func:`snapshot_label` maps either name to the file. A stage
the loop skips (its callable is ``None``) emits no event and writes no file.

Observers here never swallow an error: a failed snapshot or log write raises
out of the loop, because a provenance trail that fails silently is the defect
this package exists to remove. In the tracked execution path an observer error
aborts an inline-driver wave and leaves the unit's claim leased until its lease
expires; it is not recorded as a unit failure. These observers are meant for a
consumer's own loop.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from content_pipeline.pipeline.convergence_loop import (
    STAGES,
    LoopEvent,
    LoopEventKind,
)

_PHASES = ("before", "after")


def snapshot_label(stage: str, phase: str) -> str:
    """Name of the snapshot holding the store ``phase`` ``stage`` runs.

    ``("fill", "before")`` -> ``"after-apply"`` (the file the previous stage
    wrote); ``("grade", "before")`` -> ``"before-grade"``; ``(s, "after")`` ->
    ``"after-<s>"``.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")
    if phase not in _PHASES:
        raise ValueError(f"unknown phase {phase!r}; expected one of {_PHASES}")
    if phase == "after":
        return f"after-{stage}"
    index = STAGES.index(stage)
    if index == 0:
        return f"before-{stage}"
    return f"after-{STAGES[index - 1]}"


class StageSnapshotter:
    """LoopObserver writing the store at each stage boundary.

    ``write(store, path)`` persists one store to ``path`` (for a path store use
    ``provenance.record.snapshot_file`` wrapped to copy the live file; for a
    ``CandidateStore`` wrap ``dump_store``). The file is written to a sibling
    temporary name and renamed into place, so an interrupted write never
    leaves a plausible-looking partial snapshot. A ``write`` error propagates
    and stops the loop; a ``write`` that returns without creating the file
    raises ``FileNotFoundError`` naming the store that was to be copied.
    """

    def __init__(
        self,
        bundle_dir: Any,
        *,
        write: Callable[[Any, Path], None],
        suffix: str = ".json",
    ) -> None:
        self.bundle_dir = Path(bundle_dir)
        self._write = write
        self.suffix = suffix

    def path_for(self, cycle: int, label: str) -> Path:
        """Where the snapshot ``label`` of ``cycle`` is written."""
        return self.bundle_dir / f"cycle-{cycle}" / f"store.{label}{self.suffix}"

    def _snap(self, cycle: int, label: str, store: Any) -> None:
        dst = self.path_for(cycle, label)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".partial")
        try:
            self._write(store, tmp)
            if not tmp.exists():
                # The write callback produced nothing (a path-store copier
                # returns without writing when its source is absent). Name the
                # store, not the temporary file inside the audit directory.
                what = (
                    f"store file '{store}'"
                    if isinstance(store, (str, os.PathLike))
                    else f"store {type(store).__name__} object"
                )
                raise FileNotFoundError(
                    f"cannot snapshot {dst.name} (cycle {cycle}): the {what} "
                    f"is missing, so the write produced no file for {dst}"
                )
            os.replace(tmp, dst)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def __call__(self, event: LoopEvent) -> None:
        if event.cycle is None:
            return
        if event.kind is LoopEventKind.CYCLE_STARTED:
            self._snap(event.cycle, snapshot_label(STAGES[0], "before"), event.store)
        elif event.kind is LoopEventKind.STAGE_FINISHED and event.stage in STAGES:
            self._snap(event.cycle, snapshot_label(event.stage, "after"), event.store)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value.value if hasattr(value, "value") else value
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return repr(value)


class EventLog:
    """LoopObserver appending one JSON line per event (the liveness trail).

    The store is omitted. Each line is flushed and closed before the loop
    continues, so a tail of the file shows the last boundary reached. An error
    carried by the event is recorded as ``"Type: message"``. A write error
    propagates.
    """

    def __init__(self, path: Any) -> None:
        self.path = Path(path)

    def __call__(self, event: LoopEvent) -> None:
        error = event.error
        line = {
            "kind": event.kind.value,
            "cycle": event.cycle,
            "stage": event.stage,
            "at": event.at,
            "elapsed_s": event.elapsed_s,
            "round": _jsonable(event.round),
            "verdict": _jsonable(event.verdict),
            "error": None if error is None else f"{type(error).__name__}: {error}",
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="ascii") as handle:
            handle.write(json.dumps(line, ensure_ascii=True) + "\n")
            handle.flush()


__all__ = ["StageSnapshotter", "EventLog", "snapshot_label"]
