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
import re
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

    def test_record_lists_contains_every_expected_key(self):
        # Pins the exact set: dropping any of these silently flips that key's
        # override merge from patch-by-id to outright replace, with no other
        # test to catch the regression.
        assert set(og.RECORD_LISTS) == {
            "tiers",
            "backends",
            "lexicon",
            "ladders",
            "rungs",
            "tests",
            "gates",
            "pulls",
            "items",
            "notes",
            "examples",
            "backend_notes",
        }


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


def shipped():
    return yaml.safe_load(og.DEFAULTS_PATH.read_text(encoding="utf-8"))


class TestShippedDefaults:
    """The shipped layer is the SSOT for the skill's guidance -- keep it valid."""

    def test_shipped_defaults_parse_and_carry_the_expected_shape(self):
        data = shipped()
        assert data["schema_version"] == 2
        backend_ids = [b["id"] for b in data["backends"]]
        assert data["default_backend"] in backend_ids
        assert {"agent", "codex"} <= set(backend_ids)
        assert [l["id"] for l in data["ladders"]] == ["agent", "codex"]
        assert data["resolution"]

    def test_every_ladder_names_a_declared_backend(self):
        data = shipped()
        ids = {b["id"] for b in data["backends"]}
        for ladder in data["ladders"]:
            assert ladder["id"] in ids, ladder["id"]

    def test_backend_capability_tier_lists_name_that_backends_rungs(self):
        data = shipped()
        for backend in data["backends"]:
            declared = (backend.get("capabilities") or {}).get("tiers")
            if not declared:
                continue
            ladder = next(l for l in data["ladders"] if l["id"] == backend["id"])
            rungs = {r["id"] for r in ladder["rungs"]}
            assert set(declared) <= rungs, backend["id"]

    def test_exactly_one_terminal_rung_per_ladder_and_it_is_last(self):
        """Ordered elimination needs a rung that is unreachable except by
        falling through -- and it can only be the last one."""
        for ladder in shipped()["ladders"]:
            terminal = [r["id"] for r in ladder["rungs"] if r.get("terminal")]
            assert terminal == [ladder["rungs"][-1]["id"]], ladder["id"]

    def test_only_a_terminal_rung_may_state_no_criteria(self):
        for ladder in shipped()["ladders"]:
            for rung in ladder["rungs"]:
                if not rung.get("criteria"):
                    assert rung.get("terminal"), rung["id"]

    def test_every_criterion_names_a_skill_term(self):
        data = shipped()
        skills = {t["id"] for t in data["lexicon"] if t.get("kind") == "skill"}
        for ladder in data["ladders"]:
            for rung in ladder["rungs"]:
                if rung.get("shape"):
                    assert rung["shape"] in skills, rung["id"]
                for group in rung.get("criteria") or []:
                    ids = group["terms"] if isinstance(group, dict) else group
                    assert set(ids) <= skills, rung["id"]

    def test_glossed_terms_carry_a_gloss_and_bare_terms_do_not(self):
        for term in shipped()["lexicon"]:
            if term.get("kind") != "skill":
                continue
            if term.get("render") == "glossed":
                assert term.get("gloss"), term["id"]
            else:
                assert not term.get("gloss"), term["id"]

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

    def test_batch_launcher_is_wrapped_in_cmd(self, monkeypatch):
        """A .cmd is not an executable -- CreateProcess refuses it bare.

        Without the wrap, a CLI installed by npm or scoop reports as absent on
        exactly the machines where it IS installed.
        """
        captured = {}

        class Proc:
            returncode = 0
            stdout = b"codex-cli 1.2.3\n"

        def fake_run(argv, **kw):
            captured["argv"] = list(argv)
            return Proc()

        monkeypatch.setattr(og.os, "name", "nt")
        monkeypatch.setattr(og.shutil, "which", lambda name: "C:/tools/codex.cmd")
        monkeypatch.setattr(og.subprocess, "run", fake_run)
        ok, _ = og.detect_backend({"detect": {"command": ["codex", "--version"]}})
        assert ok is True
        assert captured["argv"][:3] == ["cmd", "/c", "C:/tools/codex.cmd"]

    def test_plain_executable_is_not_wrapped(self, monkeypatch):
        captured = {}

        class Proc:
            returncode = 0
            stdout = b"thing 1.0\n"

        def fake_run(argv, **kw):
            captured["argv"] = list(argv)
            return Proc()

        monkeypatch.setattr(og.os, "name", "nt")
        monkeypatch.setattr(og.shutil, "which", lambda name: "C:/tools/thing.exe")
        monkeypatch.setattr(og.subprocess, "run", fake_run)
        og.detect_backend({"detect": {"command": ["thing", "--version"]}})
        assert captured["argv"][0] == "C:/tools/thing.exe"

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


def cfg(**over):
    """A minimal but complete decision-tree config."""
    base = {
        "resolution": "first match wins",
        "lexicon": [
            {"id": "known", "kind": "skill", "render": "glossed",
             "gloss": "describe done before doing it"},
            {"id": "open", "kind": "skill", "render": "bare"},
            {"id": "novel", "kind": "skill", "render": "glossed", "gloss": "no pattern applies"},
            {"id": "mechanical", "kind": "concept"},
        ],
        "shape": {
            "title": "Shape the unit",
            "tests": [{"id": "axes", "text": "{known} or {open}."}],
        },
        "ladders": [
            {
                "id": "agent",
                "label": "Claude",
                "rungs": [
                    {"id": "top", "model": "fable", "criteria": [["novel"]]},
                    {"id": "workhorse", "model": "sonnet", "criteria": [], "terminal": True,
                     "text": "terminal default."},
                ],
                "guards": ["There is no haiku rung."],
            }
        ],
        "backends": [{"id": "agent", "name": "Agent", "detect": {"always": True}}],
        "capacity": {"source": "none"},
    }
    base.update(over)
    return base


class TestRender:
    def test_renders_the_shipped_policy_end_to_end(self, layered, monkeypatch):
        monkeypatch.setattr(og, "DEFAULTS_PATH", _shipped_path())
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "## 1. Shape the unit" in text
        assert "## Dispatch backends" in text
        assert "## Capacity" in text
        assert "Layers applied: shipped" in text

    def test_unavailable_rung_is_marked_and_instructed_against(self, layered):
        layered("shipped", cfg(capacity={"source": "none", "tier_overrides": {"top": "unavailable"}}))
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "`top`: unavailable" in text
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

    def test_ladder_gated_on_a_missing_backend_is_hidden(self, layered):
        ghost = {
            "id": "ghost",
            "label": "Ghost",
            "rungs": [{"id": "gated", "model": "phantom", "criteria": [], "terminal": True}],
        }
        layered("shipped", cfg(
            ladders=cfg()["ladders"] + [ghost],
            backends=[
                {"id": "agent", "name": "Agent", "detect": {"always": True}},
                {"id": "ghost", "detect": {"command": ["no-such-binary-xyz"]}},
            ],
        ))
        config, provenance = og.resolve_config(layered.project_root)
        # Body only: the footer echoes tmp paths, which carry the test's own name.
        body = og.render(config, provenance).split("\n---\n")[0]
        assert "phantom" not in body and "gated" not in body

    def test_ladder_on_a_present_backend_is_shown(self, layered):
        here = {
            "id": "here",
            "label": "Here",
            "rungs": [{"id": "gated", "model": "phantom", "criteria": [], "terminal": True}],
        }
        layered("shipped", cfg(
            ladders=cfg()["ladders"] + [here],
            backends=[
                {"id": "agent", "name": "Agent", "detect": {"always": True}},
                {"id": "here", "name": "Here", "detect": {"always": True}},
            ],
        ))
        config, provenance = og.resolve_config(layered.project_root)
        assert "phantom" in og.render(config, provenance)

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

    def test_every_shipped_capability_key_is_rendered(self):
        data = yaml.safe_load(og.DEFAULTS_PATH.read_text(encoding="utf-8"))
        for backend in data["backends"]:
            for key in (backend.get("capabilities") or {}):
                assert key in og.CAPABILITY_KEYS, (
                    f"{backend['id']}.capabilities.{key} would not be rendered"
                )


class TestOrderedElimination:
    """Rungs are tested in order and the first match wins. The semantics are
    not inferable from the content, so they are stated at the top; the order
    itself is data, so a renderer must not reorder it."""

    def test_resolution_is_stated_before_any_block(self, layered):
        layered("shipped", cfg())
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "**Resolution.** first match wins" in text
        assert text.index("Resolution.") < text.index("## 1.")

    def test_rungs_render_in_declared_order_and_are_numbered(self, layered):
        layered("shipped", cfg())
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "1. **fable**" in text
        assert "2. **sonnet**" in text
        assert text.index("**fable**") < text.index("**sonnet**")

    def test_shipped_ladders_keep_their_principle_order(self, layered, monkeypatch):
        monkeypatch.setattr(og, "DEFAULTS_PATH", _shipped_path())
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        for earlier, later in (("**fable**", "**opus**"), ("**opus**", "**sonnet**")):
            assert text.index(earlier) < text.index(later), (earlier, later)

    def test_blocks_render_in_principle_order(self, layered, monkeypatch):
        monkeypatch.setattr(og, "DEFAULTS_PATH", _shipped_path())
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        expected = ["Shape the unit", "Backend", "Tier", "Agent type", "Effort", "Announce"]
        headings = [line.split(". ", 1)[1] for line in text.splitlines()
                    if re.match(r"^## \d+\. ", line)]
        assert [h.split(" ")[0] for h in headings] == [e.split(" ")[0] for e in expected]

    def test_block_numbering_closes_the_gap_when_a_block_drops(self, monkeypatch, tmp_path):
        monkeypatch.setattr(og.shutil, "which", lambda name: None)
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        text = og.render(config, provenance)
        numbers = [int(m.group(1)) for m in re.finditer(r"^## (\d+)\. ", text, re.M)]
        assert numbers == list(range(1, len(numbers) + 1))


class TestGlossing:
    """Every orchestration is a fresh read, so a term whose natural reading
    diverges from its test carries its gloss once, at first occurrence."""

    def _text(self, monkeypatch, tmp_path, with_codex=True):
        if not with_codex:
            monkeypatch.setattr(og.shutil, "which", lambda name: None)
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        return og.render(config, provenance).split("\n## Dispatch backends")[0]

    def _glossed(self, term):
        return {t["id"]: t for t in shipped()["lexicon"]}[term].get("gloss")

    @pytest.mark.parametrize("with_codex", [True, False])
    def test_every_rendered_glossed_term_is_glossed_exactly_once(
        self, monkeypatch, tmp_path, with_codex
    ):
        text = self._text(monkeypatch, tmp_path, with_codex)
        for term in shipped()["lexicon"]:
            if term.get("kind") != "skill" or term.get("render") != "glossed":
                continue
            gloss = f"`{term['id']}` ({term['gloss']})"
            occurrences = text.count(gloss)
            if f"`{term['id']}`" in text:
                assert occurrences == 1, f"{term['id']} glossed {occurrences}x"
            else:
                assert occurrences == 0, term["id"]

    @pytest.mark.parametrize("with_codex", [True, False])
    def test_a_gloss_precedes_every_bare_use_of_its_term(
        self, monkeypatch, tmp_path, with_codex
    ):
        text = self._text(monkeypatch, tmp_path, with_codex)
        for term in shipped()["lexicon"]:
            if term.get("kind") != "skill" or term.get("render") != "glossed":
                continue
            marker = f"`{term['id']}`"
            if marker not in text:
                continue
            first = text.index(marker)
            gloss_at = text.index(f"{marker} ({term['gloss']})")
            assert gloss_at == first, term["id"]

    def test_a_glossed_terms_gloss_lands_in_a_block_both_variants_render(
        self, monkeypatch, tmp_path
    ):
        """A term used in both variants may not have its only gloss inside a
        Codex-only block, or the Codex-absent reader never sees it."""
        without = self._text(monkeypatch, tmp_path, with_codex=False)
        for term in shipped()["lexicon"]:
            if term.get("kind") != "skill" or term.get("render") != "glossed":
                continue
            if f"`{term['id']}`" in without:
                assert f"`{term['id']}` ({term['gloss']})" in without, term["id"]

    def test_bare_terms_never_carry_a_gloss(self, monkeypatch, tmp_path):
        text = self._text(monkeypatch, tmp_path)
        assert "`abortable` (" not in text
        assert "`schema` (" not in text

    def test_second_occurrence_is_bare(self, layered):
        layered("shipped", cfg(shape={
            "title": "Shape",
            "tests": [{"id": "a", "text": "{known} first."}, {"id": "b", "text": "{known} again."}],
        }))
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "`known` (describe done before doing it) first." in text
        assert "`known` again." in text

    def test_concept_terms_never_reach_the_artifact(self, monkeypatch, tmp_path):
        """A concept term selects no branch, so rendering one as a criterion
        would put an unactionable word in a decision position."""
        text = self._text(monkeypatch, tmp_path)
        for term in shipped()["lexicon"]:
            if term.get("kind") == "concept":
                assert f"`{term['id']}`" not in text, term["id"]


class TestNegativeGuards:
    """A rung something must NOT be used for, and a rung that does not exist,
    are decisions rather than rationale: without them a reader who knows the
    model exists invents the dispatch."""

    def _both(self, monkeypatch, tmp_path):
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        with_codex = og.render(config, provenance)
        monkeypatch.setattr(og.shutil, "which", lambda name: None)
        config, provenance = og.resolve_config(tmp_path / "no-project")
        return with_codex, og.render(config, provenance)

    def test_backend_independent_guards_render_in_both_variants(self, monkeypatch, tmp_path):
        with_codex, without = self._both(monkeypatch, tmp_path)
        for guard in (
            "There is no haiku rung.",
            "Any doubt resolves to the rung below.",
            "Never down-tier a unit that meets the fable bar to harvest the discount.",
            "Never this rung:",
            "is never a rung criterion",
        ):
            assert guard in with_codex, guard
            assert guard in without, guard

    def test_every_shipped_guard_string_reaches_the_with_codex_variant(
        self, monkeypatch, tmp_path
    ):
        with_codex, _ = self._both(monkeypatch, tmp_path)
        for ladder in shipped()["ladders"]:
            for guard in ladder.get("guards") or []:
                assert og.fold(guard).split(" -- ")[0].rstrip(".") in with_codex
            for rung in ladder["rungs"]:
                for guard in rung.get("guards") or []:
                    assert og.fold(guard).split(" --")[0].rstrip(".") in with_codex

    def test_guards_render_without_a_render_required_flag(self, layered):
        """render_required is a backstop, not the mechanism -- an untagged
        guard still renders."""
        ladder = dict(cfg()["ladders"][0], guards=["No untagged guard may vanish."])
        layered("shipped", cfg(ladders=[ladder]))
        config, provenance = og.resolve_config(layered.project_root)
        assert "No untagged guard may vanish." in og.render(config, provenance)


class TestRenderScope:
    """`render_scope: principles-only` marks genuine policy that is not a
    per-unit routing decision, so it does not earn tokens in a file read once
    per orchestration."""

    @staticmethod
    def _probe(raw: str) -> str:
        """A probe string as it would ACTUALLY appear in rendered output.

        Two traps, both hit for real:
        - `Terms.fill()` rewrites every `{term}` reference on every render
          path, so probing the raw YAML text asserts on a string the renderer
          can never emit; the assertion then passes whether or not the record
          rendered.
        - Expanding through `Terms` does not fix it either, because glossing is
          stateful: a fresh `Terms` yields the glossed first-occurrence form,
          while the same term renders bare once seen earlier in the document.

        So probe on the longest BRACE-FREE literal run instead -- text the
        renderer passes through untouched regardless of gloss state.
        """
        literal = max(re.split(r"\{[a-z][a-z0-9-]*\}", raw), key=len)
        probe = og.fold(literal).strip(" :;,.-")[:40]
        assert len(probe) >= 12, f"no usable brace-free probe in {raw!r}"
        return probe

    def _principles_only_probes(self):
        for test in shipped()["shape"]["tests"]:
            if test.get("render_scope") == "principles-only":
                yield test["id"], self._probe(test["text"])
        for ladder in shipped()["ladders"]:
            for note in ladder.get("notes") or []:
                if note.get("render_scope") == "principles-only":
                    yield note["id"], self._probe(note["text"])

    def test_principles_only_records_do_not_render(self, monkeypatch, tmp_path):
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        text = og.render(config, provenance)
        probes = list(self._principles_only_probes())
        for record_id, probe in probes:
            assert probe not in text, record_id
        assert len(probes) >= 2, (
            "the shipped data should still carry principles-only records"
        )

    def test_the_principles_only_probes_are_not_vacuous(self, monkeypatch, tmp_path):
        """Positive control: with the flag cleared, every probe MUST appear.

        Without this, the assertion above degrades silently the moment a probe
        stops matching the rendered form -- which is exactly how it was broken
        before (it probed unexpanded `{term}` braces).
        """
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        for record in list(config["shape"]["tests"]) + [
            n for lad in config["ladders"] for n in (lad.get("notes") or [])
        ]:
            record.pop("render_scope", None)
        text = og.render(config, provenance)
        for record_id, probe in self._principles_only_probes():
            assert probe in text, (
                f"probe for {record_id!r} never appears even when rendered -- "
                "the negative assertion is vacuous"
            )

    def test_a_principles_only_record_is_still_merged_into_the_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, _ = og.resolve_config(tmp_path / "no-project")
        ids = [t["id"] for t in config["shape"]["tests"]]
        assert "who-authors-the-specification" in ids

    def test_renders_helper_only_rejects_principles_only(self):
        assert og.renders({"id": "x"}) is True
        assert og.renders({"id": "x", "render_scope": "principles-only"}) is False
        assert og.renders("a plain string") is True


class TestCodexAbsentVariant:
    """One source, two variants. Without Codex the backend block has nothing
    to choose and the Codex ladder cannot be dispatched to."""

    @pytest.fixture
    def variants(self, monkeypatch, tmp_path):
        """Both variants from one source. Rendered with-Codex FIRST: the
        without-Codex run patches `which` for the rest of the test."""
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        present = og.render(config, provenance)
        monkeypatch.setattr(og.shutil, "which", lambda name: None)
        config, provenance = og.resolve_config(tmp_path / "no-project")
        return present, og.render(config, provenance)

    @pytest.fixture
    def with_codex(self, variants):
        return variants[0]

    @pytest.fixture
    def without(self, variants):
        return variants[1]

    def test_backend_block_is_omitted_entirely(self, without):
        assert "## 2. Backend" not in without
        assert "Gates --" not in without
        assert "Pulls --" not in without

    def test_backend_block_renders_when_codex_is_present(self, with_codex):
        assert "## 2. Backend" in with_codex
        assert "Gates --" in with_codex and "Pulls --" in with_codex

    def test_codex_ladder_and_its_rungs_disappear(self, without):
        for probe in ("Codex ladder", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra"):
            assert probe not in without, probe

    def test_ladder_headings_appear_only_when_more_than_one_renders(self, without, with_codex):
        assert "### Claude ladder" in with_codex
        assert "### " not in without.split("## Dispatch backends")[0]

    def test_fan_out_collapse_test_survives_without_codex(self, without):
        assert "`fan-out`" in without
        assert "collapses into a single shell command" in without

    def test_the_hole_is_disclosed_in_one_clause(self, without, with_codex):
        """Silence about a known gap reads as an oversight and invites the
        reader to invent the answer the collapse test exists to prevent."""
        assert "sequence the units or handle them inline" in without
        assert "sequence the units or handle them inline" not in with_codex

    def test_codex_only_effort_and_announce_notes_drop(self, without, with_codex):
        assert "effort is a real dial" in with_codex
        assert "effort is a real dial" not in without
        assert "the pull term instead" in with_codex
        assert "the pull term instead" not in without

    def test_claude_side_effort_asymmetry_survives_in_both(self, without, with_codex):
        for text in (without, with_codex):
            assert "NOT dialable per call" in text
            assert "opts.effort" in text

    def test_plan_checkpoint_shape_tests_render_in_both_variants(self, without, with_codex):
        """P0.6-P0.8 live in shaping, which both variants render."""
        for text in (without, with_codex):
            assert "Route the plan itself through this tree" in text
            assert "not an independent reviewer" in text
            assert "defaults to TWO units" in text

    def test_second_family_hole_is_disclosed_without_codex(self, without, with_codex):
        """Same convention as the fan-out hole: the P0.8 test renders in the
        Codex-absent variant with its one-clause disclosure, not silently."""
        assert "dispatch the primary review alone" in without
        assert "dispatch the primary review alone" not in with_codex

    def test_plan_announce_examples_follow_backend_presence(self, without, with_codex):
        assert "delegating plan review to fable" in with_codex
        assert "delegating plan review to fable" in without
        assert "plan review second opinion" in with_codex
        assert "plan review second opinion" not in without


class TestNoBareCodenames:
    """The bare codenames are not dispatchable, and a policy that names them
    is a policy that fails every time it is followed.

    SHAPE ONLY. These assertions prove an id is well-FORMED, never that it is
    dispatchable -- a renamed or retired model passes every one of them. The
    live check is `scripts/check_model_dispatch.py`, which dispatches a trivial
    prompt to every rung's model and validates the `-c` config keys with
    `--strict-config`. It cannot live here: it needs network, a login, and real
    usage. Run it by hand when the ladders or the dispatch flags change.
    """

    CODENAMES = ("luna", "terra", "sol")

    @pytest.mark.parametrize("with_codex", [True, False])
    def test_no_unqualified_codename_anywhere_in_the_output(
        self, monkeypatch, tmp_path, with_codex
    ):
        if not with_codex:
            monkeypatch.setattr(og.shutil, "which", lambda name: None)
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        text = og.render(config, provenance)
        for name in self.CODENAMES:
            # A codename is legal only as the tail of a fully qualified id.
            for match in re.finditer(rf"\b{name}\b", text, re.I):
                prefix = text[max(0, match.start() - 8):match.start()]
                assert prefix.endswith("gpt-5.6-"), f"bare `{name}` at {match.start()}"

    def test_shipped_data_never_writes_a_bare_codename(self):
        raw = og.DEFAULTS_PATH.read_text(encoding="utf-8")
        for name in self.CODENAMES:
            for match in re.finditer(rf"\b{name}\b", raw):
                prefix = raw[max(0, match.start() - 8):match.start()]
                assert prefix.endswith("gpt-5.6-"), f"bare `{name}` in the shipped data"


class TestRequestOnlyBackend:
    """Grok is present-but-not-eligible: fully documented so a user-named
    dispatch can be driven correctly, and reachable by nothing else.

    Two independent mechanisms carry that, and both are asserted here because
    either alone leaks. No ladder keeps it out of every TIER decision; the
    `selection` line keeps it out of the BACKEND decision, which happens first
    and would otherwise see a third backend carrying a ready-made command.
    """

    ONLY_MODEL = "grok-4.6"

    @staticmethod
    def _render(monkeypatch, tmp_path, *, present):
        """Force grok's detection either way rather than patching `shutil.which`.

        `detect_backend` RUNS the command, so a faked PATH hit still fails the
        probe -- and the honest version (let the real machine decide) makes the
        present-variant assertions pass only where grok happens to be
        installed. Stubbing the one backend's verdict keeps both variants true
        on every machine, including CI.
        """
        real = og.detect_backend

        def detect(backend):
            if backend.get("id") == "grok":
                return (present, "stubbed") if present else (False, "stubbed absent")
            return real(backend)

        monkeypatch.setattr(og, "detect_backend", detect)
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        return og.render(config, provenance)

    def test_shipped_grok_record_has_no_ladder_and_claims_no_tiers(self):
        data = shipped()
        grok = next(b for b in data["backends"] if b["id"] == "grok")
        assert not (grok.get("capabilities") or {}).get("tiers")
        assert "grok" not in {l["id"] for l in data["ladders"]}
        assert grok.get("selection"), "a ladder-less backend must state its condition"

    def test_shipped_data_names_only_the_sanctioned_grok_model(self):
        """`grok models` also offers 4.5. Naming it anywhere in the policy is
        one copy-paste away from dispatching it."""
        raw = og.DEFAULTS_PATH.read_text(encoding="utf-8")
        for match in re.finditer(r"\bgrok-[0-9][^\s`'\"]*", raw):
            assert match.group(0) == self.ONLY_MODEL, match.group(0)

    def test_the_record_renders_with_its_selection_line_when_detected(
        self, monkeypatch, tmp_path
    ):
        text = self._render(monkeypatch, tmp_path, present=True)
        assert "Grok CLI" in text
        assert "**Selection.**" in text
        assert self.ONLY_MODEL in text
        # No ladder -> the capability line says so rather than naming rungs.
        assert "n/a (no tier selection)" in text

    def test_selection_precedes_the_mechanics(self, monkeypatch, tmp_path):
        """A reader who has reached the command has already decided to launch."""
        text = self._render(monkeypatch, tmp_path, present=True)
        assert text.index("**Selection.**") < text.index("--always-approve")

    def test_the_whole_record_disappears_when_grok_is_absent(
        self, monkeypatch, tmp_path
    ):
        text = self._render(monkeypatch, tmp_path, present=False)
        for probe in ("Grok CLI", "grok-4.6", "--always-approve", "--no-subagents"):
            assert probe not in text, probe

    def test_grok_is_named_by_no_gate_pull_or_rung(self):
        """The decision tree must not reach it -- being listed is not being
        eligible, and a pull naming it would make it eligible."""
        data = shipped()
        block = data["backend"]
        for row in (block.get("gates") or []) + (block.get("pulls") or []):
            assert row["backend"] != "grok", row["id"]
        for ladder in data["ladders"]:
            for rung in ladder["rungs"]:
                assert "grok" not in str(rung.get("model", "")), rung["id"]

    def test_selection_is_optional_and_absent_backends_render_no_such_line(self):
        """Only a restricted backend gets the heading; the others must not
        acquire one by accident."""
        data = shipped()
        for backend in data["backends"]:
            if backend["id"] != "grok":
                assert "selection" not in backend, backend["id"]


class TestUserSpokenCodenames:
    """A user says `sol`; `-m` needs `gpt-5.6-sol`. The policy has to carry the
    RESOLUTION, not only the prohibition -- and has to carry it without writing
    a bare codename into shipped data (see TestNoBareCodenames)."""

    def test_the_codex_ladder_states_how_a_spoken_codename_resolves(self):
        guards = " ".join(
            next(l for l in shipped()["ladders"] if l["id"] == "codex")["guards"]
        )
        assert "gpt-5.6-" in guards
        assert "resolve" in guards.lower()

    def test_the_resolution_rule_renders(self, monkeypatch, tmp_path):
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        text = og.render(config, provenance)
        assert "resolve it to the `gpt-5.6-` form" in text


class TestRungRendering:
    def test_or_groups_and_conjunctions_render(self, layered):
        rung = {
            "id": "top", "model": "fable",
            "criteria": [["known", "novel"], ["open"]],
        }
        ladder = dict(cfg()["ladders"][0])
        ladder["rungs"] = [rung, cfg()["ladders"][0]["rungs"][1]]
        layered("shipped", cfg(ladders=[ladder]))
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "+ `novel` (no pattern applies); or `open`" in text

    def test_a_where_clause_qualifies_only_its_group(self, layered):
        rung = {"id": "top", "model": "fable",
                "criteria": [["open"], {"terms": ["novel"], "where": "up-effort would not do"}]}
        ladder = dict(cfg()["ladders"][0], rungs=[rung, cfg()["ladders"][0]["rungs"][1]])
        layered("shipped", cfg(ladders=[ladder]))
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "`open`; or `novel` (no pattern applies) where up-effort would not do" in text

    def test_shape_restriction_renders(self, layered):
        rung = {"id": "top", "model": "fable", "shape": "open", "criteria": [["novel"]]}
        ladder = dict(cfg()["ladders"][0], rungs=[rung, cfg()["ladders"][0]["rungs"][1]])
        layered("shipped", cfg(ladders=[ladder]))
        config, provenance = og.resolve_config(layered.project_root)
        assert "`open` work only" in og.render(config, provenance)

    def test_effort_renders_only_where_it_is_dialable(self, layered):
        rung = {"id": "top", "model": "gpt", "effort": "max", "criteria": [["novel"]]}
        ladder = dict(cfg()["ladders"][0], rungs=[rung, cfg()["ladders"][0]["rungs"][1]])
        layered("shipped", cfg(ladders=[ladder]))
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "**gpt** at `max` effort" in text
        assert "**sonnet** at" not in text

    def test_the_gate_and_announcement_forms_render(self, layered, monkeypatch):
        monkeypatch.setattr(og, "DEFAULTS_PATH", _shipped_path())
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "Gate: write \"qualifies on <criterion>" in text
        assert "Announced as `(known, default)` or `(open, condensation)`" in text

    def test_terminal_rung_states_no_test_of_its_own(self, layered):
        layered("shipped", cfg())
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "2. **sonnet** -- terminal default." in text


class TestAgentTypesAndAnnouncement:
    def _shipped_text(self, layered, monkeypatch):
        monkeypatch.setattr(og, "DEFAULTS_PATH", _shipped_path())
        config, provenance = og.resolve_config(layered.project_root)
        return og.render(config, provenance)

    def test_agent_types_render_after_the_tier_and_before_effort(self, layered, monkeypatch):
        text = self._shipped_text(layered, monkeypatch)
        assert text.index("Agent type") > text.index(". Tier")
        assert text.index("Agent type") < text.index("Effort")
        for name in ("`Explore`", "`Plan`", "`general-purpose`"):
            assert name in text

    def test_announcement_form_and_examples_render(self, layered, monkeypatch):
        text = self._shipped_text(layered, monkeypatch)
        assert "delegating <what> to <model> (<terms that fired>)" in text
        assert "delegating crash diagnosis to opus (open, inference)" in text

    def test_announcement_examples_use_only_skill_terms(self):
        data = shipped()
        skills = {t["id"] for t in data["lexicon"] if t.get("kind") == "skill"}
        for example in data["announce"]["examples"]:
            inside = re.search(r"\(([^)]*)\)$", example["text"]).group(1)
            assert {t.strip() for t in inside.split(",")} <= skills, example["id"]

    def test_no_prices_dates_or_now_relative_phrasing(self, layered, monkeypatch):
        text = self._shipped_text(layered, monkeypatch).split("## Dispatch backends")[0]
        assert not re.search(r"\$\d", text)
        assert not re.search(r"\b20\d\d-\d\d-\d\d\b", text)
        for word in ("recently", "currently", "new ", "just shipped"):
            assert word not in text.lower(), word


class TestLayeringOverridesTheTree:
    """Users have override files against this data; patching by id must keep
    working across the reshape."""

    def test_a_user_layer_patches_a_rung_by_id(self, layered):
        layered("shipped", cfg())
        layered("user", {"ladders": [{"id": "agent", "rungs": [
            {"id": "workhorse", "model": "my-model"}]}]})
        config, provenance = og.resolve_config(layered.project_root)
        rungs = config["ladders"][0]["rungs"]
        assert [r["id"] for r in rungs] == ["top", "workhorse"]
        assert rungs[1]["model"] == "my-model"
        assert rungs[1]["terminal"] is True  # untouched fields survive
        assert "**my-model**" in og.render(config, provenance)

    def test_a_user_layer_appends_a_rung(self, layered):
        layered("shipped", cfg())
        layered("user", {"ladders": [{"id": "agent", "rungs": [
            {"id": "mine", "model": "extra", "criteria": [["novel"]]}]}]})
        config, _ = og.resolve_config(layered.project_root)
        assert [r["id"] for r in config["ladders"][0]["rungs"]] == ["top", "workhorse", "mine"]

    def test_a_user_layer_disables_a_rung(self, layered):
        layered("shipped", cfg())
        layered("user", {"ladders": [{"id": "agent", "rungs": [
            {"id": "top", "disabled": True}]}]})
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "**fable**" not in text
        assert "1. **sonnet**" in text  # renumbered, not left with a hole

    def test_a_user_layer_patches_a_lexicon_gloss(self, layered):
        layered("shipped", cfg())
        layered("user", {"lexicon": [{"id": "known", "gloss": "my own gloss"}]})
        config, provenance = og.resolve_config(layered.project_root)
        assert "`known` (my own gloss)" in og.render(config, provenance)

    def test_a_user_layer_disables_a_whole_ladder(self, layered):
        layered("shipped", cfg())
        layered("user", {"ladders": [{"id": "agent", "disabled": True}]})
        config, provenance = og.resolve_config(layered.project_root)
        assert "**fable**" not in og.render(config, provenance)

    def test_visible_rungs_tracks_the_merged_data(self, layered):
        layered("shipped", cfg())
        layered("user", {"ladders": [{"id": "agent", "rungs": [{"id": "mine"}]}]})
        config, _ = og.resolve_config(layered.project_root)
        assert og.visible_rungs(config, {"agent"}) == {"top", "workhorse", "mine"}
        assert og.visible_rungs(config, set()) == set()


class TestBackendBlock:
    CONFIG = dict(
        cfg(),
        backend={
            "title": "Backend",
            "requires_backend": "other",
            "intro": "where does it run",
            "default": "agent",
            "gates_intro": "Gates. Any one resolves to",
            "pulls_intro": "Pulls. To",
            "gates": [{"id": "g", "term": "known", "backend": "agent"}],
            "pulls": [{"id": "p", "term": "novel", "backend": "other"}],
        },
    )

    def _render(self, layered, backends):
        layered("shipped", dict(self.CONFIG, backends=backends))
        config, provenance = og.resolve_config(layered.project_root)
        return og.render(config, provenance)

    BOTH = [
        {"id": "agent", "name": "Agent", "detect": {"always": True}},
        {"id": "other", "name": "Other", "detect": {"always": True}},
    ]
    ONE = [{"id": "agent", "name": "Agent", "detect": {"always": True}}]

    def test_gates_and_pulls_group_under_their_backend(self, layered):
        text = self._render(layered, self.BOTH)
        assert "Gates. Any one resolves to **Agent**:" in text
        assert "Pulls. To **Other**:" in text
        assert "Default: **Agent**." in text

    def test_block_disappears_when_its_required_backend_is_absent(self, layered):
        text = self._render(layered, self.ONE)
        assert "where does it run" not in text
        assert "Gates." not in text

    def test_rows_naming_a_missing_backend_are_dropped(self, layered):
        cfgd = dict(self.CONFIG)
        cfgd["backend"] = dict(self.CONFIG["backend"], requires_backend=None)
        layered("shipped", dict(cfgd, backends=self.ONE))
        config, provenance = og.resolve_config(layered.project_root)
        body = og.render(config, provenance).split("\n---\n")[0]
        assert "Gates." in body
        assert "Pulls." not in body

    def test_absent_block_is_not_an_error(self, layered):
        layered("shipped", cfg())
        config, provenance = og.resolve_config(layered.project_root)
        assert "Gates" not in og.render(config, provenance)


class TestEffortBlock:
    def test_structured_effort_renders_as_tests(self, layered):
        layered("shipped", cfg(effort={
            "title": "Effort",
            "intro": "after the tier",
            "note": "not dialable",
            "raise_when": ["it is ambiguous"],
            "lower_when": ["it is mechanical"],
        }))
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "after the tier" in text and "not dialable" in text
        assert "- Raise: it is ambiguous." in text
        assert "- Lower: it is mechanical." in text

    def test_legacy_string_effort_still_renders(self, layered):
        """An override written against the old prose schema must not vanish."""
        layered("shipped", cfg(effort="just some prose about effort"))
        config, provenance = og.resolve_config(layered.project_root)
        assert "just some prose about effort" in og.render(config, provenance)

    def test_absent_block_is_not_an_error(self, layered):
        layered("shipped", cfg())
        config, provenance = og.resolve_config(layered.project_root)
        assert "Effort" not in og.render(config, provenance)


class TestGateLeaks:
    """Regressions for confirmed leaks of gated content into the guidance.
    Each of these rendered an absent backend's tier by name."""

    def _cfg(self, **over):
        base = cfg(
            ladders=[
                cfg()["ladders"][0],
                {
                    "id": "ghost",
                    "label": "Ghost",
                    "rungs": [{"id": "gated", "model": "phantom", "criteria": [],
                               "terminal": True}],
                },
            ],
            backends=[
                {"id": "agent", "name": "Agent", "detect": {"always": True},
                 "capabilities": {"tiers": ["workhorse", "gated"]}},
                {"id": "ghost", "detect": {"command": ["no-such-binary-xyz"]}},
            ],
        )
        base.update(over)
        return base

    def _body(self, layered, config_data):
        layered("shipped", config_data)
        config, provenance = og.resolve_config(layered.project_root)
        return og.render(config, provenance).split("\n---\n")[0]

    def test_rung_override_naming_a_gated_rung_does_not_leak_it(self, layered):
        body = self._body(
            layered,
            self._cfg(capacity={"source": "none", "tier_overrides": {"gated": "unavailable"}}),
        )
        assert "gated" not in body and "phantom" not in body

    def test_rung_override_on_a_visible_rung_still_renders(self, layered):
        body = self._body(
            layered,
            self._cfg(capacity={"source": "none", "tier_overrides": {"workhorse": "unavailable"}}),
        )
        assert "`workhorse`: unavailable" in body
        assert "Do not dispatch to" in body

    def test_backend_capabilities_do_not_advertise_a_gated_rung(self, layered):
        assert "gated" not in self._body(layered, self._cfg())

    def test_backend_capabilities_do_not_advertise_a_disabled_rung(self, layered):
        data = self._cfg()
        data["ladders"][0]["rungs"][1] = {"id": "workhorse", "disabled": True}
        assert "workhorse" not in self._body(layered, data)


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


class TestCriteriaFailClosed:
    """A criteria group is a CONJUNCTION.

    Dropping one unresolvable conjunct renders a strictly WIDER test than the
    data specifies. On this ladder that silently widens the gate on the most
    expensive rung -- the exact direction every guard in the policy exists to
    prevent -- so an unresolvable id must invalidate its whole group, and a
    non-terminal rung left with no group at all must raise rather than render
    an empty test that first-match-wins reads as unconditional.
    """

    @staticmethod
    def _claude_ladder(config):
        for ladder in config["ladders"]:
            if str(ladder.get("id")) == "agent":
                return ladder
        raise AssertionError("no agent ladder in shipped data")

    @staticmethod
    def _guarded_rung(ladder):
        for rung in ladder["rungs"]:
            for group in rung.get("criteria") or []:
                ids = group.get("terms") if isinstance(group, dict) else group
                if ids and len(ids) > 1:
                    return rung
        raise AssertionError("no multi-term conjunction in shipped data")

    def test_a_disabled_conjunct_drops_the_whole_group_not_just_the_term(self):
        """Group granularity: a surviving alternative still renders, but the
        group containing the unresolvable id vanishes WHOLE -- its other
        conjuncts must not survive on their own, which would widen the test."""
        rung = {
            "id": "probe",
            "model": "probe-model",
            "criteria": [["alpha", "beta"], ["gamma"]],
        }
        lexicon = [
            {"id": "alpha", "kind": "skill", "name": "alpha"},
            {"id": "beta", "kind": "skill", "name": "beta"},
            {"id": "gamma", "kind": "skill", "name": "gamma"},
        ]
        full = og.rung_criteria(rung, og.Terms(list(lexicon)))
        assert "`alpha`" in full and "`beta`" in full and "`gamma`" in full

        degraded_lexicon = [dict(r) for r in lexicon]
        for record in degraded_lexicon:
            if record["id"] == "beta":
                record["disabled"] = True
        degraded = og.rung_criteria(rung, og.Terms(degraded_lexicon))

        assert "`gamma`" in degraded, "the surviving alternative should still render"
        assert "`beta`" not in degraded
        assert "`alpha`" not in degraded, (
            "alpha survived after its sibling conjunct was disabled -- "
            "the conjunction was widened rather than dropped"
        )

    def test_shape_alone_cannot_stand_in_as_a_rungs_whole_test(self):
        """`shape` NARROWS a criteria match; it is not a test on its own.

        Caught by a smoke test, not by the assertion above: after every group was
        invalidated the top rung still rendered as "`open` work only", which
        matches every unit of that shape -- the same widening, through a
        different door.
        """
        rung = {
            "id": "probe",
            "model": "probe-model",
            "shape": "open",
            "criteria": [["nonexistent-term"]],
        }
        terms = og.Terms([{"id": "open", "kind": "skill", "name": "open"}])
        with pytest.raises(og.UnrenderableRung):
            og.rung_criteria(rung, terms)

    def test_a_rung_whose_only_test_is_shape_still_renders(self):
        """A rung that declares no criteria at all is a different case -- it was
        authored as shape-only, so shape IS its test."""
        rung = {"id": "probe", "model": "probe-model", "shape": "open"}
        terms = og.Terms([{"id": "open", "kind": "skill", "name": "open"}])
        assert "work only" in og.rung_criteria(rung, terms)

    def test_disabling_a_conjunct_raises_rather_than_rendering_a_shape_only_rung(
        self, monkeypatch, tmp_path
    ):
        """End-to-end: the shipped top rung has both criteria and a shape."""
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, _ = og.resolve_config(tmp_path / "no-project")
        rung = self._guarded_rung(self._claude_ladder(config))
        group = rung["criteria"][0]
        ids = list(group.get("terms") if isinstance(group, dict) else group)
        if not rung.get("shape"):
            pytest.skip("shipped guarded rung carries no shape restriction")
        for record in config["lexicon"]:
            if str(record.get("id")) in ids:
                record["disabled"] = True
        with pytest.raises(og.UnrenderableRung):
            og.rung_criteria(rung, og.Terms(config["lexicon"]))

    def test_a_non_terminal_rung_with_no_resolvable_criteria_raises(self):
        rung = {
            "id": "probe",
            "model": "probe-model",
            "criteria": [["nonexistent-term"]],
        }
        with pytest.raises(og.UnrenderableRung) as excinfo:
            og.rung_criteria(rung, og.Terms([]))
        assert "nonexistent-term" in str(excinfo.value)

    def test_a_terminal_rung_may_state_no_criteria(self):
        rung = {"id": "probe", "model": "probe-model", "terminal": True}
        assert og.rung_criteria(rung, og.Terms([])) == ""

    def test_a_typo_in_a_term_id_cannot_widen_a_conjunction(self):
        rung = {
            "id": "probe",
            "model": "probe-model",
            "terminal": True,
            "criteria": [["real", "typoed"]],
        }
        terms = og.Terms([{"id": "real", "kind": "skill", "name": "real"}])
        assert og.rung_criteria(rung, terms) == ""


class TestStaleOverrideWarning:
    """A schema-1 override merges cleanly and then contributes nothing.

    Silence is the worst outcome there: the user's policy is not in force and
    nothing says so. The layering half was preserved precisely for these users.
    """

    def test_a_schema_1_key_warns_in_the_rendered_output(self, monkeypatch, tmp_path):
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        assert "Stale override" not in og.render(config, provenance)

        config["tiers"] = [{"id": "cheapest", "model": "haiku"}]
        text = og.render(config, provenance)
        assert "Stale override" in text
        assert "`tiers`" in text
        assert "configuration.md" in text

    def test_every_legacy_key_is_detected(self):
        for key in og.LEGACY_SCHEMA_1_KEYS:
            assert og.legacy_schema_keys({key: "x"}) == [key]

    def test_a_clean_schema_2_config_reports_no_stale_keys(self):
        assert og.legacy_schema_keys(shipped()) == []
