"""Plugin tiers: decide, from recorded answers and what is installed, what to do.

A ``plugins[]`` manifest entry may carry a ``tier`` (``required``,
``recommended`` or ``not_recommended``) and two one-line reasons, ``why_install``
and ``why_skip``. This module is the resolver. It is PURE: it reads plain data
and returns plain data, so the engine decides when to run it and what to do
with the result, and nothing here touches the registry, settings files or the
network.

The rules, from the user's rulings:

- Only RECOMMENDED plugins that are NOT installed are ever asked about, and
  only NOT-RECOMMENDED plugins that ARE installed are ever asked about.
- A recorded answer is acted on without further confirmation. A recommended,
  uninstalled plugin the user wants is installed; a not-recommended, installed
  plugin the user does not want is uninstalled.
- A removal bootstrap cannot perform is never reported done. A ``project``
  install (enabled by the tracked ``.claude/settings.json``) is never written
  to; a ``user`` install is removed only when the answer carries the
  user-scope confirmation. Both come back as ``unsatisfied`` every pass until
  the inventory agrees.
- Satisfaction is never recorded: each call recomputes from the inventory.
- An entry may also carry ``uninstall: true`` (see
  :func:`resolve_uninstall_entries`): a declarative order to remove the
  plugin's LOCAL-scope install for this project, independent of any tier. It
  is how a profile switch removes what the previous profile added.

The authored ``install`` value on a tiered entry is ignored (it is always
``manual`` for legacy safety, see :func:`profiles.validate_plugin_entries`);
this module decides the install mode and scope and returns rewritten entries.

Inventory shape (any object with these attributes, or a mapping with these
keys; one record per applicable install of a plugin)::

    ref           "marketplace:plugin"
    scope         "user" | "project" | "local"
    project_path  the project the record applies to, or None
    installed     True when the plugin is really installed there; False for a
                  dormant record (registered, not enabled)

The caller passes only installs that apply to THIS project. Records may repeat
a (scope, project) pair; uninstalls are deduplicated here.

Stdlib only: imported from hooks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple

from .profiles import (
    ANSWER_SCOPE_USER,
    QUESTIONED_TIERS,
    TIER_NOT_RECOMMENDED,
    TIER_RECOMMENDED,
    TIER_REQUIRED,
    TIERS,
    WHY_LINE_MAX,
    sanitize_description,
)

SCOPE_USER = "user"
SCOPE_PROJECT = "project"
SCOPE_LOCAL = "local"
SCOPES = (SCOPE_USER, SCOPE_PROJECT, SCOPE_LOCAL)

ACTION_INSTALL = "install"
ACTION_UNINSTALL = "uninstall"
ACTION_ASK = "ask"
ACTION_NONE = "none"
ACTION_UNSATISFIED = "unsatisfied"
ACTION_NOTICE = "notice"
ACTION_CONFLICT = "conflict"

#: Scope an install the resolver orders is made at. Runtime never writes the
#: tracked project settings, so a wanted plugin is installed per user, per
#: project: the ignored ``settings.local.json``.
INSTALL_SCOPE = SCOPE_LOCAL

WHY_INSTALL = "why_install"
WHY_SKIP = "why_skip"

REASON_NO_PROFILE = "no profile is selected; tiers do nothing"
REASON_DISABLED = "disabled in the manifest (enabled: false)"
REASON_PROJECT_INSTALL = (
    "enabled by the shared settings.json; cannot remove per user"
)
REASON_USER_INSTALL = (
    "user-scope install; needs the user-scope double-check -- run the profile "
    "skill"
)


class PluginTierError(ValueError):
    """The resolver was handed data it will not guess about."""


class InstallRecord(Protocol):
    """One applicable install of a plugin (see the module docstring)."""

    ref: str
    scope: str
    project_path: Optional[str]
    installed: bool


@dataclass(frozen=True)
class PluginAction:
    """One thing to do, or one thing that cannot be done, for a plugin.

    A plugin can yield several (an uninstall of its local install plus an
    ``unsatisfied`` for its project install). ``scope`` and ``project_path``
    are set for ``install`` / ``uninstall`` / ``unsatisfied`` actions that
    target a specific install; ``reason`` is display text for ``unsatisfied``,
    ``none`` and ``ask``.
    """

    ref: str
    tier: str
    action: str
    scope: Optional[str] = None
    project_path: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class Question:
    """One question for the user, with its two reasons in the order to show.

    ``why_lines`` is ``((kind, text), ...)`` with ``kind`` ``why_install`` or
    ``why_skip``: install reason first for a recommended plugin, skip reason
    first for a not-recommended one. ``scopes`` are the scopes of the
    plugin's current installs (empty for a recommended, uninstalled plugin).
    ``needs_user_scope_confirm`` is True when answering "do not want" would
    touch a user-scope install, which needs the double-check.
    """

    ref: str
    tier: str
    why_lines: Tuple[Tuple[str, str], ...]
    why_install: str
    why_skip: str
    scopes: Tuple[str, ...] = ()
    needs_user_scope_confirm: bool = False


@dataclass(frozen=True)
class TierResolution:
    """What the resolver decided.

    ``plugins`` is the rewritten ``plugins[]`` list, every entry in its input
    position: untiered entries are copied unchanged; a tiered entry carries an
    explicit ``install`` (``auto`` with scope ``local`` to install it,
    otherwise ``manual`` so it is only kept up to date). ``actions`` lists all
    decisions; ``uninstalls`` and ``unsatisfied`` are its two filtered views;
    ``questions`` are recommended first, then not-recommended, each group in
    manifest order.
    """

    plugins: Tuple[Dict[str, Any], ...]
    actions: Tuple[PluginAction, ...]
    questions: Tuple[Question, ...]

    @property
    def installs(self) -> Tuple[PluginAction, ...]:
        return tuple(a for a in self.actions if a.action == ACTION_INSTALL)

    @property
    def uninstalls(self) -> Tuple[PluginAction, ...]:
        return tuple(a for a in self.actions if a.action == ACTION_UNINSTALL)

    @property
    def unsatisfied(self) -> Tuple[PluginAction, ...]:
        return tuple(a for a in self.actions if a.action == ACTION_UNSATISFIED)


def resolve_plugin_tiers(
    entries: Iterable[Mapping[str, Any]],
    answers: Mapping[str, Mapping[str, Any]],
    inventory: Iterable[Any],
    *,
    profile_active: bool = True,
) -> TierResolution:
    """Decide the action for every tiered entry in ``entries``.

    ``entries`` is the effective manifest's ``plugins`` list. ``answers`` is
    the BOUND recorded answers as ``{ref: {"want": bool, "scope": "user"|None}}``
    (``profiles.answers_by_ref(state)``). ``inventory`` is the installs, as
    described in the module docstring. ``profile_active`` is False when no
    profile is selected (status unselected, none, unknown or invalid): tiered
    entries then stay ``manual`` with no action and no question.

    Raises :class:`PluginTierError` on an unknown tier, a tiered entry with no
    ref or without the why lines its tier requires, a malformed answer, or a
    malformed inventory record.
    """
    installed_by_ref = _installed_by_ref(inventory)
    answer_by_ref = _validated_answers(answers)

    rewritten: List[Dict[str, Any]] = []
    actions: List[PluginAction] = []
    recommended_questions: List[Question] = []
    skip_questions: List[Question] = []

    for entry in entries:
        entry = dict(entry)
        tier = entry.get("tier")
        if tier is None:
            rewritten.append(entry)
            continue
        if tier not in TIERS:
            raise PluginTierError(
                "plugin %r: unknown tier %r" % (entry.get("ref"), tier)
            )
        ref = entry.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            raise PluginTierError("a tiered plugin entry has no ref: %r" % (entry,))
        ref = ref.strip()
        why = _why_lines(entry, ref, tier)

        if not profile_active:
            entry["install"] = "manual"
            rewritten.append(entry)
            actions.append(PluginAction(ref, tier, ACTION_NONE, reason=REASON_NO_PROFILE))
            continue

        installs = installed_by_ref.get(ref, [])
        answer = answer_by_ref.get(ref)
        entry_actions, question, install_it = _decide(
            ref, tier, entry, installs, answer, why
        )
        actions.extend(entry_actions)
        if question is not None:
            if tier == TIER_RECOMMENDED:
                recommended_questions.append(question)
            else:
                skip_questions.append(question)

        if install_it:
            entry["install"] = "auto"
            entry["scope"] = INSTALL_SCOPE
        else:
            entry["install"] = "manual"
        rewritten.append(entry)

    return TierResolution(
        plugins=tuple(rewritten),
        actions=tuple(actions),
        questions=tuple(recommended_questions + skip_questions),
    )


# ---------------------------------------------------------------------------
# Uninstall entries (``uninstall: true``)
# ---------------------------------------------------------------------------

UNINSTALL_KEY = "uninstall"

REASON_UNINSTALL_SATISFIED = "no registry record for this project; nothing to uninstall"
REASON_UNINSTALL_OTHER_SCOPE = (
    "bootstrap uninstalls at local scope only; remove it at that scope by hand"
)


@dataclass(frozen=True)
class UninstallResolution:
    """What :func:`resolve_uninstall_entries` decided.

    ``plugins`` is the entry list in its input positions. An entry whose
    uninstall is carried out or satisfied is rewritten ``install: manual`` so
    the plugin phase never installs a plugin the manifest asks to remove (an
    entry that omits ``install`` would otherwise default to ``auto``); an entry
    in conflict keeps whatever the rest of the manifest says. ``actions`` holds
    ``uninstall`` (a local install to remove), ``none`` (satisfied), ``notice``
    (user/project installs bootstrap will not touch) and ``conflict``.
    """

    plugins: Tuple[Dict[str, Any], ...]
    actions: Tuple[PluginAction, ...]

    @property
    def uninstalls(self) -> Tuple[PluginAction, ...]:
        return tuple(a for a in self.actions if a.action == ACTION_UNINSTALL)

    @property
    def conflicts(self) -> Tuple[PluginAction, ...]:
        return tuple(a for a in self.actions if a.action == ACTION_CONFLICT)

    @property
    def notices(self) -> Tuple[PluginAction, ...]:
        return tuple(a for a in self.actions if a.action == ACTION_NOTICE)

    @property
    def satisfied(self) -> Tuple[PluginAction, ...]:
        return tuple(a for a in self.actions if a.action == ACTION_NONE)


def resolve_uninstall_entries(
    entries: Iterable[Mapping[str, Any]],
    answers: Mapping[str, Mapping[str, Any]],
    inventory: Iterable[Any],
    *,
    profile_active: bool = True,
) -> UninstallResolution:
    """Decide the action for every ``uninstall: true`` entry in ``entries``.

    ``entries`` is the effective ``plugins`` list AFTER :func:`resolve_plugin_tiers`
    ran over it (layers merge entries by ``ref``, so a local ``uninstall`` entry
    and a profile's tiered entry for the same plugin are one entry). ``answers``,
    ``inventory`` and ``profile_active`` are as for :func:`resolve_plugin_tiers`.

    - A plugin the rest of the manifest wants installed is a CONFLICT: nothing
      is uninstalled and the entry says so every pass, so a plugin is never
      installed and removed in turn. Wanted means ``install: "auto"`` written
      on the entry, tier ``required``, tier ``recommended`` unless the user
      answered no, or tier ``not_recommended`` kept by a yes answer. Tiers
      count only while a profile is selected (``profile_active``).
    - Otherwise the plugin's LOCAL-scope install for this project is uninstalled.
      A DORMANT record (registered, enabled in no settings file of its scope)
      counts as an install here: the entry asks for the registry record to go,
      and an unenabled record still holds it. No record for this project means
      the entry is satisfied: not a failure, and nothing to fix. User and
      project records are never touched; they come back as a ``notice``.

    ``uninstall`` must be a boolean; ``false`` makes the entry an ordinary
    one. Raises :class:`PluginTierError` for a non-boolean ``uninstall``, an
    ``uninstall: true`` entry with no ref, or a malformed answer or inventory.
    """
    installed_by_ref = _installed_by_ref(inventory, count_dormant=True)
    answer_by_ref = _validated_answers(answers)

    rewritten: List[Dict[str, Any]] = []
    actions: List[PluginAction] = []
    for entry in entries:
        entry = dict(entry)
        flag = entry.get(UNINSTALL_KEY)
        if flag is None or flag is False:
            rewritten.append(entry)
            continue
        if flag is not True:
            raise PluginTierError(
                "plugin %r: '%s' must be true or false, not %r"
                % (entry.get("ref"), UNINSTALL_KEY, flag)
            )
        ref = entry.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            raise PluginTierError(
                "an '%s' entry has no ref: %r" % (UNINSTALL_KEY, entry)
            )
        ref = ref.strip()
        tier = entry.get("tier") or ""

        wanted = wanted_installed(entry, answer_by_ref.get(ref), profile_active)
        if wanted:
            actions.append(PluginAction(
                ref, tier, ACTION_CONFLICT,
                reason="also wanted installed (%s); not uninstalled" % wanted))
            rewritten.append(entry)
            continue

        entry["install"] = "manual"
        rewritten.append(entry)
        installs = installed_by_ref.get(ref, [])
        local = _local_targets(installs)
        for path in local:
            actions.append(PluginAction(
                ref, tier, ACTION_UNINSTALL, scope=SCOPE_LOCAL, project_path=path))
        others = tuple(dict.fromkeys(
            r["scope"] for r in installs if r["scope"] != SCOPE_LOCAL))
        if others:
            actions.append(PluginAction(
                ref, tier, ACTION_NOTICE, scope=",".join(others),
                reason=REASON_UNINSTALL_OTHER_SCOPE))
        if not installs:
            actions.append(PluginAction(
                ref, tier, ACTION_NONE, reason=REASON_UNINSTALL_SATISFIED))

    return UninstallResolution(plugins=tuple(rewritten), actions=tuple(actions))


def wanted_installed(entry, answer, profile_active):
    """Why the manifest wants the plugin installed, or ``""`` when it does not.

    Shared by :func:`resolve_uninstall_entries` (a wanted plugin is a conflict)
    and ``profiles.plan_switch_uninstalls`` (a wanted plugin gets no entry).
    ``answer`` is the recorded ``{"want": bool, ...}`` for the ref, or ``None``.
    """
    if entry.get("install") == "auto":
        return "install: auto"
    if not profile_active:
        return ""
    tier = entry.get("tier")
    if tier == TIER_REQUIRED:
        return "tier required"
    if tier == TIER_RECOMMENDED:
        if answer is None:
            return "tier recommended, not answered"
        return "tier recommended, answered yes" if answer["want"] else ""
    if tier == TIER_NOT_RECOMMENDED and answer is not None and answer["want"]:
        return "tier not_recommended, kept by answer"
    return ""


def _local_targets(installs):
    """Distinct normalized project paths of the local installs."""
    paths = []
    for record in installs:
        if record["scope"] != SCOPE_LOCAL:
            continue
        path = _normalize_path(record["project_path"])
        if path not in paths:
            paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# Per-tier decisions
# ---------------------------------------------------------------------------


def _decide(ref, tier, entry, installs, answer, why):
    """Return ``(actions, question_or_None, install_it)`` for one tiered entry."""
    if tier == TIER_REQUIRED:
        return _decide_installing(ref, tier, entry, installs)

    if tier == TIER_RECOMMENDED:
        if installs:
            return ([PluginAction(ref, tier, ACTION_NONE, reason="installed")],
                    None, False)
        if answer is None:
            question = _question(ref, tier, why, installs)
            return ([PluginAction(ref, tier, ACTION_ASK, reason="not answered")],
                    question, False)
        if answer["want"]:
            return _decide_installing(ref, tier, entry, installs)
        return ([PluginAction(ref, tier, ACTION_NONE, reason="declined")],
                None, False)

    # not_recommended
    if not installs:
        return ([PluginAction(ref, tier, ACTION_NONE, reason="not installed")],
                None, False)
    if answer is None:
        question = _question(ref, tier, why, installs)
        return ([PluginAction(ref, tier, ACTION_ASK, reason="not answered")],
                question, False)
    if answer["want"]:
        return ([PluginAction(ref, tier, ACTION_NONE, reason="kept")], None, False)
    return _removals(ref, tier, installs, answer), None, False


def _decide_installing(ref, tier, entry, installs):
    """A plugin that should be installed: install it unless it already is."""
    if installs:
        return ([PluginAction(ref, tier, ACTION_NONE, reason="installed")],
                None, False)
    if entry.get("enabled") is False:
        return ([PluginAction(ref, tier, ACTION_NONE, reason=REASON_DISABLED)],
                None, False)
    return ([PluginAction(ref, tier, ACTION_INSTALL, scope=INSTALL_SCOPE)],
            None, True)


def _removals(ref, tier, installs, answer):
    """Actions that remove a plugin the user does not want, install by install.

    Duplicate installs of one target collapse to one action: a user-scope
    uninstall does not depend on a record's project path, and a local or
    project target is keyed by its normalized path.
    """
    actions = []
    seen = set()
    confirmed_user = answer.get("scope") == ANSWER_SCOPE_USER
    for scope in (SCOPE_LOCAL, SCOPE_USER, SCOPE_PROJECT):
        for record in installs:
            if record["scope"] != scope:
                continue
            path = _normalize_path(record["project_path"])
            key = (scope, None) if scope == SCOPE_USER else (scope, path)
            if key in seen:
                continue
            seen.add(key)
            if scope == SCOPE_LOCAL:
                actions.append(PluginAction(
                    ref, tier, ACTION_UNINSTALL, scope=scope, project_path=path))
            elif scope == SCOPE_USER and confirmed_user:
                actions.append(PluginAction(
                    ref, tier, ACTION_UNINSTALL, scope=scope))
            elif scope == SCOPE_USER:
                actions.append(PluginAction(
                    ref, tier, ACTION_UNSATISFIED, scope=scope,
                    reason=REASON_USER_INSTALL))
            else:
                actions.append(PluginAction(
                    ref, tier, ACTION_UNSATISFIED, scope=scope, project_path=path,
                    reason=REASON_PROJECT_INSTALL))
    return actions


def _question(ref, tier, why, installs):
    kinds = (WHY_INSTALL, WHY_SKIP) if tier == TIER_RECOMMENDED else (WHY_SKIP, WHY_INSTALL)
    scopes = tuple(dict.fromkeys(r["scope"] for r in installs))
    return Question(
        ref=ref,
        tier=tier,
        why_lines=tuple((kind, why[kind]) for kind in kinds),
        why_install=why[WHY_INSTALL],
        why_skip=why[WHY_SKIP],
        scopes=scopes,
        needs_user_scope_confirm=SCOPE_USER in scopes,
    )


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def _why_lines(entry, ref, tier):
    """Sanitized why lines; both are required for a questioned tier."""
    lines = {}
    for key in (WHY_INSTALL, WHY_SKIP):
        value = entry.get(key)
        if value is None and tier in QUESTIONED_TIERS:
            raise PluginTierError("plugin %s: tier %s requires '%s'" % (ref, tier, key))
        if value is not None and not isinstance(value, str):
            raise PluginTierError("plugin %s: '%s' must be a string" % (ref, key))
        text = sanitize_description(value or "", WHY_LINE_MAX)
        if tier in QUESTIONED_TIERS and not text:
            raise PluginTierError(
                "plugin %s: '%s' must hold printable ASCII text" % (ref, key)
            )
        lines[key] = text
    return lines


def _validated_answers(answers):
    out = {}
    for ref, value in answers.items():
        if not isinstance(value, Mapping) or not isinstance(value.get("want"), bool):
            raise PluginTierError(
                "answer for %r must be an object with a boolean 'want'" % (ref,)
            )
        scope = value.get("scope")
        if scope is not None and scope != ANSWER_SCOPE_USER:
            raise PluginTierError(
                "answer for %r: 'scope' must be %r when present"
                % (ref, ANSWER_SCOPE_USER)
            )
        out[ref] = {"want": value["want"], "scope": scope}
    return out


def _installed_by_ref(inventory, count_dormant=False):
    """Really-installed records, as plain dicts, grouped by ref.

    Dormant records (``installed`` False) do not count by default: a plugin
    that is registered but not enabled anywhere is not installed, so it is
    neither asked about nor taken as satisfying a tier. ``count_dormant`` keeps
    them, for a caller that removes registry records (``uninstall: true``).
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for record in inventory:
        ref = _field(record, "ref")
        scope = _field(record, "scope")
        installed = _field(record, "installed")
        path = _field(record, "project_path", default=None)
        if not isinstance(ref, str) or not ref:
            raise PluginTierError("inventory record has no ref: %r" % (record,))
        if scope not in SCOPES:
            raise PluginTierError(
                "inventory record for %s: scope %r is not one of %s"
                % (ref, scope, ", ".join(SCOPES))
            )
        if not isinstance(installed, bool):
            raise PluginTierError(
                "inventory record for %s: 'installed' must be a bool" % ref
            )
        if path is not None and not isinstance(path, str):
            raise PluginTierError(
                "inventory record for %s: 'project_path' must be text or None" % ref
            )
        if installed or count_dormant:
            grouped.setdefault(ref, []).append(
                {"scope": scope, "project_path": path}
            )
    return grouped


_MISSING = object()


def _field(record, name, default=_MISSING):
    if isinstance(record, Mapping):
        value = record.get(name, default)
    else:
        value = getattr(record, name, default)
    if value is _MISSING:
        raise PluginTierError("inventory record %r has no %r" % (record, name))
    return value


def _normalize_path(path):
    """Slash-separated, repeats collapsed, no trailing slash; case is kept.

    Only enough to compare two spellings of the same recorded path. The
    inventory owner has already decided which records apply to this project.
    """
    if path is None:
        return None
    text = re.sub(r"/+", "/", path.replace("\\", "/"))
    if len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text
