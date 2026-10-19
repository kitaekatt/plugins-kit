"""Profile resolution over the four layered bootstrap manifests.

A profile is a named bundle of manifest content declared under a ``profiles``
object in any layered ``bootstrap.json``. One profile is SELECTED by a
``profile`` string, and the selected profile (plus everything it ``extends``)
is overlaid onto the merged base manifest with the ordinary manifest merge
rules. The result is an effective manifest the rest of the engine consumes
without knowing profiles exist.

Two asymmetries are deliberate and carry most of the design:

- ``profiles`` (the DEFINITIONS) may be declared in any layer, because they are
  content a team shares. ``profile`` (the SELECTION) is honored only from the
  two ``bootstrap.local.json`` layers, because it is a per-machine choice; a
  committed layer that carries one gets a visible warning rather than silent
  effect, so a repository cannot decide for a checkout.
- An empty ``profiles`` means the feature is INERT (status ``no_profiles``), no
  matter what ``profile`` says. A project that never adopted profiles must
  never be warned about, prompted about, or failed for them.

Plugin tiers ride on the same machinery. ``plugins[]`` entries may carry a
``tier`` (see :data:`TIERS`) with two why lines; the per-machine answers to the
resulting questions are recorded under ``plugin_choices`` in the project-local
file, bound to the profile they were recorded under, and handed to
``plugin_tiers`` through :attr:`ProfileState.plugin_answers`. A tracked
``profile_prompt: false`` turns the built-in profile prompt off for that one
project.

A profile switch also retires the previous profile's plugins declaratively:
``plan_switch_uninstalls`` computes which ``uninstall: true`` plugin entries to
add and drop, and the writers below merge them into the project-local file in
the same write as the selection. The engine carries the entries out
(``plugin_tiers.resolve_uninstall_entries``); no record of what a profile
installed is kept.

Everything here is pure with named exceptions -- ``write_selection``,
``write_selection_with_answers``, ``clear_selection``, ``write_uninstall_entries``
and ``mark_prompted`` write, and ``ensure_vcs_excluded`` shells out to Git. Message
TEXT for the engine's log lines stays in the engine, as elsewhere in
``bootstrap_lib``; what this module returns are structured outcomes plus the
two agent-facing strings (the question and the prompt directive) that have to
be built where the state lives.

Stdlib only: ``bootstrap_lib`` is imported from SessionStart hooks and from
plugins whose venv may not be provisioned yet.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from .agent_skills_check import _apply_git_file_exclusion
from .atomic_write import write_atomic
from .manifest_merge import merge_manifests

#: Layer kinds, lowest priority first. ``legacy`` is ~/.claude/user-bootstrap.json.
LAYER_KINDS = ("legacy", "user", "user_local", "project", "project_local")

#: Kinds whose ``profile`` key selects, highest priority first.
SELECTION_KINDS = ("project_local", "user_local")

#: Kinds that may declare ``profiles`` and make the write target project-local.
PROJECT_KINDS = ("project", "project_local")

#: Reserved selection meaning "base manifest only, and stop asking".
NONE_SELECTION = "none"

#: Reserved selection that CLEARS the choice. Written to the project-local
#: file it masks a ``profile`` left in a lower layer, so the project reads as
#: unselected (``clear`` cannot simply delete the key: the user-local layer
#: would then select again).
UNSET_SELECTION = "unset"

#: Plugin tiers a ``plugins[]`` entry may declare.
TIER_REQUIRED = "required"
TIER_RECOMMENDED = "recommended"
TIER_NOT_RECOMMENDED = "not_recommended"
TIERS = (TIER_REQUIRED, TIER_RECOMMENDED, TIER_NOT_RECOMMENDED)

#: Tiers whose entries must carry both why lines.
QUESTIONED_TIERS = (TIER_RECOMMENDED, TIER_NOT_RECOMMENDED)

#: Tiered entries are authored ``install: "manual"`` so an engine that does
#: not know tiers never installs one (legacy-safe authoring).
TIER_INSTALL_MODE = "manual"

#: The one value a recorded answer's ``scope`` may take: the user confirmed
#: acting on a user-scope install.
ANSWER_SCOPE_USER = "user"

#: Top-level keys this module owns; none may appear in a profile body.
_RESERVED_BODY_KEYS = ("profiles", "profile", "plugin_choices", "profile_prompt")

#: Why lines are single display lines, sanitized like profile descriptions but
#: allowed more room than an option label.
WHY_LINE_MAX = 200

#: A profile name is a file-safe, lower-case identifier.
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: Statuses that leave a choice for the user to make.
PROMPTABLE_STATUSES = ("unselected", "unknown")

#: AskUserQuestion shows a small option set; one slot is spent on the leading
#: defer/keep option, so at most three profiles are offered directly. Past that
#: the options collapse to the lead plus one "type the name" option and the
#: profiles are printed to the user as an ordinary chat list instead (D10) --
#: a list the agent prints reads better than a name crammed into an option
#: label, and it can carry each profile's parents and description in full.
MAX_PROFILE_OPTIONS = 3
QUESTION_HEADER = "Profile"

#: Label of the option that sends the user to Other when the profiles do not
#: fit as options.
TYPED_CHOICE_LABEL = "Select a Profile Now"

#: Descriptions are sanitized before they reach a question. The cap is a hard
#: truncation with no ellipsis: an ellipsis is decoration that costs three of
#: the characters it claims to stand in for (engine-internals display rule 3).
DESCRIPTION_MAX = 100

#: Cross-session double-prompt guard. Two sessions starting together each see
#: no marker of their own, so the marker DIRECTORY is what stops the second one
#: asking the same question a beat later.
PROMPT_GUARD_SECONDS = 600

#: Prompt markers are session-scoped and tiny; pruning bounds the directory
#: without needing a separate sweep anywhere else.
MARKER_TTL_SECONDS = 7 * 24 * 60 * 60

_MARKER_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")
_MARKER_NAME_MAX = 128

#: Header written above the generated rule in .git/info/exclude.
_GIT_EXCLUDE_HEADER = "# plugins-kit bootstrap: generated profile selection"

#: The selection file a project-local write creates; excluded from Git because
#: it is a per-machine choice, like every other bootstrap.local.json.
PROJECT_LOCAL_REL = ".claude/bootstrap.local.json"

ENV_SESSION_ID = "CLAUDE_CODE_SESSION_ID"
ENV_SESSION_ATTENDED = "CLAUDE_CODE_SESSION_ATTENDED"


class ProfileWriteError(Exception):
    """A selection could not be persisted, or the Git exclusion failed.

    Raised BEFORE any write when the target file is unreadable, not JSON, or
    not a JSON object -- a hand-authored manifest is never rewritten from a
    guess about what it meant.
    """


@dataclass(frozen=True)
class ProfileInfo:
    """One declared profile, as authored (descriptions are sanitized later)."""

    name: str
    description: str = ""
    extends: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileState:
    """The resolution outcome for one layered-manifest load.

    ``status`` is one of:

    - ``no_profiles`` -- no profile is declared anywhere; the feature is inert.
    - ``unselected``  -- profiles exist and none is chosen; the user may be asked.
    - ``none``        -- the base manifest was chosen explicitly; never asked again.
    - ``selected``    -- ``selected`` names a declared profile; ``chain`` was applied.
    - ``unknown``     -- a name is selected that no layer declares; base only.
    - ``invalid``     -- a declaration error, or an unparseable local layer; base only.

    ``source`` is the file the selection was read from, ``chain`` the applied
    linearization (parents first, selected profile last), ``write_target`` the
    file a new selection should be written to.
    """

    status: str
    selected: Optional[str] = None
    source: Optional[str] = None
    chain: Tuple[str, ...] = ()
    available: Tuple[ProfileInfo, ...] = ()
    warnings: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()
    write_target: Optional[str] = None
    profile_prompt: bool = True
    plugin_answers: Tuple["PluginAnswer", ...] = ()
    answers_stale: bool = False


@dataclass(frozen=True)
class PluginAnswer:
    """One recorded answer to a plugin question.

    ``want`` True means the plugin should be installed, False that it should
    not be. ``scope`` is ``"user"`` only when the user confirmed acting on a
    user-scope install; otherwise ``None``.
    """

    ref: str
    want: bool
    decided: str = ""
    scope: Optional[str] = None


def answers_by_ref(state):
    """The bound answers as ``{ref: {"want": bool, "scope": str|None}}``.

    The plain-data form ``plugin_tiers.resolve_plugin_tiers`` consumes.
    """
    return {
        a.ref: {"want": a.want, "scope": a.scope}
        for a in state.plugin_answers
    }


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_layers(layers, project_dir=None, home=None):
    """Merge the layers, apply the selected profile, and report the state.

    ``layers`` is a sequence of ``(path, kind, manifest)`` LOWEST PRIORITY
    FIRST, with ``kind`` in :data:`LAYER_KINDS`. A ``manifest`` of ``None``
    means that file exists but could not be parsed -- the caller already
    reports the parse error itself, and passing it through here is what lets
    rule 5 refuse to prompt on top of a half-read configuration.

    Returns ``(effective_manifest, ProfileState)``. ``profiles``, ``profile``,
    ``plugin_choices`` and ``profile_prompt`` are stripped from the effective
    manifest in EVERY status, so no downstream check can observe them and no
    engine pass can act on a profile twice.
    """
    entries = [(path, kind, data) for path, kind, data in layers]

    merged = {}
    for _, _, data in entries:
        if isinstance(data, dict):
            merged = merge_manifests(merged, data)
    base = _strip_profile_keys(merged)

    warnings = []
    selected, source = _read_selection(entries, warnings)
    prompt_enabled, prompt_warnings = _read_profile_prompt(entries)
    choices_profile, choice_answers, choice_warnings = _read_plugin_choices(entries)
    raw_profiles = merged.get("profiles")

    # D9: an empty (or absent) `profiles` is the inert case, whatever `profile`
    # says. Reported with no warning and no error -- a project that does not use
    # profiles must not pay for a stale key in someone's local file.
    if not raw_profiles:
        return base, ProfileState(
            status="no_profiles",
            write_target=_user_local_path(home),
            profile_prompt=prompt_enabled,
        )

    warnings.extend(prompt_warnings)
    warnings.extend(choice_warnings)

    if isinstance(raw_profiles, dict):
        available, errors = _validate_profiles(raw_profiles)
    else:
        available, errors = (), ["'profiles' must be an object of profile names"]
    errors = list(errors)
    errors.extend(validate_plugin_entries(merged.get("plugins"), "plugins"))

    write_target = _write_target(entries, project_dir, home)

    if errors:
        return base, ProfileState(
            status="invalid",
            available=available,
            warnings=tuple(warnings),
            errors=tuple(errors),
            write_target=write_target,
            profile_prompt=prompt_enabled,
        )

    # Rule 5: an unparseable LOCAL layer could be the one that holds the
    # selection, so the selection is not knowable. The parse error itself is
    # the caller's to report; duplicating it here would double the failure.
    if any(data is None and kind in SELECTION_KINDS for _, kind, data in entries):
        return base, ProfileState(
            status="invalid",
            available=available,
            warnings=tuple(warnings),
            write_target=write_target,
            profile_prompt=prompt_enabled,
        )

    names = [info.name for info in available]
    if selected == UNSET_SELECTION:
        # A cleared choice reads exactly like one never made.
        selected, source = None, None
    if selected is None:
        status = "unselected"
    elif selected == NONE_SELECTION:
        status = "none"
    elif selected not in names:
        status = "unknown"
        warnings.append(
            "profile '%s' selected in %s is not declared; the base manifest was "
            "used" % (selected, source)
        )
    else:
        status = "selected"

    chain = ()
    effective = base
    if status == "selected":
        graph = {info.name: info.extends for info in available}
        chain = tuple(_linearize(selected, graph))
        for name in chain:
            effective = merge_manifests(effective, _profile_body(raw_profiles[name]))

        # An overlay can turn an untiered base entry into a tiered one, or
        # replace a tiered entry's install mode, so the authored-entry lint
        # above is repeated over the merged result.
        merged_errors = [
            e for e in validate_plugin_entries(effective.get("plugins"), "plugins")
            if e not in errors
        ]
        if merged_errors:
            return base, ProfileState(
                status="invalid",
                available=available,
                warnings=tuple(warnings),
                errors=tuple(merged_errors),
                write_target=write_target,
                profile_prompt=prompt_enabled,
            )

    # Answers bind to the profile they were recorded under: recorded for any
    # other profile (or while none is selected) they are stale and ignored.
    plugin_answers = ()
    answers_stale = False
    if choices_profile is not None:
        if status == "selected" and choices_profile == selected:
            plugin_answers = choice_answers
        else:
            answers_stale = True

    return effective, ProfileState(
        status=status,
        selected=selected if status in ("selected", "none", "unknown") else None,
        source=source,
        chain=chain,
        available=available,
        warnings=tuple(warnings),
        errors=(),
        write_target=write_target,
        profile_prompt=prompt_enabled,
        plugin_answers=plugin_answers,
        answers_stale=answers_stale,
    )


def _strip_profile_keys(manifest):
    """The manifest without the keys this module owns."""
    return {k: v for k, v in manifest.items() if k not in _RESERVED_BODY_KEYS}


def _profile_body(body):
    """A profile's manifest content: everything that is not its own metadata."""
    return {
        k: v for k, v in body.items()
        if k not in ("extends", "description") + _RESERVED_BODY_KEYS
    }


def _read_selection(entries, warnings):
    """Return ``(selected, source_path)`` and append any layer warnings.

    A ``profile`` outside the two local layers is IGNORED with a warning naming
    the file: acting on it would let a committed manifest decide a per-machine
    choice, and dropping it silently would leave the author believing it took.

    The highest local layer that carries a ``profile`` wins, so
    :data:`UNSET_SELECTION` in the project-local file is returned before the
    user-local layer is consulted and masks it; the caller reads it as
    "nothing selected".
    """
    by_kind = {}
    for path, kind, data in entries:
        if not isinstance(data, dict):
            continue
        value = data.get("profile")
        if value is None:
            continue
        if kind not in SELECTION_KINDS:
            warnings.append(
                "'profile' in %s is ignored -- select a profile in "
                "bootstrap.local.json" % path
            )
            continue
        if not isinstance(value, str):
            warnings.append("'profile' in %s is not a string and was ignored" % path)
            continue
        if not value.strip():
            warnings.append("'profile' in %s is empty and was ignored" % path)
            continue
        by_kind[kind] = (value.strip(), path)

    for kind in SELECTION_KINDS:
        if kind in by_kind:
            return by_kind[kind]
    return None, None


def _read_profile_prompt(entries):
    """Return ``(enabled, warnings)`` for the per-project prompt switch.

    ``profile_prompt: false`` is honored only from the TRACKED project layer,
    where a team that ships its own profile setup says so for that project
    alone. Read from any other layer it would switch the prompt off for every
    project, so it is ignored with a warning naming the file.
    """
    enabled = True
    warnings = []
    for path, kind, data in entries:
        if not isinstance(data, dict) or data.get("profile_prompt") is None:
            continue
        if kind != "project":
            warnings.append(
                "'profile_prompt' in %s is ignored -- set it in the project's "
                "tracked .claude/bootstrap.json" % path
            )
            continue
        value = data["profile_prompt"]
        if not isinstance(value, bool):
            warnings.append(
                "'profile_prompt' in %s is not true or false and was ignored" % path
            )
            continue
        enabled = value
    return enabled, warnings


def _parse_answer(ref, value):
    """Return ``(PluginAnswer, None)`` or ``(None, problem)``."""
    if not isinstance(ref, str) or not ref.strip():
        return None, "answer key %r is not a plugin ref" % (ref,)
    if not isinstance(value, dict):
        return None, "answer for '%s' must be an object" % ref
    want = value.get("want")
    if not isinstance(want, bool):
        return None, "answer for '%s': 'want' must be true or false" % ref
    decided = value.get("decided", "")
    if not isinstance(decided, str):
        return None, "answer for '%s': 'decided' must be a string" % ref
    scope = value.get("scope")
    if scope is not None and scope != ANSWER_SCOPE_USER:
        return None, "answer for '%s': 'scope' must be 'user' when present" % ref
    return PluginAnswer(ref=ref.strip(), want=want, decided=decided, scope=scope), None


def _read_plugin_choices(entries):
    """Return ``(bound_profile, answers, warnings)`` from the project-local layer.

    Recorded answers are a per-machine, per-project record, so they are read
    from the project-local file only; the key in any other layer is ignored
    with a warning naming the file. A malformed record is ignored (the user is
    asked again) rather than half-applied, and says so.
    """
    warnings = []
    profile = None
    answers = ()
    for path, kind, data in entries:
        if not isinstance(data, dict) or data.get("plugin_choices") is None:
            continue
        if kind != "project_local":
            warnings.append(
                "'plugin_choices' in %s is ignored -- answers are recorded in "
                "the project's bootstrap.local.json" % path
            )
            continue
        record = data["plugin_choices"]
        bound = record.get("profile") if isinstance(record, dict) else None
        raw_answers = record.get("answers") if isinstance(record, dict) else None
        if (not isinstance(bound, str) or not bound.strip()
                or not isinstance(raw_answers, dict)):
            warnings.append(
                "'plugin_choices' in %s needs a 'profile' name and an 'answers' "
                "object and was ignored" % path
            )
            continue
        parsed = []
        for ref, value in raw_answers.items():
            answer, problem = _parse_answer(ref, value)
            if problem:
                warnings.append("'plugin_choices' in %s: %s; ignored" % (path, problem))
                continue
            parsed.append(answer)
        profile = bound.strip()
        answers = tuple(parsed)
    return profile, answers, warnings


def validate_plugin_entries(entries, where="plugins"):
    """Return lint errors for the tier fields of a ``plugins`` list.

    ``entries`` is the raw list (``None`` and a non-list are the engine's to
    report, not this lint's). Only entries that declare a ``tier`` are held to
    the tier rules:

    - ``tier`` is one of :data:`TIERS` and the entry has a ``ref``;
    - ``install`` is ``"manual"`` (legacy-safe authoring: an engine that does
      not know tiers must never install or remove a tiered plugin);
    - ``recommended`` and ``not_recommended`` entries carry both ``why_install``
      and ``why_skip`` as non-empty text; a ``required`` entry's why lines are
      optional but must be text when present.
    """
    if not isinstance(entries, list):
        return []
    errors = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or "tier" not in entry:
            continue
        ref = entry.get("ref")
        label = "%s[%s]" % (where, ref if isinstance(ref, str) and ref else index)
        if not isinstance(ref, str) or not ref.strip():
            errors.append("%s: a tiered entry needs a 'ref'" % label)
        tier = entry["tier"]
        if tier not in TIERS:
            errors.append(
                "%s: tier %r is not one of %s" % (label, tier, ", ".join(TIERS))
            )
            continue
        if entry.get("install") != TIER_INSTALL_MODE:
            errors.append(
                "%s: a tiered entry must set install to \"manual\" so an older "
                "engine never installs or removes it" % label
            )
        for key in ("why_install", "why_skip"):
            value = entry.get(key)
            if value is None:
                if tier in QUESTIONED_TIERS:
                    errors.append("%s: tier %s requires '%s'" % (label, tier, key))
                continue
            if not isinstance(value, str):
                errors.append("%s: '%s' must be a string" % (label, key))
            elif tier in QUESTIONED_TIERS and not sanitize_description(
                    value, WHY_LINE_MAX):
                errors.append(
                    "%s: '%s' must hold printable ASCII text" % (label, key)
                )
    return errors


def _validate_profiles(raw_profiles):
    """Return ``(available, errors)``; any error makes the whole set unusable.

    D5: profile errors never block the base manifest, so the caller keeps
    provisioning from it -- but a partially valid profile set is not applied,
    because an author who mistyped a parent name wants to hear about it rather
    than get most of what they asked for.
    """
    infos = []
    errors = []

    for name, body in raw_profiles.items():
        if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
            errors.append(
                "invalid profile name %r -- use lower-case letters, digits, '-' "
                "or '_' (1-32 characters)" % (name,)
            )
            continue
        if name == NONE_SELECTION:
            errors.append(
                "'none' is reserved as the base-manifest selection and cannot "
                "name a profile"
            )
            continue
        if name == UNSET_SELECTION:
            errors.append(
                "'unset' is reserved as the cleared selection and cannot "
                "name a profile"
            )
            continue
        if not isinstance(body, dict):
            errors.append("profile '%s' must be an object" % name)
            continue

        for reserved in ("profiles", "profile"):
            if reserved in body:
                errors.append(
                    "profile '%s' declares '%s'; profiles do not nest"
                    % (name, reserved)
                )
        for reserved in ("plugin_choices", "profile_prompt"):
            if reserved in body:
                errors.append(
                    "profile '%s' declares '%s'; it is a project-level key, "
                    "not profile content" % (name, reserved)
                )
        errors.extend(validate_plugin_entries(
            body.get("plugins"), "profile '%s': plugins" % name
        ))

        description = body.get("description", "")
        if description is not None and not isinstance(description, str):
            errors.append("profile '%s': 'description' must be a string" % name)
            description = ""

        extends, extends_errors = _read_extends(name, body.get("extends"))
        errors.extend(extends_errors)

        infos.append(ProfileInfo(
            name=name,
            description=description or "",
            extends=extends,
        ))

    declared = {info.name for info in infos}
    graph = {}
    for info in infos:
        parents = []
        for parent in info.extends:
            if parent == info.name:
                errors.append("profile '%s' extends itself" % info.name)
                continue
            if parent not in declared:
                errors.append(
                    "profile '%s' extends '%s', which is not declared"
                    % (info.name, parent)
                )
                continue
            parents.append(parent)
        graph[info.name] = tuple(parents)

    errors.extend(_cycle_errors(graph))
    return tuple(infos), errors


def _read_extends(name, value):
    """Normalize a profile's ``extends`` to a tuple of names."""
    if value is None:
        return (), []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return (), ["profile '%s': 'extends' must be a list of profile names" % name]
    return tuple(value), []


def _cycle_errors(graph):
    """One error per inheritance cycle, reported as the path around it."""
    errors = []
    reported = set()
    state = {}
    stack = []

    def visit(node):
        state[node] = 1
        stack.append(node)
        for parent in graph.get(node, ()):
            if state.get(parent, 0) == 1:
                cycle = stack[stack.index(parent):] + [parent]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    errors.append(
                        "profile inheritance cycle: %s" % " -> ".join(cycle)
                    )
            elif state.get(parent, 0) == 0:
                visit(parent)
        stack.pop()
        state[node] = 2

    for node in graph:
        if state.get(node, 0) == 0:
            visit(node)
    return errors


def _linearize(name, graph):
    """Depth-first linearization: every parent before its child, once each.

    Diamond-safe -- a profile reached by two paths keeps its FIRST position, so
    a shared ancestor is applied before both descendants and never re-applied
    between them. Requires an acyclic graph, which validation has established.
    """
    order = []
    seen = set()

    def visit(node):
        if node in seen:
            return
        seen.add(node)
        for parent in graph.get(node, ()):
            visit(parent)
        order.append(node)

    visit(name)
    return order


def _write_target(entries, project_dir, home):
    """D1: write beside the highest layer that declares profiles.

    A project that ships profile definitions gets its selection written into
    the project's own local file, so one checkout's choice does not follow the
    user into every other project.
    """
    project_declares = any(
        kind in PROJECT_KINDS and isinstance(data, dict) and data.get("profiles")
        for _, kind, data in entries
    )
    if project_declares and project_dir:
        return os.path.join(project_dir, ".claude", "bootstrap.local.json")
    return _user_local_path(home)


def _user_local_path(home):
    base = home or os.path.expanduser("~")
    return os.path.join(base, ".claude", "bootstrap.local.json")


# ---------------------------------------------------------------------------
# The question and its directive
# ---------------------------------------------------------------------------


def sanitize_description(text, limit=DESCRIPTION_MAX):
    """Printable-ASCII, single-spaced, hard-truncated with NO ellipsis.

    Descriptions are authored in a manifest and rendered into an
    AskUserQuestion option, so they are treated as untrusted display text:
    anything outside 0x20-0x7E is dropped (tabs and newlines become a space so
    words do not run together) and the result is cut at ``limit``.
    """
    if not isinstance(text, str):
        return ""
    chars = []
    for ch in text:
        if " " <= ch <= "~":
            chars.append(ch)
        elif ch in "\t\r\n":
            chars.append(" ")
    return " ".join("".join(chars).split())[:limit]


def needs_typed_choice(state):
    """True when there are too many profiles to offer one option each.

    The threshold is read off ``available`` rather than off the options a
    particular mode would show, so the user meets the same shape of question
    whether they are choosing for the first time or switching.
    """
    return (
        state.status != "no_profiles"
        and len(state.available) > MAX_PROFILE_OPTIONS
    )


def render_profile_listing(state):
    """Every available profile as plain text, one per line.

    Shape, with both optional parts present::

        engineer (extends base, tools) -- Engineering machine setup.

    The name is always present; ``(extends ...)`` appears only when the profile
    declares parents, and the description only when it has one -- sanitized
    exactly as an option description is, because it reaches the user through
    the same untrusted route.

    Returns ``""`` when nothing is declared. Two callers need this listing --
    the prompt directive embeds it for the agent to print, and the CLI's
    ``--json`` payload carries it -- so it is rendered once, here.
    """
    lines = []
    for info in state.available:
        line = info.name
        if info.extends:
            line += " (extends %s)" % ", ".join(info.extends)
        description = sanitize_description(info.description)
        if description:
            line += " -- %s" % description
        lines.append(line)
    return "\n".join(lines)


def build_question(state, mode="first_run"):
    """The AskUserQuestion payload for a profile choice, or None.

    ``None`` under ``no_profiles``: there is nothing to ask. ``mode`` is
    ``first_run`` (lead option defers) or ``switch`` (lead option keeps the
    current selection).

    Past :data:`MAX_PROFILE_OPTIONS` profiles the payload carries exactly two
    options -- the lead one and :data:`TYPED_CHOICE_LABEL` -- and names no
    profile in the question text; the full list is printed to the user
    beforehand (see :func:`render_profile_listing`).
    """
    if state.status == "no_profiles":
        return None

    if mode == "switch":
        lead = {
            "label": "Keep current",
            "description": _keep_current_description(state),
        }
        candidates = [p for p in state.available if p.name != state.selected]
    else:
        lead = {
            "label": "Not now",
            "description": "Bootstrap asks again on a later pass.",
        }
        candidates = list(state.available)

    typed_choice = needs_typed_choice(state)
    options = [lead]
    if typed_choice:
        options.append({
            "label": TYPED_CHOICE_LABEL,
            "description": _typed_choice_description(candidates, state),
        })
    else:
        for info in candidates[:MAX_PROFILE_OPTIONS]:
            options.append({
                "label": info.name,
                "description": sanitize_description(info.description)
                or "Apply the '%s' profile." % info.name,
            })

    return {
        "question": _question_text(mode, typed_choice),
        "header": QUESTION_HEADER,
        "multiSelect": False,
        "options": options,
    }


def _typed_choice_description(candidates, state):
    """Point the user at Other, with a real profile name as the example."""
    pool = candidates or list(state.available)
    example = pool[0].name if pool else NONE_SELECTION
    return (
        "Pick Other and type a name from the list, for example '%s', or "
        "'%s' for the base manifest." % (example, NONE_SELECTION)
    )


def _keep_current_description(state):
    if state.status == "none":
        return "Keep the base manifest (no profile)."
    if state.selected:
        return "Keep the '%s' profile." % state.selected
    return "Leave the selection as it is."


def _question_text(mode, typed_choice):
    if mode == "switch":
        lines = ["Which bootstrap profile should this project use?"]
    else:
        lines = [
            "This project declares bootstrap profiles. Which one should "
            "bootstrap apply?"
        ]
    if typed_choice:
        # No enumeration here: the profiles were printed above, in full, with
        # room for their parents and descriptions.
        lines.append("The available profiles are listed above.")
    lines.append(
        "Type a profile name, or 'none' for the base manifest with no profile, "
        "into Other."
    )
    return " ".join(lines)


def prompt_directive(state, run_cmd):
    """The additionalContext directive that asks the user to pick a profile.

    This text lands in a consumer's session through the channel that also
    carries untrusted content, so it claims no authority it cannot back: it
    describes what the commands do and leaves every choice, including doing
    nothing, with the user (docs/reference/agent-directive-standards.md).
    """
    question = build_question(state, "first_run")
    if question is None:
        return ""
    listing_step = ""
    if needs_typed_choice(state):
        listing_step = (
            " More profiles are declared than fit as options, so print this "
            "list to the user first, as an ordinary chat message with one "
            "profile per line:\n%s\n" % render_profile_listing(state)
        )
    return (
        "Bootstrap found profile definitions for this project and no profile is "
        "selected.%s Ask the user with the AskUserQuestion tool, using exactly "
        "this question: %s\n"
        'If the user picks or types a profile name, run `bash "%s" profile set '
        "<name>` FROM THE PROJECT ROOT (the manifests bootstrap reads are "
        "resolved from the working directory). If the user asks for the base "
        "manifest, run "
        '`bash "%s" profile set none`, which records that choice and stops this '
        'prompt. On "Not now", do nothing further and do not ask again this '
        "session; bootstrap asks again on a later pass. Tell the user they can "
        "change the selection at any time with /bootstrap profile -- switching "
        "changes what bootstrap provisions from the next pass on, and "
        "uninstalls, at local scope for this project, the plugins the previous "
        "profile required or recommended that the new selection does not list."
        % (listing_step, json.dumps(question, sort_keys=True), run_cmd, run_cmd)
    )


# ---------------------------------------------------------------------------
# Prompt gating
# ---------------------------------------------------------------------------


def attended_signal_missing(env):
    """True when the harness never set the attended signal at all.

    Absent is NOT the same as "0": an older Claude Code sets neither, and the
    caller logs one quiet entry for that rather than treating the session as
    unattended for a reason it can explain.
    """
    return ENV_SESSION_ATTENDED not in env


def should_prompt(state, env, marker_dir):
    """True when this pass may ask the user to choose a profile.

    Every clause is a refusal to interrupt: only an open choice is worth
    asking about, only an attended session has anyone to answer, and a session
    is asked at most once -- with a short directory-wide guard so two sessions
    starting together do not both ask (F13). A project whose tracked manifest
    sets ``profile_prompt: false`` is never asked by bootstrap: it runs its own
    setup. The switch lives on the per-project ``state``, so other projects
    that declare profiles are unaffected.
    """
    if not state.profile_prompt:
        return False
    if state.status not in PROMPTABLE_STATUSES:
        return False
    if env.get(ENV_SESSION_ATTENDED) != "1":
        return False
    marker = _marker_name(env.get(ENV_SESSION_ID))
    if not marker:
        return False
    if os.path.exists(os.path.join(marker_dir, marker)):
        return False

    now = time.time()
    for path in _marker_paths(marker_dir):
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if 0 <= age < PROMPT_GUARD_SECONDS:
            return False
    return True


def mark_prompted(env, marker_dir):
    """Record that this session was prompted; prune expired markers.

    Returns the marker path, or ``None`` when there is no usable session id.
    """
    marker = _marker_name(env.get(ENV_SESSION_ID))
    if not marker:
        return None
    path = os.path.join(marker_dir, marker)
    write_atomic(path, "%d\n" % int(time.time()))
    _prune_markers(marker_dir)
    return path


def _marker_name(session_id):
    """A file-safe marker name, or "" when the session id is unusable.

    The session id reaches this module from the environment, so it is
    sanitized rather than trusted: every character outside ``[A-Za-z0-9._-]``
    becomes an underscore, and a name that would address a directory entry
    other than a marker ("." or "..") is refused outright.
    """
    if not isinstance(session_id, str):
        return ""
    cleaned = _MARKER_NAME_RE.sub("_", session_id.strip())[:_MARKER_NAME_MAX]
    if cleaned in ("", ".", ".."):
        return ""
    return cleaned


def _marker_paths(marker_dir):
    try:
        names = os.listdir(marker_dir)
    except OSError:
        return []
    return [os.path.join(marker_dir, name) for name in names]


def _prune_markers(marker_dir):
    now = time.time()
    for path in _marker_paths(marker_dir):
        try:
            if now - os.path.getmtime(path) <= MARKER_TTL_SECONDS:
                continue
            os.remove(path)
        except OSError:
            continue


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_selection(path, value, uninstall_edit=None):
    """Set (or with ``value=None`` remove) the ``profile`` key in ``path``.

    Every other key is preserved and the file is replaced atomically, because
    ``~/.claude/bootstrap.local.json`` has a concurrent writer: the engine's
    self-registration rewrites it on every full pass (F11).

    Recorded plugin answers belong to one profile: a write that changes the
    selection (or removes it) drops ``plugin_choices`` in the same write, so
    switching away and back never revives answers given for the earlier
    choice. Re-writing the profile the answers are bound to keeps them.

    ``uninstall_edit`` (an :class:`UninstallEdit`) is applied to ``path``'s
    ``plugins`` list in the same atomic write; pass it only when ``path`` is
    the project-local file (see :func:`write_uninstall_entries` otherwise).
    Returns the :class:`UninstallChange` actually made.

    Raises :class:`ProfileWriteError` WITHOUT writing when the file cannot be
    read, is not valid JSON, or is not a JSON object.
    """
    existing = _read_json_object(path)
    changed = False

    if value is None:
        if "profile" in existing:
            existing.pop("profile")
            changed = True
    elif existing.get("profile") != value:
        existing["profile"] = value
        changed = True

    choices = existing.get("plugin_choices")
    bound = choices.get("profile") if isinstance(choices, dict) else None
    if "plugin_choices" in existing and (value is None or bound != value):
        existing.pop("plugin_choices")
        changed = True

    change = apply_uninstall_edit(existing, uninstall_edit, path)
    if change:
        changed = True

    if not changed:
        return change
    write_atomic(path, json.dumps(existing, indent=2) + "\n")
    return change


def write_selection_with_answers(path, profile, answers, decided=None,
                                 uninstall_edit=None):
    """Record ``profile`` and its plugin ``answers`` in ONE atomic write.

    ``answers`` maps a plugin ref to ``{"want": bool}`` plus, for a ``want``
    False that the user confirmed on a user-scope install, ``"scope": "user"``.
    Each answer is stamped with ``decided`` (default: today, ``YYYY-MM-DD``)
    unless it carries its own. The answers are bound to ``profile``. Every
    other key in the file is preserved. With no answers any earlier
    ``plugin_choices`` is removed, so a re-applied profile never keeps answers
    the user did not just give.

    Profile and answers share one ``os.replace``: a convergence pass started
    afterwards sees both or neither, never a new profile with the old answers.
    ``uninstall_edit`` (an :class:`UninstallEdit`) rides in the same write, so
    the pass never sees the new profile without the uninstall entries that
    retire the old one. Returns the :class:`UninstallChange` made.

    Raises :class:`ProfileWriteError` WITHOUT writing when ``profile`` is not
    a selectable profile name (``none`` and ``unset`` are not -- see
    :func:`clear_selection`), an answer is malformed, or the file is unusable.
    """
    if not isinstance(profile, str) or not PROFILE_NAME_RE.match(profile):
        raise ProfileWriteError(
            "cannot record answers for %r: not a profile name" % (profile,)
        )
    if profile in (NONE_SELECTION, UNSET_SELECTION):
        raise ProfileWriteError(
            "'%s' is reserved and cannot be applied with answers" % profile
        )
    stamp = decided if decided is not None else time.strftime("%Y-%m-%d")
    if not isinstance(answers, dict):
        raise ProfileWriteError("answers must be an object of plugin refs")

    recorded = {}
    for ref, value in answers.items():
        answer, problem = _parse_answer(ref, value)
        if problem:
            raise ProfileWriteError("%s; nothing was written" % problem)
        entry = {"want": answer.want, "decided": answer.decided or stamp}
        if answer.scope:
            entry["scope"] = answer.scope
        recorded[answer.ref] = entry

    existing = _read_json_object(path)
    existing["profile"] = profile
    existing.pop("plugin_choices", None)
    if recorded:
        existing["plugin_choices"] = {"profile": profile, "answers": recorded}
    change = apply_uninstall_edit(existing, uninstall_edit, path)
    write_atomic(path, json.dumps(existing, indent=2) + "\n")
    return change


def clear_selection(path, uninstall_edit=None):
    """Clear the choice: ``profile`` becomes ``"unset"``, answers are removed.

    One atomic write that always happens, even when no file or key existed,
    because ``"unset"`` in the project-local file is what stops a ``profile``
    left in the user-local file from selecting again (see
    :data:`UNSET_SELECTION`). Every other key is preserved.
    ``uninstall_edit`` rides in the same write; returns the
    :class:`UninstallChange` made.

    Raises :class:`ProfileWriteError` WITHOUT writing when the file is unusable.
    """
    existing = _read_json_object(path)
    existing["profile"] = UNSET_SELECTION
    existing.pop("plugin_choices", None)
    change = apply_uninstall_edit(existing, uninstall_edit, path)
    write_atomic(path, json.dumps(existing, indent=2) + "\n")
    return change


# ---------------------------------------------------------------------------
# Uninstall entries a profile switch writes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UninstallEdit:
    """Which ``uninstall: true`` entries a profile switch adds and drops.

    ``add`` are plugin refs to mark ``uninstall: true``; ``remove`` are refs
    whose ``uninstall: true`` mark is dropped. The two never overlap.
    """

    add: Tuple[str, ...] = ()
    remove: Tuple[str, ...] = ()

    def __bool__(self):
        return bool(self.add or self.remove)


@dataclass(frozen=True)
class UninstallChange:
    """What an :class:`UninstallEdit` actually changed in a file.

    An entry already in the requested state is in neither list: the edit is
    idempotent.
    """

    written: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()
    path: Optional[str] = None

    def __bool__(self):
        return bool(self.written or self.removed)


def _entry_ref(entry):
    ref = entry.get("ref") if isinstance(entry, dict) else None
    return ref.strip() if isinstance(ref, str) and ref.strip() else None


def _unique(items):
    return list(dict.fromkeys(items))


def plan_switch_uninstalls(old_state, old_manifest, new_state, new_manifest):
    """The uninstall entries a switch from ``old_state`` to ``new_state`` needs.

    ``*_manifest`` are the EFFECTIVE manifests (``resolve_layers`` output) for
    the old and the new selection, so each already holds its profile chain
    merged over the base layers.

    - ``add``: when the old selection is a profile (status ``selected``) and
      the new selection is a different one, or ``none``, or ``unset``: every
      plugin the old effective manifest tiers ``required`` or ``recommended``
      that the new effective manifest does not list at ALL, in any tier.
      "Not listed" also keeps out anything in the base ``plugins[]``, because
      the new effective manifest includes the base. Re-selecting the same
      profile adds nothing.
    - ``remove``: whenever the new selection is a profile, every plugin it
      tiers ``required`` or ``recommended``: an earlier switch may have marked
      it, and a plugin the profile wants must not carry an uninstall entry.
    """
    new_plugins = _plugin_entries(new_manifest)
    new_refs = {_entry_ref(e) for e in new_plugins} - {None}

    add = []
    switched = old_state.status == "selected" and not (
        new_state.status == "selected" and new_state.selected == old_state.selected
    )
    if switched:
        for entry in _plugin_entries(old_manifest):
            ref = _entry_ref(entry)
            if (ref and ref not in new_refs
                    and entry.get("tier") in (TIER_REQUIRED, TIER_RECOMMENDED)):
                add.append(ref)

    remove = []
    if new_state.status == "selected":
        for entry in new_plugins:
            ref = _entry_ref(entry)
            if ref and entry.get("tier") in (TIER_REQUIRED, TIER_RECOMMENDED):
                remove.append(ref)

    return UninstallEdit(add=tuple(_unique(add)), remove=tuple(_unique(remove)))


def _plugin_entries(manifest):
    plugins = manifest.get("plugins") if isinstance(manifest, dict) else None
    return [e for e in plugins if isinstance(e, dict)] if isinstance(plugins, list) else []


def apply_uninstall_edit(existing, edit, path=None):
    """Apply ``edit`` to the parsed manifest ``existing`` in place.

    Returns the :class:`UninstallChange`. An entry that carries only ``ref``
    and ``uninstall`` is removed outright; one with other keys loses just its
    ``uninstall`` mark, so a hand-written entry is never clobbered. Every
    other key and entry is preserved, and ``plugins`` is not created when
    nothing needs writing. Raises :class:`ProfileWriteError` when ``plugins``
    exists but is not a list, before ``existing`` is touched.
    """
    if not edit:
        return UninstallChange(path=path)
    plugins = existing.get("plugins")
    if plugins is None:
        plugins = []
    elif not isinstance(plugins, list):
        raise ProfileWriteError(
            "%s: 'plugins' is not a list; nothing was written"
            % (path or "the manifest")
        )

    drop = set(edit.remove)
    removed, kept = [], []
    for item in plugins:
        ref = _entry_ref(item)
        if ref in drop and item.get("uninstall") is True:
            removed.append(ref)
            if set(item) - {"ref", "uninstall"}:
                kept.append({k: v for k, v in item.items() if k != "uninstall"})
            continue
        kept.append(item)

    written = []
    for ref in edit.add:
        entry = next((i for i in kept if _entry_ref(i) == ref), None)
        if entry is None:
            kept.append({"ref": ref, "uninstall": True})
            written.append(ref)
        elif entry.get("uninstall") is not True:
            entry["uninstall"] = True
            written.append(ref)

    if written or removed:
        existing["plugins"] = kept
    return UninstallChange(
        written=tuple(written), removed=tuple(_unique(removed)), path=path)


def write_uninstall_entries(path, edit):
    """Apply ``edit`` to ``path`` in its own atomic write (no selection change).

    For the case where the selection goes to a different file than the
    project-local one that holds the entries. Returns the
    :class:`UninstallChange`; writes nothing when it is empty.
    """
    existing = _read_json_object(path)
    change = apply_uninstall_edit(existing, edit, path)
    if change:
        write_atomic(path, json.dumps(existing, indent=2) + "\n")
    return change


def _read_json_object(path):
    """The JSON object in ``path`` (``{}`` when absent or empty).

    Raises :class:`ProfileWriteError` when the file cannot be read, is not
    valid JSON, or is not an object -- a hand-authored file is never rewritten
    from a guess about what it meant.
    """
    existing = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise ProfileWriteError("could not read %s: %s" % (path, exc))
        if text.strip():
            try:
                existing = json.loads(text)
            except ValueError as exc:
                raise ProfileWriteError(
                    "%s is not valid JSON (%s); nothing was written" % (path, exc)
                )
            if not isinstance(existing, dict):
                raise ProfileWriteError(
                    "%s does not hold a JSON object; nothing was written" % path
                )
    return existing


def ensure_vcs_excluded(project_dir):
    """Exclude the project's ``bootstrap.local.json`` from Git.

    Returns a description of what was done, or ``None`` when ``project_dir`` is
    not a Git repository (there is nothing to exclude). Raises
    :class:`ProfileWriteError` when Git is present but the exclusion could not
    be established -- a silent failure here would leave a per-machine selection
    looking committable.
    """
    status, detail = _apply_git_file_exclusion(
        project_dir, PROJECT_LOCAL_REL, header=_GIT_EXCLUDE_HEADER
    )
    if status == "error":
        raise ProfileWriteError(detail)
    if status == "none":
        return None
    return detail
