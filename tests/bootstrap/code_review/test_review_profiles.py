"""Tests for bootstrap_lib.code_review.review_profiles.

Every test resolves against a complete tmp defaults layer (``DEFAULTS``),
monkeypatched over ``rp.DEFAULTS_PATH``. Tests that pin the SHIPPED table live
in test_shipped_review_profiles.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from bootstrap_lib import model_declaration
from bootstrap_lib.code_review import lane_prompts as lp
from bootstrap_lib.code_review import review_profiles as rp


A = "reviewer_a_claude_md_compliance"
B = "reviewer_b_diff_only_bugs"
C = "reviewer_c_introduced_code"


def _e(model_id: str, effort: str) -> dict[str, str]:
    """One complete model entry."""
    return {"id": model_id, "effort": effort}


DEFAULTS: dict[str, Any] = {
    "profiles": [
        {
            "id": "data_only",
            "selection": {
                "data_only_extensions": [".csv", ".yaml", ".yml", ".json", ".tsv", ".md"]
            },
            "reviewers": [
                {"name": A, "model": [_e("sonnet", "low")]},
                {"name": B, "model": [_e("sonnet", "low")]},
            ],
            "validator_models": {
                "bug": [_e("sonnet", "medium")],
                "claude_md": [_e("sonnet", "medium")],
            },
        },
        {
            "id": "code",
            "selection": {},
            "reviewers": [
                {"name": A, "model": [_e("sonnet", "low")]},
                {"name": B, "model": [_e("opus", "medium")]},
                {"name": C, "model": [_e("sol", "high"), _e("opus", "high")]},
            ],
            "validator_models": {
                "bug": [_e("opus", "high")],
                "claude_md": [_e("sonnet", "medium")],
            },
        },
    ]
}


def _write_yaml(path: Path, value: dict[str, Any]) -> None:
    """Write a test layer and create only its isolated parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(value, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def defaults_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the shipped layer at a complete tmp defaults file."""
    path = tmp_path / "defaults" / rp.CONFIG_NAME
    _write_yaml(path, DEFAULTS)
    monkeypatch.setattr(rp, "DEFAULTS_PATH", path)
    return path


def _layers(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return isolated home, project, and project-config paths."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project_config = project / ".claude" / rp.CONFIG_NAME
    return home, project, project_config


def _user_path(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".claude" / "config" / rp.CONFIG_NAME


def _resolved(
    tmp_path: Path,
    *,
    user: dict[str, Any] | None = None,
    project: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve test layers without consulting the real home directory."""
    home, project_root, project_path = _layers(tmp_path)
    if user is not None:
        _write_yaml(_user_path(tmp_path), user)
    if project is not None:
        _write_yaml(project_path, project)
    config, _provenance = rp.resolve_config(project_root, home=home)
    return config


def _main(tmp_path: Path, *extra: str) -> int:
    home, project_root, _project_path = _layers(tmp_path)
    return rp.main(["--project-root", str(project_root), "--home", str(home), *extra])


def _profile(config: dict[str, Any], profile_id: str) -> dict[str, Any]:
    """Find one resolved profile by id."""
    return next(profile for profile in config["profiles"] if profile["id"] == profile_id)


def _reviewer(config: dict[str, Any], profile_id: str, name: str) -> dict[str, Any]:
    return next(
        reviewer
        for reviewer in _profile(config, profile_id)["reviewers"]
        if reviewer["name"] == name
    )


def _reviewer_layer(name: str, model: Any, profile_id: str = "code") -> dict[str, Any]:
    """A layer stating exactly one reviewer record with ``model``."""
    return {"profiles": [{"id": profile_id, "reviewers": [{"name": name, "model": model}]}]}


def _validator_layer(model: Any) -> dict[str, Any]:
    """A layer stating the code profile's `bug` validator as ``model``."""
    return {"profiles": [{"id": "code", "validator_models": {"bug": model}}]}


def _findings(tmp_path: Path, **layers: dict[str, Any]) -> list[str]:
    """Resolve and return the findings the IncompleteConfigError carries."""
    with pytest.raises(rp.IncompleteConfigError) as excinfo:
        _resolved(tmp_path, **layers)
    return excinfo.value.findings


# --------------------------------------------------------------------------
# merge semantics
# --------------------------------------------------------------------------


def test_patch_merges_profile_reviewer_and_validator_in_place(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [{"name": B, "model": [_e("sonnet", "high")]}],
                    "validator_models": {"bug": [_e("sonnet", "low")]},
                }
            ]
        },
    )

    assert [profile["id"] for profile in config["profiles"]] == ["data_only", "code"]
    code = _profile(config, "code")
    assert [reviewer["name"] for reviewer in code["reviewers"]] == [A, B, C]
    assert code["reviewers"][1]["model"] == [_e("sonnet", "high")]
    assert code["validator_models"] == {
        "bug": [_e("sonnet", "low")],
        "claude_md": [_e("sonnet", "medium")],
    }


def test_unknown_profiles_and_validator_reasons_append(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "data_only",
                    "validator_models": {"security": [_e("sonnet", "low")]},
                },
                {
                    "id": "security",
                    "selection": {},
                    "reviewers": [{"name": B, "model": [_e("opus", "high")]}],
                    "validator_models": {
                        "bug": [_e("opus", "high")],
                        "claude_md": [_e("sonnet", "low")],
                    },
                },
            ]
        },
    )

    assert [profile["id"] for profile in config["profiles"]] == [
        "data_only",
        "code",
        "security",
    ]
    assert list(_profile(config, "data_only")["validator_models"]) == [
        "bug",
        "claude_md",
        "security",
    ]


def test_the_entry_list_replaces_wholesale_across_layers(tmp_path: Path) -> None:
    """A higher layer's list is the whole list: no entry or effort leaks up."""
    config = _resolved(
        tmp_path,
        user=_reviewer_layer(C, [_e("luna", "medium")]),
    )

    assert _reviewer(config, "code", C)["model"] == [_e("luna", "medium")]


def test_project_layer_has_highest_precedence(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user=_reviewer_layer(B, [_e("sonnet", "low")]),
        project=_reviewer_layer(B, [_e("opus", "max")]),
    )

    assert _reviewer(config, "code", B)["model"] == [_e("opus", "max")]


def test_plain_extension_list_replaces_instead_of_merging(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {"id": "data_only", "selection": {"data_only_extensions": [".toml", ".ini"]}}
            ]
        },
    )

    assert _profile(config, "data_only")["selection"] == {
        "data_only_extensions": [".toml", ".ini"]
    }


def test_disabled_profile_and_reviewer_are_removed(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {"id": "code", "disabled": True},
                {"id": "data_only", "reviewers": [{"name": B, "disabled": True}]},
            ]
        },
    )

    assert [profile["id"] for profile in config["profiles"]] == ["data_only"]
    assert [reviewer["name"] for reviewer in config["profiles"][0]["reviewers"]] == [A]


def test_unknown_reviewer_name_is_rejected_with_supported_lanes(tmp_path: Path) -> None:
    with pytest.raises(rp.ConfigError, match="reviewer_security") as excinfo:
        _resolved(
            tmp_path,
            user=_reviewer_layer("reviewer_security", [_e("sonnet", "low")], "data_only"),
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
                            {"name": name, "disabled": True} for name in (A, B, C)
                        ],
                    }
                ]
            },
        )


# --------------------------------------------------------------------------
# completeness findings
# --------------------------------------------------------------------------


def test_an_entry_without_effort_is_a_finding_naming_layer_profile_lane_and_entry(
    tmp_path: Path,
) -> None:
    findings = _findings(
        tmp_path,
        user=_reviewer_layer(C, [_e("sol", "high"), {"id": "opus"}]),
    )

    assert findings == [
        f"user {_user_path(tmp_path)}: profiles[code].reviewers[{C}].model[1] (opus): "
        "missing effort"
    ]


@pytest.mark.parametrize(
    ("label", "model", "location"),
    [
        ("bare string", "opus", ".model (opus)"),
        ("list of strings", ["opus"], ".model[0] (opus)"),
    ],
)
def test_a_bare_string_model_is_a_finding_not_a_crash(
    tmp_path: Path, label: str, model: Any, location: str
) -> None:
    findings = _findings(tmp_path, user=_reviewer_layer(C, model))

    assert len(findings) == 1, label
    assert f"profiles[code].reviewers[{C}]{location}" in findings[0], label
    assert "entry 'opus' states no effort" in findings[0], label
    assert "{id: opus, effort: <level>}" in findings[0], label


def test_lane_level_effort_is_an_error_naming_the_per_entry_form(tmp_path: Path) -> None:
    findings = _findings(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {"name": B, "model": [_e("opus", "high")], "effort": "high"}
                    ],
                }
            ]
        },
    )

    assert len(findings) == 1
    assert f"profiles[code].reviewers[{B}].effort" in findings[0]
    assert "`effort` on a lane was removed: state it on each model entry" in findings[0]
    assert "{id: <model>, effort: <level>}" in findings[0]


def test_a_model_override_without_efforts_is_a_finding_over_a_complete_lower_layer(
    tmp_path: Path,
) -> None:
    """The shipped layer is complete; the user layer's own list is not."""
    findings = _findings(tmp_path, user=_reviewer_layer(C, [{"id": "luna"}]))

    assert findings == [
        f"user {_user_path(tmp_path)}: profiles[code].reviewers[{C}].model[0] (luna): "
        "missing effort"
    ]


def test_a_reviewer_record_without_a_model_is_a_finding(tmp_path: Path) -> None:
    """A sparse patch no longer inherits the lower layer's model list."""
    findings = _findings(
        tmp_path,
        user={"profiles": [{"id": "code", "reviewers": [{"name": B, "disabled": False}]}]},
    )

    assert len(findings) == 1
    assert f"profiles[code].reviewers[{B}].model: missing" in findings[0]


def test_a_disabled_only_record_is_exempt(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user={"profiles": [{"id": "code", "reviewers": [{"name": B, "disabled": True}]}]},
    )

    assert [reviewer["name"] for reviewer in _profile(config, "code")["reviewers"]] == [A, C]


def test_anything_under_a_disabled_profile_is_exempt(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "data_only",
                    "disabled": True,
                    "reviewers": [{"name": A, "model": "sonnet", "effort": "low"}],
                    "validator_models": {"bug": "sonnet"},
                }
            ]
        },
    )

    assert [profile["id"] for profile in config["profiles"]] == ["code"]


def test_resolve_raises_on_an_incomplete_layer(tmp_path: Path) -> None:
    """The error is a ConfigError, so existing callers still see one."""
    with pytest.raises(rp.ConfigError) as excinfo:
        _resolved(tmp_path, project=_validator_layer([{"id": "opus"}]))

    assert isinstance(excinfo.value, rp.IncompleteConfigError)
    assert "validator_models.bug[0] (opus): missing effort" in str(excinfo.value)


def test_an_incomplete_shipped_layer_is_a_finding(
    tmp_path: Path, defaults_path: Path
) -> None:
    """The shipped layer is held to the same rule as an override."""
    incomplete = yaml.safe_load(yaml.safe_dump(DEFAULTS))
    incomplete["profiles"][0]["reviewers"][0]["model"] = "sonnet"
    _write_yaml(defaults_path, incomplete)

    findings = _findings(tmp_path)

    assert findings == [
        f"shipped {defaults_path}: profiles[data_only].reviewers[{A}].model (sonnet): "
        "entry 'sonnet' states no effort; write [{id: sonnet, effort: <level>}]"
    ]


def test_findings_aggregate_across_all_three_layers(
    tmp_path: Path, defaults_path: Path
) -> None:
    """One resolve reports every gap in shipped, user and project together."""
    shipped = yaml.safe_load(yaml.safe_dump(DEFAULTS))
    shipped["profiles"][1]["validator_models"]["claude_md"] = "sonnet"
    _write_yaml(defaults_path, shipped)

    findings = _findings(
        tmp_path,
        user=_reviewer_layer(A, [{"id": "sonnet"}]),
        project=_reviewer_layer(B, ["opus", {"id": "sonnet"}]),
    )

    home, _project_root, project_path = _layers(tmp_path)
    assert [finding.split(":", 1)[0] for finding in findings] == [
        f"shipped {defaults_path}",
        f"user {_user_path(tmp_path)}",
        f"project {project_path}",
        f"project {project_path}",
    ]
    assert "validator_models.claude_md (sonnet)" in findings[0]
    assert f"reviewers[{A}].model[0] (sonnet): missing effort" in findings[1]
    assert f"reviewers[{B}].model[0] (opus): entry 'opus' states no effort" in findings[2]
    assert f"reviewers[{B}].model[1] (sonnet): missing effort" in findings[3]


# --------------------------------------------------------------------------
# entry structure
# --------------------------------------------------------------------------


def test_a_validator_keeps_its_entry_order(tmp_path: Path) -> None:
    entries = [_e("opus", "high"), _e("sonnet", "low")]
    resolved = rp.apply_model_priority(
        _resolved(tmp_path, user=_validator_layer(entries))
    )

    assert _profile(resolved, "code")["validator_models"]["bug"] == entries
    rp.validate_config(resolved)


def test_a_validator_may_lead_with_a_non_agent_entry(tmp_path: Path) -> None:
    entries = [_e("luna", "high"), _e("sonnet", "low")]
    resolved = rp.apply_model_priority(
        _resolved(tmp_path, user=_validator_layer(entries))
    )

    assert _profile(resolved, "code")["validator_models"]["bug"] == entries


def test_a_validator_with_no_agent_entry_is_refused(tmp_path: Path) -> None:
    with pytest.raises(rp.ConfigError) as excinfo:
        _resolved(tmp_path, user=_validator_layer([_e("luna", "high")]))

    message = str(excinfo.value)
    assert "validator_models.bug" in message
    assert "no validator route" in message
    assert "luna" in message


@pytest.mark.parametrize(
    ("label", "model", "fragment"),
    [
        ("empty list", [], "empty"),
        ("non-entry element", [_e("opus", "high"), 7], "{id: <model>, effort: <level>}"),
        ("blank id", [_e("   ", "high")], "string"),
        ("missing id", [{"effort": "high"}], "required field missing: id"),
        ("bare mapping", _e("opus", "high"), "got a mapping"),
        ("unknown effort", [_e("opus", "minimal")], "unknown effort 'minimal'"),
    ],
)
def test_an_invalid_model_is_rejected(
    tmp_path: Path, label: str, model: Any, fragment: str
) -> None:
    with pytest.raises(rp.ConfigError) as excinfo:
        _resolved(tmp_path, user=_reviewer_layer(C, model))

    assert not isinstance(excinfo.value, rp.IncompleteConfigError), label
    message = str(excinfo.value)
    assert ".model" in message, label
    assert fragment in message, label


def test_an_unknown_entry_field_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(rp.ConfigError) as excinfo:
        _resolved(
            tmp_path,
            user=_reviewer_layer(C, [{"id": "opus", "effort": "high", "harness": "claude"}]),
        )

    message = str(excinfo.value)
    assert f"reviewers[0].model[0]" in message
    assert "unknown field(s): 'harness'" in message
    assert "known fields: effort, id" in message


def test_a_duplicate_entry_id_is_rejected_through_model_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []
    real_parse = model_declaration.parse

    def spy(value: Any) -> list[str]:
        seen.append(value)
        return real_parse(value)

    monkeypatch.setattr(model_declaration, "parse", spy)
    with pytest.raises(rp.ConfigError) as excinfo:
        _resolved(
            tmp_path,
            user=_reviewer_layer(C, [_e("sol", "high"), _e("opus", "high"), _e("sol", "low")]),
        )

    message = str(excinfo.value)
    assert ".model[2]" in message
    assert "duplicate id 'sol'" in message
    assert ["sol", "opus", "sol"] in seen


def test_the_shared_declaration_grammar_still_rejects_mappings() -> None:
    """model_declaration is untouched: entries are this module's shape only."""
    with pytest.raises(model_declaration.DeclarationError):
        model_declaration.parse(_e("opus", "high"))
    with pytest.raises(model_declaration.DeclarationError):
        model_declaration.parse([_e("opus", "high")])
    assert model_declaration.parse(["opus", " sol "]) == ["opus", "sol"]


def test_a_leftover_peer_when_available_names_its_replacement(tmp_path: Path) -> None:
    with pytest.raises(rp.ConfigError) as excinfo:
        _resolved(
            tmp_path,
            user={
                "profiles": [
                    {"id": "code", "reviewers": [{"name": C, "peer_when_available": True}]}
                ]
            },
        )

    message = str(excinfo.value)
    assert "peer_when_available" in message
    assert "was removed" in message


def test_a_model_fallbacks_field_is_an_unknown_field(tmp_path: Path) -> None:
    with pytest.raises(rp.ConfigError, match="unknown field\\(s\\): 'model_fallbacks'"):
        _resolved(
            tmp_path,
            user={
                "profiles": [
                    {
                        "id": "code",
                        "reviewers": [
                            {
                                "name": C,
                                "model": [_e("sol", "high")],
                                "model_fallbacks": [],
                            }
                        ],
                    }
                ]
            },
        )


# --------------------------------------------------------------------------
# validators
# --------------------------------------------------------------------------


def test_a_validator_entry_resolves_to_a_one_entry_list(tmp_path: Path) -> None:
    resolved = rp.apply_model_priority(
        _resolved(tmp_path, user=_validator_layer([_e(" haiku ", "low")]))
    )

    assert _profile(resolved, "code")["validator_models"]["bug"] == [_e("haiku", "low")]
    rp.validate_config(resolved)


@pytest.mark.parametrize(
    ("label", "model", "fragment"),
    [
        ("empty list", [], "empty"),
        ("duplicate", [_e("opus", "high"), _e("opus", "low")], "duplicate"),
        ("non-string id", [_e(7, "high")], "string"),  # type: ignore[arg-type]
        ("blank", "  ", "string"),
        ("bare mapping", _e("opus", "high"), "got a mapping"),
    ],
)
def test_an_invalid_validator_is_rejected(
    tmp_path: Path, label: str, model: Any, fragment: str
) -> None:
    with pytest.raises(rp.ConfigError) as excinfo:
        _resolved(tmp_path, user=_validator_layer(model))

    assert not isinstance(excinfo.value, rp.IncompleteConfigError), label
    message = str(excinfo.value)
    assert "validator_models.bug" in message, label
    assert fragment in message, label


@pytest.mark.parametrize(
    ("label", "model", "fragment"),
    [
        ("scalar", "opus", "validator_models.bug (opus): entry 'opus' states no effort"),
        ("string list", ["opus"], "validator_models.bug[0] (opus): entry 'opus' states no effort"),
        ("no effort", [{"id": "opus"}], "validator_models.bug[0] (opus): missing effort"),
    ],
)
def test_a_validator_requires_exactly_one_entry_with_effort(
    tmp_path: Path, label: str, model: Any, fragment: str
) -> None:
    findings = _findings(tmp_path, user=_validator_layer(model))

    assert len(findings) == 1, label
    assert fragment in findings[0], label


# --------------------------------------------------------------------------
# projection
# --------------------------------------------------------------------------


def test_the_projection_carries_each_entrys_effort(tmp_path: Path) -> None:
    resolved = rp.apply_model_priority(
        _resolved(tmp_path, user=_reviewer_layer(C, [_e("luna", "xhigh"), _e("sonnet", "low")]))
    )
    rendered = rp.render_projection(resolved)
    table = yaml.safe_load(rendered)
    code = next(p for p in table["profiles"] if p["id"] == "code")

    assert code["reviewers"][2] == {
        "name": C,
        "model": [_e("luna", "xhigh"), _e("sonnet", "low")],
    }
    assert code["validator_models"] == {
        "bug": [_e("opus", "high")],
        "claude_md": [_e("sonnet", "medium")],
    }
    for profile in table["profiles"]:
        for reviewer in profile["reviewers"]:
            assert set(reviewer) == {"name", "model"}
            for entry in reviewer["model"]:
                assert list(entry) == ["id", "effort"]
                assert entry["effort"] in rp.EFFORT_LEVELS


def test_peer_prefixed_entry_is_an_ordinary_id(tmp_path: Path) -> None:
    resolved = rp.apply_model_priority(
        _resolved(tmp_path, user=_reviewer_layer(C, [_e("peer:opus", "high"), _e("opus", "high")]))
    )

    assert _reviewer(resolved, "code", C)["model"] == [
        _e("peer:opus", "high"),
        _e("opus", "high"),
    ]


def test_projecting_an_entry_without_effort_is_refused(tmp_path: Path) -> None:
    """A caller that hands in an incomplete table gets an error, not a table."""
    config = _resolved(tmp_path)
    _reviewer(config, "code", C)["model"] = ["sol", "opus"]

    with pytest.raises(rp.ConfigError) as excinfo:
        rp.canonical_projection(config)
    with pytest.raises(rp.ConfigError):
        rp.apply_model_priority(config)

    assert C in str(excinfo.value)
    assert "resolve_config" in str(excinfo.value)


def test_validate_config_rejects_an_incomplete_resolved_table(tmp_path: Path) -> None:
    config = _resolved(tmp_path)
    del _profile(config, "code")["validator_models"]["bug"][0]["effort"]

    with pytest.raises(rp.IncompleteConfigError, match="missing effort"):
        rp.validate_config(config)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "layer"),
    [
        ("unknown field", {"unexpected": True}),
        ("missing profile fields", {"profiles": [{"id": "new_profile"}]}),
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
                            {"name": "same", "model": [_e("sonnet", "low")]},
                            {"name": "same", "model": [_e("opus", "low")]},
                        ],
                        "validator_models": {},
                    }
                ]
            },
        ),
        ("empty reviewer model", _reviewer_layer(B, "")),
        ("empty reviewer name", _reviewer_layer("  ", [_e("sonnet", "low")])),
    ],
)
def test_invalid_configuration_exits_2(
    tmp_path: Path,
    label: str,
    layer: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI rejects invalid layers before emitting a table, in both modes."""
    _home, _project_root, project_path = _layers(tmp_path)
    _write_yaml(project_path, layer)

    for extra in ((), ("--check",)):
        assert _main(tmp_path, *extra) == 2, (label, extra)
        captured = capsys.readouterr()
        assert captured.out == "", label
        assert "review profiles config error:" in captured.err
        assert str(project_path) in captured.err


def test_cli_prints_yaml_once_and_provenance_for_shipped_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _main(tmp_path) == 0
    captured = capsys.readouterr()
    output = captured.out
    assert captured.err == ""
    assert output.count("profiles:\n") == 1
    assert "Layers applied: shipped." in output
    assert "To change this policy, create: user (" in output
    assert "project (" in output
    table = yaml.safe_load(output.split("\n---\n")[0])
    code = next(p for p in table["profiles"] if p["id"] == "code")
    assert code["reviewers"][2]["model"] == [_e("sol", "high"), _e("opus", "high")]


def test_render_exits_1_with_every_finding_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A review stops at render time: no table, every finding named."""
    _write_yaml(_user_path(tmp_path), _reviewer_layer(C, ["sol", "opus"]))

    assert _main(tmp_path) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "model[0] (sol): entry 'sol' states no effort" in captured.err
    assert "model[1] (opus): entry 'opus' states no effort" in captured.err


def test_check_reports_every_finding_not_just_the_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_yaml(
        _user_path(tmp_path),
        {
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {"name": A, "model": "sonnet"},
                        {"name": B, "model": [_e("opus", "high")], "effort": "high"},
                        {"name": C, "model": [{"id": "sol"}, {"id": "opus"}]},
                    ],
                    "validator_models": {"bug": "opus"},
                }
            ]
        },
    )

    assert _main(tmp_path, "--check") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [line for line in captured.err.splitlines() if line.startswith("user ")]
    assert len(lines) == 5
    assert any(f"reviewers[{A}].model (sonnet)" in line for line in lines)
    assert any(f"reviewers[{B}].effort" in line for line in lines)
    assert any(f"reviewers[{C}].model[0] (sol)" in line for line in lines)
    assert any(f"reviewers[{C}].model[1] (opus)" in line for line in lines)
    assert any("validator_models.bug (opus)" in line for line in lines)


def test_check_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """0 complete, 1 findings, 2 a malformed or invalid layer."""
    assert _main(tmp_path, "--check") == 0
    captured = capsys.readouterr()
    assert captured.out == "review profiles: complete. Layers applied: shipped.\n"

    _write_yaml(_user_path(tmp_path), _reviewer_layer(C, ["sol"]))
    assert _main(tmp_path, "--check") == 1

    _write_yaml(_user_path(tmp_path), _reviewer_layer(C, [_e("sol", "minimal")]))
    assert _main(tmp_path, "--check") == 2

    _user_path(tmp_path).write_text("profiles: [\n", encoding="utf-8")
    assert _main(tmp_path, "--check") == 2
