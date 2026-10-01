"""Replay one convergence-loop stage from a saved snapshot, with an edited input.

Stdlib only. The source bundle (written by a run recorded with
:class:`~content_pipeline.provenance.snapshot.StageSnapshotter` and
:func:`~content_pipeline.provenance.record.start_run`) is only READ. The replay
writes a new run under a caller-supplied ``out_bundle``: the edited store before
the stage, the store after it, and a provenance record whose ``source`` names
the source run, cycle, stage and snapshot (with its sha256).

A stage that reads inputs the record did not hash (a shared lookup table, a
template directory) yields a link that looks complete and is not: pass such
inputs through ``inputs`` so the record hashes them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from content_pipeline.pipeline.convergence_loop import STAGES, Stage
from content_pipeline.provenance.record import (
    ProvenanceRecord,
    SourceLink,
    json_dump,
    json_load,
    portable_path,
    sha256_file,
    start_run,
)
from content_pipeline.provenance.snapshot import StageSnapshotter, snapshot_label


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of :func:`replay_stage`."""

    store: Any
    record: ProvenanceRecord
    bundle_dir: Path


def _inside(child: Path, parent: Path) -> bool:
    child = Path(os.path.abspath(child))
    parent = Path(os.path.abspath(parent))
    return child == parent or parent in child.parents


def _write_atomic(write: Callable[[Any, Path], None], store: Any, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".partial")
    try:
        write(store, tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def replay_stage(
    source_bundle: Any,
    *,
    cycle: int,
    stage: str,
    run: Stage,
    out_bundle: Any,
    root: Any,
    materialize: Callable[[Path, Path], Any],
    write: Callable[[Any, Path], None],
    edit: Optional[Callable[[Any], Any]] = None,
    edits: Sequence[str] = (),
    params: Optional[Mapping[str, Any]] = None,
    inputs: Optional[Mapping[str, Optional[Path]]] = None,
    load: Callable[[Path], Any] = json_load,
    suffix: str = ".json",
    record_filename: str = "run.json",
) -> ReplayResult:
    """Re-run ``stage`` of ``cycle`` from the source bundle's snapshot.

    ``materialize(snapshot, workdir)`` turns the snapshot file into a live
    store under ``out_bundle/work``; ``edit(store)`` may change it (mutate in
    place, or return a replacement); ``run(store, cycle)`` is the stage
    callable, whose returned store (when not None) replaces the working one.
    ``write(store, path)`` persists a store (as for ``StageSnapshotter``).
    ``suffix`` and ``record_filename`` must match how the source was written.

    Raises ``ValueError`` for an unknown stage or an ``out_bundle`` that is, or
    lies inside, the source bundle; ``FileNotFoundError`` when the source
    record or the stage's snapshot is missing (a stage the source skipped
    wrote none). Nothing is written before those checks pass.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")
    source_bundle = Path(source_bundle)
    out_bundle = Path(out_bundle)
    root = Path(root)
    if _inside(out_bundle, source_bundle):
        raise ValueError("out_bundle must be outside the source bundle")

    source_record_path = source_bundle / record_filename
    if not source_record_path.is_file():
        raise FileNotFoundError(f"source run record not found: {source_record_path}")
    source_record = load(source_record_path)
    source_run_id = source_record["run_id"]

    label = snapshot_label(stage, "before")
    snapshot = StageSnapshotter(source_bundle, write=write, suffix=suffix).path_for(
        cycle, label
    )
    if not snapshot.is_file():
        raise FileNotFoundError(
            f"snapshot {label!r} of cycle {cycle} not found in source bundle: "
            f"{snapshot} (a stage the source run skipped writes none)"
        )

    link = SourceLink(
        run_id=source_run_id,
        record=portable_path(source_record_path, root=root),
        cycle=cycle,
        stage=stage,
        snapshot=portable_path(snapshot, root=root),
        snapshot_sha256=sha256_file(snapshot),
    )
    recorded_edits = list(edits)
    if edit is not None and not recorded_edits:
        recorded_edits.append("edit: " + getattr(edit, "__name__", "callable"))

    out_snaps = StageSnapshotter(out_bundle, write=write, suffix=suffix)
    with start_run(
        out_bundle,
        root=root,
        params=params,
        inputs=inputs,
        source=link,
        edits=recorded_edits,
        filename=record_filename,
    ) as recorder:
        workdir = out_bundle / "work"
        workdir.mkdir(parents=True, exist_ok=True)
        store = materialize(snapshot, workdir)
        if edit is not None:
            edited = edit(store)
            store = store if edited is None else edited
        before = out_snaps.path_for(cycle, f"before-{stage}")
        _write_atomic(write, store, before)

        result = run(store, cycle)
        store = store if result is None else result
        after = out_snaps.path_for(cycle, snapshot_label(stage, "after"))
        _write_atomic(write, store, after)

        recorder.finish(
            result={
                "cycle": cycle,
                "stage": stage,
                "before": portable_path(before, root=root),
                "after": portable_path(after, root=root),
            }
        )
    return ReplayResult(store=store, record=recorder.record, bundle_dir=out_bundle)


__all__ = ["ReplayResult", "replay_stage"]
