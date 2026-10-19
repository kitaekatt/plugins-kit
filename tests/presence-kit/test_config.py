"""presence_config: layers, merge rules, off/on, $comment keys and loud errors."""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "presence-kit" / "scripts"
SHIPPED = SCRIPTS.parent / "defaults" / "presence-kit.json"
sys.path.insert(0, str(SCRIPTS))

import presence_config as pc  # noqa: E402
from presence_config import BEHAVIOR_IDS, ConfigError  # noqa: E402

DEFAULT_VALUES = {"afk_quiet_minutes": 10, "departure_words": ["afk"], "waiter_model": "haiku"}


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text, encoding="utf-8")
    return path


def full_defaults(behaviors=None, values=None) -> dict:
    return {
        "behaviors": behaviors if behaviors is not None else {bid: True for bid in BEHAVIOR_IDS},
        "values": values if values is not None else dict(DEFAULT_VALUES),
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the home directory at a tmp dir and clear the config-dir override."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home_dir


@pytest.fixture
def defaults(tmp_path) -> Path:
    return write(tmp_path / "shipped" / "presence-kit.json", SHIPPED.read_text(encoding="utf-8"))


@pytest.fixture
def user_file(tmp_path) -> Path:
    return tmp_path / "cfg" / "config" / "presence-kit.json"


def env_for(tmp_path) -> dict:
    return {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}


# --- shipped defaults and layers -------------------------------------------------

def test_shipped_defaults_define_every_id_and_value():
    raw = json.loads(SHIPPED.read_text(encoding="utf-8"))
    assert list(raw["behaviors"]) == list(BEHAVIOR_IDS)
    assert all(v is True for v in raw["behaviors"].values())
    layer = pc.load_layer(SHIPPED, "defaults")
    assert layer["values"] == DEFAULT_VALUES


def test_defaults_only_resolves_with_all_layers_absent(tmp_path, defaults):
    cfg = pc.resolve(None, env=env_for(tmp_path), defaults_path=defaults)
    assert cfg.behaviors == {bid: True for bid in BEHAVIOR_IDS}
    assert cfg.values == DEFAULT_VALUES
    assert [(l.name, l.present) for l in cfg.layers] == [("defaults", True), ("user", False)]


def test_layer_paths_order_and_project_optional(tmp_path, home):
    names = [name for name, _ in pc.layer_paths(None, {})]
    assert names == ["defaults", "user"]
    proj = tmp_path / "proj"
    paths = pc.layer_paths(proj, {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")})
    assert [name for name, _ in paths] == ["defaults", "user", "project"]
    assert paths[1][1] == tmp_path / "cfg" / "config" / "presence-kit.json"
    assert paths[2][1] == proj / ".claude" / "presence-kit.json"


def test_user_layer_uses_claude_config_dir_not_home(tmp_path, home, defaults):
    write(home / ".claude" / "config" / "presence-kit.json", {"behaviors": {"recap-on-return": False}})
    write(tmp_path / "cfg" / "config" / "presence-kit.json", {"values": {"afk_quiet_minutes": 15}})
    cfg = pc.resolve(None, env=env_for(tmp_path), defaults_path=defaults)
    assert cfg.values["afk_quiet_minutes"] == 15
    assert cfg.enabled("recap-on-return") is True


def test_user_layer_defaults_to_home_claude(tmp_path, home, defaults):
    write(home / ".claude" / "config" / "presence-kit.json", {"behaviors": {"recap-on-return": False}})
    cfg = pc.resolve(None, env={}, defaults_path=defaults)
    assert cfg.enabled("recap-on-return") is False
    assert cfg.layers[1].path == home / ".claude" / "config" / "presence-kit.json"
    assert cfg.layers[1].present is True


def test_claude_config_dir_tilde_is_expanded(tmp_path, home, defaults):
    write(home / "cc" / "config" / "presence-kit.json", {"values": {"waiter_model": "sonnet"}})
    cfg = pc.resolve(None, env={"CLAUDE_CONFIG_DIR": "~/cc"}, defaults_path=defaults)
    assert cfg.values["waiter_model"] == "sonnet"
    assert cfg.layers[1].path == home / "cc" / "config" / "presence-kit.json"


def test_project_layer_overrides_user_and_is_reported(tmp_path, home, defaults):
    write(tmp_path / "cfg" / "config" / "presence-kit.json", {"values": {"afk_quiet_minutes": 15}})
    proj = tmp_path / "proj"
    write(proj / ".claude" / "presence-kit.json", {"values": {"afk_quiet_minutes": 20}})
    cfg = pc.resolve(proj, env=env_for(tmp_path), defaults_path=defaults)
    assert cfg.values["afk_quiet_minutes"] == 20
    assert [(l.name, l.present) for l in cfg.layers] == [
        ("defaults", True),
        ("user", True),
        ("project", True),
    ]


def test_project_layer_absent_without_project_dir(tmp_path, defaults):
    cfg = pc.resolve(None, env=env_for(tmp_path), defaults_path=defaults)
    assert "project" not in [l.name for l in cfg.layers]


def test_defaults_path_is_the_shipped_file_by_default(tmp_path, home):
    cfg = pc.resolve(None, env=env_for(tmp_path))
    assert cfg.layers[0].path == SHIPPED
    assert cfg.values == DEFAULT_VALUES


# --- merge rules -----------------------------------------------------------------

def test_objects_merge_key_by_key(tmp_path, home, defaults):
    write(tmp_path / "cfg" / "config" / "presence-kit.json", {"behaviors": {"recap-on-return": False}})
    proj = tmp_path / "proj"
    write(proj / ".claude" / "presence-kit.json", {"behaviors": {"ending-recap": False}})
    cfg = pc.resolve(proj, env=env_for(tmp_path), defaults_path=defaults)
    assert cfg.enabled("recap-on-return") is False
    assert cfg.enabled("ending-recap") is False
    others = [bid for bid in BEHAVIOR_IDS if bid not in ("recap-on-return", "ending-recap")]
    assert all(cfg.enabled(bid) for bid in others)
    assert set(cfg.behaviors) == set(BEHAVIOR_IDS)


def test_lists_replace_and_do_not_concatenate(tmp_path, home, defaults):
    write(tmp_path / "cfg" / "config" / "presence-kit.json", {"values": {"departure_words": ["afk", "leave"]}})
    proj = tmp_path / "proj"
    write(proj / ".claude" / "presence-kit.json", {"values": {"departure_words": ["quit"]}})
    cfg = pc.resolve(proj, env=env_for(tmp_path), defaults_path=defaults)
    assert cfg.values["departure_words"] == ["quit"]


def test_scalars_replace(tmp_path, home, defaults):
    write(tmp_path / "cfg" / "config" / "presence-kit.json", {"values": {"waiter_model": "sonnet"}})
    proj = tmp_path / "proj"
    write(proj / ".claude" / "presence-kit.json", {"values": {"waiter_model": "opus"}})
    cfg = pc.resolve(proj, env=env_for(tmp_path), defaults_path=defaults)
    assert cfg.values["waiter_model"] == "opus"


def test_to_json_shape(tmp_path, home, defaults):
    cfg = pc.resolve(None, env=env_for(tmp_path), defaults_path=defaults)
    obj = cfg.to_json()
    assert list(obj) == ["behaviors", "values", "layers"]
    assert list(obj["behaviors"]) == list(BEHAVIOR_IDS)
    assert obj["values"] == DEFAULT_VALUES
    assert obj["layers"][0] == {"name": "defaults", "path": str(defaults), "present": True}


def test_enabled_unknown_id_is_loud(tmp_path, home, defaults):
    cfg = pc.resolve(None, env=env_for(tmp_path), defaults_path=defaults)
    with pytest.raises(ValueError):
        cfg.enabled("no-such-behavior")


# --- off / on ----------------------------------------------------------------------

@pytest.mark.parametrize("off_value", [False, "off", " OFF ", "Off"])
def test_false_and_off_disable(tmp_path, home, defaults, off_value):
    write(tmp_path / "cfg" / "config" / "presence-kit.json", {"behaviors": {"waiting-guard": off_value}})
    cfg = pc.resolve(None, env=env_for(tmp_path), defaults_path=defaults)
    assert cfg.enabled("waiting-guard") is False


@pytest.mark.parametrize("on_value", [True, "on", " ON ", "On"])
def test_true_and_on_reenable_a_lower_layers_off(tmp_path, home, defaults, on_value):
    write(tmp_path / "cfg" / "config" / "presence-kit.json", {"behaviors": {"park-questions": "off"}})
    proj = tmp_path / "proj"
    write(proj / ".claude" / "presence-kit.json", {"behaviors": {"park-questions": on_value}})
    cfg = pc.resolve(proj, env=env_for(tmp_path), defaults_path=defaults)
    assert cfg.enabled("park-questions") is True


def test_project_off_beats_user_on(tmp_path, home, defaults):
    write(tmp_path / "cfg" / "config" / "presence-kit.json", {"behaviors": {"terse-while-away": "on"}})
    proj = tmp_path / "proj"
    write(proj / ".claude" / "presence-kit.json", {"behaviors": {"terse-while-away": "off"}})
    cfg = pc.resolve(proj, env=env_for(tmp_path), defaults_path=defaults)
    assert cfg.enabled("terse-while-away") is False


@pytest.mark.parametrize("bid", BEHAVIOR_IDS)
def test_each_id_can_be_switched_off_alone(tmp_path, home, defaults, bid):
    write(tmp_path / "cfg" / "config" / "presence-kit.json", {"behaviors": {bid: "off"}})
    cfg = pc.resolve(None, env=env_for(tmp_path), defaults_path=defaults)
    assert cfg.enabled(bid) is False
    assert all(cfg.enabled(other) for other in BEHAVIOR_IDS if other != bid)


def test_resolved_behaviors_always_hold_all_nine_as_bool(tmp_path, home, defaults):
    write(tmp_path / "cfg" / "config" / "presence-kit.json", {"behaviors": {"notify-for-input": "ON"}})
    cfg = pc.resolve(None, env=env_for(tmp_path), defaults_path=defaults)
    assert list(cfg.behaviors) == list(BEHAVIOR_IDS)
    assert all(type(v) is bool for v in cfg.behaviors.values())


# --- $comment keys -----------------------------------------------------------------

def test_comment_keys_ignored_at_every_level(tmp_path, home, defaults):
    write(
        tmp_path / "cfg" / "config" / "presence-kit.json",
        {
            "$comment": "top level note",
            "behaviors": {"$comment": "section note", "$comment-why": "x", "ending-recap": False},
            "values": {"$comment": "section note", "afk_quiet_minutes": 12},
        },
    )
    cfg = pc.resolve(None, env=env_for(tmp_path), defaults_path=defaults)
    assert cfg.enabled("ending-recap") is False
    assert cfg.values["afk_quiet_minutes"] == 12
    assert set(cfg.behaviors) == set(BEHAVIOR_IDS)


def test_comment_only_file_is_valid(tmp_path, home, defaults):
    write(tmp_path / "cfg" / "config" / "presence-kit.json", {"$comment": "nothing yet"})
    cfg = pc.resolve(None, env=env_for(tmp_path), defaults_path=defaults)
    assert cfg.values == DEFAULT_VALUES


# --- value validation (valid shapes) ----------------------------------------------

@pytest.mark.parametrize(
    "name,value",
    [
        ("afk_quiet_minutes", 1),
        ("departure_words", ["afk", "Leave_now", "quit2"]),
        ("waiter_model", "claude-haiku-5-5"),
    ],
)
def test_valid_values_are_accepted(user_file, name, value):
    write(user_file, {"values": {name: value}})
    layer = pc.load_layer(user_file, "user")
    assert layer["values"] == {name: value}


# --- loud errors: exact text, no fallback ----------------------------------------

def _error_for_user(user_file, content) -> ConfigError:
    write(user_file, content)
    with pytest.raises(ConfigError) as info:
        pc.load_layer(user_file, "user")
    return info.value


def test_unreadable_directory_at_file_path(user_file):
    user_file.mkdir(parents=True)
    with pytest.raises(ConfigError) as info:
        pc.load_layer(user_file, "user")
    err = info.value
    assert err.kind == "unreadable"
    assert err.text.startswith(f"{user_file}: cannot read -- ")


def test_invalid_utf8_is_unreadable(user_file):
    user_file.parent.mkdir(parents=True)
    user_file.write_bytes(b'{"values": {"waiter_model": "\xff\xfe"}}')
    with pytest.raises(ConfigError) as info:
        pc.load_layer(user_file, "user")
    assert info.value.kind == "unreadable"
    assert info.value.text.startswith(f"{user_file}: cannot read -- ")


@pytest.mark.parametrize("content", ["{not json", "", "   \n", "Feff{}"])
def test_malformed_json_including_empty_and_bom(user_file, content):
    err = _error_for_user(user_file, content)
    assert err.kind == "malformed-json"
    assert err.text.startswith(f"{user_file}: malformed JSON -- ")


@pytest.mark.parametrize(
    "content,type_name",
    [("[]", "array"), ('"x"', "string"), ("3", "number"), ("null", "null"), ("true", "boolean")],
)
def test_root_not_object(user_file, content, type_name):
    err = _error_for_user(user_file, content)
    assert err.kind == "root-not-object"
    assert err.text == f"{user_file}: config root must be a JSON object, got {type_name}"


def test_unknown_top_level_key(user_file):
    err = _error_for_user(user_file, {"foo": 1})
    assert err.kind == "unknown-key"
    assert err.key == "foo"
    assert err.text == f"{user_file}: unknown key 'foo'; allowed: behaviors, values"


@pytest.mark.parametrize(
    "content,key,type_name",
    [
        ({"behaviors": []}, "behaviors", "array"),
        ({"values": "x"}, "values", "string"),
        ({"values": None}, "values", "null"),
    ],
)
def test_section_not_object(user_file, content, key, type_name):
    err = _error_for_user(user_file, content)
    assert err.kind == "section-not-object"
    assert err.text == f"{user_file}: '{key}' must be an object, got {type_name}"


def test_unknown_behavior_id(user_file):
    err = _error_for_user(user_file, {"behaviors": {"nope": True}})
    assert err.kind == "unknown-behavior"
    assert err.key == "nope"
    assert err.text == (
        f"{user_file}: unknown behavior id 'nope'; valid ids: {', '.join(BEHAVIOR_IDS)}"
    )


@pytest.mark.parametrize("bad", [1, 0, "yes", "onn", "", None, [], {}, 1.0])
def test_bad_behavior_value(user_file, bad):
    err = _error_for_user(user_file, {"behaviors": {"recap-on-return": bad}})
    assert err.kind == "bad-behavior-value"
    assert err.key == "recap-on-return"
    assert err.text == (
        f"{user_file}: behavior 'recap-on-return' accepts only true, false, "
        f'"on" or "off"; got {json.dumps(bad)}'
    )


def test_unknown_value_name(user_file):
    err = _error_for_user(user_file, {"values": {"nope": 1}})
    assert err.kind == "unknown-value"
    assert err.text == (
        f"{user_file}: unknown value 'nope'; valid names: afk_quiet_minutes, departure_words, waiter_model"
    )


@pytest.mark.parametrize("bad", [0, -1, 1.5, "15", True, None])
def test_afk_quiet_minutes_must_be_positive_int(user_file, bad):
    err = _error_for_user(user_file, {"values": {"afk_quiet_minutes": bad}})
    assert err.kind == "bad-value-type"
    assert err.text == (
        f"{user_file}: value 'afk_quiet_minutes' must be a positive integer; got {json.dumps(bad)}"
    )


@pytest.mark.parametrize("bad", [[], "afk", ["a b"], [""], [1], ["ok", "bad-word"], ["x-y"]])
def test_departure_words_must_be_non_empty_word_list(user_file, bad):
    err = _error_for_user(user_file, {"values": {"departure_words": bad}})
    assert err.kind == "bad-value-type"
    assert err.text == (
        f"{user_file}: value 'departure_words' must be a non-empty list of words "
        f"(letters, digits, underscore); got {json.dumps(bad)}"
    )


@pytest.mark.parametrize("bad", ["", "gpt 4", 5, None, ["haiku"]])
def test_waiter_model_must_be_name_without_spaces(user_file, bad):
    err = _error_for_user(user_file, {"values": {"waiter_model": bad}})
    assert err.kind == "bad-value-type"
    assert err.text == (
        f"{user_file}: value 'waiter_model' must be a non-empty model name without spaces; "
        f"got {json.dumps(bad)}"
    )


def test_defaults_missing_a_behavior_is_loud(tmp_path):
    behaviors = {bid: True for bid in BEHAVIOR_IDS if bid != "ending-recap"}
    path = write(tmp_path / "shipped.json", full_defaults(behaviors=behaviors))
    with pytest.raises(ConfigError) as info:
        pc.load_layer(path, "defaults")
    assert info.value.kind == "defaults-incomplete"
    assert info.value.text == f"{path}: shipped defaults must define behavior 'ending-recap'"


def test_defaults_missing_a_value_is_loud(tmp_path):
    values = {"afk_quiet_minutes": 10, "departure_words": ["afk"]}
    path = write(tmp_path / "shipped.json", full_defaults(values=values))
    with pytest.raises(ConfigError) as info:
        pc.load_layer(path, "defaults")
    assert info.value.kind == "defaults-incomplete"
    assert info.value.text == f"{path}: shipped defaults must define value 'waiter_model'"


def test_absent_defaults_file_is_loud(tmp_path):
    path = tmp_path / "missing" / "presence-kit.json"
    with pytest.raises(ConfigError) as info:
        pc.load_layer(path, "defaults")
    assert info.value.kind == "defaults-incomplete"


def test_partial_user_layer_is_fine(user_file):
    write(user_file, {"behaviors": {"recap-on-return": False}})
    layer = pc.load_layer(user_file, "user")
    assert layer == {"behaviors": {"recap-on-return": False}, "values": {}}


def test_absent_user_layer_is_empty(user_file):
    assert pc.load_layer(user_file, "user") == {}


def test_broken_user_file_raises_from_resolve_and_never_falls_back(tmp_path, home, defaults):
    user = tmp_path / "cfg" / "config" / "presence-kit.json"
    write(user, "{broken")
    with pytest.raises(ConfigError) as info:
        pc.resolve(None, env=env_for(tmp_path), defaults_path=defaults)
    assert info.value.kind == "malformed-json"
    assert info.value.path == str(user)
    assert info.value.text.startswith(f"{user}: malformed JSON -- ")


def test_broken_project_file_raises_even_with_good_user_file(tmp_path, home, defaults):
    write(tmp_path / "cfg" / "config" / "presence-kit.json", {"values": {"afk_quiet_minutes": 15}})
    proj = tmp_path / "proj"
    write(proj / ".claude" / "presence-kit.json", {"values": {"afk_quiet_minutes": 0}})
    with pytest.raises(ConfigError) as info:
        pc.resolve(proj, env=env_for(tmp_path), defaults_path=defaults)
    assert info.value.kind == "bad-value-type"
    assert info.value.key == "afk_quiet_minutes"
    assert info.value.text.startswith(f"{proj / '.claude' / 'presence-kit.json'}: ")


def test_config_error_attributes(user_file):
    err = _error_for_user(user_file, {"values": {"nope": 1}})
    assert err.kind == "unknown-value"
    assert err.path == str(user_file)
    assert err.key == "nope"
    assert err.text == str(err)


# --- departure pattern --------------------------------------------------------------

def test_departure_pattern_whole_word_any_case():
    pattern = pc.departure_pattern(["afk"])
    assert pattern.search("I am AFK now").group(0) == "AFK"
    assert pattern.search("going afk, back later") is not None
    assert pattern.search("afkward silence") is None
    assert pattern.search("unafk") is None


def test_departure_pattern_several_words():
    pattern = pc.departure_pattern(["afk", "leaving"])
    assert pattern.search("I'm leaving soon").group(0) == "leaving"
    assert pattern.search("keep going") is None


def test_departure_pattern_rejects_empty_list():
    with pytest.raises(ValueError):
        pc.departure_pattern([])


# --- message texts -------------------------------------------------------------------

def test_line_wraps_ids_pid_and_configuring_reference():
    assert pc.line("afk-on-quiet,recap-on-return", "p1", "x") == (
        "presence-kit [afk-on-quiet,recap-on-return] prompt p1: x See references/configuring.md."
    )


def test_config_error_message_through_line():
    text = pc.P4_CONFIG_SYSTEM.format(err_text="/a/b.json: malformed JSON -- x")
    assert pc.line(pc.CONFIG_IDS, "p9", text) == (
        "presence-kit [config] prompt p9: /a/b.json: malformed JSON -- x "
        "Presence behavior is unavailable until the file is fixed. See references/configuring.md."
    )
    assert pc.line(pc.CONFIG_IDS, "p9", pc.P5_CONFIG_CONTEXT) == (
        "presence-kit [config] prompt p9: presence behavior is unavailable because its "
        "configuration is invalid; apply no presence-kit rule this turn. "
        "See references/configuring.md."
    )


def test_section_seven_texts_are_exact():
    assert pc.P1_AWAY.format(since="2026-10-17T09:00:00Z", goal_part=" [goal: ship it]") == (
        "the user counts as away since 2026-10-17T09:00:00Z [goal: ship it]. "
        "Apply the away behaviors in references/presence.md that are on."
    )
    assert pc.P3_DEPARTURE.format(word="afk") == (
        'the prompt contains the departure word "afk"; if this is the user saying they are '
        "leaving, invoke `presence-kit:afk`."
    )
    assert pc.R_RETURN.format(pid="p2") == "the user is back; invoke `presence-kit:recap return p2`."
    assert pc.E_AFK.format(pid="p3") == "invoke `presence-kit:recap afk-ending p3`."
    assert pc.S6_UNRESOLVED.format(label=pc.S6_LABELS["W"]) == (
        "unresolved after 3 nudges: turn did not end with `Waiting on <what>`."
    )
    assert pc.N5_CRONS_MALFORMED == (
        "skipped owned-work checks: the Stop payload's session_crons is not a list."
    )
