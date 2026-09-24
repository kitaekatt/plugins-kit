"""Tests for bootstrap_lib.code_review.review_profiles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from bootstrap_lib.code_review import lane_prompts as lp
from bootstrap_lib.code_review import review_profiles as rp


FIXTURE = Path(__file__).with_name("shipped_review_profiles.yaml")


def _write_yaml(path: Path, value: dict[str, Any]) -> None:
    """Write a test layer and create only its isolated parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(value, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def _layers(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return isolated home, project, and project-config paths."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project_config = project / ".claude" / rp.CONFIG_NAME
    return home, project, project_config


def _resolved(
    tmp_path: Path,
    *,
    user: dict[str, Any] | None = None,
    project: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve test layers without consulting the real home directory."""
    home, project_root, project_path = _layers(tmp_path)
    if user is not None:
        _write_yaml(home / ".claude" / "config" / rp.CONFIG_NAME, user)
    if project is not None:
        _write_yaml(project_path, project)
    config, _provenance = rp.resolve_config(project_root, home=home)
    return config


def _profile(config: dict[str, Any], profile_id: str) -> dict[str, Any]:
    """Find one resolved profile by id."""
    return next(profile for profile in config["profiles"] if profile["id"] == profile_id)


def test_shipped_only_render_matches_pre_seam_bytes(tmp_path: Path) -> None:
    """The shipped executable projection is pinned byte-for-byte.

    The shipped `[sol, opus]` declaration for reviewer C resolves to `sol`
    with `opus` left in `model_fallbacks`. The review skill joins the two back
    into the declaration it hands to `describe`.
    """
    home, project_root, _project_path = _layers(tmp_path)
    config, provenance = rp.resolve_config(project_root, home=home)
    config = rp.apply_model_priority(config)

    assert provenance[0][0:3:2] == ("shipped", "applied")
    assert all(layer != "user" or status == "absent" for layer, _path, status in provenance)
    assert all(layer != "project" or status == "absent" for layer, _path, status in provenance)
    assert rp.render_projection(config).encode("utf-8") == FIXTURE.read_bytes()


def test_patch_merges_profile_reviewer_and_validator_in_place(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {"name": "reviewer_b_diff_only_bugs", "model": "sonnet"}
                    ],
                    "validator_models": {"bug": "sonnet"},
                }
            ]
        },
    )

    assert [profile["id"] for profile in config["profiles"]] == ["data_only", "code"]
    code = _profile(config, "code")
    assert [reviewer["name"] for reviewer in code["reviewers"]] == [
        "reviewer_a_claude_md_compliance",
        "reviewer_b_diff_only_bugs",
        "reviewer_c_introduced_code",
    ]
    assert code["reviewers"][1]["model"] == "sonnet"
    assert code["validator_models"] == {"bug": "sonnet", "claude_md": "sonnet"}


def test_unknown_profiles_and_validator_reasons_append(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "data_only",
                    "validator_models": {"security": "sonnet"},
                },
                {
                    "id": "security",
                    "selection": {},
                    "reviewers": [
                        {"name": "reviewer_b_diff_only_bugs", "model": "opus"}
                    ],
                    "validator_models": {"bug": "opus", "claude_md": "sonnet"},
                },
            ]
        },
    )

    assert [profile["id"] for profile in config["profiles"]] == [
        "data_only",
        "code",
        "security",
    ]
    data_only = _profile(config, "data_only")
    assert data_only["validator_models"] == {
        "bug": "sonnet",
        "claude_md": "sonnet",
        "security": "sonnet",
    }
    assert list(data_only["validator_models"]) == ["bug", "claude_md", "security"]


def test_unknown_reviewer_name_is_rejected_with_supported_lanes(tmp_path: Path) -> None:
    with pytest.raises(rp.ConfigError, match="reviewer_security") as excinfo:
        _resolved(
            tmp_path,
            user={
                "profiles": [
                    {
                        "id": "data_only",
                        "reviewers": [
                            {"name": "reviewer_security", "model": "sonnet"}
                        ],
                    }
                ]
            },
        )

    supported = sorted(lp.KNOWN_LANES - {"validator"})
    assert str(supported) in str(excinfo.value)


def test_all_profiles_disabled_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(rp.ConfigError, match="at least one active review profile"):
        _resolved(
            tmp_path,
            user={
                "profiles": [
                    {"id": "data_only", "disabled": True},
                    {"id": "code", "disabled": True},
                ]
            },
        )


def test_profile_with_no_active_reviewers_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(rp.ConfigError, match="profile 'code'.*reviewer"):
        _resolved(
            tmp_path,
            user={
                "profiles": [
                    {
                        "id": "code",
                        "reviewers": [
                            {
                                "name": name,
                                "disabled": True,
                            }
                            for name in (
                                "reviewer_a_claude_md_compliance",
                                "reviewer_b_diff_only_bugs",
                                "reviewer_c_introduced_code",
                            )
                        ],
                    }
                ]
            },
        )
def test_disabled_profile_and_reviewer_are_removed(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {"id": "code", "disabled": True},
                {
                    "id": "data_only",
                    "reviewers": [
                        {
                            "name": "reviewer_b_diff_only_bugs",
                            "disabled": True,
                        }
                    ],
                },
            ]
        },
    )

    assert [profile["id"] for profile in config["profiles"]] == ["data_only"]
    assert [reviewer["name"] for reviewer in config["profiles"][0]["reviewers"]] == [
        "reviewer_a_claude_md_compliance"
    ]


def test_plain_extension_list_replaces_instead_of_merging(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "data_only",
                    "selection": {"data_only_extensions": [".toml", ".ini"]},
                }
            ]
        },
    )

    assert _profile(config, "data_only")["selection"] == {
        "data_only_extensions": [".toml", ".ini"]
    }


def test_project_layer_has_highest_precedence(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {"name": "reviewer_b_diff_only_bugs", "model": "sonnet"}
                    ],
                }
            ]
        },
        project={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {"name": "reviewer_b_diff_only_bugs", "model": "opus"}
                    ],
                }
            ]
        },
    )

    assert _profile(config, "code")["reviewers"][1]["model"] == "opus"


@pytest.mark.parametrize(
    ("label", "layer"),
    [
        ("unknown field", {"unexpected": True}),
        (
            "missing profile fields",
            {"profiles": [{"id": "new_profile"}]},
        ),
        (
            "duplicate profile ids",
            {
                "profiles": [
                    {"id": "new_profile", "selection": {}, "reviewers": [], "validator_models": {}},
                    {"id": "new_profile", "selection": {}, "reviewers": [], "validator_models": {}},
                ]
            },
        ),
        (
            "duplicate reviewer names",
            {
                "profiles": [
                    {
                        "id": "new_profile",
                        "selection": {},
                        "reviewers": [
                            {"name": "same", "model": "sonnet"},
                            {"name": "same", "model": "opus"},
                        ],
                        "validator_models": {},
                    }
                ]
            },
        ),
        (
            "empty reviewer model",
            {
                "profiles": [
                    {
                        "id": "code",
                        "reviewers": [{"name": "reviewer_b_diff_only_bugs", "model": ""}],
                    }
                ]
            },
        ),
        (
            "empty reviewer name",
            {
                "profiles": [
                    {
                        "id": "code",
                        "reviewers": [{"name": "  ", "model": "sonnet"}],
                    }
                ]
            },
        ),
    ],
)
def test_invalid_configuration_exits_nonzero(
    tmp_path: Path,
    label: str,
    layer: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI rejects invalid layers before emitting a table."""
    _home, project_root, project_path = _layers(tmp_path)
    _write_yaml(project_path, layer)

    assert rp.main(["--project-root", str(project_root), "--home", str(_home)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "", label
    assert "review profiles config error:" in captured.err
    assert str(project_path) in captured.err


def test_cli_prints_yaml_once_and_provenance_for_shipped_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home, project_root, _project_path = _layers(tmp_path)

    assert rp.main(["--project-root", str(project_root), "--home", str(home)]) == 0
    output = capsys.readouterr().out
    assert output.count("profiles:\n") == 1
    assert "Layers applied: shipped." in output
    assert "To change this policy, create: user (" in output
    assert "project (" in output
    assert "description:" not in output
    assert "guidance:" not in output
    assert "rationale:" not in output


# --------------------------------------------------------------------------
# model priority lists
# --------------------------------------------------------------------------


def _reviewer(config: dict[str, Any], profile_id: str, name: str) -> dict[str, Any]:
    return next(
        reviewer
        for reviewer in _profile(config, profile_id)["reviewers"]
        if reviewer["name"] == name
    )


def test_shipped_default_gives_reviewer_c_a_sol_opus_declaration(tmp_path: Path) -> None:
    """Migration step 5: `[peer:opus, opus]` became `[sol, opus]` (design D1)."""
    config = _resolved(tmp_path)

    assert _reviewer(config, "code", "reviewer_c_introduced_code")["model"] == [
        "sol",
        "opus",
    ]
    listed = [
        (profile["id"], reviewer["name"])
        for profile in config["profiles"]
        for reviewer in profile["reviewers"]
        if not isinstance(reviewer["model"], str)
    ]
    assert listed == [("code", "reviewer_c_introduced_code")]


def test_a_string_model_is_unchanged(tmp_path: Path) -> None:
    """A plain string keeps its meaning: a one-entry list that resolves to itself."""
    config = rp.apply_model_priority(
        _resolved(
            tmp_path,
            user={
                "profiles": [
                    {
                        "id": "code",
                        "reviewers": [
                            {"name": "reviewer_c_introduced_code", "model": "sonnet"}
                        ],
                    }
                ]
            },
        )
    )

    code = _profile(config, "code")
    assert [reviewer["model"] for reviewer in code["reviewers"]] == [
        "sonnet",
        "opus",
        "sonnet",
    ]


def test_peer_prefixed_entry_is_no_longer_rewritten(tmp_path: Path) -> None:
    """Migration step 12 (direction 12): `peer:` is dropped.

    An entry spelled `peer:<name>` is now an ordinary, unresolved id -- no
    seat discovery runs, and it is chosen as the lane's literal model exactly
    like any other entry. (Whether an unresolvable id like this is later
    skipped is a DISPATCH-time concern, direction 13, out of scope here.)
    """
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {
                            "name": "reviewer_c_introduced_code",
                            "model": ["peer:opus", "opus"],
                        }
                    ],
                }
            ]
        },
    )

    resolved = rp.apply_model_priority(config)

    reviewer = _reviewer(resolved, "code", "reviewer_c_introduced_code")
    assert reviewer["model"] == "peer:opus"
    assert reviewer["model_fallbacks"] == ["opus"]


def test_a_user_model_replaces_the_shipped_list_wholesale(tmp_path: Path) -> None:
    """The defect this field shape fixes: `model: fable` means fable."""
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {"name": "reviewer_c_introduced_code", "model": "fable"}
                    ],
                }
            ]
        },
    )
    assert _reviewer(config, "code", "reviewer_c_introduced_code")["model"] == "fable"

    resolved = rp.apply_model_priority(config)

    assert _reviewer(resolved, "code", "reviewer_c_introduced_code")["model"] == "fable"


def test_a_user_list_replaces_the_shipped_list_element_for_element(
    tmp_path: Path,
) -> None:
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {
                            "name": "reviewer_c_introduced_code",
                            "model": ["luna", "sonnet"],
                        }
                    ],
                }
            ]
        },
    )

    assert _reviewer(config, "code", "reviewer_c_introduced_code")["model"] == [
        "luna",
        "sonnet",
    ]


def _with_model(tmp_path: Path, model: Any) -> dict[str, Any]:
    """Resolve a table whose reviewer_c lane states exactly ``model``."""
    return _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {"name": "reviewer_c_introduced_code", "model": model}
                    ],
                }
            ]
        },
    )


def test_a_single_string_model_has_an_empty_fallback_chain(tmp_path: Path) -> None:
    """One entry is a chain of one, and "nothing left" is stated, not absent."""
    resolved = rp.apply_model_priority(_with_model(tmp_path, "sonnet"))

    reviewer = _reviewer(resolved, "code", "reviewer_c_introduced_code")
    assert reviewer["model"] == "sonnet"
    assert reviewer["model_fallbacks"] == []


def test_the_entries_after_the_chosen_one_become_the_fallback_chain(
    tmp_path: Path,
) -> None:
    """`[luna, sonnet]` runs on luna and keeps sonnet for a failed dispatch."""
    resolved = rp.apply_model_priority(_with_model(tmp_path, ["luna", "sonnet"]))

    reviewer = _reviewer(resolved, "code", "reviewer_c_introduced_code")
    assert reviewer["model"] == "luna"
    assert reviewer["model_fallbacks"] == ["sonnet"]


def test_the_rendered_table_carries_the_fallback_chain(tmp_path: Path) -> None:
    """The agent reading the table can see what the lane may fall over to."""
    resolved = rp.apply_model_priority(_with_model(tmp_path, ["luna", "sonnet"]))
    rendered = rp.render_projection(resolved)

    assert "    model: luna\n    model_fallbacks:\n    - sonnet\n" in rendered
    table = yaml.safe_load(rendered)
    code = next(p for p in table["profiles"] if p["id"] == "code")
    assert code["reviewers"][2] == {
        "name": "reviewer_c_introduced_code",
        "model": "luna",
        "model_fallbacks": ["sonnet"],
    }


def test_a_resolved_table_carrying_a_chain_still_validates(tmp_path: Path) -> None:
    """The derived field is a known field, not a typo the schema rejects."""
    resolved = rp.apply_model_priority(_with_model(tmp_path, ["luna", "sonnet"]))

    rp.validate_config(resolved)


@pytest.mark.parametrize(
    ("label", "fallbacks"),
    [
        ("non-list", "sonnet"),
        ("non-string entry", ["sonnet", 7]),
        ("empty entry", ["sonnet", "   "]),
    ],
)
def test_an_invalid_model_fallbacks_is_rejected(
    tmp_path: Path, label: str, fallbacks: Any
) -> None:
    resolved = rp.apply_model_priority(_resolved(tmp_path))
    _reviewer(resolved, "code", "reviewer_c_introduced_code")[
        "model_fallbacks"
    ] = fallbacks

    with pytest.raises(rp.ConfigError) as excinfo:
        rp.validate_config(resolved)

    assert ".model_fallbacks" in str(excinfo.value), label


def test_a_leftover_peer_when_available_names_its_replacement(tmp_path: Path) -> None:
    with pytest.raises(rp.ConfigError) as excinfo:
        _resolved(
            tmp_path,
            user={
                "profiles": [
                    {
                        "id": "code",
                        "reviewers": [
                            {
                                "name": "reviewer_c_introduced_code",
                                "peer_when_available": True,
                            }
                        ],
                    }
                ]
            },
        )

    message = str(excinfo.value)
    assert "peer_when_available" in message
    assert "was removed" in message
    assert "model: [<name>, <name>]" in message


@pytest.mark.parametrize(
    ("label", "model"),
    [
        ("empty list", []),
        ("non-string entry", ["opus", 7]),
        ("empty entry", ["opus", "   "]),
        ("mapping", {"peer": "opus"}),
    ],
)
def test_an_invalid_model_is_rejected(
    tmp_path: Path, label: str, model: Any
) -> None:
    with pytest.raises(rp.ConfigError) as excinfo:
        _resolved(
            tmp_path,
            user={
                "profiles": [
                    {
                        "id": "code",
                        "reviewers": [
                            {"name": "reviewer_c_introduced_code", "model": model}
                        ],
                    }
                ]
            },
        )

    assert ".model" in str(excinfo.value), label


def test_the_projection_only_ever_carries_a_resolved_string(tmp_path: Path) -> None:
    """The runner reads this table, so a priority list must never reach it."""
    resolved = rp.apply_model_priority(_resolved(tmp_path))
    projection = rp.canonical_projection(resolved)

    for profile in projection["profiles"]:
        for reviewer in profile["reviewers"]:
            # `effort` is optional and omitted when unset, so it is allowed but
            # never required; `model` must always be present and resolved.
            assert set(reviewer) <= {"name", "model", "model_fallbacks", "effort"}
            assert {"name", "model", "model_fallbacks"} <= set(reviewer)
            assert isinstance(reviewer["model"], str)
            assert all(isinstance(entry, str) for entry in reviewer["model_fallbacks"])
            if "effort" in reviewer:
                assert reviewer["effort"] in rp.EFFORT_LEVELS


def test_shipped_effort_is_low_on_the_compliance_lane_only(tmp_path: Path) -> None:
    """The compliance lane is the only lane shipped at a stated effort.

    Every other lane omits `effort` and so inherits the session's level, which
    is the behavior all lanes had before the field existed.
    """
    resolved = rp.apply_model_priority(_resolved(tmp_path))
    projection = rp.canonical_projection(resolved)

    stated = {
        (profile["id"], reviewer["name"]): reviewer["effort"]
        for profile in projection["profiles"]
        for reviewer in profile["reviewers"]
        if "effort" in reviewer
    }
    assert stated == {
        ("data_only", "reviewer_a_claude_md_compliance"): "low",
        ("code", "reviewer_a_claude_md_compliance"): "low",
    }


def test_effort_merges_by_name_without_disturbing_model(tmp_path: Path) -> None:
    """A layer restating only `effort` keeps the shipped model for that lane."""
    config = _resolved(
        tmp_path,
        project={
            "profiles": [
                {
                    "id": "data_only",
                    "reviewers": [
                        {"name": "reviewer_a_claude_md_compliance", "effort": "high"}
                    ],
                }
            ]
        },
    )
    reviewer = _profile(config, "data_only")["reviewers"][0]

    assert reviewer["effort"] == "high"
    assert reviewer["model"] == "sonnet"


def test_an_unknown_effort_is_a_hard_error(tmp_path: Path) -> None:
    """Effort is a closed menu, so a typo fails at resolve, not at dispatch."""
    with pytest.raises(rp.ConfigError) as excinfo:
        _resolved(
            tmp_path,
            project={
                "profiles": [
                    {
                        "id": "data_only",
                        "reviewers": [
                            {
                                "name": "reviewer_a_claude_md_compliance",
                                "effort": "minimal",
                            }
                        ],
                    }
                ]
            },
        )

    message = str(excinfo.value)
    assert "minimal" in message
    assert "low" in message


def test_a_lane_may_state_effort_without_stating_a_model(tmp_path: Path) -> None:
    """Effort alone is a valid patch of a known reviewer, like `disabled`."""
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {"name": "reviewer_b_diff_only_bugs", "effort": "max"}
                    ],
                }
            ]
        },
    )
    reviewer = _profile(config, "code")["reviewers"][1]

    assert reviewer["name"] == "reviewer_b_diff_only_bugs"
    assert reviewer["effort"] == "max"
    assert reviewer["model"] == "opus"


def test_projecting_an_unresolved_list_is_refused(tmp_path: Path) -> None:
    """A caller that skips resolution gets an error, never a list on stdout."""
    with pytest.raises(rp.ConfigError) as excinfo:
        rp.canonical_projection(_resolved(tmp_path))

    message = str(excinfo.value)
    assert "reviewer_c_introduced_code" in message
    assert "apply_model_priority" in message


def test_cli_prints_the_resolved_table_with_no_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI has no peer diagnostic flag left -- nothing prints on stderr."""
    home, project_root, _project_path = _layers(tmp_path)

    assert rp.main(["--project-root", str(project_root), "--home", str(home)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    table = yaml.safe_load(captured.out.split("\n---\n")[0])
    code = next(p for p in table["profiles"] if p["id"] == "code")
    assert code["reviewers"][2]["model"] == "sol"


# --------------------------------------------------------------------------
# The shared declaration format (bootstrap_lib.model_declaration)
# --------------------------------------------------------------------------


def _with_validator(tmp_path: Path, model: Any) -> dict[str, Any]:
    """Resolve a table whose code profile's `bug` validator states ``model``."""
    return _resolved(
        tmp_path,
        user={"profiles": [{"id": "code", "validator_models": {"bug": model}}]},
    )


def test_a_reviewer_model_naming_one_id_twice_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(rp.ConfigError) as excinfo:
        _with_model(tmp_path, ["sol", "opus", "sol"])

    message = str(excinfo.value)
    assert ".model" in message
    assert "duplicate" in message
    assert "'sol'" in message


def test_a_reviewer_model_parses_through_the_shared_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One grammar: the reviewer field is checked by model_declaration.parse."""
    from bootstrap_lib import model_declaration

    seen: list[Any] = []
    real_parse = model_declaration.parse

    def spy(value: Any) -> list[str]:
        seen.append(value)
        return real_parse(value)

    monkeypatch.setattr(model_declaration, "parse", spy)
    _with_model(tmp_path, ["luna", "sonnet"])
    assert ["luna", "sonnet"] in seen


def test_a_one_element_validator_list_is_accepted_and_resolves_to_its_id(
    tmp_path: Path,
) -> None:
    config = _with_validator(tmp_path, ["sonnet"])

    resolved = rp.apply_model_priority(config)
    assert _profile(resolved, "code")["validator_models"]["bug"] == "sonnet"
    table = yaml.safe_load(rp.render_projection(resolved))
    code = next(p for p in table["profiles"] if p["id"] == "code")
    assert code["validator_models"] == {"bug": "sonnet", "claude_md": "sonnet"}


def test_a_scalar_validator_still_resolves_to_its_id(tmp_path: Path) -> None:
    resolved = rp.apply_model_priority(_with_validator(tmp_path, "haiku"))
    assert _profile(resolved, "code")["validator_models"]["bug"] == "haiku"


@pytest.mark.parametrize(
    ("label", "model", "fragment"),
    [
        ("empty list", [], "empty"),
        ("two entries", ["opus", "sonnet"], "exactly one"),
        ("duplicate", ["opus", "opus"], "duplicate"),
        ("non-string entry", [7], "string"),
        ("blank", "  ", "string"),
    ],
)
def test_an_invalid_validator_declaration_is_rejected(
    tmp_path: Path, label: str, model: Any, fragment: str
) -> None:
    with pytest.raises(rp.ConfigError) as excinfo:
        _with_validator(tmp_path, model)

    message = str(excinfo.value)
    assert "validator_models.bug" in message, label
    assert fragment in message, label


def test_a_resolved_table_with_a_validator_list_still_validates(tmp_path: Path) -> None:
    config = _with_validator(tmp_path, ["opus"])
    rp.validate_config(config)
