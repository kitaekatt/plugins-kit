"""Tests for bootstrap_lib.code_review.review_profiles."""

from __future__ import annotations

import sys
import types
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
    """The shipped executable projection is pinned byte-for-byte.

    The autouse fixture below makes the owner absent, so the shipped
    `[peer:opus, opus]` priority list resolves to `opus` -- the projection the
    fixture has always pinned.
    """
    home, project_root, _project_path = _layers(tmp_path)
    config, provenance = rp.resolve_config(project_root, home=home)
    config, _disclosures, _diagnostics = rp.apply_model_priority(config)

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


# --------------------------------------------------------------------------
# model priority lists: the optional llm-scripting-kit seats edge
# --------------------------------------------------------------------------


class _FakeSeat:
    """A stand-in for llm_scripting_kit.seats.Seat with only what we read."""

    def __init__(self, relation: str, endpoint: str) -> None:
        self.relation = relation
        self.endpoint = endpoint


class _FakeSeatsResult:
    """A stand-in for llm_scripting_kit.seats.SeatsResult."""

    def __init__(self, *seats: _FakeSeat) -> None:
        self.seats = tuple(seats)


def _discover(*seats: _FakeSeat, record: list[Any] | None = None) -> Any:
    """Build a fabricated discover_seats that never touches the network."""

    def discover_seats(self_ref: str, **kwargs: Any) -> _FakeSeatsResult:
        if record is not None:
            record.append((self_ref, kwargs))
        return _FakeSeatsResult(*seats)

    return discover_seats


# Captured before the autouse fixture below replaces the attribute, so a test
# that wants the REAL three-state probe can restore it.
_REAL_PROBE = rp._probe_discover_seats


@pytest.fixture(autouse=True)
def _no_real_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test hermetic on a machine where the owner IS installed.

    ``main`` probes for the owner on each run, so without this a developer box
    with llm-scripting-kit linked would run real reachability probes during the
    suite. Tests that exercise a specific state override this explicitly.
    """
    monkeypatch.setattr(
        rp,
        "_probe_discover_seats",
        lambda: (None, rp._peer_seats_absent_diagnosis("test isolation")),
    )


def _shipped(tmp_path: Path) -> dict[str, Any]:
    """Resolve the shipped-only table, whose code profile carries a peer entry."""
    return _resolved(tmp_path)


def _reviewer(config: dict[str, Any], profile_id: str, name: str) -> dict[str, Any]:
    return next(
        reviewer
        for reviewer in _profile(config, profile_id)["reviewers"]
        if reviewer["name"] == name
    )


def test_shipped_default_gives_reviewer_c_one_peer_priority_list(tmp_path: Path) -> None:
    """The shipped priority list is exactly one lane in exactly one profile."""
    config = _shipped(tmp_path)

    assert _reviewer(config, "code", "reviewer_c_introduced_code")["model"] == [
        "peer:opus",
        "opus",
    ]
    listed = [
        (profile["id"], reviewer["name"])
        for profile in config["profiles"]
        for reviewer in profile["reviewers"]
        if not isinstance(reviewer["model"], str)
    ]
    assert listed == [("code", "reviewer_c_introduced_code")]


def test_a_string_model_is_unchanged_and_probes_nothing(tmp_path: Path) -> None:
    """A plain string keeps its old meaning: a one-entry list that resolves."""
    calls: list[Any] = []
    config, disclosures, diagnostics = rp.apply_model_priority(
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
        ),
        discover=_discover(_FakeSeat("BESIDE", "beside-seat"), record=calls),
    )

    code = _profile(config, "code")
    assert [reviewer["model"] for reviewer in code["reviewers"]] == [
        "sonnet",
        "opus",
        "sonnet",
    ]
    assert calls == []
    assert disclosures == []
    assert diagnostics == []


def test_a_peer_entry_that_resolves_substitutes_and_discloses(tmp_path: Path) -> None:
    calls: list[Any] = []
    config, disclosures, diagnostics = rp.apply_model_priority(
        _shipped(tmp_path),
        project_root=tmp_path,
        discover=_discover(
            _FakeSeat("UP", "up-seat"),
            _FakeSeat("BESIDE", "beside-seat"),
            record=calls,
        ),
    )

    code = _profile(config, "code")
    assert _reviewer(config, "code", "reviewer_c_introduced_code")["model"] == (
        "beside-seat"
    )
    # Only the lane whose list carries a peer entry moves.
    assert code["reviewers"][1]["model"] == "opus"
    assert diagnostics == []
    assert len(disclosures) == 1
    line = disclosures[0]
    assert line.startswith("model-priority: profile 'code' lane ")
    assert "reviewer_c_introduced_code" in line
    assert "'peer:opus'" in line
    assert "'beside-seat'" in line
    assert "BESIDE" in line
    assert rp.PEER_SEATS_FRONTIER in line
    # The peer target is the self reference, and the probe is bounded.
    assert calls[0][0] == "opus"
    assert calls[0][1]["timeout"] == rp.PEER_SEATS_TIMEOUT_S
    assert calls[0][1]["project_root"] == str(tmp_path)


def test_a_present_owner_without_a_seat_falls_through_and_notes_the_skip(
    tmp_path: Path,
) -> None:
    config, disclosures, diagnostics = rp.apply_model_priority(
        _shipped(tmp_path), discover=_discover(_FakeSeat("UP", "up-seat"))
    )

    assert _reviewer(config, "code", "reviewer_c_introduced_code")["model"] == "opus"
    assert diagnostics == []
    assert len(disclosures) == 1
    line = disclosures[0]
    assert line.startswith("model-priority: profile 'code' lane ")
    assert "skipped priority entry 'peer:opus'" in line
    assert "no reachable BESIDE seat" in line
    assert "runs on 'opus'" in line


def test_absent_owner_is_silent_and_diagnosed_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rung 2: the owner was never installed."""
    monkeypatch.setattr(rp, "_probe_discover_seats", _REAL_PROBE)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)

    config, disclosures, diagnostics = rp.apply_model_priority(_shipped(tmp_path))

    assert _reviewer(config, "code", "reviewer_c_introduced_code")["model"] == "opus"
    assert disclosures == []
    assert len(diagnostics) == 1
    assert diagnostics[0].startswith("absent:")
    assert "claude plugin install llm-scripting-kit@plugins-kit" in diagnostics[0]
    assert "too old" not in diagnostics[0]


def test_too_old_owner_is_silent_and_diagnosed_apart_from_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rung 3: the owner imports but predates the seats module."""
    monkeypatch.setattr(rp, "_probe_discover_seats", _REAL_PROBE)
    monkeypatch.setitem(
        sys.modules, "llm_scripting_kit", types.ModuleType("llm_scripting_kit")
    )
    monkeypatch.delitem(sys.modules, "llm_scripting_kit.seats", raising=False)

    config, disclosures, diagnostics = rp.apply_model_priority(_shipped(tmp_path))

    assert _reviewer(config, "code", "reviewer_c_introduced_code")["model"] == "opus"
    assert disclosures == []
    assert len(diagnostics) == 1
    assert diagnostics[0].startswith("too old or stale:")
    assert "claude plugin update llm-scripting-kit@plugins-kit" in diagnostics[0]
    assert rp.PEER_SEATS_FRONTIER_VERSION in diagnostics[0]
    assert "is not installed" not in diagnostics[0]


def test_stale_after_uninstall_still_imports_but_stays_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rung 4: a leftover copy imports; the frontier symbol does not exist."""
    monkeypatch.setattr(rp, "_probe_discover_seats", _REAL_PROBE)
    owner = types.ModuleType("llm_scripting_kit")
    stale_seats = types.ModuleType("llm_scripting_kit.seats")
    owner.seats = stale_seats  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", owner)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.seats", stale_seats)

    config, disclosures, diagnostics = rp.apply_model_priority(_shipped(tmp_path))

    assert _reviewer(config, "code", "reviewer_c_introduced_code")["model"] == "opus"
    assert disclosures == []
    assert len(diagnostics) == 1
    assert diagnostics[0].startswith("too old or stale:")
    assert "discover_seats is missing" in diagnostics[0]


def test_a_raising_probe_degrades_to_the_next_entry(tmp_path: Path) -> None:
    def explode(self_ref: str, **kwargs: Any) -> Any:
        raise RuntimeError("registry unreadable")

    config, disclosures, diagnostics = rp.apply_model_priority(
        _shipped(tmp_path), discover=explode
    )

    assert _reviewer(config, "code", "reviewer_c_introduced_code")["model"] == "opus"
    assert len(disclosures) == 1
    assert "skipped priority entry 'peer:opus' (seat discovery failed)" in (
        disclosures[0]
    )
    assert len(diagnostics) == 1
    assert "RuntimeError: registry unreadable" in diagnostics[0]


def test_a_malformed_seats_result_degrades_rather_than_raising(tmp_path: Path) -> None:
    """An unexpected owner shape falls through to the next entry."""
    config, disclosures, _diagnostics = rp.apply_model_priority(
        _shipped(tmp_path), discover=lambda self_ref, **kwargs: object()
    )

    assert _reviewer(config, "code", "reviewer_c_introduced_code")["model"] == "opus"
    assert "no reachable BESIDE seat" in disclosures[0]


def test_a_list_whose_entries_all_fail_is_a_configuration_error(tmp_path: Path) -> None:
    """Every entry is a peer entry and none resolves: name the lane and list."""
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {
                            "name": "reviewer_c_introduced_code",
                            "model": ["peer:opus", "peer:sonnet"],
                        }
                    ],
                }
            ]
        },
    )

    with pytest.raises(rp.ConfigError) as excinfo:
        rp.apply_model_priority(config, discover=_discover(_FakeSeat("UP", "up-seat")))

    message = str(excinfo.value)
    assert "reviewer_c_introduced_code" in message
    assert "'code'" in message
    assert "['peer:opus', 'peer:sonnet']" in message
    assert "no entry resolved" in message


def test_a_bare_peer_string_is_a_one_entry_list_and_errors_when_unresolved(
    tmp_path: Path,
) -> None:
    """`model: peer:opus` is legal; it just has nothing to fall back to."""
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {"name": "reviewer_c_introduced_code", "model": "peer:opus"}
                    ],
                }
            ]
        },
    )

    resolved, disclosures, _diag = rp.apply_model_priority(
        config, discover=_discover(_FakeSeat("BESIDE", "beside-seat"))
    )
    assert _reviewer(resolved, "code", "reviewer_c_introduced_code")["model"] == (
        "beside-seat"
    )
    assert len(disclosures) == 1

    with pytest.raises(rp.ConfigError) as excinfo:
        rp.apply_model_priority(config, discover=_discover(_FakeSeat("UP", "up")))
    assert "['peer:opus']" in str(excinfo.value)


def test_a_user_model_replaces_the_shipped_list_wholesale(tmp_path: Path) -> None:
    """The defect this field shape fixes: `model: fable` means fable."""
    calls: list[Any] = []
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

    resolved, disclosures, diagnostics = rp.apply_model_priority(
        config, discover=_discover(_FakeSeat("BESIDE", "beside-seat"), record=calls)
    )

    assert _reviewer(resolved, "code", "reviewer_c_introduced_code")["model"] == "fable"
    assert calls == []
    assert disclosures == []
    assert diagnostics == []


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
                            "model": ["peer:sonnet", "sonnet"],
                        }
                    ],
                }
            ]
        },
    )

    assert _reviewer(config, "code", "reviewer_c_introduced_code")["model"] == [
        "peer:sonnet",
        "sonnet",
    ]


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
    assert "model: [peer:<name>, <name>]" in message


@pytest.mark.parametrize(
    ("label", "model"),
    [
        ("empty list", []),
        ("non-string entry", ["opus", 7]),
        ("empty entry", ["opus", "   "]),
        ("bare peer prefix", ["peer:", "opus"]),
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


def test_no_owner_artifact_states_the_model_that_will_run(tmp_path: Path) -> None:
    """EN-5: without the owner the rendered table is true as read."""
    config, disclosures, _diagnostics = rp.apply_model_priority(_shipped(tmp_path))
    rendered = rp.render_projection(config)

    assert disclosures == []
    assert "peer:" not in rendered
    table = yaml.safe_load(rendered)
    code = next(p for p in table["profiles"] if p["id"] == "code")
    assert code["reviewers"][2] == {
        "name": "reviewer_c_introduced_code",
        "model": "opus",
    }


def test_the_projection_only_ever_carries_a_resolved_string(tmp_path: Path) -> None:
    """The runner reads this table, so a priority list must never reach it."""
    resolved, _disclosures, _diag = rp.apply_model_priority(_shipped(tmp_path))
    projection = rp.canonical_projection(resolved)

    for profile in projection["profiles"]:
        for reviewer in profile["reviewers"]:
            assert set(reviewer) == {"name", "model"}
            assert isinstance(reviewer["model"], str)


def test_projecting_an_unresolved_list_is_refused(tmp_path: Path) -> None:
    """A caller that skips resolution gets an error, never a list on stdout."""
    with pytest.raises(rp.ConfigError) as excinfo:
        rp.canonical_projection(_shipped(tmp_path))

    message = str(excinfo.value)
    assert "reviewer_c_introduced_code" in message
    assert "apply_model_priority" in message


def test_the_probe_runs_again_on_every_call(tmp_path: Path) -> None:
    """EN-6: nothing is cached across invocations."""
    calls: list[Any] = []
    discover = _discover(_FakeSeat("BESIDE", "beside-seat"), record=calls)

    rp.apply_model_priority(_shipped(tmp_path), discover=discover)
    rp.apply_model_priority(_shipped(tmp_path), discover=discover)

    assert len(calls) == 2


def test_one_probe_per_distinct_peer_target_within_a_single_call(
    tmp_path: Path,
) -> None:
    calls: list[Any] = []
    config = _resolved(
        tmp_path,
        user={
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {
                            "name": "reviewer_a_claude_md_compliance",
                            "model": ["peer:opus", "opus"],
                        }
                    ],
                }
            ]
        },
    )
    resolved, disclosures, _diag = rp.apply_model_priority(
        config, discover=_discover(_FakeSeat("BESIDE", "beside-seat"), record=calls)
    )

    assert len(calls) == 1
    code = _profile(resolved, "code")
    assert [reviewer["model"] for reviewer in code["reviewers"]] == [
        "beside-seat",
        "opus",
        "beside-seat",
    ]
    assert len(disclosures) == 2


def test_cli_discloses_a_substitution_on_stderr_and_keeps_stdout_parseable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, project_root, _project_path = _layers(tmp_path)
    monkeypatch.setattr(
        rp,
        "_probe_discover_seats",
        lambda: (_discover(_FakeSeat("BESIDE", "beside-seat")), None),
    )

    assert rp.main(["--project-root", str(project_root), "--home", str(home)]) == 0
    captured = capsys.readouterr()

    table = yaml.safe_load(captured.out.split("\n---\n")[0])
    code = next(p for p in table["profiles"] if p["id"] == "code")
    assert code["reviewers"][2]["model"] == "beside-seat"
    assert "peer:" not in captured.out
    assert captured.err.count("\n") == 1
    assert captured.err.startswith("model-priority: profile 'code' lane ")


def test_cli_is_silent_about_an_absent_owner_until_explain_is_asked_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, project_root, _project_path = _layers(tmp_path)
    monkeypatch.setattr(rp, "_probe_discover_seats", _REAL_PROBE)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)

    assert rp.main(["--project-root", str(project_root), "--home", str(home)]) == 0
    assert capsys.readouterr().err == ""

    assert (
        rp.main(
            [
                "--project-root",
                str(project_root),
                "--home",
                str(home),
                "--explain-peer-seats",
            ]
        )
        == 0
    )
    err = capsys.readouterr().err
    assert err.startswith("model-priority: absent:")
    assert err.count("\n") == 1


def test_cli_exits_nonzero_when_no_entry_of_a_list_resolves(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home, project_root, project_path = _layers(tmp_path)
    _write_yaml(
        project_path,
        {
            "profiles": [
                {
                    "id": "code",
                    "reviewers": [
                        {
                            "name": "reviewer_c_introduced_code",
                            "model": ["peer:opus"],
                        }
                    ],
                }
            ]
        },
    )

    assert rp.main(["--project-root", str(project_root), "--home", str(home)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "review profiles config error:" in captured.err
    assert "reviewer_c_introduced_code" in captured.err
