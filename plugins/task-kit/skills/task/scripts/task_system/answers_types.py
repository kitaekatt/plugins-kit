"""Frozen data types of the task questions-and-answers system (summary-on-commit-plan.md, D1 and the interface block).

This module is an interface freeze: later units (answer store, answer chain, publisher) code against these
types, so a change here is reported, never made in passing. It holds data only; loading, reading and git
access live in ``questions``, ``question_inputs`` and ``answer_scripts``.

``Question``, ``InputBundle``, ``Answerer`` and ``RunFn`` are the plan's interface block. ``Catalog``,
``CommitInfo``, ``RepoLog`` and ``ScriptContext`` are the types the plan's module interfaces name without
defining (``load_catalog -> Catalog``; ``modification_order(repo_top, folders, log)``; ``ScriptContext``).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class Question:
    id: str
    version: int
    title: str
    applies_to: tuple[str, ...]
    inputs: tuple[str, ...]
    max_input_chars: int
    script: str | None
    prompt: str | None
    answer_schema: Mapping[str, Any]  # read-only mapping
    grounding: tuple[str, ...]
    models: tuple[str, ...] | None
    effort: str | None
    source: Path
    digest: str  # sha256 hex of the canonical definition


@dataclass(frozen=True)
class InputBundle:
    task_id: str
    texts: tuple[tuple[str, str], ...]  # (reader, canonical text), declared order
    truncated: tuple[str, ...]  # readers whose text was cut at their limit

    def prompt_text(self) -> str:
        """``=== <reader> ===\\n<text>\\n`` for each reader, in declared order."""
        return "".join(f"=== {name} ===\n{text}\n" for name, text in self.texts)


@dataclass(frozen=True)
class Answerer:
    kind: str  # "model" | "script"
    entry: str | None
    name: str | None
    tier: str | None
    effort: str | None
    attempts: tuple[tuple[str, str], ...]  # (entry, outcome)


# declaration.run(names, request, *, project_root, requirements, exclude, max_attempts, reachability_cache)
#   -> RunResult
# A deadline, wherever one is taken, is an absolute time.monotonic() value in seconds, or None.
RunFn = Callable[..., Any]


@dataclass(frozen=True)
class Catalog:
    """The active questions, in catalog order (shipped order, fleet-only additions after, sorted by id)."""

    version: int
    questions: tuple[Question, ...]
    retired: tuple[str, ...] = ()  # ids a fleet file retired

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(question.id for question in self.questions)

    def get(self, question_id: str) -> Question | None:
        for question in self.questions:
            if question.id == question_id:
                return question
        return None


@dataclass(frozen=True)
class CommitInfo:
    sha: str  # full hex
    date: str  # committer date, ISO 8601 with offset
    subject: str


@dataclass(frozen=True)
class RepoLog:
    """One ``git log --topo-order`` of a repository, grouped by folder.

    ``commits`` is newest first in topological order; the position of a commit is its rank (a smaller
    position is newer). ``touched`` maps a folder (posix path relative to ``top``) to the ascending
    positions of the commits that changed a path inside it, outside that folder's own ``answers/``
    subfolder. Build it with ``RepoLog.build``.
    """

    top: Path
    head: str | None  # HEAD at read time; None in a repository without commits
    commits: tuple[CommitInfo, ...]
    touched: Mapping[str, tuple[int, ...]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        top: Path,
        head: str | None,
        commits: Sequence[CommitInfo],
        changes: Sequence[Sequence[str]],
    ) -> "RepoLog":
        """``changes[i]`` lists the paths (posix, relative to ``top``) changed by ``commits[i]``."""
        if len(commits) != len(changes):
            raise ValueError("commits and changes must have equal length")
        index: dict[str, list[int]] = {}
        for position, paths in enumerate(changes):
            for path in paths:
                parts = path.split("/")
                for depth in range(1, len(parts)):
                    folder = "/".join(parts[:depth])
                    if parts[depth] == "answers":
                        continue  # the folder's own answers/ subfolder is not a task-record change
                    positions = index.setdefault(folder, [])
                    if not positions or positions[-1] != position:
                        positions.append(position)
        return cls(
            top=Path(top),
            head=head,
            commits=tuple(commits),
            touched={folder: tuple(positions) for folder, positions in index.items()},
        )

    def touching(self, rel_folder: str) -> tuple[CommitInfo, ...]:
        """The commits that changed ``rel_folder`` outside its ``answers/``, newest first."""
        return tuple(self.commits[position] for position in self.touched.get(rel_folder, ()))

    def rank(self, rel_folder: str) -> int | None:
        """Position of the newest commit touching ``rel_folder`` outside its ``answers/``; None if none."""
        positions = self.touched.get(rel_folder)
        return positions[0] if positions else None


@dataclass(frozen=True)
class ScriptContext:
    """What a script answerer may read: the task folder, its repository, the per-read log, the clock."""

    folder: Path
    repo_top: Path
    log: RepoLog | None  # None when the folder is not in a repository with a log
    now: datetime.datetime  # naive local time, as log.md stamps are

    @property
    def rel_folder(self) -> str:
        return self.folder.resolve().relative_to(self.repo_top.resolve()).as_posix()
