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

Everything here is pure with two named exceptions -- ``write_selection`` and
``mark_prompted`` write, and ``ensure_vcs_excluded`` shells out to Git. Message
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

#: A profile name is a file-safe, lower-case identifier.
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: Statuses that leave a choice for the user to make.
PROMPTABLE_STATUSES = ("unselected", "unknown")

#: AskUserQuestion shows a small option set; one slot is spent on the leading
#: defer/keep option, so at most three profiles are offered directly and the
#: rest are named in the question text (D10).
MAX_PROFILE_OPTIONS = 3
QUESTION_HEADER = "Profile"

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

    Returns ``(effective_manifest, ProfileState)``. ``profiles`` and ``profile``
    are stripped from the effective manifest in EVERY status, so no downstream
    check can observe them and no engine pass can act on a profile twice.
    """
    entries = [(path, kind, data) for path, kind, data in layers]

    merged = {}
    for _, _, data in entries:
        if isinstance(data, dict):
            merged = merge_manifests(merged, data)
    base = _strip_profile_keys(merged)

    warnings = []
    selected, source = _read_selection(entries, warnings)
    raw_profiles = merged.get("profiles")

    # D9: an empty (or absent) `profiles` is the inert case, whatever `profile`
    # says. Reported with no warning and no error -- a project that does not use
    # profiles must not pay for a stale key in someone's local file.
    if not raw_profiles:
        return base, ProfileState(
            status="no_profiles",
            write_target=_user_local_path(home),
        )

    if isinstance(raw_profiles, dict):
        available, errors = _validate_profiles(raw_profiles)
    else:
        available, errors = (), ["'profiles' must be an object of profile names"]

    write_target = _write_target(entries, project_dir, home)

    if errors:
        return base, ProfileState(
            status="invalid",
            available=available,
            warnings=tuple(warnings),
            errors=tuple(errors),
            write_target=write_target,
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
        )

    names = [info.name for info in available]
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

    return effective, ProfileState(
        status=status,
        selected=selected if status in ("selected", "none", "unknown") else None,
        source=source,
        chain=chain,
        available=available,
        warnings=tuple(warnings),
        errors=(),
        write_target=write_target,
    )


def _strip_profile_keys(manifest):
    """The manifest without the two keys this module owns."""
    return {k: v for k, v in manifest.items() if k not in ("profiles", "profile")}


def _profile_body(body):
    """A profile's manifest content: everything that is not its own metadata."""
    return {
        k: v for k, v in body.items()
        if k not in ("extends", "description", "profiles", "profile")
    }


def _read_selection(entries, warnings):
    """Return ``(selected, source_path)`` and append any layer warnings.

    A ``profile`` outside the two local layers is IGNORED with a warning naming
    the file: acting on it would let a committed manifest decide a per-machine
    choice, and dropping it silently would leave the author believing it took.
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
        if not isinstance(body, dict):
            errors.append("profile '%s' must be an object" % name)
            continue

        for reserved in ("profiles", "profile"):
            if reserved in body:
                errors.append(
                    "profile '%s' declares '%s'; profiles do not nest"
                    % (name, reserved)
                )

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


def build_question(state, mode="first_run"):
    """The AskUserQuestion payload for a profile choice, or None.

    ``None`` under ``no_profiles``: there is nothing to ask. ``mode`` is
    ``first_run`` (lead option defers) or ``switch`` (lead option keeps the
    current selection).
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

    shown = candidates[:MAX_PROFILE_OPTIONS]
    options = [lead]
    for info in shown:
        options.append({
            "label": info.name,
            "description": sanitize_description(info.description)
            or "Apply the '%s' profile." % info.name,
        })

    return {
        "question": _question_text(state, mode, candidates, shown),
        "header": QUESTION_HEADER,
        "multiSelect": False,
        "options": options,
    }


def _keep_current_description(state):
    if state.status == "none":
        return "Keep the base manifest (no profile)."
    if state.selected:
        return "Keep the '%s' profile." % state.selected
    return "Leave the selection as it is."


def _question_text(state, mode, candidates, shown):
    if mode == "switch":
        lines = ["Which bootstrap profile should this project use?"]
    else:
        lines = [
            "This project declares bootstrap profiles. Which one should "
            "bootstrap apply?"
        ]
    if len(candidates) > len(shown):
        lines.append(
            "All profiles: %s." % ", ".join(info.name for info in candidates)
        )
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
    return (
        "Bootstrap found profile definitions for this project and no profile is "
        "selected. Ask the user with the AskUserQuestion tool, using exactly "
        "this question: %s\n"
        'If the user picks a profile name, run `bash "%s" profile set <name>` '
        "FROM THE PROJECT ROOT (the manifests bootstrap reads are resolved from "
        "the working directory). If the user asks for the base manifest, run "
        '`bash "%s" profile set none`, which records that choice and stops this '
        'prompt. On "Not now", do nothing further and do not ask again this '
        "session; bootstrap asks again on a later pass. Tell the user they can "
        "change the selection at any time with /bootstrap profile -- switching "
        "changes what bootstrap provisions from the next pass on and uninstalls "
        "nothing."
        % (json.dumps(question, sort_keys=True), run_cmd, run_cmd)
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
    starting together do not both ask (F13).
    """
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


def write_selection(path, value):
    """Set (or with ``value=None`` remove) the ``profile`` key in ``path``.

    Every other key is preserved and the file is replaced atomically, because
    ``~/.claude/bootstrap.local.json`` has a concurrent writer: the engine's
    self-registration rewrites it on every full pass (F11).

    Raises :class:`ProfileWriteError` WITHOUT writing when the file cannot be
    read, is not valid JSON, or is not a JSON object.
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

    if value is None:
        if "profile" not in existing:
            return
        existing.pop("profile")
    else:
        existing["profile"] = value

    write_atomic(path, json.dumps(existing, indent=2) + "\n")


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
