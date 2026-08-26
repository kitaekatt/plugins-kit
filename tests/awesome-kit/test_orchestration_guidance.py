"""Tests for the orchestrate skill's policy renderer.

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
        assert data["schema_version"] == 3
        assert "backend" not in data
        assert "ladders" not in data
        assert isinstance(data["routing"], list)
        backend_ids = [b["id"] for b in data["backends"]]
        assert {"agent", "codex"} <= set(backend_ids)
        assert data["resolution"]

    def test_routing_rows_have_shapes_and_models_in_priority_order(self):
        data = shipped()
        skills = {t["id"] for t in data["lexicon"] if t.get("kind") == "skill"}
        assert data["routing"][-1]["shape"] == []
        for row in data["routing"]:
            assert set(row["shape"]) <= skills
            assert row["models"]
            assert all(isinstance(model, str) for model in row["models"])

    def test_shipped_routing_uses_only_the_two_namespaces(self):
        data = shipped()
        models = [model for row in data["routing"] for model in row["models"]]
        assert models == ["sol", "agent:fable", "sol", "luna", "agent:sonnet"]
        assert all(model.startswith("agent:") or ":" not in model for model in models)

    def test_shipped_defaults_render_with_the_expected_sections(
        self, capsys, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        monkeypatch.setattr(og, "discover_model_definitions", lambda _root: ({}, []))
        monkeypatch.setattr(
            og,
            "detect_backend",
            lambda backend: (
                (True, "stubbed")
                if backend.get("id") in {"agent", "codex"}
                else (False, "stubbed absent")
            ),
        )
        assert og.main(["--project-root", str(tmp_path / "project")]) == 0
        text = capsys.readouterr().out
        for section in ("Shape the unit", "Routing", "Agent type", "Effort", "Announce", "Dispatch backends", "Capacity"):
            assert section in text

    def test_lexicon_and_routing_shape_terms_stay_in_sync(self):
        data = shipped()
        reference = (
            og.DEFAULTS_PATH.parent.parent / "references" / "lexicon.md"
        ).read_text(encoding="utf-8")
        reference_ids = set(re.findall(r"^### `([^`]+)`", reference, re.M))
        yaml_ids = {str(term["id"]) for term in data["lexicon"]}
        assert yaml_ids == reference_ids
        skills = {
            str(term["id"])
            for term in data["lexicon"]
            if term.get("kind") == "skill" and not term.get("disabled")
        }
        for row in data["routing"]:
            assert set(row.get("shape") or []) <= skills

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
    """A minimal but complete routing config."""
    base = {
        "schema_version": 3,
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
        "routing": [
            {"shape": ["novel"], "models": ["agent:fable"]},
            {"shape": [], "models": ["agent:sonnet"]},
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

    def test_unresolvable_model_is_not_rendered(self, layered):
        layered("shipped", cfg(routing=[{"shape": ["novel"], "models": ["missing"]}]))
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "missing" not in text.split("\n---\n")[0]

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

    def test_routing_row_with_unknown_shape_is_hidden(self, layered):
        layered("shipped", cfg(
            routing=[{"shape": ["missing-shape"], "models": ["agent:fable"]}],
        ))
        config, provenance = og.resolve_config(layered.project_root)
        body = og.render(config, provenance).split("\n---\n")[0]
        assert "missing-shape" not in body

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
    """Routing rows preserve declaration order and model priority."""

    def test_resolution_is_stated_before_any_block(self, layered):
        layered("shipped", cfg())
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "**Resolution.** first match wins" in text
        assert text.index("Resolution.") < text.index("## 1.")

    def test_models_render_in_declared_priority_order(self, layered):
        layered("shipped", cfg())
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "1. If `novel`" in text
        assert "then **sonnet**" in text or "**sonnet**" in text
        assert text.index("**fable**") < text.index("**sonnet**")

    def test_first_matching_row_semantics_are_stated(self, layered):
        layered("shipped", cfg())
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "first matching shape wins" in text
        assert "launch or transport error" in text

    def test_blocks_render_in_principle_order(self, layered, monkeypatch):
        monkeypatch.setattr(og, "DEFAULTS_PATH", _shipped_path())
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        expected = ["Shape the unit", "Routing", "Agent type", "Effort", "Announce"]
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
        return re.split(r"\n## \d+\. Effort", og.render(config, provenance))[0]

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
    def test_routing_guards_render(self, layered):
        layered("shipped", cfg(routing=[{
            "shape": ["novel"],
            "models": ["agent:fable"],
            "guards": ["Keep this route explicit."],
        }]))
        config, provenance = og.resolve_config(layered.project_root)
        assert "Keep this route explicit." in og.render(config, provenance)

    def test_routing_gate_renders(self, layered):
        layered("shipped", cfg(routing=[{
            "shape": ["novel"],
            "models": ["agent:fable"],
            "gate": "write the reason before dispatch",
        }]))
        config, provenance = og.resolve_config(layered.project_root)
        assert "Gate: write the reason before dispatch" in og.render(config, provenance)


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
        for record in list(config["shape"]["tests"]):
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
    """Registry-backed routes disappear when their harness is absent."""

    @pytest.fixture
    def variants(self, monkeypatch, tmp_path):
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        monkeypatch.setattr(
            og,
            "discover_model_definitions",
            lambda _root: ({
                "sol": {"id": "sol", "harness": "codex", "model": "gpt-5.6-sol"},
                "luna": {"id": "luna", "harness": "codex", "model": "gpt-5.6-luna"},
            }, []),
        )
        real = og.detect_backend

        def detect(backend):
            if backend.get("id") == "codex":
                return True, "stubbed codex"
            if backend.get("id") in {"grok", "model-endpoints"}:
                return False, "stubbed absent"
            return real(backend)

        monkeypatch.setattr(og, "detect_backend", detect)
        config, provenance = og.resolve_config(tmp_path / "no-project")
        present = og.render(config, provenance)
        monkeypatch.setattr(og, "detect_backend", lambda backend: (
            (False, "stubbed codex") if backend.get("id") == "codex" else detect(backend)
        ))
        config, provenance = og.resolve_config(tmp_path / "no-project")
        return present, og.render(config, provenance)

    @pytest.fixture
    def with_codex(self, variants):
        return variants[0]

    @pytest.fixture
    def without(self, variants):
        return variants[1]

    def test_registry_model_is_rendered_when_codex_is_present(self, with_codex):
        assert "codex/sol" in with_codex
        assert "codex/luna" in with_codex

    def test_registry_model_is_skipped_when_codex_is_absent(self, without):
        assert "codex/sol" not in without
        assert "codex/luna" not in without
        assert "agent:fable" not in without
        assert "fable" in without

    def test_fan_out_row_is_skipped_without_a_surviving_model(self, without):
        body = without.split("\n---\n")[0]
        assert "If `fan-out`" not in body

    def test_fan_out_collapse_test_survives_without_codex(self, without):
        assert "`fan-out`" in without
        assert "collapses into a single shell command" in without

    def test_the_hole_is_disclosed_in_one_clause(self, without, with_codex):
        """Silence about a known gap reads as an oversight and invites the
        reader to invent the answer the collapse test exists to prevent."""
        assert "sequence the units or handle them inline" in without
        assert "sequence the units or handle them inline" not in with_codex

    def test_plan_checkpoint_shape_tests_render_in_both_variants(self, without, with_codex):
        """P0.6-P0.8 live in shaping, which both variants render."""
        for text in (without, with_codex):
            assert "Route the plan itself through this tree" in text
            assert "not an independent reviewer" in text
            assert "defaults to TWO units" in text

    def test_agent_member_survives_when_registry_model_is_skipped(self, without):
        assert "fable" in without


class TestNoBareCodenames:
    """Registry entry ids are the public routing names."""

    def test_shipped_routing_uses_entry_ids_not_provider_slugs(self):
        data = shipped()
        models = [model for row in data["routing"] for model in row["models"]]
        assert "sol" in models and "luna" in models
        assert all("gpt-5.6-" not in model for model in models)


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
        assert all("grok" not in model for row in data["routing"] for model in row["models"])
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
        assert "grok" not in "\n".join(
            line for line in text.splitlines() if line.startswith("- tiers:")
        )

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
        assert all("grok" not in model for row in data["routing"] for model in row["models"])

    REQUEST_ONLY = {"grok", "model-endpoints"}

    def test_selection_is_optional_and_absent_backends_render_no_such_line(self):
        """Only a restricted backend gets the heading; the others must not
        acquire one by accident."""
        data = shipped()
        for backend in data["backends"]:
            if backend["id"] not in self.REQUEST_ONLY:
                assert "selection" not in backend, backend["id"]


class TestModelEndpointsDetect:
    """The `model_endpoints` detect kind: fail-closed, parallel, and silent
    about a machine that never opted in."""

    ENTRY = {"base_url": "http://endpoint.invalid:8080/v1", "model": "a-model"}

    @staticmethod
    def _registry(tmp_path, monkeypatch, text, *, via_override=True):
        path = tmp_path / "model-endpoints.yaml"
        path.write_text(text, encoding="utf-8")
        if via_override:
            monkeypatch.setenv(og.MODEL_ENDPOINTS_ENV, str(path))
        else:
            monkeypatch.delenv(og.MODEL_ENDPOINTS_ENV, raising=False)
            home = tmp_path / "home"
            (home / ".claude" / "config").mkdir(parents=True)
            (home / ".claude" / "config" / "model-endpoints.yaml").write_text(
                text, encoding="utf-8"
            )
            monkeypatch.setattr(og.Path, "home", classmethod(lambda cls: home))
        return path

    @staticmethod
    def _probes(monkeypatch, verdicts):
        """Answer each probe by base_url, without touching the network."""

        def fake(entry, environ=None, timeout=og.MODEL_ENDPOINTS_PROBE_TIMEOUT):
            return verdicts[entry["base_url"]]

        monkeypatch.setattr(og, "probe_model_endpoint", fake)

    @staticmethod
    def _rule(**over):
        rule = {"model_endpoints": True}
        rule.update(over)
        return {"id": "model-endpoints", "detect": rule}

    def test_no_registry_anywhere_is_unavailable_and_names_the_convention(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv(og.MODEL_ENDPOINTS_ENV, raising=False)
        monkeypatch.setattr(og.Path, "home", classmethod(lambda cls: tmp_path))
        ok, reason = og.detect_backend(self._rule())
        assert ok is False
        assert "no model-endpoints registry" in reason
        assert "model-endpoints.yaml" in reason

    def test_present_but_unparseable_registry_is_unavailable_not_an_exception(
        self, tmp_path, monkeypatch
    ):
        self._registry(tmp_path, monkeypatch, "models: [oops\n  - :\n")
        ok, reason = og.detect_backend(self._rule())
        assert ok is False
        assert "unreadable" in reason

    def test_a_schema_invalid_registry_names_the_defect(self, tmp_path, monkeypatch):
        self._registry(tmp_path, monkeypatch, "models:\n  one:\n    model: m\n")
        ok, reason = og.detect_backend(self._rule())
        assert ok is False
        assert "base_url" in reason

    def test_a_default_naming_no_entry_is_a_defect(self, tmp_path, monkeypatch):
        self._registry(
            tmp_path,
            monkeypatch,
            "default: nope\nmodels:\n  one:\n    base_url: http://x/v1\n    model: m\n",
        )
        ok, reason = og.detect_backend(self._rule())
        assert ok is False and "names no entry" in reason

    def test_a_dangling_override_is_loud_where_a_missing_convention_is_silent(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(og.MODEL_ENDPOINTS_ENV, str(tmp_path / "nowhere.yaml"))
        ok, reason = og.detect_backend(self._rule())
        assert ok is False
        assert "unreadable" in reason and og.MODEL_ENDPOINTS_ENV in reason

    def test_the_override_is_honored_over_the_convention(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            og.Path, "home", classmethod(lambda cls: tmp_path / "unused-home")
        )
        self._registry(
            tmp_path,
            monkeypatch,
            "models:\n  one:\n    base_url: http://endpoint.invalid:8080/v1\n    model: a-model\n",
        )
        self._probes(monkeypatch, {"http://endpoint.invalid:8080/v1": (True, "")})
        ok, reason = og.detect_backend(self._rule())
        assert ok is True and "one (a-model)" in reason

    def test_the_conventional_path_is_read_with_no_override_set(
        self, tmp_path, monkeypatch
    ):
        self._registry(
            tmp_path,
            monkeypatch,
            "models:\n  one:\n    base_url: http://endpoint.invalid:8080/v1\n    model: a-model\n",
            via_override=False,
        )
        self._probes(monkeypatch, {"http://endpoint.invalid:8080/v1": (True, "")})
        assert og.detect_backend(self._rule())[0] is True

    def test_a_missing_required_command_is_unavailable_before_any_probe(
        self, tmp_path, monkeypatch
    ):
        self._registry(
            tmp_path,
            monkeypatch,
            "models:\n  one:\n    base_url: http://endpoint.invalid:8080/v1\n    model: a-model\n",
        )

        def boom(*a, **kw):
            raise AssertionError("probed despite a missing required command")

        monkeypatch.setattr(og, "probe_model_endpoint", boom)
        monkeypatch.setattr(og.shutil, "which", lambda name: None)
        ok, reason = og.detect_backend(
            self._rule(require_commands=["definitely-not-a-real-harness-xyz"])
        )
        assert ok is False
        assert "not found on PATH" in reason

    def test_every_entry_down_is_unavailable(self, tmp_path, monkeypatch):
        self._registry(
            tmp_path,
            monkeypatch,
            "models:\n"
            "  one:\n    base_url: http://a.invalid/v1\n    model: m1\n"
            "  two:\n    base_url: http://b.invalid/v1\n    model: m2\n",
        )
        self._probes(
            monkeypatch,
            {
                "http://a.invalid/v1": (False, "URLError"),
                "http://b.invalid/v1": (False, "URLError"),
            },
        )
        ok, reason = og.detect_backend(self._rule())
        assert ok is False
        assert "no registered model endpoint is up" in reason
        assert "one" in reason and "two" in reason

    def test_one_entry_up_detects_and_the_roster_lists_up_and_down(
        self, tmp_path, monkeypatch
    ):
        self._registry(
            tmp_path,
            monkeypatch,
            "models:\n"
            "  one:\n    base_url: http://a.invalid/v1\n    model: m1\n"
            "  two:\n    base_url: http://b.invalid/v1\n    model: m2\n",
        )
        self._probes(
            monkeypatch,
            {
                "http://a.invalid/v1": (True, ""),
                "http://b.invalid/v1": (False, "URLError"),
            },
        )
        ok, reason = og.detect_backend(self._rule())
        assert ok is True
        assert reason.startswith("up: one (m1) @ http://a.invalid/v1")
        assert "down: two (m2) @ http://b.invalid/v1 (URLError)" in reason

    def test_a_keyed_entry_with_an_unset_variable_reports_down_by_name(
        self, monkeypatch
    ):
        monkeypatch.delenv("SOME_ENDPOINT_KEY", raising=False)
        ok, note = og.probe_model_endpoint(
            {"base_url": "http://a.invalid/v1", "model": "m", "key_env": "SOME_ENDPOINT_KEY"}
        )
        assert ok is False and note == "SOME_ENDPOINT_KEY unset"

    def test_a_keyed_entry_sends_a_bearer_token(self, monkeypatch):
        seen = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            return Response()

        monkeypatch.setattr(og.urllib.request, "urlopen", fake_urlopen)
        ok, _ = og.probe_model_endpoint(
            {"base_url": "http://a.invalid/v1/", "model": "m", "key_env": "K"},
            environ={"K": "secret"},
        )
        assert ok is True
        assert seen["url"] == "http://a.invalid/v1/models"
        assert seen["auth"] == "Bearer secret"

    def test_a_probe_never_raises(self, monkeypatch):
        def explode(request, timeout=None):
            raise OSError("no route to host")

        monkeypatch.setattr(og.urllib.request, "urlopen", explode)
        assert og.probe_model_endpoint(self.ENTRY) == (False, "OSError")

    def test_a_non_2xx_response_is_down(self, monkeypatch):
        class Response:
            status = 503

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(og.urllib.request, "urlopen", lambda *a, **kw: Response())
        assert og.probe_model_endpoint(self.ENTRY) == (False, "HTTP 503")

    def test_entries_are_probed_in_parallel_not_one_timeout_each(self, monkeypatch):
        """Four slow entries must cost about ONE timeout, not four."""
        delay = 0.4
        entries = [
            (f"e{n}", {"base_url": f"http://{n}.invalid/v1", "model": f"m{n}"})
            for n in range(4)
        ]

        def slow(entry, environ=None, timeout=og.MODEL_ENDPOINTS_PROBE_TIMEOUT):
            time.sleep(delay)
            return False, "timeout"

        monkeypatch.setattr(og, "probe_model_endpoint", slow)
        started = time.monotonic()
        results = og.probe_model_endpoints(entries)
        elapsed = time.monotonic() - started
        assert len(results) == 4
        assert elapsed < delay * len(entries) / 2, elapsed


class TestRequestOnlyModelEndpoints:
    """The model-endpoints record is present-but-not-eligible on the same two
    mechanisms as grok, and both are asserted because either alone leaks: no
    ladder keeps it out of every TIER decision, and the `selection` line keeps
    it out of the BACKEND decision, which happens first.

    It carries a third obligation grok does not: every machine- and
    model-specific value must come from the user's registry at dispatch time,
    because this file ships publicly.
    """

    BACKEND_ID = "model-endpoints"

    @staticmethod
    def _render(monkeypatch, tmp_path, *, present):
        """Stub this one backend's verdict, as the grok class does: the real
        rule reads a registry and probes a network, so neither variant would be
        true on every machine."""
        real = og.detect_backend

        def detect(backend):
            if backend.get("id") == TestRequestOnlyModelEndpoints.BACKEND_ID:
                return (
                    (True, "up: one (a-model) @ http://endpoint.invalid:8080/v1")
                    if present
                    else (False, "stubbed absent")
                )
            return real(backend)

        monkeypatch.setattr(og, "detect_backend", detect)
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        return og.render(config, provenance)

    def _record(self):
        return next(b for b in shipped()["backends"] if b["id"] == self.BACKEND_ID)

    def test_the_record_has_no_ladder_and_claims_no_tiers(self):
        record = self._record()
        assert not (record.get("capabilities") or {}).get("tiers")
        assert all(
            self.BACKEND_ID not in model
            for row in shipped()["routing"]
            for model in row["models"]
        )
        assert record.get("selection"), "a ladder-less backend must state its condition"

    def test_it_is_named_by_no_gate_pull_or_rung(self):
        data = shipped()
        assert all(
            self.BACKEND_ID not in model
            for row in data["routing"]
            for model in row["models"]
        )

    def test_the_record_renders_with_its_selection_line_when_detected(
        self, monkeypatch, tmp_path
    ):
        text = self._render(monkeypatch, tmp_path, present=True)
        assert "Registered model endpoints (codex harness)" in text
        assert "**Selection.**" in text
        assert "up: one (a-model)" in text

    def test_selection_precedes_the_mechanics(self, monkeypatch, tmp_path):
        text = self._render(monkeypatch, tmp_path, present=True)
        assert text.index("**Selection.**") < text.index("model_providers.registry")

    def test_the_whole_record_disappears_when_undetected(self, monkeypatch, tmp_path):
        text = self._render(monkeypatch, tmp_path, present=False)
        for probe in (
            "Registered model endpoints",
            "model_providers.registry",
            "model-endpoints-dispatch.md",
        ):
            assert probe not in text, probe

    def test_the_detect_rule_requires_the_harness_it_documents(self):
        rule = self._record()["detect"]
        assert rule["model_endpoints"] is True
        assert rule["require_commands"] == ["codex"]

    def test_no_machine_or_model_specific_value_is_shipped(self):
        """The public-repo guard, mechanical: every host- or model-shaped value
        in this record is a placeholder, the registry convention path, or the
        override variable name."""
        record = self._record()
        blob = yaml.safe_dump(record)
        # No URL may carry a literal host: every one is a <placeholder>.
        for match in re.finditer(r"https?://[^\s'\"]+", blob):
            assert match.group(0).startswith("http://<entry"), match.group(0)
        # The only registry location named is the home-relative convention.
        for match in re.finditer(r"[~][^\s`'\"]*", blob):
            assert match.group(0) == "~/.claude/config/model-endpoints.yaml", match.group(0)
        assert "MODEL_ENDPOINTS_REGISTRY" in blob
        # The model and its context window come from the registry, not from here.
        for field in ("<entry base_url>", "<entry model>", "<entry context_window"):
            assert field in record["command"], field

    def test_the_references_it_points_at_exist(self):
        record = self._record()
        base = og.DEFAULTS_PATH.parent.parent
        for name in ("model-endpoints-dispatch.md", "codex-dispatch.md"):
            assert name in record["dispatch"], name
            assert (base / "references" / name).is_file(), name


class TestRoutingRendering:
    def test_fallthrough_uses_the_next_model_in_the_same_row(self, layered):
        layered(
            "shipped",
            cfg(routing=[{"shape": ["novel"], "models": ["agent:fable", "agent:sonnet"]}]),
        )
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "If `novel`" in text
        assert "try **fable**, then **sonnet**" in text
        assert "continue to the next model" in text

    def test_empty_shape_is_the_default_route(self, layered):
        layered("shipped", cfg(routing=[{"shape": [], "models": ["agent:haiku"]}]))
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "If anything: try **haiku**." in text

    def test_announcement_text_names_the_entry_and_fallthrough(self):
        assert og.announcement_text("the change", "codex/sol", ["novel"]) == (
            "delegating the change to codex/sol (novel)"
        )
        assert og.announcement_text(
            "the retry", "sonnet", [], fell_through_from="sol"
        ) == "delegating the retry to sonnet (default; fell through from sol)"


class TestAgentTypesAndAnnouncement:
    def _shipped_text(self, layered, monkeypatch):
        monkeypatch.setattr(og, "DEFAULTS_PATH", _shipped_path())
        config, provenance = og.resolve_config(layered.project_root)
        return og.render(config, provenance)

    def test_agent_types_render_after_routing_and_before_effort(self, layered, monkeypatch):
        text = self._shipped_text(layered, monkeypatch)
        assert text.index("Agent type") > text.index("Routing")
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
    """Users have override files against this data; routing is a plain list."""

    def test_a_user_layer_replaces_the_routing_list(self, layered):
        layered("shipped", cfg())
        replacement = {"routing": [{"shape": ["open"], "models": ["agent:haiku"]}]}
        layered("user", replacement)
        config, provenance = og.resolve_config(layered.project_root)
        assert config["routing"] == replacement["routing"]
        text = og.render(config, provenance)
        assert "If `open`: try **haiku**." in text
        assert "**fable**" not in text

    def test_a_user_layer_patches_a_lexicon_gloss(self, layered):
        layered("shipped", cfg())
        layered("user", {"lexicon": [{"id": "known", "gloss": "my own gloss"}]})
        config, provenance = og.resolve_config(layered.project_root)
        assert "`known` (my own gloss)" in og.render(config, provenance)



class TestEffortBlock:
    def test_structured_effort_renders_as_tests(self, layered):
        layered("shipped", cfg(effort={
            "title": "Effort",
            "intro": "after routing",
            "note": "not dialable",
            "raise_when": ["it is ambiguous"],
            "lower_when": ["it is mechanical"],
        }))
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "after routing" in text and "not dialable" in text
        assert "- Raise: it is ambiguous." in text
        assert "- Lower: it is mechanical." in text

    def test_string_effort_still_renders(self, layered):
        """A prose override remains renderable."""
        layered("shipped", cfg(effort="just some prose about effort"))
        config, provenance = og.resolve_config(layered.project_root)
        assert "just some prose about effort" in og.render(config, provenance)

    def test_absent_block_is_not_an_error(self, layered):
        layered("shipped", cfg())
        config, provenance = og.resolve_config(layered.project_root)
        assert "Effort" not in og.render(config, provenance)


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
        layered("shipped", {"routing": [{"shape": [], "models": ["agent:sonnet"]}],
                            "backends": [{"id": "agent", "detect": {"always": True}}],
                            "capacity": {"source": "none"}})
        layered("project", {"backends": [{"id": "evil", "detect": {"command": ["calc.exe"]}}]})
        config, provenance = og.resolve_config(layered.project_root)
        evil = next(b for b in config["backends"] if b["id"] == "evil")
        assert "command" not in evil["detect"]
        assert any("executable field" in status for _, _, status in provenance)

    def test_project_capacity_command_is_stripped(self, layered):
        layered("shipped", {"routing": [], "backends": [{"id": "agent"}],
                            "capacity": {"source": "command", "command": ["safe"]}})
        layered("project", {"capacity": {"command": ["evil.exe"]}})
        config, _ = og.resolve_config(layered.project_root)
        assert config["capacity"]["command"] == ["safe"]

    def test_user_layer_may_still_declare_commands(self, layered):
        """Machine-level trust is a different question from repo-level trust."""
        layered("shipped", {"routing": [], "backends": [{"id": "agent"}],
                            "capacity": {"source": "none"}})
        layered("user", {"capacity": {"command": ["my-probe"]}})
        config, _ = og.resolve_config(layered.project_root)
        assert config["capacity"]["command"] == ["my-probe"]

    def test_a_harmless_project_layer_is_untouched(self, layered):
        layered("shipped", {"routing": [{"shape": [], "models": ["agent:sonnet"]}], "backends": [{"id": "agent"}],
                            "capacity": {"source": "none"}})
        layered("project", {"routing": [{"shape": [], "models": ["agent:haiku"]}]})
        config, provenance = og.resolve_config(layered.project_root)
        assert config["routing"][0]["models"] == ["agent:haiku"]
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


class TestRoutingResolution:
    def test_an_unknown_model_is_skipped_and_an_empty_row_is_dropped(self, layered):
        config_data = cfg(
            routing=[
                {"shape": ["novel"], "models": ["missing"]},
                {"shape": ["open"], "models": ["also-missing"]},
                {"shape": [], "models": ["agent:sonnet"]},
            ]
        )
        layered("shipped", config_data)
        config, _ = og.resolve_config(layered.project_root)
        routes, notes = og.resolve_routing_models(config, {}, {})
        assert [route["shape"] for route in routes] == [[]]
        assert any("missing" in note for note in notes)
        assert any("no model resolves" in note for note in notes)

    def test_only_agent_prefix_is_a_reserved_namespace(self, layered):
        layered(
            "shipped",
            cfg(routing=[{"shape": [], "models": ["codex:sol", "agent:sonnet"]}]),
        )
        config, _ = og.resolve_config(layered.project_root)
        routes, notes = og.resolve_routing_models(config, {}, {})
        assert [model["target"] for model in routes[0]["models"]] == ["sonnet"]
        assert any("only `agent:`" in note for note in notes)


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
        config["ladders"] = [{"id": "old"}]
        config["rungs"] = [{"id": "old-rung"}]
        config["backend"] = {"id": "old-backend"}
        text = og.render(config, provenance)
        assert "Stale override" in text
        for key in ("tiers", "ladders", "rungs", "backend"):
            assert f"`{key}`" in text
        assert "configuration.md" in text

    def test_every_legacy_key_is_detected(self):
        for key in og.LEGACY_SCHEMA_1_KEYS:
            assert og.legacy_schema_keys({key: "x"}) == [key]

    def test_a_clean_schema_3_config_reports_no_stale_keys(self):
        assert og.legacy_schema_keys(shipped()) == []
