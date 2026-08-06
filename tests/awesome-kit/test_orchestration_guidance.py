"""Tests for the orchestrate skill's policy generator.

Covers the three-layer merge, record-list merging by id, backend detection,
capacity reporting (including the snapshot contract with claude-ui-kit), and
the shape of the rendered guidance.

The module under test re-execs into awesome-kit's provisioned venv on import;
tests/conftest.py sets _BOOTSTRAP_GUARD_VENV_REEXEC so that is a no-op here
(see plugins/CLAUDE.md -- without it, importing would abandon the pytest
process itself).
"""

import json
import time

import pytest
import yaml

import orchestration_guidance as og


# --------------------------------------------------------------------------
# Merge semantics
# --------------------------------------------------------------------------


class TestDeepMerge:
    def test_mappings_merge_key_by_key(self):
        base = {"capacity": {"source": "auto", "max_age_minutes": 30}}
        override = {"capacity": {"max_age_minutes": 5}}
        assert og.deep_merge(base, override) == {
            "capacity": {"source": "auto", "max_age_minutes": 5}
        }

    def test_scalars_replace(self):
        assert og.deep_merge({"default_tier": "workhorse"}, {"default_tier": "top"})[
            "default_tier"
        ] == "top"

    def test_plain_lists_replace_rather_than_union(self):
        base = {"caps": {"tiers": ["a", "b"]}}
        merged = og.deep_merge(base, {"caps": {"tiers": ["c"]}})
        assert merged["caps"]["tiers"] == ["c"]

    def test_base_is_not_mutated(self):
        base = {"capacity": {"source": "auto"}}
        og.deep_merge(base, {"capacity": {"source": "none"}})
        assert base["capacity"]["source"] == "auto"


class TestRecordListMerge:
    def test_known_id_patches_in_place(self):
        base = {"tiers": [{"id": "workhorse", "model": "sonnet", "use_for": "most"}]}
        merged = og.deep_merge(base, {"tiers": [{"id": "workhorse", "model": "custom"}]})
        assert merged["tiers"] == [
            {"id": "workhorse", "model": "custom", "use_for": "most"}
        ]

    def test_new_id_appends_and_keeps_order(self):
        base = {"backends": [{"id": "agent"}]}
        merged = og.deep_merge(base, {"backends": [{"id": "mine", "name": "Mine"}]})
        assert [b["id"] for b in merged["backends"]] == ["agent", "mine"]

    def test_disabled_record_is_dropped_by_active(self):
        base = {"backends": [{"id": "agent"}, {"id": "codex"}]}
        merged = og.deep_merge(base, {"backends": [{"id": "codex", "disabled": True}]})
        assert [b["id"] for b in og.active(merged["backends"])] == ["agent"]

    def test_records_without_ids_replace_the_list(self):
        base = {"tiers": [{"id": "workhorse"}]}
        merged = og.deep_merge(base, {"tiers": [{"model": "x"}]})
        assert merged["tiers"] == [{"model": "x"}]


# --------------------------------------------------------------------------
# Layer resolution
# --------------------------------------------------------------------------


@pytest.fixture
def layered(tmp_path, monkeypatch):
    """Redirect all three layers into tmp_path; returns a writer per layer."""
    shipped = tmp_path / "shipped.yaml"
    user = tmp_path / "user.yaml"
    project_root = tmp_path / "project"
    (project_root / ".claude").mkdir(parents=True)

    monkeypatch.setattr(og, "DEFAULTS_PATH", shipped)
    monkeypatch.setattr(og, "user_config_path", lambda: user)

    def write(layer, data):
        target = {
            "shipped": shipped,
            "user": user,
            "project": project_root / ".claude" / og.CONFIG_NAME,
        }[layer]
        target.write_text(yaml.safe_dump(data), encoding="utf-8")

    write.project_root = project_root
    return write


class TestResolveConfig:
    def test_precedence_is_shipped_then_user_then_project(self, layered):
        layered("shipped", {"default_tier": "workhorse", "default_backend": "agent"})
        layered("user", {"default_tier": "high-reasoning"})
        layered("project", {"default_tier": "top"})
        config, _ = og.resolve_config(layered.project_root)
        assert config["default_tier"] == "top"
        assert config["default_backend"] == "agent"  # untouched keys survive

    def test_user_layer_applies_when_project_absent(self, layered):
        layered("shipped", {"default_tier": "workhorse"})
        layered("user", {"default_tier": "cheapest"})
        config, provenance = og.resolve_config(layered.project_root)
        assert config["default_tier"] == "cheapest"
        assert dict((l, s) for l, _, s in provenance)["project"] == "absent"

    def test_provenance_reports_each_layer(self, layered):
        layered("shipped", {"default_tier": "workhorse"})
        layered("user", {})
        config, provenance = og.resolve_config(layered.project_root)
        status = {layer: state for layer, _, state in provenance}
        assert status == {"shipped": "applied", "user": "empty", "project": "absent"}

    def test_comment_only_override_is_empty_not_an_error(self, tmp_path, layered):
        layered("shipped", {"default_tier": "workhorse"})
        (tmp_path / "user.yaml").write_text("# nothing here\n", encoding="utf-8")
        config, _ = og.resolve_config(layered.project_root)
        assert config["default_tier"] == "workhorse"

    def test_non_mapping_layer_is_rejected(self, tmp_path, layered):
        layered("shipped", {"default_tier": "workhorse"})
        (tmp_path / "user.yaml").write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must be a mapping"):
            og.resolve_config(layered.project_root)


class TestShippedDefaults:
    """The shipped layer is the SSOT for the skill's guidance -- keep it valid."""

    def test_shipped_defaults_parse_and_carry_the_expected_shape(self):
        data = yaml.safe_load(og.DEFAULTS_PATH.read_text(encoding="utf-8"))
        assert data["schema_version"] == 1
        tier_ids = [t["id"] for t in data["tiers"]]
        assert data["default_tier"] in tier_ids
        backend_ids = [b["id"] for b in data["backends"]]
        assert data["default_backend"] in backend_ids
        assert {"agent", "codex"} <= set(backend_ids)

    def test_codex_backed_tiers_declare_their_gate(self):
        data = yaml.safe_load(og.DEFAULTS_PATH.read_text(encoding="utf-8"))
        codex_backend = next(b for b in data["backends"] if b["id"] == "codex")
        for tier_id in codex_backend["capabilities"]["tiers"]:
            tier = next(t for t in data["tiers"] if t["id"] == tier_id)
            assert tier.get("backend") == "codex", tier_id

    def test_shipped_policy_mentions_no_codex_when_codex_is_absent(self, monkeypatch, tmp_path):
        """The load-bearing requirement: no Codex content reaches the skill on a
        machine without Codex -- not the backend, not its tiers, not a mention.

        The user and project layers are isolated deliberately: without that, a
        developer's own override file feeds into this guard and it starts
        passing or failing for reasons unrelated to the gating logic.
        """
        monkeypatch.setattr(og.shutil, "which", lambda name: None)
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "no-user-config.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        text = og.render(config, provenance).lower()
        assert "codex" not in text
        assert "sol" not in text.split()
        assert "terra" not in text
        assert "## dispatch backends" in text  # the agent backend still renders

    def test_shipped_capability_flags_are_strings_not_yaml_booleans(self):
        """`network: yes` parses as True and renders as "network: True"."""
        data = yaml.safe_load(og.DEFAULTS_PATH.read_text(encoding="utf-8"))
        for backend in data["backends"]:
            network = (backend.get("capabilities") or {}).get("network")
            assert not isinstance(network, bool), backend["id"]


# --------------------------------------------------------------------------
# Backend detection
# --------------------------------------------------------------------------


class TestDetectBackend:
    def test_always_rule(self):
        assert og.detect_backend({"detect": {"always": True}})[0] is True

    def test_missing_rule_defaults_to_available(self):
        assert og.detect_backend({"id": "x"})[0] is True

    def test_command_not_on_path(self):
        ok, reason = og.detect_backend(
            {"detect": {"command": ["definitely-not-a-real-binary-xyz", "--version"]}}
        )
        assert ok is False
        assert "not found on PATH" in reason

    def test_command_resolves_through_pathext(self, monkeypatch):
        """Windows installs CLIs as foo.cmd, which CreateProcess misses by bare name."""
        seen = {}

        def fake_which(name):
            seen["name"] = name
            return "C:/tools/codex.cmd"

        class Proc:
            returncode = 0
            stdout = b"codex-cli 1.2.3\n"

        monkeypatch.setattr(og.shutil, "which", fake_which)
        monkeypatch.setattr(og.subprocess, "run", lambda argv, **kw: Proc())
        ok, reason = og.detect_backend({"detect": {"command": ["codex", "--version"]}})
        assert (ok, reason) == (True, "codex-cli 1.2.3")
        assert seen["name"] == "codex"

    def test_nonzero_exit_is_unavailable(self, monkeypatch):
        class Proc:
            returncode = 127
            stdout = b""

        monkeypatch.setattr(og.shutil, "which", lambda name: "/usr/bin/thing")
        monkeypatch.setattr(og.subprocess, "run", lambda argv, **kw: Proc())
        ok, reason = og.detect_backend({"detect": {"command": ["thing"]}})
        assert ok is False and "exited 127" in reason

    def test_path_rule(self, tmp_path):
        target = tmp_path / "runner"
        assert og.detect_backend({"detect": {"path": str(target)}})[0] is False
        target.write_text("", encoding="utf-8")
        assert og.detect_backend({"detect": {"path": str(target)}})[0] is True


# --------------------------------------------------------------------------
# Capacity
# --------------------------------------------------------------------------


def snapshot(five=10.0, seven=20.0, age_min=0.0):
    now = time.time() - age_min * 60
    return {
        "captured_at": now,
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": time.time() + 3600},
            "seven_day": {"used_percentage": seven, "resets_at": time.time() + 86400},
        },
    }


class TestLoadSnapshot:
    def test_source_none_disables_reporting(self):
        snap, note = og.load_snapshot({"source": "none"})
        assert snap is None and "disabled" in note

    def test_missing_file_names_the_producer(self, tmp_path):
        snap, note = og.load_snapshot(
            {"source": "auto", "snapshot_path": str(tmp_path / "absent.json")}
        )
        assert snap is None
        assert "claude-ui-kit" in note

    def test_auto_reads_the_snapshot_file(self, tmp_path):
        path = tmp_path / "rate-limits.json"
        path.write_text(json.dumps(snapshot()), encoding="utf-8")
        snap, _ = og.load_snapshot({"source": "auto", "snapshot_path": str(path)})
        assert snap["rate_limits"]["five_hour"]["used_percentage"] == 10.0

    def test_corrupt_snapshot_degrades_instead_of_raising(self, tmp_path):
        path = tmp_path / "rate-limits.json"
        path.write_text("{not json", encoding="utf-8")
        snap, note = og.load_snapshot({"source": "auto", "snapshot_path": str(path)})
        assert snap is None and "not valid JSON" in note

    def test_command_source_parses_stdout(self, monkeypatch):
        class Proc:
            returncode = 0
            stdout = json.dumps(snapshot()).encode()

        monkeypatch.setattr(og.subprocess, "run", lambda argv, **kw: Proc())
        snap, _ = og.load_snapshot({"source": "command", "command": ["probe"]})
        assert "rate_limits" in snap

    def test_command_source_without_command_is_reported(self):
        snap, note = og.load_snapshot({"source": "command"})
        assert snap is None and "no command is configured" in note


class TestWindowRows:
    def test_remaining_is_the_complement_of_used(self):
        rows, _ = og.window_rows(snapshot(five=42.0), {})
        five = next(r for r in rows if r["label"] == "5-hour")
        assert five["remaining"] == 58

    def test_thresholds_classify_windows(self):
        capacity = {"thresholds": {"warn_remaining_pct": 25, "critical_remaining_pct": 10}}
        rows, _ = og.window_rows(snapshot(five=80.0, seven=95.0), capacity)
        states = {r["label"]: r["state"] for r in rows}
        assert states == {"5-hour": "low", "7-day": "CRITICAL"}

    def test_stale_snapshot_is_flagged(self):
        rows, stale = og.window_rows(snapshot(age_min=90), {"max_age_minutes": 30})
        assert rows and stale is not None and "indicative only" in stale

    def test_fresh_snapshot_is_not_flagged(self):
        _, stale = og.window_rows(snapshot(age_min=1), {"max_age_minutes": 30})
        assert stale is None

    def test_null_windows_are_skipped_not_guessed(self):
        raw = {"captured_at": time.time(), "rate_limits": {"five_hour": {"used_percentage": None}}}
        rows, _ = og.window_rows(raw, {})
        assert rows == []


class TestFormatReset:
    @pytest.mark.parametrize(
        "minutes,expected",
        [(-5, "reset due"), (45, "resets in ~45min"), (300, "resets in ~5h"), (5760, "resets in ~4d")],
    )
    def test_scales_units(self, minutes, expected):
        assert og.format_reset(minutes) == expected


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


class TestRender:
    def test_renders_the_shipped_policy_end_to_end(self, layered, monkeypatch):
        monkeypatch.setattr(og, "DEFAULTS_PATH", _shipped_path())
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "## Model tiers" in text
        assert "## Dispatch backends" in text
        assert "## Capacity" in text
        assert "Layers applied: shipped" in text

    def test_unavailable_tier_is_marked_and_instructed_against(self, layered):
        layered(
            "shipped",
            {
                "default_tier": "workhorse",
                "tiers": [{"id": "workhorse", "model": "sonnet"}, {"id": "top", "model": "fable"}],
                "backends": [{"id": "agent", "detect": {"always": True}}],
                "capacity": {"source": "none", "tier_overrides": {"top": "unavailable"}},
            },
        )
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "top (UNAVAILABLE)" in text
        assert "Do not dispatch to `top`" in text

    def test_undetected_backend_leaves_no_trace(self, layered):
        """An absent backend must not be named at all -- mentioning it invites
        dispatch to something that is not installed."""
        layered(
            "shipped",
            {
                "backends": [
                    {"id": "agent", "detect": {"always": True}},
                    {
                        "id": "ghost",
                        "name": "Ghost runner",
                        "detect": {"command": ["no-such-binary-xyz"]},
                        "dispatch": "ghost run --brief <file>",
                        "gotchas": ["ghost eats your homework"],
                    },
                ],
                "capacity": {"source": "none"},
            },
        )
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance).lower()
        assert "ghost" not in text
        assert "agent" in text

    def test_tier_gated_on_a_missing_backend_is_hidden(self, layered):
        layered(
            "shipped",
            {
                "default_tier": "workhorse",
                "tiers": [
                    {"id": "workhorse", "model": "sonnet"},
                    {"id": "gated", "model": "phantom", "backend": "ghost"},
                ],
                "backends": [
                    {"id": "agent", "detect": {"always": True}},
                    {"id": "ghost", "detect": {"command": ["no-such-binary-xyz"]}},
                ],
                "capacity": {"source": "none"},
            },
        )
        config, provenance = og.resolve_config(layered.project_root)
        # Body only: the footer echoes tmp paths, which carry the test's own name.
        body = og.render(config, provenance).split("\n---\n")[0]
        assert "phantom" not in body and "gated" not in body

    def test_tier_gated_on_a_present_backend_is_shown(self, layered):
        layered(
            "shipped",
            {
                "default_tier": "workhorse",
                "tiers": [
                    {"id": "workhorse", "model": "sonnet"},
                    {"id": "gated", "model": "phantom", "backend": "here"},
                ],
                "backends": [{"id": "here", "detect": {"always": True}}],
                "capacity": {"source": "none"},
            },
        )
        config, provenance = og.resolve_config(layered.project_root)
        assert "phantom" in og.render(config, provenance)

    def test_effort_column_appears_only_when_configured(self, layered):
        layered(
            "shipped",
            {
                "tiers": [{"id": "workhorse", "model": "sonnet"}],
                "backends": [{"id": "agent"}],
                "capacity": {"source": "none"},
            },
        )
        config, provenance = og.resolve_config(layered.project_root)
        assert "| Effort |" not in og.render(config, provenance)
        layered(
            "user",
            {"tiers": [{"id": "workhorse", "effort": "medium"}]},
        )
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "| Effort |" in text and "medium" in text

    def test_avoid_when_renders_as_a_negative_instruction(self, layered):
        layered(
            "shipped",
            {
                "tiers": [{"id": "top", "model": "fable", "avoid_when": "the work is patterned"}],
                "backends": [{"id": "agent"}],
                "capacity": {"source": "none"},
            },
        )
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "NOT this tier when" in text and "the work is patterned" in text

    def test_capacity_unknown_says_so_rather_than_guessing(self, layered, tmp_path):
        layered(
            "shipped",
            {
                "backends": [{"id": "agent"}],
                "capacity": {"source": "auto", "snapshot_path": str(tmp_path / "gone.json")},
            },
        )
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "capacity unknown" in text
        assert "Assume nothing" in text

    def test_absent_layers_are_advertised_as_the_way_to_override(self, layered):
        layered("shipped", {"backends": [{"id": "agent"}], "capacity": {"source": "none"}})
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "To change this policy, create:" in text
        assert og.CONFIG_NAME in text


def _shipped_path():
    """The real shipped defaults, for the end-to-end render test."""
    from pathlib import Path

    return (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "awesome-kit"
        / "skills"
        / "orchestrate"
        / "defaults"
        / "orchestration.yaml"
    )


class TestCli:
    def test_paths_lists_three_layers(self, capsys, layered):
        assert og.main(["--paths", "--project-root", str(layered.project_root)]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert [line.split("\t")[0] for line in lines] == ["shipped", "user", "project"]

    def test_explain_prints_provenance_then_resolved_config(self, capsys, layered):
        layered("shipped", {"default_tier": "workhorse"})
        layered("user", {"default_tier": "top"})
        assert og.main(["--explain", "--project-root", str(layered.project_root)]) == 0
        out = capsys.readouterr().out
        assert "shipped  applied" in out
        assert "default_tier: top" in out

    def test_broken_config_exits_nonzero_with_a_reason(self, capsys, layered, tmp_path):
        layered("shipped", {"default_tier": "workhorse"})
        (tmp_path / "user.yaml").write_text("just a string\n", encoding="utf-8")
        assert og.main(["--project-root", str(layered.project_root)]) == 1
        assert "orchestration config error" in capsys.readouterr().err


class TestCapabilityRendering:
    """Capability keys are rendered from an allowlist, so a key added to the
    shipped defaults but not to that list is silently dropped."""

    RENDERED = ("tiers", "isolation", "effort", "network", "returns")

    def test_every_shipped_capability_key_is_rendered(self):
        data = yaml.safe_load(og.DEFAULTS_PATH.read_text(encoding="utf-8"))
        for backend in data["backends"]:
            for key in (backend.get("capabilities") or {}):
                assert key in self.RENDERED, (
                    f"{backend['id']}.capabilities.{key} would not be rendered"
                )


class TestLadders:
    """Tiers group into one ladder per backend. Comparing rungs across ladders
    is the wrong axis -- the decision there is the backend, not the model."""

    CONFIG = {
        "default_backend": "agent",
        "default_tier": "workhorse",
        "tiers": [
            {"id": "workhorse", "model": "sonnet"},
            {"id": "top", "model": "fable"},
            {"id": "other-mid", "model": "terra", "backend": "other"},
            {"id": "other-top", "model": "sol", "backend": "other"},
        ],
        "backends": [
            {"id": "agent", "name": "Agent", "detect": {"always": True}},
            {"id": "other", "name": "Other", "detect": {"always": True}},
        ],
        "capacity": {"source": "none"},
    }

    def test_unmarked_tiers_ride_the_default_backend(self):
        assert og.tier_backend({"id": "workhorse"}, self.CONFIG) == "agent"
        assert og.tier_backend({"id": "x", "backend": "other"}, self.CONFIG) == "other"

    def test_grouped_by_backend_with_default_first(self):
        grouped = og.ladders(self.CONFIG, {"agent", "other"})
        assert [backend for backend, _ in grouped] == ["agent", "other"]
        assert [t["id"] for t in grouped[0][1]] == ["workhorse", "top"]
        assert [t["id"] for t in grouped[1][1]] == ["other-mid", "other-top"]

    def test_a_missing_backend_takes_its_whole_ladder(self):
        grouped = og.ladders(self.CONFIG, {"agent"})
        assert [backend for backend, _ in grouped] == ["agent"]

    def test_render_emits_one_table_per_ladder(self, layered):
        layered("shipped", self.CONFIG)
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "### Agent ladder" in text
        assert "### Other ladder" in text
        assert "tiers compare within a ladder" in text
        assert text.count("| Tier | Model |") == 2

    def test_single_ladder_renders_without_headings(self, layered):
        single = dict(self.CONFIG)
        single["tiers"] = [{"id": "workhorse", "model": "sonnet"}]
        single["backends"] = [{"id": "agent", "name": "Agent", "detect": {"always": True}}]
        layered("shipped", single)
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "ladder" not in text.split("## Dispatch backends")[0]
        assert text.count("| Tier | Model |") == 1

    def test_cross_cutting_prose_renders_once_not_per_ladder(self, layered):
        cfg = dict(self.CONFIG, pool_economics="POOL NOTE")
        layered("shipped", cfg)
        config, provenance = og.resolve_config(layered.project_root)
        assert og.render(config, provenance).count("POOL NOTE") == 1


class TestEffortRendering:
    def test_structured_effort_renders_as_tests(self, layered):
        layered("shipped", {
            "tiers": [{"id": "workhorse", "model": "sonnet"}],
            "backends": [{"id": "agent", "detect": {"always": True}}],
            "capacity": {"source": "none"},
            "effort": {"raise_when": ["it is ambiguous"], "lower_when": ["it is mechanical"],
                       "note": "up-effort before up-tier"},
        })
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "Raise effort when" in text and "it is ambiguous" in text
        assert "Lower effort when" in text and "it is mechanical" in text
        assert "up-effort before up-tier" in text

    def test_legacy_string_effort_still_renders(self, layered):
        """An override written against the old prose schema must not vanish."""
        layered("shipped", {
            "tiers": [{"id": "workhorse"}],
            "backends": [{"id": "agent"}],
            "capacity": {"source": "none"},
            "effort": "just some prose about effort",
        })
        config, provenance = og.resolve_config(layered.project_root)
        assert "just some prose about effort" in og.render(config, provenance)


class TestBackendSelection:
    CONFIG = {
        "default_backend": "agent",
        "tiers": [{"id": "workhorse", "model": "sonnet"}],
        "backends": [
            {"id": "agent", "name": "Agent", "detect": {"always": True}},
            {"id": "ghost", "name": "Ghost", "detect": {"command": ["no-such-binary-xyz"]}},
        ],
        "capacity": {"source": "none"},
        "backend_selection": {
            "default": "agent",
            "gates": [{"test": "needs MCP tools", "backend": "agent", "why": "only it sees them"}],
            "pulls": [{"test": "a whole set at once", "backend": "ghost", "why": "one result file"}],
        },
    }

    def test_gates_and_pulls_render_for_available_backends(self, layered):
        cfg = dict(self.CONFIG)
        cfg["backends"] = [{"id": "agent", "name": "Agent", "detect": {"always": True}},
                           {"id": "ghost", "name": "Ghost", "detect": {"always": True}}]
        layered("shipped", cfg)
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "### Choosing a backend" in text
        assert "needs MCP tools" in text and "a whole set at once" in text
        assert "Default: **Agent**" in text

    def test_rows_naming_a_missing_backend_are_dropped(self, layered):
        """Same omission rule as backends and tiers -- no guidance toward
        something that is not installed."""
        layered("shipped", self.CONFIG)
        config, provenance = og.resolve_config(layered.project_root)
        body = og.render(config, provenance).split("\n---\n")[0].lower()
        assert "a whole set at once" not in body
        assert "ghost" not in body
        assert "needs mcp tools" in body

    def test_section_omitted_entirely_when_no_rows_survive(self, layered):
        cfg = dict(self.CONFIG)
        cfg["backend_selection"] = {"default": "agent", "gates": [], "pulls": [
            {"test": "x", "backend": "ghost"}]}
        layered("shipped", cfg)
        config, provenance = og.resolve_config(layered.project_root)
        assert "Choosing a backend" not in og.render(config, provenance)

    def test_absent_block_is_not_an_error(self, layered):
        cfg = {k: v for k, v in self.CONFIG.items() if k != "backend_selection"}
        layered("shipped", cfg)
        config, provenance = og.resolve_config(layered.project_root)
        assert "Choosing a backend" not in og.render(config, provenance)


class TestShippedBackendSelection:
    def test_shipped_selection_names_only_declared_backends(self):
        data = yaml.safe_load(og.DEFAULTS_PATH.read_text(encoding="utf-8"))
        ids = {b["id"] for b in data["backends"]}
        sel = data["backend_selection"]
        assert sel["default"] in ids
        for row in sel["gates"] + sel["pulls"]:
            assert row["backend"] in ids, row


class TestImplementationRouting:
    """Implementation routes on specification quality; the tier table's
    reasoning axis is the wrong one for code."""

    BLOCK = {
        "routing": [
            {"spec": "unambiguous change", "tier": "workhorse"},
            {"spec": "new but well specified", "tier": "high-reasoning"},
            {"spec": "not specified", "tier": "none", "action": "specify it first"},
        ],
        "single_unit": "one unit at high-reasoning when not novel",
        "top_tier": "the top tier is NOT an implementation tier",
    }

    def _render(self, layered, block):
        layered("shipped", {
            "tiers": [{"id": "workhorse", "model": "sonnet"}],
            "backends": [{"id": "agent", "detect": {"always": True}}],
            "capacity": {"source": "none"},
            "implementation": block,
        })
        config, provenance = og.resolve_config(layered.project_root)
        return og.render(config, provenance)

    def test_routes_render_with_their_tiers(self, layered):
        text = self._render(layered, self.BLOCK)
        assert "unambiguous change -> `workhorse`" in text
        assert "new but well specified -> `high-reasoning`" in text

    def test_unspecified_work_routes_to_specify_not_to_a_tier(self, layered):
        text = self._render(layered, self.BLOCK)
        assert "not specified -> **specify first**" in text
        assert "specify it first" in text

    def test_single_unit_and_top_tier_notes_render(self, layered):
        text = self._render(layered, self.BLOCK)
        assert "one unit at high-reasoning when not novel" in text
        assert "the top tier is NOT an implementation tier" in text

    def test_legacy_string_form_still_renders(self, layered):
        assert "just prose" in self._render(layered, "just prose")

    def test_absent_block_is_not_an_error(self, layered):
        layered("shipped", {
            "tiers": [{"id": "workhorse"}],
            "backends": [{"id": "agent"}],
            "capacity": {"source": "none"},
        })
        config, provenance = og.resolve_config(layered.project_root)
        assert "Implementation" not in og.render(config, provenance)

    def test_shipped_routing_targets_are_real_tiers(self):
        data = yaml.safe_load(og.DEFAULTS_PATH.read_text(encoding="utf-8"))
        ids = {t["id"] for t in data["tiers"]}
        for row in data["implementation"]["routing"]:
            if row["tier"] != "none":
                assert row["tier"] in ids, row


class TestGateLeaks:
    """Regressions for confirmed leaks of gated content into the guidance.
    Each of these rendered an absent backend's tier by name."""

    def _cfg(self, **over):
        cfg = {
            "default_backend": "agent",
            "tiers": [
                {"id": "workhorse", "model": "sonnet"},
                {"id": "gated", "model": "phantom", "backend": "ghost"},
            ],
            "backends": [
                {"id": "agent", "name": "Agent", "detect": {"always": True},
                 "capabilities": {"tiers": ["workhorse", "gated"]}},
                {"id": "ghost", "detect": {"command": ["no-such-binary-xyz"]}},
            ],
            "capacity": {"source": "none"},
        }
        cfg.update(over)
        return cfg

    def _body(self, layered, cfg):
        layered("shipped", cfg)
        config, provenance = og.resolve_config(layered.project_root)
        return og.render(config, provenance).split("\n---\n")[0]

    def test_tier_override_naming_a_gated_tier_does_not_leak_it(self, layered):
        cfg = self._cfg(capacity={"source": "none", "tier_overrides": {"gated": "unavailable"}})
        body = self._body(layered, cfg)
        assert "gated" not in body and "phantom" not in body

    def test_tier_override_on_a_visible_tier_still_renders(self, layered):
        cfg = self._cfg(capacity={"source": "none", "tier_overrides": {"workhorse": "unavailable"}})
        body = self._body(layered, cfg)
        assert "workhorse (UNAVAILABLE)" in body
        assert "Do not dispatch to" in body

    def test_backend_capabilities_do_not_advertise_a_gated_tier(self, layered):
        assert "gated" not in self._body(layered, self._cfg())

    def test_backend_capabilities_do_not_advertise_a_disabled_tier(self, layered):
        cfg = self._cfg()
        cfg["tiers"][0] = {"id": "workhorse", "model": "sonnet", "disabled": True}
        assert "workhorse" not in self._body(layered, cfg)


class TestDetectFailsClosed:
    """An undetectable backend must vanish, not render. Failing open would
    advertise mechanics for a tool that is not installed."""

    def test_always_false_is_unavailable(self):
        ok, reason = og.detect_backend({"detect": {"always": False}})
        assert ok is False and "always" in reason

    def test_non_mapping_detect_is_unavailable(self):
        ok, reason = og.detect_backend({"detect": "codex --version"})
        assert ok is False and "malformed" in reason

    def test_empty_detect_mapping_is_unavailable(self):
        ok, reason = og.detect_backend({"detect": {}})
        assert ok is False and "no recognized rule" in reason

    def test_absent_detect_key_still_means_available(self):
        assert og.detect_backend({"id": "x"})[0] is True


class TestProjectLayerCannotExecute:
    """The project layer is a file in whatever repo is the cwd; honoring an
    executable field from it would run that repo's chosen program on render."""

    def test_project_detect_command_is_stripped(self, layered):
        layered("shipped", {"tiers": [{"id": "w"}],
                            "backends": [{"id": "agent", "detect": {"always": True}}],
                            "capacity": {"source": "none"}})
        layered("project", {"backends": [{"id": "evil", "detect": {"command": ["calc.exe"]}}]})
        config, provenance = og.resolve_config(layered.project_root)
        evil = next(b for b in config["backends"] if b["id"] == "evil")
        assert "command" not in evil["detect"]
        assert any("executable field" in status for _, _, status in provenance)

    def test_project_capacity_command_is_stripped(self, layered):
        layered("shipped", {"tiers": [{"id": "w"}], "backends": [{"id": "agent"}],
                            "capacity": {"source": "command", "command": ["safe"]}})
        layered("project", {"capacity": {"command": ["evil.exe"]}})
        config, _ = og.resolve_config(layered.project_root)
        assert config["capacity"]["command"] == ["safe"]

    def test_user_layer_may_still_declare_commands(self, layered):
        """Machine-level trust is a different question from repo-level trust."""
        layered("shipped", {"tiers": [{"id": "w"}], "backends": [{"id": "agent"}],
                            "capacity": {"source": "none"}})
        layered("user", {"capacity": {"command": ["my-probe"]}})
        config, _ = og.resolve_config(layered.project_root)
        assert config["capacity"]["command"] == ["my-probe"]

    def test_a_harmless_project_layer_is_untouched(self, layered):
        layered("shipped", {"tiers": [{"id": "w", "model": "a"}], "backends": [{"id": "agent"}],
                            "capacity": {"source": "none"}})
        layered("project", {"tiers": [{"id": "w", "model": "b"}]})
        config, provenance = og.resolve_config(layered.project_root)
        assert config["tiers"][0]["model"] == "b"
        assert not any("executable field" in s for _, _, s in provenance)


class TestHostileCapacityInput:
    """load_snapshot degrades carefully on bad input; window_rows must not
    undo that by assuming shapes."""

    def test_null_thresholds_does_not_crash(self):
        snap = {"captured_at": time.time(),
                "rate_limits": {"five_hour": {"used_percentage": 10}}}
        rows, _ = og.window_rows(snap, {"thresholds": None})
        assert rows and rows[0]["remaining"] == 90

    def test_non_mapping_rate_limits_degrades(self):
        rows, stale = og.window_rows({"captured_at": 0, "rate_limits": ["oops"]}, {})
        assert rows == [] and stale is None

    def test_missing_rate_limits_degrades(self):
        assert og.window_rows({"captured_at": 0}, {})[0] == []
