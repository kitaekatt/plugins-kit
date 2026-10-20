"""Translate one task item into one llm-kit job, or say exactly why not.

Translation is a pure decision: it reads the task folder and returns either an
immutable :class:`JobSpec` or typed :class:`Reason` values. It creates NOTHING
-- no run directory, no job file, no ledger -- so a refusal leaves no trace
except the block the caller then records in the task folder. The user's ruling
is to block the task and fix the wording rather than build a job system that
copes with any wording, so there is no repair, guessing or fuzzy matching here.

An item is job-ready only when all of these hold (architecture, "Job-ready
contract"): the task validates with no errors and no warnings; its stored
status is ``active``; the item exists exactly once and is ``available``;
plan.md has exactly one non-empty section keyed by the item id; any ``accept``
argv lists are well formed; the chosen worker resolves to ``claude-cli`` or
``codex-cli`` (and Codex is proven); the project, task folder and acceptance
script resolve; llm-kit is usable; and no interactive hold covers the task.

A reason is BLOCKING when it describes the item or its configuration (the
caller records it in the task folder as a block) and NOT blocking when it
describes the moment (llm-kit unavailable, a harness hold): those leave the
task untouched and are retried or surfaced.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import yaml

from ..validate import validate_ref
from . import endpoint_helper
from . import findings as findings_mod
from . import floor, plan_sections, repo_identity, subagents as sub_agents, vcs
from .config import DashboardConfig
from .inventory import project_id_for
from .mutation import raw_items, stored_status
from .task_result import DESCRIPTION as RESULT_DESCRIPTION

# Codex workers stay refused until a live deny-policy test shows the same floor
# (git, publication, delete, nested-worker) holds on the Codex side. Flip this
# only with that evidence (tests/task-kit/test_dashboard_codex_safety_live.py).
CODEX_FLOOR_PROVED = False

MAX_FINDINGS_SHOWN = 5


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str
    blocking: bool = True

    def line(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class RunOptions:
    """Per-run choices; defaults come from :class:`DashboardConfig`."""

    worker: str | None = None
    entry: str | None = None
    effort: str | None = None
    network: bool = False
    timeout_s: float | None = None

    def resolved(self, config: DashboardConfig) -> "RunOptions":
        return RunOptions(
            worker=self.worker or config.default_worker,
            entry=self.entry or (config.entry_for(self.worker or config.default_worker)
                                 if (self.worker or config.default_worker) in ("claude", "codex") else None),
            effort=self.effort if self.effort is not None else config.effort,
            network=self.network or config.codex_network,
            timeout_s=self.timeout_s if self.timeout_s is not None else config.default_timeout_s,
        )


@dataclass(frozen=True)
class StartSnapshot:
    revision: str
    vcs: bool
    head: str | None
    branch: str | None
    status: tuple[str, ...]
    log_lines: int


@dataclass(frozen=True)
class JobSpec:
    run_id: str
    project_id: str
    project_root: str
    task_ref: str
    folder: str
    item_id: str
    item_title: str
    worker: str
    entry: str
    timeout_s: float
    document: Mapping
    accept: tuple[tuple[str, ...], ...]
    start: StartSnapshot
    contract_argv: tuple[str, ...]
    network: bool = False
    deny_tools: tuple[str, ...] = ()  # the tool-level denials this run's disallowed_tools value carries
    subagents: Mapping = field(default_factory=dict)  # the run's sub-agent decision, as recorded in spec.json
    subagent_env: Mapping = field(default_factory=dict)  # the documented limits an ON run adds to its command environment
    helper_offers: tuple[dict, ...] = ()  # the endpoint helpers offered to this run (the server issues the grant from these)


@dataclass(frozen=True)
class Translation:
    reasons: tuple[Reason, ...] = ()
    spec: JobSpec | None = None

    @property
    def ready(self) -> bool:
        return self.spec is not None and not self.reasons

    @property
    def blocking(self) -> tuple[Reason, ...]:
        return tuple(r for r in self.reasons if r.blocking)


# -- prompts ----------------------------------------------------------------


def _system_prompt(*, root: str, folder: str, task_ref: str, item_id: str, revision: str,
                   helpers: str = "", work_alone: str = "", sub_agents: str = "") -> str:
    return "\n".join([
        "You are an unattended worker completing exactly ONE task item in a shared checkout.",
        f"Project root: {root}",
        f"Task folder: {folder} (task ref {task_ref})",
        f"Item id: {item_id}",
        f"Source revision of the task folder when this run started: {revision}",
        "",
        "Permissions and allowed writes:",
        "- You may edit files inside the project root and this task folder, and run the project's relevant tests.",
        "- You may NOT create or switch branches or worktrees, stage, commit, push, tag, stash, reset, rebase,",
        "  publish, delete files, or run any nested driver (claude -p, codex exec, task-kit, llm-kit).",
        "  Those commands are denied; do not try to route around the denial. A person reviews and commits.",
        "- Never rewrite or discard work in the tree that you did not make.",
        "",
        "Working rules:",
        f"- {work_alone}",
        "- Do exactly the one item described below. Do not start sibling items.",
        "- When the item is complete, remove it from the task_items block in plan.md, add an Accomplished line",
        "  to plan.md, and append a dated log.md entry that names the item id, using the task folder's own conventions.",
        "- If you cannot finish (a decision is needed, access is missing, the instructions are unclear or conflict),",
        "  stop and report blocked instead of guessing. A person will resolve it in Claude Code and a fresh run follows.",
        "",
        *([sub_agents, ""] if sub_agents else []),
        *([helpers, ""] if helpers else []),
        RESULT_DESCRIPTION,
    ])


def _user_prompt(*, item: dict, heading: str, body: str, folder: str, revision: str) -> str:
    entry = yaml.safe_dump(item, sort_keys=False, default_flow_style=False).rstrip()
    return "\n".join([
        f"Complete this task item (task folder {folder}, source revision {revision}).",
        "",
        "Item entry from plan.md:",
        entry,
        "",
        f"Detail section from plan.md ({heading}):",
        body,
        "",
        f"Before you start, read {folder}/CLAUDE.md for the task's context and any reads it requires.",
    ])


# -- translation ------------------------------------------------------------


def _accept_lists(item: dict) -> tuple[tuple[tuple[str, ...], ...], list[Reason]]:
    if "accept" not in item:
        return (), []
    value = item["accept"]
    bad = (
        not isinstance(value, list) or not value
        or not all(isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv) for argv in value)
    )
    if bad:
        return (), [Reason("bad-accept", "accept must be a non-empty list of non-empty argv lists of strings")]
    return tuple(tuple(argv) for argv in value), []


def translate(
    *,
    project_root: Path,
    task_ref: str,
    item_id: str,
    options: RunOptions,
    config: DashboardConfig,
    edge,
    hold_for: Callable[[str], str | None],
    run_id: str,
    run_dir: Path,
    accept_script: Path,
    python: str | None = None,
    revision: str,
    subagents: "sub_agents.Decision | None" = None,
) -> Translation:
    """Decide whether ``item_id`` of ``task_ref`` can run now.

    ``revision`` is the task folder's current revision (the caller read it
    under its mutation rules); ``hold_for(folder)`` returns a description of an
    interactive hold on the task folder (an absolute path string), or None.
    ``run_dir`` is only NAMED in the contract argv; nothing is created in it. ``subagents`` is the run's
    native sub-agent decision (``subagents.decide``); without one the run has sub-agents off.
    """
    project_root = Path(project_root)
    folder = project_root / task_ref
    reasons: list[Reason] = []
    options = options.resolved(config)

    # 1. validation, then 2. stored status
    result = validate_ref(task_ref, project_root)
    findings = findings_mod.gating_of(result)
    for finding in findings[:MAX_FINDINGS_SHOWN]:
        reasons.append(Reason("validation", finding))
    if len(findings) > MAX_FINDINGS_SHOWN:
        reasons.append(Reason("validation", f"{len(findings) - MAX_FINDINGS_SHOWN} more finding(s); run task validate {task_ref}"))
    task_yaml = _read(folder / "task.yaml")
    plan_text = _read(folder / "plan.md")
    status = stored_status(task_yaml) if task_yaml is not None else None
    if status != "active":
        reasons.append(Reason("task-not-active", f"the task's stored status is {status!r}; only an active task runs"))

    # 3. the item, exactly once and available
    item: dict | None = None
    items = raw_items(plan_text) if plan_text is not None else None
    if items is None:
        reasons.append(Reason("no-items-block", "plan.md has no task_items block"))
    else:
        matches = [i for i in items if i.get("id") == item_id]
        if not matches:
            reasons.append(Reason("item-missing", f"no item {item_id!r} in plan.md"))
        elif len(matches) > 1:
            reasons.append(Reason("item-duplicate", f"{len(matches)} items share the id {item_id!r}"))
        else:
            item = matches[0]
            if item.get("state") != "available":
                reasons.append(Reason("item-not-available", f"the item's state is {item.get('state')!r}, not 'available'"))

    # 4. the plan section and the accept argv lists
    section = plan_sections.find_item_section(plan_text or "", item_id)
    if section.status != plan_sections.FOUND:
        reasons.append(Reason(
            f"section-{section.status}",
            f"plan.md needs exactly one non-empty section headed with the item id (e.g. '### {item_id} -- title'); found: {section.status}",
        ))
    accept: tuple[tuple[str, ...], ...] = ()
    if item is not None:
        accept, accept_reasons = _accept_lists(item)
        reasons.extend(accept_reasons)

    # 5. worker, model entry and effort
    adapter = _worker_reasons(options, edge, reasons)
    _effort_reasons(options, edge, reasons, adapter)
    offers = _helper_offers(config, edge, project_root, reasons)

    # 6. paths
    script = Path(accept_script)
    if not script.is_absolute() or not script.is_file():
        reasons.append(Reason("path", f"the acceptance script {script} does not exist"))
    if not project_root.is_dir() or not os.access(project_root, os.W_OK):
        reasons.append(Reason("path", f"the project root {project_root} is not a writable directory"))
    if not folder.is_dir():
        reasons.append(Reason("path", f"the task folder {folder} does not exist"))
    _timeout_reasons(options, reasons)

    # 7. llm-kit, 8. interactive hold (neither blocks the task)
    edge_status = edge.status()
    if not edge_status.available:
        reasons.append(Reason("llm-kit-unavailable", edge_status.detail, blocking=False))
    held = hold_for(str(folder))
    if held:
        reasons.append(Reason("held", held, blocking=False))

    if reasons or item is None or section.status != plan_sections.FOUND:
        return Translation(reasons=tuple(reasons))

    # Everything is known: build the job. Nothing is written.
    facts = vcs.facts(project_root)
    log_text = _read(folder / "log.md") or ""
    start = StartSnapshot(
        revision=revision, vcs=facts.vcs, head=facts.head, branch=facts.branch,
        status=facts.status, log_lines=len(log_text.splitlines()),
    )
    py = python or sys.executable
    contract = (
        py, str(Path(accept_script).resolve()), task_ref, item_id, run_id, revision,
        start.head or "-", start.branch or "-", str(Path(run_dir)),
    )
    decision = subagents if subagents is not None else sub_agents.not_decided()
    deny_tools = decision.deny_tools
    work_alone, sub_agents_section = decision.prompt_lines()
    job_options = worker_job_options(adapter, options.effort, deny_tools)
    job = {
        "id": f"{_slug(task_ref)}--{item_id}--{run_id}",
        "prompt": {
            "system": _system_prompt(root=str(project_root), folder=str(folder), task_ref=task_ref, item_id=item_id, revision=revision,
                                     helpers=endpoint_helper.prompt_section(offers, python=py),
                                     work_alone=work_alone, sub_agents=sub_agents_section),
            "user": _user_prompt(item=item, heading=section.heading or item_id, body=section.body, folder=str(folder), revision=revision),
        },
        "models": [options.entry],
        "directory": str(project_root),
        "workspace": {"isolate": False},
        "max_attempts": 1,
        "contract": {"command": list(contract), "directory": str(project_root)},
        "options": job_options,
    }
    document = {"jobs": [job], "max_parallel": 1}
    spec = JobSpec(
        run_id=run_id, project_id=project_id_for(project_root), project_root=str(project_root),
        task_ref=task_ref, folder=str(folder), item_id=item_id, item_title=str(item.get("title", item_id)),
        worker=options.worker, entry=options.entry or "", timeout_s=float(options.timeout_s),
        document=document, accept=accept, start=start, contract_argv=contract, network=options.network,
        deny_tools=deny_tools, helper_offers=tuple(offers), subagents=decision.to_json(), subagent_env=decision.env,
    )
    return Translation(reasons=(), spec=spec)


def worker_job_options(adapter: str | None, effort: str | None, deny_tools: tuple[str, ...] | list[str]) -> dict:
    """The one builder of a job's ``options``: the Claude floor plus ``deny_tools`` as ONE comma-joined string.

    The cli observation (cli_check.py) builds its probe argv from this same function, so what it observes is
    byte-identical to what a run emits.
    """
    options: dict = {"system_prompt_mode": "append"}
    if adapter == "claude-cli":
        options["disallowed_tools"] = floor.disallowed_tools_value(tuple(deny_tools))
    if effort:
        options["effort"] = effort
    return options


# Reason codes that describe a RUN's options, not the task: a run whose options are refused ends
# `failed` (bad-options) and the task is left alone; they never block the task.
OPTION_REASON_CODES = frozenset({"bad-worker", "codex-floor-unproved", "model-entry", "bad-effort", "timeout",
                                 "cli-deny-ignored", "endpoint-helper"})


def _effort_reasons(options: RunOptions, edge, reasons: list[Reason], adapter: str | None) -> None:
    """An effort must be on the chosen entry's menu (checked only once the entry resolved to a worker adapter)."""
    if not options.effort or adapter is None:
        return
    try:
        menu = tuple(edge.effort_menu(options.entry))
    except Exception as exc:  # noqa: BLE001 -- the edge raises its own error type; report it as a reason
        reasons.append(Reason("bad-effort", str(exc), blocking=False))
        return
    if options.effort not in menu:
        shown = ", ".join(menu) if menu else "none (it takes no effort)"
        reasons.append(Reason("bad-effort", f"effort {options.effort!r} is not on the menu of {options.entry!r}: {shown}"))


def _timeout_reasons(options: RunOptions, reasons: list[Reason]) -> None:
    if options.timeout_s is None or options.timeout_s <= 0:
        reasons.append(Reason("timeout", "the run timeout must be positive"))


def option_reasons(options: RunOptions, config: DashboardConfig, edge, project_root: Path | str | None = None) -> list[Reason]:
    """Everything wrong with a run's OPTIONS alone (worker, entry, effort, timeout, offered helpers), as typed reasons.

    The same checks :func:`translate` makes, so a request can be refused for them before anything is
    queued or written. Needs the edge to be available (it resolves the entry and its effort menu).
    ``project_root`` is the checkout the run executes in; without it the helpers offered there are not checked.
    """
    resolved = options.resolved(config)
    reasons: list[Reason] = []
    adapter = _worker_reasons(resolved, edge, reasons)
    _effort_reasons(resolved, edge, reasons, adapter)
    _timeout_reasons(resolved, reasons)
    if project_root is not None:
        _helper_offers(config, edge, Path(project_root), reasons)
    return reasons


def _helper_offers(config: DashboardConfig, edge, project_root: Path, reasons: list[Reason]) -> list[dict]:
    """The endpoint helpers offered to the checkout at ``project_root``; a reason for each offer a run cannot honor.

    Consent is by repository identity (``repo_identity.repo_key_for``), never by display name. A helper's models
    must be TRANSPORT entries (text-only endpoints): a harness entry is an agent, and an agent is a worker, never
    a helper. A model the registry does not know is not refused here; the call that names it ends ``unroutable``
    and says why.
    """
    if not config.endpoint_helpers:
        return []
    try:
        key = repo_identity.repo_key_for(project_root)
    except vcs.GitError as exc:
        reasons.append(Reason("endpoint-helper", f"the checkout's origin remote cannot be read, so no endpoint helper can be offered: {exc}"))
        return []
    offers = endpoint_helper.offers_for(config, key)
    if not offers or not edge.status().available:
        return offers
    for offer in offers:
        for model in offer["models"]:
            found = edge.entry(model, project_root=project_root)  # a registry that will not load raises (EdgeError)
            if found is not None and found.kind != "transport":
                reasons.append(Reason(
                    "endpoint-helper",
                    f"endpoint helper {offer['name']!r} names model entry {model!r}, a {found.kind} entry"
                    + (f" ({found.harness})" if found.harness else "")
                    + "; a helper's models must be transport entries (text-only endpoints), because an agent is a worker, never a helper"))
    return offers


def _worker_reasons(options: RunOptions, edge, reasons: list[Reason]) -> str | None:
    if options.worker not in ("claude", "codex"):
        reasons.append(Reason(
            "bad-worker",
            f"worker {options.worker!r} is not allowed: only claude and codex work on tasks (endpoints are sub-work helpers only)",
        ))
        return None
    if options.worker == "codex" and not CODEX_FLOOR_PROVED:
        reasons.append(Reason(
            "codex-floor-unproved",
            "Codex workers are refused until a live Codex-side policy test shows the same git/publication/delete/"
            "nested-worker floor Claude gets; llm-kit's dropped disallowed-tools field is not enforcement",
        ))
        return None
    if not options.entry:
        reasons.append(Reason("model-entry", "no model entry is configured for this worker"))
        return None
    if not edge.status().available:
        return None  # reported once, as the non-blocking llm-kit-unavailable reason
    try:
        entry = edge.entry(options.entry)
    except Exception as exc:  # noqa: BLE001 -- the edge raises its own error type; report it as a reason
        reasons.append(Reason("model-entry", str(exc), blocking=False))
        return None
    if entry is None:
        reasons.append(Reason("model-entry", f"model entry {options.entry!r} is not in the model registry"))
        return None
    if entry.adapter is None:
        reasons.append(Reason(
            "model-entry",
            f"model entry {options.entry!r} is a {entry.kind} entry"
            + (f" ({entry.harness})" if entry.harness else "")
            + "; only claude-cli and codex-cli entries can work on tasks",
        ))
        return None
    if (options.worker == "claude") != (entry.adapter == "claude-cli"):
        reasons.append(Reason("model-entry", f"model entry {options.entry!r} runs on {entry.adapter}, not on the {options.worker} worker"))
        return None
    return entry.adapter


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _slug(task_ref: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in task_ref).strip("-")
