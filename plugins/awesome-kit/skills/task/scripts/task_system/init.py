"""The ``init`` verb (spec section 7.1) -- create a task folder + scaffolding.

``init <stub|desc> [--dest tmp|dev/tasks] [--type hand-off]``

Pre: the target path ``<dest>/<stub>`` (default dest ``tmp``) does not already
exist -- else error telling the user to use ``update``. Steps: scaffold the
type's files (CLAUDE.md + plan.md + log.md + task.yaml for hand-off), seed
``task.yaml`` (``_schema_version: "1"``, ``type``, ``title``,
``status: active``), seed the markdown files from the hand-off template
(SKILL.md's eight-``##``-section CLAUDE.md contract, with placeholder/seeded
content), then run ``validate``.

Invariant (spec 7.1): **the output is always a valid ``active`` task.** If the
scaffolded result does not validate to the standard below, init fails and
removes the folder it created -- it never leaves a partial or ``invalid``
folder behind.

Readings chosen in Step 2 (flagged in the implementation report):

- **Uncommitted-dev/tasks warning.** An ``init`` into ``dev/tasks/`` is
  uncommitted by definition, and ``validate`` warns on uncommitted dev/tasks
  folders (spec 9) -- that warning is expected and unavoidable at creation
  time (the user owns the commit; spec 7.4). Reading: init succeeds when
  validation reports **zero errors, classification ``active``, and every
  warning is the expected uncommitted-dev/tasks warning** (possible only when
  dest is ``dev/tasks``). Any other finding fails init. For ``dest=tmp`` the
  result must therefore be fully clean (zero errors AND zero warnings). The
  general-case warning in validate.py is untouched -- it still surfaces (and
  still gates ``work``) on every later ``validate`` of the folder.

- **Stub/title derivation.** If the argument already looks like a stub --
  it fullmatches ``[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?`` (lowercase kebab/safe
  chars, alphanumeric first and last, no whitespace, no ``/``) -- it is used
  verbatim as the folder name, and the title is derived from it by replacing
  ``-``/``_`` runs with spaces and capitalizing the first character
  (``fix-the-run`` -> ``Fix the run``). Otherwise the argument is a freeform
  description: the description (stripped) becomes the title verbatim, and the
  stub is derived by lowercasing, replacing every non-``[a-z0-9]`` run with
  ``-``, stripping leading/trailing ``-``, and truncating to 60 chars
  (re-stripped). A description that derives an empty stub is an error. Either
  way the folder name is a single safe path segment -- path-shaped arguments
  (containing ``/``) are treated as descriptions and sanitized, never as
  paths.

- **Output.** ``init_task`` returns (and the CLI prints) the **absolute**
  folder path. The task id remains the canonical project-relative path
  (spec 5); the absolute form is printed because it is unambiguous regardless
  of the caller's cwd.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from . import resolve
from .types import TaskType, get_type
from .validate import validate_ref

_STUB_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")
_MAX_STUB_LEN = 60
_EXPECTED_DEV_TASKS_WARNING = "uncommitted dev/tasks folder"


class InitError(ValueError):
    """init could not produce a valid ``active`` task."""


def derive_stub_and_title(stub_or_desc: str) -> tuple[str, str]:
    """Derive ``(stub, title)`` per the module-docstring derivation rule."""
    arg = stub_or_desc.strip()
    if not arg:
        raise InitError("empty stub/description")
    if _STUB_RE.fullmatch(arg):
        title = re.sub(r"[-_]+", " ", arg)
        return arg, title[:1].upper() + title[1:]
    stub = re.sub(r"[^a-z0-9]+", "-", arg.lower()).strip("-")
    stub = stub[:_MAX_STUB_LEN].rstrip("-")
    if not stub:
        raise InitError(
            f"cannot derive a folder stub from {arg!r} "
            "(no usable [a-z0-9] characters)"
        )
    return stub, arg


# --- hand-off scaffolding templates ----------------------------------------
# The CLAUDE.md scaffold reuses the hand-off template: the eight ## sections
# (with their required ### subsections) under a single `# Project Overview`,
# per the task skill's references/handoff-template.md and the worked
# example at references/example-claude-md.md. Placeholders use the example's
# <fill: ...> angle-bracket convention.

_CLAUDE_MD_TEMPLATE = """\
# Project Overview

## Where we are today

Task "{title}" was just initialized at `{canonical}`. No work has happened
yet; this file is a scaffold awaiting its first real hand-off pass.

### Environment

- cwd: the project root containing `{canonical}`.
- <fill: source control, platform, key tool versions, env quirks>

## Where we want to get to

<fill: the goal, stated falsifiably so the next agent can tell when it is
done>

## Immediate Priorities

Live menu: `task items` (plan.md's task_items block is the source of truth;
reference items here by backticked id, never restating their state).

- Replace the placeholders in this scaffold: fill this file's sections,
  enumerate the open items in plan.md's task_items block, and write the
  first concrete steps into `plan.md`.

## Project vocabulary

- <fill: terms, stage names, and decoders for paths whose on-disk literal
  disagrees with the prose name>

## Protocols

### Always-invoke skills (BEFORE any doc reads)

Invoke every `Skill(...)` line `task work` emitted, in the order printed,
before reading any doc. That block is the complete required set (this task's
`skills_to_invoke` plus the always-required baseline); there is no second
list to consult. Skill invocations are pre-authorized -- do not ask.

To add a skill to the set, edit `task.yaml` via
`task update --skill-to-invoke <name>` (repeatable; REPLACES the stored
list) -- never by listing it here.

### Required reads on turn 1

1. `plan.md` -- accomplished + next concrete actions.

### Opening response protocol

After invoking the always-invoke skills AND reading the required docs above,
BEFORE any tool use, end the first turn with:

> "Read plan.md. Current goal: <restated in own words>. Starting with:
> <first concrete action>. Unclear / blocked on: <issue, or 'none'>."

### Communication protocol

`/verbose-updates` three-part end-of-turn template:

> What changed: <action + paths>.
> Where it sits: <relation to in-flight work>.
> Required user action: <one decision OR "All requested work complete -
> ready to end session">.

## Behaviors

### Autonomy status

- <fill: decisions the user has CLAIMED and the presence pattern to expect;
  all else is granted where the user's CLAUDE.md says so>

### Authorizations

- <fill: standing pre-authorized actions, or "none yet">

### Rules to follow

- ASCII only in all files; no absolute paths in the artifacts.
- <fill: project-specific operational rules>

### Sub-agent orchestration -- main-context preservation

- Push bounded heavy work to sub-agents; main reads reports, not inputs.
- <fill: project-specific orchestration rules>

### Anti-patterns to avoid

- Silent-go-to-work: do the opening response protocol before tools.
- <fill: project-specific anti-patterns>

## Relevant files

### Project folder

Contents of `{canonical}/` -- this task's own working tree.

- `CLAUDE.md` -- self (this file); auto-loaded orientation.
- `plan.md` -- accomplished + forward overview (its task_items block is the
  open-item menu); required read on turn 1.
- `log.md` -- on-demand history.
- `task.yaml` -- the structured task record (status lives here).

### External files

- <fill: files outside the task folder this work depends on, one-line
  purpose each>
"""

_PLAN_MD_TEMPLATE = """\
# Plan: {title}

## Accomplished

- (nothing yet -- task just initialized)

## Forward overview

```yaml
task_items:
  items: []
```

1. <fill: the first concrete step, in actionable detail; enumerate the open
   items in the task_items block above as they take shape>
"""

_LOG_MD_TEMPLATE = """\
# Log: {title}

(no entries yet -- rotate completed-step detail and decision rationale here)
"""


def _scaffold_contents(
    ttype: TaskType, title: str, canonical: str
) -> dict[str, str]:
    """File name -> seeded content for the type's scaffolding set."""
    if ttype.name != "hand-off":
        # v1 registers exactly one type; a future type needs its own
        # templates wired here when it is registered (spec 2.5).
        raise InitError(f"no scaffolding template for type {ttype.name!r}")
    contents = {
        "task.yaml": yaml.safe_dump(
            {
                "task": {
                    "_schema_version": "1",
                    "type": ttype.name,
                    "title": title,
                    "status": "active",
                }
            },
            sort_keys=False,
            default_flow_style=False,
        ),
        "CLAUDE.md": _CLAUDE_MD_TEMPLATE.format(title=title, canonical=canonical),
        "plan.md": _PLAN_MD_TEMPLATE.format(title=title),
        "log.md": _LOG_MD_TEMPLATE.format(title=title),
    }
    assert set(contents) == set(ttype.scaffolding)
    return contents


def _unexpected_findings(errors: list[str], warnings: list[str], dest: str) -> list[str]:
    """Findings that fail init, per the uncommitted-dev/tasks reading."""
    unexpected = [f"error: {e}" for e in errors]
    for w in warnings:
        expected = dest == resolve.LOCATION_DEV_TASKS and w.startswith(
            _EXPECTED_DEV_TASKS_WARNING
        )
        if not expected:
            unexpected.append(f"warning: {w}")
    return unexpected


def init_task(
    stub_or_desc: str,
    project_root: Path,
    *,
    dest: str = "tmp",
    task_type: str = "hand-off",
) -> Path:
    """Create the folder + scaffolding for a new task; return its path.

    Implements the spec 7.1 ``init`` contract (see the module docstring).
    Raises InitError on any failure; on a post-creation failure the created
    folder is removed first (no partial folder is left behind).
    """
    if dest not in resolve.KNOWN_ROOTS:
        raise InitError(
            f"unknown dest {dest!r} (expected one of: "
            + ", ".join(resolve.KNOWN_ROOTS)
            + ")"
        )
    ttype = get_type(task_type)
    if ttype is None:
        raise InitError(f"unknown type: {task_type!r} names no registered task type")

    stub, title = derive_stub_and_title(stub_or_desc)
    if stub == resolve.ARCHIVED_DIRNAME:
        raise InitError(
            f"stub {stub!r} is reserved under {dest}/ (the parking directory "
            "for archived tasks)"
        )
    canonical = f"{dest}/{stub}"
    folder = (project_root / canonical).absolute()
    if folder.exists():
        raise InitError(
            f"{canonical} already exists -- use update, not init"
        )

    contents = _scaffold_contents(ttype, title, canonical)
    folder.mkdir(parents=True)
    try:
        for fname in ttype.scaffolding:
            (folder / fname).write_text(contents[fname], encoding="utf-8")

        result = validate_ref(canonical, project_root)
        unexpected = _unexpected_findings(result.errors, result.warnings, dest)
        if result.classification != "active" or unexpected:
            findings = "\n".join(unexpected) or (
                f"classification: {result.classification}"
            )
            raise InitError(
                f"init produced a task that does not validate clean "
                f"({canonical}); folder removed:\n{findings}"
            )
    except BaseException:
        # Invariant: init never leaves a partial/invalid folder behind.
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder
