"""presence_config -- presence-kit configuration layers, validation and hook message texts.

Leaf module, stdlib only. Layers, lowest first (an absent file is skipped, not an
error): defaults (plugin defaults/presence-kit.json), user (<config dir>/config/
presence-kit.json; <config dir> is $CLAUDE_CONFIG_DIR, else ~/.claude), project
(<project>/.claude/presence-kit.json). Each file is validated alone, then merged:
objects key by key, later wins; lists and scalars replace. Any error raises
ConfigError; nothing falls back to defaults. The one exception: an unknown behavior
id in the user or project layer is ignored and recorded (`Config.ignored`), because
a file written for another version of the plugin must not switch presence off; the
shipped defaults layer stays strict.

The section-7 message texts live here too. Templates keep their {placeholders};
callers format them and wrap every emitted hook line with line().
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BEHAVIOR_IDS: tuple[str, ...] = (
    "afk-on-departure",
    "afk-on-quiet",
    "autonomous-on-handover",
    "recap-on-return",
    "waiting-guard",
    "liveness-watch",
    "ending-recap",
    "terse-while-away",
    "park-questions",
    "notify-for-input",
)
VALUE_NAMES: tuple[str, ...] = (
    "afk_quiet_minutes",
    "departure_words",
    "waiter_model",
    "progress_minutes",
    "watch_after_minutes",
)

_TOP_KEYS = ("behaviors", "values")
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_DEFAULTS_FILE = _PLUGIN_DIR / "defaults" / "presence-kit.json"
_FILE_NAME = "presence-kit.json"

_VALUE_EXPECT = {
    "afk_quiet_minutes": "a positive integer",
    "departure_words": "a non-empty list of words (letters, digits, underscore)",
    "waiter_model": "a non-empty model name without spaces",
    "progress_minutes": "a non-negative integer (0 turns PROGRESS lines off)",
    "watch_after_minutes": "a non-negative integer (0 asks for a liveness watch on every job at once)",
}


class ConfigError(Exception):
    """A loud configuration error. `text` is '{path}: {detail}'."""

    def __init__(self, kind: str, path: Path | str, key: str | None, detail: str) -> None:
        self.kind = kind
        self.path = str(path)
        self.key = key
        self.text = f"{path}: {detail}"
        super().__init__(self.text)


@dataclass(frozen=True)
class Layer:
    name: str
    path: Path
    present: bool


@dataclass(frozen=True)
class IgnoredKey:
    """A behavior id a user or project layer set that this plugin does not know."""

    layer: str
    path: Path
    key: str


@dataclass(frozen=True)
class Config:
    behaviors: dict[str, bool]
    values: dict[str, Any]
    layers: tuple[Layer, ...]
    ignored: tuple[IgnoredKey, ...] = ()

    def enabled(self, behavior_id: str) -> bool:
        if behavior_id not in self.behaviors:
            raise ValueError(f"unknown behavior id '{behavior_id}'")
        return self.behaviors[behavior_id]

    def to_json(self) -> dict[str, Any]:
        return {
            "behaviors": dict(self.behaviors),
            "values": dict(self.values),
            "layers": [
                {"name": layer.name, "path": str(layer.path), "present": layer.present}
                for layer in self.layers
            ],
            "ignored": [
                {"layer": item.layer, "path": str(item.path), "id": item.key}
                for item in self.ignored
            ],
        }


def _config_dir(env: Mapping[str, str]) -> Path:
    raw = env.get("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".claude"


def layer_paths(project_dir: Path | None, env: Mapping[str, str]) -> list[tuple[str, Path]]:
    """The layers in merge order (lowest first). The project layer exists only with a project dir."""
    paths = [
        ("defaults", _DEFAULTS_FILE),
        ("user", _config_dir(env) / "config" / _FILE_NAME),
    ]
    if project_dir is not None:
        paths.append(("project", Path(project_dir) / ".claude" / _FILE_NAME))
    return paths


def _is_comment(key: Any) -> bool:
    return isinstance(key, str) and key.startswith("$comment")


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _check_behavior(path: Path, key: str, value: Any) -> bool:
    """The on/off value of a KNOWN behavior id (the caller has screened unknown ids)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("on", "off"):
        return value.strip().lower() == "on"
    raise ConfigError(
        "bad-behavior-value",
        path,
        key,
        f"behavior '{key}' accepts only true, false, \"on\" or \"off\"; got {json.dumps(value)}",
    )


def _check_value(path: Path, key: str, value: Any) -> Any:
    if key not in VALUE_NAMES:
        raise ConfigError(
            "unknown-value",
            path,
            key,
            f"unknown value '{key}'; valid names: {', '.join(VALUE_NAMES)}",
        )
    if key in ("afk_quiet_minutes", "progress_minutes", "watch_after_minutes"):
        floor = 1 if key == "afk_quiet_minutes" else 0
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= floor
    elif key == "departure_words":
        ok = (
            isinstance(value, list)
            and len(value) > 0
            and all(isinstance(w, str) and re.fullmatch(r"\w+", w) for w in value)
        )
    else:  # waiter_model
        ok = isinstance(value, str) and len(value) > 0 and re.search(r"\s", value) is None
    if not ok:
        raise ConfigError(
            "bad-value-type",
            path,
            key,
            f"value '{key}' must be {_VALUE_EXPECT[key]}; got {json.dumps(value)}",
        )
    return value


def _defaults_incomplete(path: Path, behaviors: dict, values: dict) -> None:
    for bid in BEHAVIOR_IDS:
        if bid not in behaviors:
            raise ConfigError(
                "defaults-incomplete",
                path,
                bid,
                f"shipped defaults must define behavior '{bid}'",
            )
    for name in VALUE_NAMES:
        if name not in values:
            raise ConfigError(
                "defaults-incomplete",
                path,
                name,
                f"shipped defaults must define value '{name}'",
            )


def load_layer(path: Path, name: str) -> dict:
    """Validate one layer file. Returns {"behaviors": {id: bool}, "values": {...},
    "ignored": [id, ...]} with only the keys the file sets; {} when the file is absent
    (the defaults layer must exist). An unknown behavior id is an error in the defaults
    layer and is listed in "ignored" (its value unread) in any other layer."""
    path = Path(path)
    if not path.exists():
        if name == "defaults":
            _defaults_incomplete(path, {}, {})
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError("unreadable", path, None, f"cannot read -- {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError("malformed-json", path, None, f"malformed JSON -- {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(
            "root-not-object",
            path,
            None,
            f"config root must be a JSON object, got {_json_type(data)}",
        )

    behaviors: dict[str, bool] = {}
    values: dict[str, Any] = {}
    ignored: list[str] = []
    for key, section in data.items():
        if _is_comment(key):
            continue
        if key not in _TOP_KEYS:
            raise ConfigError(
                "unknown-key",
                path,
                key,
                f"unknown key '{key}'; allowed: behaviors, values",
            )
        if not isinstance(section, dict):
            raise ConfigError(
                "section-not-object",
                path,
                key,
                f"'{key}' must be an object, got {_json_type(section)}",
            )
        for item_key, item_value in section.items():
            if _is_comment(item_key):
                continue
            if key == "behaviors":
                if item_key not in BEHAVIOR_IDS:
                    if name == "defaults":
                        raise ConfigError(
                            "unknown-behavior",
                            path,
                            item_key,
                            f"unknown behavior id '{item_key}'; valid ids: {', '.join(BEHAVIOR_IDS)}",
                        )
                    ignored.append(item_key)
                    continue
                behaviors[item_key] = _check_behavior(path, item_key, item_value)
            else:
                values[item_key] = _check_value(path, item_key, item_value)

    if name == "defaults":
        _defaults_incomplete(path, behaviors, values)
    return {"behaviors": behaviors, "values": values, "ignored": ignored}


def resolve(
    project_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    defaults_path: Path | None = None,
) -> Config:
    """Merge every present layer. Raises ConfigError on the first invalid file."""
    env = os.environ if env is None else env
    paths = layer_paths(project_dir, env)
    if defaults_path is not None:
        paths[0] = ("defaults", Path(defaults_path))
    behaviors: dict[str, bool] = {}
    values: dict[str, Any] = {}
    layers: list[Layer] = []
    ignored: list[IgnoredKey] = []
    for name, path in paths:
        layer = load_layer(path, name)
        layers.append(Layer(name, path, path.exists()))
        behaviors.update(layer.get("behaviors", {}))
        values.update(layer.get("values", {}))
        ignored.extend(IgnoredKey(name, path, key) for key in layer.get("ignored", []))
    return Config(
        behaviors=behaviors, values=values, layers=tuple(layers), ignored=tuple(ignored)
    )


# A departure word is not adjacent to a word character, `-`, `:`, `/` or a backtick:
# that keeps `afk-on-departure`, `presence-kit:afk`, `/presence-kit:recap afk-ending`,
# `afk_since` and a backticked `afk` from matching while "AFK." and "(afk)" still do.
_DEPARTURE_EDGE = r"\w\-:/`"


def departure_pattern(words: Sequence[str]) -> re.Pattern:
    """Case-insensitive match for any departure word standing alone (see _DEPARTURE_EDGE)."""
    if not words:
        raise ValueError("departure_pattern needs at least one word")
    alternation = "|".join(re.escape(w) for w in words)
    return re.compile(
        rf"(?<![{_DEPARTURE_EDGE}])(?:{alternation})(?![{_DEPARTURE_EDGE}])", re.IGNORECASE
    )


def ignored_notice(cfg: Config) -> str | None:
    """The text of N6 (the ids the user and project layers set that this plugin does not
    know), or None when nothing was ignored."""
    if not cfg.ignored:
        return None
    items = "; ".join(f"'{item.key}' in {item.path}" for item in cfg.ignored)
    noun = "id" if len(cfg.ignored) == 1 else "ids"
    return N6_IGNORED.format(noun=noun, items=items, valid=", ".join(BEHAVIOR_IDS))


def line(ids: str, pid: str, text: str) -> str:
    """Every hook line: names its behavior ids, configuring.md and the prompt id."""
    return f"presence-kit [{ids}] prompt {pid}: {text} See references/configuring.md."


# Section 7 message texts (exact). Placeholders are formatted by the caller.
P1_AWAY_IDS = "terse-while-away,park-questions,notify-for-input"
P1_AWAY = (
    "the user counts as away since {since}{goal_part}. "
    "Apply the away behaviors in references/presence.md that are on."
)
P2_RECAP_IDS = "recap-on-return"
P2_RECAP = (
    "if this prompt is the user's own message, not a scheduled task, agent report, "
    "task notification or another session's message, invoke `presence-kit:recap return {pid}` "
    "before answering."
)
P3_DEPARTURE_IDS = "afk-on-departure"
# {undo} is P3_UNDO formatted with the session id; Claude runs it to correct a wrong match.
P3_UNDO = (
    '"${{BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}}" '
    '"${{PRESENCE_KIT_ROOT:?presence-kit is not provisioned}}/scripts/presence.py" '
    "state set present --session {sid}"
)
_P3_WRONG = (
    "If that is wrong (the word is quoted or discussed, or the prompt is not the user's own "
    "message), run `{undo}` and tell the user once in one line"
)
P3_DEPARTURE = (
    'set the user afk because the prompt contains the departure word "{word}". '
    + _P3_WRONG
    + "; otherwise invoke `presence-kit:afk` to cover running work."
)
# Already afk (a repeated departure keeps the user afk). The return clause is added only
# with recap-on-return on, because the hook withholds P2 on this prompt.
P3_DEPARTURE_KEPT = (
    'kept the user afk because the prompt contains the departure word "{word}". '
    + _P3_WRONG
    + "{return_clause}; otherwise invoke `presence-kit:afk` to cover running work."
)
P3_KEPT_RETURN_CLAUSE = (
    " (if it was the user's own message, they are back: also invoke "
    "`presence-kit:recap return {pid}`)"
)
CONFIG_IDS = "config"
P4_CONFIG_SYSTEM = "{err_text} Presence behavior is unavailable until the file is fixed."
P5_CONFIG_CONTEXT = (
    "presence behavior is unavailable because its configuration is invalid; "
    "apply no presence-kit rule this turn."
)
S1_CONFIG_SYSTEM = P4_CONFIG_SYSTEM
R_IDS = "recap-on-return"
R_RETURN = "the user is back; invoke `presence-kit:recap return {pid}`."
C_IDS = "waiting-guard"
# {counted}: the owned work the Stop hook counted, built by presence_stop.counted_work
# (one `<id> (<type>, <status>): <description>` per task), so a false positive is visible.
C_WAITER = "add a waiter agent; the terminal shows idle. Counted as running non-agent work: {counted}."
M_IDS = "liveness-watch"
# {gaps}: built by presence_stop.watch_gaps_text from the NOWATCH and REARM parts.
M_WATCH = (
    "arm a liveness watch: a Monitor on `watch.py --task <id>` for each job you own "
    "(it reports a stall and the job's end). {gaps}"
)
M_NOWATCH = "No liveness watch on: {tasks}."
M_REARM = "Liveness watch ended while the job still runs, re-arm it on: {tasks}."
E_IDS = "ending-recap"
E_AUTONOMOUS = (
    "autonomous: if {goal} is done, invoke `presence-kit:recap autonomous-done {pid}`, "
    "else keep working."
)
E_AFK = "invoke `presence-kit:recap afk-ending {pid}`."
N_QUIET_RETURN_IDS = "afk-on-quiet,recap-on-return"
N1_ENTRY_MISSING = (
    "skipped quiet-afk and return detection: the transcript has no user entry for this prompt yet."
)
N2_ENTRY_MALFORMED = (
    "skipped quiet-afk and return detection: "
    "the prompt's transcript entry has no valid timestamp or turnOrigin."
)
N3_ORIGIN_UNKNOWN = (
    "skipped quiet-afk and return detection: the prompt's turnOrigin is not a known value."
)
N_CHECKS_IDS = "waiting-guard,ending-recap"
N4_BACKGROUND_ABSENT = (
    "skipped waiting and ending checks: the Stop payload's background_tasks is missing."
)
N4_BACKGROUND_MALFORMED = (
    "skipped waiting and ending checks: "
    "the Stop payload's background_tasks is not a list of objects."
)
N5_CRONS_ABSENT = "skipped owned-work checks: the Stop payload's session_crons is missing."
N5_CRONS_MALFORMED = "skipped owned-work checks: the Stop payload's session_crons is not a list."
# N6: said once per session, by whichever hook runs first. Its state notice key is
# N6_CAUSE (the strict v3 state schema takes any notice string).
N6_CAUSE = "ignored-behavior-ids"
N6_IGNORED = "ignored unknown behavior {noun} {items}; valid ids: {valid}."
S6_UNRESOLVED = "unresolved after 3 nudges: {label}."
S6_LABELS = {
    "R": "return recap not run",
    "C": "no waiter agent covering the job",
    "M": "no running liveness watch on an owned job",
    "E": "no ending recap or goal check",
}
