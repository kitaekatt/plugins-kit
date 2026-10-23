"""The adapters between the task CLI verbs and the task answers engine.

``task.py review`` shows each task's one-line summary, which is the headline of the task's orientation answer.
``answer_missing_orientations`` makes that answer through ``answer_engine.run_pass`` (the engine ``task.py
answers refresh`` and ``task-kit answers refresh`` run): it commits and publishes only this machine's answer
files under the repository lock, and it writes neither task.yaml nor log.md. The cost classes a model may use
are the engine's: llm-kit's optional consent layer with scope ``task-kit`` (``llm_kit.registry.consent``) unless the caller names them.

``orientation_supersedes_summary`` serves ``update --summary``: whether a usable orientation answer already
takes the place of ``task.summary`` in listings.
"""

from __future__ import annotations

import dataclasses
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import answer_engine, answer_scripts, answer_store, answers_config, export, questions
from .answers_types import Catalog
from .discovery import read_task_block
from .listing import ORIENTATION_QUESTION, TaskListing, TaskView

ELIGIBLE_STATUSES = frozenset(("active", "blocked", "closed", "deferred"))
# The orientation states a review makes an answer for. ``current`` needs none; ``not-applicable`` has none.
WANTED_STATES = frozenset((answer_store.STATE_MISSING, answer_store.STATE_STALE, answer_store.STATE_INVALID))


@dataclass(frozen=True)
class ReviewAnswerReport:
    """What a review's answering step did.

    ``answered`` and ``failed`` name tasks (``project/id`` in all scope, else ``id``). ``refusals`` are the
    repository-level reasons no model could answer (llm_kit not importable). ``notes`` are advisory lines
    (a task folder with uncommitted changes is skipped, an answer was superseded, answers wait to be published).
    """

    answered: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    refusals: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def attempted(self) -> int:
        return len(self.answered) + len(self.failed)

    @property
    def broken(self) -> bool:
        return bool(self.failed or self.refusals)


def _view_root(view: TaskView, fallback: Path) -> Path:
    return view.project_root or fallback


def _view_label(listing: TaskListing, view: TaskView) -> str:
    return f"{view.project_name}/{view.id}" if listing.scope == "all" else view.id


def _wants_orientation(view: TaskView) -> bool:
    state = (view.answers or {}).get(ORIENTATION_QUESTION)
    return state is not None and state.get("state") in WANTED_STATES


def _orientation_only(catalog: Catalog) -> Catalog:
    question = catalog.get(ORIENTATION_QUESTION)
    if question is None:
        raise answers_config.AnswersConfigError(f"the question catalog holds no {ORIENTATION_QUESTION} question")
    return dataclasses.replace(catalog, questions=(question,))


def answer_missing_orientations(
    listing: TaskListing,
    project_root: Path,
    *,
    allow: Any = None,
    run: Callable[..., Any] | None = None,
    config: answers_config.AnswersConfig | None = None,
    catalog: Catalog | None = None,
    machine: Any = None,
    data_dir: Path | None = None,
    now: Callable[[], datetime.datetime] = datetime.datetime.now,
    publish: bool = True,
) -> ReviewAnswerReport:
    """Answer the orientation question of every listed task whose answer is missing, stale or invalid.

    One engine pass per task repository covers every such task of it (no batch cut). ``allow`` is the cost
    classes (None: llm-kit's consent layer, scope ``task-kit``, read only when a model must answer). ``run``, ``config``, ``catalog``,
    ``machine``, ``data_dir``, ``now`` and ``publish`` replace the defaults (tests). A listing with nothing to
    answer touches neither git nor llm-kit.
    """
    candidates = [
        view
        for view in listing.views
        if view.status in ELIGIBLE_STATUSES
        and view.current_fingerprint is not None
        and (_view_root(view, project_root) / view.id).is_dir()
        and _wants_orientation(view)
    ]
    if not candidates:
        return ReviewAnswerReport()
    if catalog is None:
        catalog = questions.load_catalog(questions.default_shipped_dir(), questions.default_fleet_dir())
    orientation = _orientation_only(catalog)
    if config is None:
        config = answers_config.load()
    if machine is None:
        machine = export.current_machine()

    failed: list[tuple[str, str]] = []
    grouped: dict[Path, list[answer_engine.TaskTarget]] = {}
    labels: dict[Path, dict[str, str]] = {}
    for view in candidates:
        folder = _view_root(view, project_root) / view.id
        label = _view_label(listing, view)
        top = answer_scripts.repo_top_of(folder)
        if top is None:
            failed.append((label, "the task folder is not inside a git work tree, so it has no answers"))
            continue
        top = top.resolve()
        grouped.setdefault(top, []).append(answer_engine.TaskTarget(folder, view.status))
        labels.setdefault(top, {})[folder.resolve().relative_to(top).as_posix()] = label

    lazy = answer_engine.LazyAllow(allow)
    answered: list[str] = []
    refusals: list[str] = []
    notes: list[str] = []
    for top, targets in sorted(grouped.items()):
        names = labels[top]
        report = answer_engine.run_pass(
            top, machine, config, budget=answer_engine.Budget(batch_size=len(targets)), targets=targets,
            catalog=orientation, allow=lazy, run=run, data_dir=data_dir, now=now, publish=publish,
        )

        def label_of(task: str) -> str:
            return names.get(task, task)

        if report.outcome == answer_engine.OUTCOME_SKIPPED:
            reason = f"the repository {top} was skipped: {report.reason}"
            failed.extend((names[rel], reason) for rel in sorted(names))
            continue
        answered.extend(label_of(item.task) for item in report.answered)
        failed.extend((label_of(item.task), item.reason) for item in report.failed)
        if report.refusal is not None:
            refusals.append(f"{report.refusal['message']} ({report.refusal['state']})")
        if report.commit_error is not None:
            failed.append((str(top), f"answers were written but not committed: {report.commit_error}"))
        notes.extend(f"{label_of(task)}: skipped: {reason}" for task, reason in report.skipped_tasks)
        notes.extend(f"{label_of(item.task)}: not written: {item.reason}" for item in report.dropped)
        notes.extend(report.notes)
        if report.unpublished:
            notes.append(f"{report.unpublished} answer file(s) in {top} are committed but not yet published")
    resolved = lazy.resolved
    if resolved is not None and resolved.note:
        notes.append(resolved.note)
    return ReviewAnswerReport(
        answered=tuple(answered),
        failed=tuple(failed),
        refusals=tuple(dict.fromkeys(refusals)),
        notes=tuple(dict.fromkeys(notes)),
    )


def orientation_supersedes_summary(
    folder: Path,
    status: str,
    *,
    catalog: Catalog | None = None,
    now: Callable[[], datetime.datetime] = datetime.datetime.now,
) -> bool:
    """Whether the task in ``folder`` has a usable orientation answer, current or stale.

    Such an answer takes the place of ``task.summary`` in ``list`` and ``review``. A catalog that cannot be
    loaded, or one without the orientation question, answers False: the legacy summary is then what is shown.
    """
    if catalog is None:
        try:
            catalog = questions.load_catalog(questions.default_shipped_dir(), questions.default_fleet_dir())
        except questions.QuestionError:
            return False
    question = catalog.get(ORIENTATION_QUESTION)
    if question is None:
        return False
    folder = Path(folder)
    state = answer_store.answer_state(
        question, folder, read_task_block(folder), status, repo_top=None, log=None, now=now()
    )
    return answer_store.orientation_headline({ORIENTATION_QUESTION: state}) is not None
