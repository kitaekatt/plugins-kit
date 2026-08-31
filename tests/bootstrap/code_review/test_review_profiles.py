"""Tests for bootstrap_lib.code_review.review_profiles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

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
    """The shipped executable projection is pinned byte-for-byte."""
    home, project_root, _project_path = _layers(tmp_path)
    config, provenance = rp.resolve_config(project_root, home=home)

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


def test_unknown_profiles_reviewers_and_validator_reasons_append(tmp_path: Path) -> None:
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "data_only",
                    "reviewers": [{"name": "reviewer_security", "model": "sonnet"}],
                    "validator_models": {"security": "sonnet"},
                },
                {
                    "id": "security",
                    "selection": {},
                    "reviewers": [{"name": "reviewer_security", "model": "opus"}],
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
    assert data_only["reviewers"][-1] == {
        "name": "reviewer_security",
        "model": "sonnet",
    }
    assert data_only["validator_models"] == {
        "bug": "sonnet",
        "claude_md": "sonnet",
        "security": "sonnet",
    }
    assert list(data_only["validator_models"]) == ["bug", "claude_md", "security"]


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
