"""Tests for the orchestrate skill's policy renderer.

Covers the four-layer merge, record-list merging by id, backend detection,
    capacity reporting (including the snapshot contract with claude-ui-kit), and
    the shape of the rendered guidance.

The module under test re-execs into awesome-kit's provisioned venv on import;
tests/conftest.py sets _BOOTSTRAP_GUARD_VENV_REEXEC so that is a no-op here
(see plugins/CLAUDE.md -- without it, importing would abandon the pytest
process itself).
"""

import json
import re
import sys
import time
from pathlib import Path

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
    """Redirect all four layers into tmp_path; returns a writer per layer."""
    shipped = tmp_path / "shipped.yaml"
    user = tmp_path / "user.yaml"
    project_root = tmp_path / "project"
    machine = tmp_path / "machine.yaml"
    (project_root / ".claude").mkdir(parents=True)

    monkeypatch.setattr(og, "DEFAULTS_PATH", shipped)
    monkeypatch.setattr(og, "user_config_path", lambda: user)
    monkeypatch.setattr(og, "machine_config_path", lambda: machine)

    def write(layer, data):
        target = {
            "shipped": shipped,
            "user": user,
            "project": project_root / ".claude" / og.CONFIG_NAME,
            "machine": machine,
        }[layer]
        target.write_text(yaml.safe_dump(data), encoding="utf-8")

    write.project_root = project_root
    return write


class TestResolveConfig:
    def test_precedence_is_shipped_then_machine_then_user_then_project(self, layered):
        layered("shipped", {"default_tier": "workhorse", "default_backend": "agent"})
        layered("machine", {"default_tier": "machine-local"})
        layered("user", {"default_tier": "high-reasoning"})
        layered("project", {"default_tier": "top"})
        config, _ = og.resolve_config(layered.project_root)
        assert config["default_tier"] == "top"
        assert config["default_backend"] == "agent"  # untouched keys survive

    def test_project_overrides_machine_layer(self, layered):
        layered("shipped", {"resolution": "shipped"})
        layered("machine", {"resolution": "machine-local"})
        layered("project", {"resolution": "project"})
        config, _ = og.resolve_config(layered.project_root)
        assert config["resolution"] == "project"

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
        assert status == {
            "shipped": "applied",
            "user": "empty",
            "project": "absent",
            "machine": "absent",
        }

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
        assert backend_ids == ["agent", "codex"]
        assert data["resolution"]

    def test_routing_rows_have_shapes_and_models_in_priority_order(self):
        data = shipped()
        skills = {t["id"] for t in data["lexicon"] if t.get("kind") == "skill"}
        assert data["routing"][-1]["shape"] == []
        for row in data["routing"]:
            assert set(row["shape"]) <= skills
            assert row["models"]
            assert all(isinstance(model, str) for model in row["models"])

        shapes = [row["shape"] for row in data["routing"]]
        assert shapes.index(["parallel-leaf", "unverifiable"]) < shapes.index(
            ["parallel-leaf", "known", "rule-applying"]
        )
        assert shapes.index(["parallel-leaf", "mutating"]) < shapes.index(
            ["parallel-leaf", "known", "rule-applying"]
        )

    def test_shipped_routing_uses_only_the_two_namespaces(self):
        data = shipped()
        models = [model for row in data["routing"] for model in row["models"]]
        assert models == [
            "sol",
            "agent:fable",
            "sol",
            "agent:sonnet",
            "agent:sonnet",
            "luna",
            "luna",
            "agent:sonnet",
        ]
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
        assert "parallel-development razor" in text
        assert "At least two leaves must be runnable on the same dependency frontier" in text
        assert "Review overlap (`premise-safe`)" in text

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


class TestReviewOverlap:
    def test_project_override_selects_the_strict_posture(self, layered):
        layered("shipped", yaml.safe_load(_shipped_path().read_text(encoding="utf-8")))
        layered("project", {"review_overlap": {"mode": "strict"}})
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "**Review overlap (`strict`).**" in text
        assert "implementation units. They wait for its verdict." in text
        assert "not a lock on unrelated work" not in text


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


def _install_repo_harness_library(monkeypatch):
    """Put the working shared libraries before any stale installed copies."""
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(repo_root / "plugins" / "bootstrap"))
    monkeypatch.syspath_prepend(
        str(repo_root / "plugins" / "llm-scripting-kit" / "lib")
    )
    for name in tuple(sys.modules):
        if name == "llm_scripting_kit" or name.startswith("llm_scripting_kit."):
            monkeypatch.delitem(sys.modules, name, raising=False)
        if name == "bootstrap_lib" or name.startswith("bootstrap_lib."):
            monkeypatch.delitem(sys.modules, name, raising=False)


def _codex_command(rendered):
    match = re.search(
        r"### Codex CLI \(`codex`\).*?```\n(.*?)\n```", rendered, re.DOTALL
    )
    assert match, rendered
    return match.group(1)


class TestCommandTextProvider:
    def _codex_setup(self, monkeypatch, tmp_path, entries):
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        monkeypatch.setattr(og, "discover_model_definitions", lambda _root: (entries, []))
        monkeypatch.setattr(og, "detect_backend", lambda _backend: (True, "stubbed"))
        config, provenance = og.resolve_config(tmp_path / "no-project")
        return config, provenance

    def test_adapter_path_renders_the_adapter_argv(self, monkeypatch, tmp_path):
        _install_repo_harness_library(monkeypatch)
        entries = {
            "sol": {
                "id": "sol",
                "harness": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
            }
        }
        config, provenance = self._codex_setup(monkeypatch, tmp_path, entries)

        from bootstrap_lib import codex

        calls = []

        def fake_builder(**kwargs):
            calls.append(kwargs)
            return ["adapter-generated", kwargs["root"], kwargs["output_file"]]

        monkeypatch.setattr(codex, "build_codex_exec_argv", fake_builder)
        rendered = og.render(config, provenance)
        command = _codex_command(rendered)

        assert command == (
            f"adapter-generated {og._placeholder_path('root')} "
            f"{og._placeholder_path('result')}"
        )
        assert calls[0]["model"] == "gpt-5.6-sol"
        assert calls[0]["effort"] == "high"

    def test_fallback_command_is_reported_by_explain_when_library_is_unavailable(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
        config, provenance = self._codex_setup(monkeypatch, tmp_path, {})
        rendered = og.render(config, provenance)
        command = _codex_command(rendered)
        expected = og.fold(
            next(b["command"] for b in shipped()["backends"] if b["id"] == "codex")
        )
        assert command == expected

        assert og.main(["--explain", "--project-root", str(tmp_path / "project")]) == 0
        explained = capsys.readouterr().out
        assert (
            "command  note      backend `codex` command adapter unavailable; "
            "using fallback command from config (llm_scripting_kit unavailable"
            in explained
        )

    def test_adapter_command_contains_only_absolute_sentinels(self, monkeypatch, tmp_path):
        _install_repo_harness_library(monkeypatch)
        entries = {
            "sol": {
                "id": "sol",
                "harness": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
            }
        }
        config, provenance = self._codex_setup(monkeypatch, tmp_path, entries)
        command = _codex_command(og.render(config, provenance))

        root = og._placeholder_path("root")
        result = og._placeholder_path("result")
        assert og.os.path.isabs(root)
        assert og.os.path.isabs(result)
        assert root in command
        assert result in command
        assert str(tmp_path) not in command
        assert str(Path.cwd()) not in command


class TestCli:
    def test_paths_lists_four_layers(self, capsys, layered):
        assert og.main(["--paths", "--project-root", str(layered.project_root)]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert [line.split("\t")[0] for line in lines] == [
            "shipped",
            "machine",
            "user",
            "project",
        ]

    def test_explain_prints_provenance_then_resolved_config(self, capsys, layered):
        layered("shipped", {"default_tier": "workhorse"})
        layered("user", {"default_tier": "top"})
        assert og.main(["--explain", "--project-root", str(layered.project_root)]) == 0
        out = capsys.readouterr().out
        assert "shipped  applied" in out
        assert "default_tier: top" in out

    def test_explain_uses_config_row_numbers_for_surviving_routes(self, capsys, layered):
        layered(
            "shipped",
            cfg(
                routing=[
                    {"shape": ["novel"], "models": ["missing"]},
                    {"shape": ["open"], "models": ["agent:haiku"]},
                    {"shape": [], "models": ["agent:sonnet"]},
                ]
            ),
        )
        assert og.main(["--explain", "--project-root", str(layered.project_root)]) == 0
        out = capsys.readouterr().out
        assert "routing  note      routing row 1:" in out
        assert "routing  row       2: open -> haiku" in out
        assert "routing  row       3: default -> sonnet" in out
        assert "routing  row       1: open -> haiku" not in out

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
        layered(
            "shipped",
            cfg(routing=[{"shape": ["novel"], "models": ["agent:fable", "agent:sonnet"]}]),
        )
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "1. If `novel`" in text
        assert "try **fable**, then **sonnet**" in text
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
        routing = re.split(r"^## \d+\. Routing\n", without, maxsplit=1, flags=re.M)[1]
        routing = routing.split("\n## ", 1)[0]
        assert re.search(
            r"If `novel`(?: \([^)]*\))? \+ `load-bearing`(?: \([^)]*\))?: try \*\*fable\*\*\.",
            routing,
        )

    def test_model_specific_content_follows_surviving_routes(self, without, with_codex):
        assert "delegating plan cross-check to codex/sol (cross-check)" in with_codex
        assert "delegating per-file API migration to codex/luna (fan-out)" in with_codex
        assert "delegating plan cross-check to codex/sol" not in without
        assert "delegating per-file API migration to codex/luna" not in without
        assert "luna carries the higher default" not in without
        assert "Raise sol from `high` to `max`" not in without

    def test_codex_effort_note_renders_when_codex_is_present(self, with_codex):
        assert "Codex-side, effort is a real dial set per dispatch" in with_codex

    def test_second_family_gap_is_disclosed_without_codex(self, without):
        assert "no second-family child -- dispatch the primary review alone" in without


class TestRegistryOnlyHarnessIsNotRoutable:
    """A harness known only through the model registry (no backends[] record)
    renders as an identity-only section but never routes -- a model a row
    prefers with no launch mechanics is worse than an absent one."""

    @pytest.fixture
    def rendered(self, monkeypatch, layered):
        layered("shipped", cfg(routing=[
            {"shape": ["novel"], "models": ["dawn", "agent:fable"]},
            {"shape": [], "models": ["agent:sonnet"]},
        ]))
        monkeypatch.setattr(
            og,
            "discover_model_definitions",
            lambda _root: ({
                "dawn": {"id": "dawn", "harness": "opencode", "model": "some/model"},
            }, []),
        )
        real = og.detect_backend
        monkeypatch.setattr(og, "detect_backend", lambda backend: (
            (True, "stubbed opencode")
            if backend.get("id") == "opencode"
            else real(backend)
        ))
        config, provenance = og.resolve_config(layered.project_root)
        return og.render(config, provenance)

    def test_registry_only_model_does_not_route(self, rendered):
        body = rendered.split("\n---\n")[0]
        assert "opencode/dawn" not in body

    def test_the_row_survives_on_its_next_model(self, rendered):
        assert "try **fable**" in rendered

    def test_identity_section_is_marked_not_dispatchable(self, rendered):
        assert "### Opencode (`opencode`)" in rendered
        assert "**Not dispatchable.**" in rendered
        assert "`dawn`" in rendered

    def test_skip_note_names_the_missing_record(self, layered):
        layered("shipped", cfg(routing=[{"shape": ["novel"], "models": ["dawn"]}]))
        config, _ = og.resolve_config(layered.project_root)
        entries = {"dawn": {"id": "dawn", "harness": "opencode", "model": "some/model"}}
        routes, notes = og.resolve_routing_models(
            config, entries, {"opencode": (True, "stubbed")}
        )
        assert routes == []
        assert any("no backends[] record" in note for note in notes)

    def test_a_backends_record_makes_the_harness_routable(self, layered):
        layered("shipped", cfg(
            routing=[{"shape": ["novel"], "models": ["dawn"]}],
            backends=[
                {"id": "agent", "detect": {"always": True}},
                {
                    "id": "opencode",
                    "detect": {"always": True},
                    "dispatch": "opencode run --brief <file>",
                },
            ],
        ))
        config, _ = og.resolve_config(layered.project_root)
        entries = {"dawn": {"id": "dawn", "harness": "opencode", "model": "some/model"}}
        routes, _ = og.resolve_routing_models(
            config, entries, {"opencode": (True, "stubbed")}
        )
        assert routes and routes[0]["models"][0]["target"] == "opencode/dawn"

    def test_default_routable_ids_use_adapter_rendered_commands(self, layered, monkeypatch):
        layered("shipped", cfg(
            routing=[{"shape": ["novel"], "models": ["dawn"]}],
            backends=[
                {"id": "agent", "detect": {"always": True}},
                {"id": "opencode", "detect": {"always": True}},
            ],
        ))
        entries = {"dawn": {"id": "dawn", "harness": "opencode", "model": "some/model"}}
        monkeypatch.setattr(og, "adapter_command_text_provider", lambda *_args, **_kwargs: "opencode run")

        config, _ = og.resolve_config(layered.project_root)
        routes, _ = og.resolve_routing_models(
            config, entries, {"opencode": (True, "stubbed")}
        )
        assert routes and routes[0]["models"][0]["target"] == "opencode/dawn"

    def test_record_without_mechanics_is_not_routable_when_adapter_fails(
        self, layered, monkeypatch, capsys
    ):
        layered("shipped", cfg(
            shape={
                "title": "Shape",
                "tests": [{
                    "id": "backend-hole",
                    "text": "Choose a dispatchable backend.",
                    "without_backend": {"opencode": "Use the fallback route."},
                }],
            },
            routing=[{"shape": ["novel"], "models": ["dawn", "agent:fable"]}],
            backends=[
                {"id": "agent", "detect": {"always": True}},
                {"id": "opencode", "detect": {"always": True}},
            ],
        ))
        entries = {
            "dawn": {"id": "dawn", "harness": "opencode", "model": "some/model"}
        }
        monkeypatch.setattr(og, "discover_model_definitions", lambda _root: (entries, []))
        monkeypatch.setattr(og, "detect_backend", lambda _backend: (True, "stubbed"))
        monkeypatch.setattr(og, "adapter_command_text_provider", lambda *_args, **_kwargs: None)

        config, provenance = og.resolve_config(layered.project_root)
        rendered = og.render(config, provenance)
        body = rendered.split("\n---\n")[0]
        assert "opencode/dawn" not in body
        assert "try **fable**" in body
        assert "Use the fallback route." in body

        assert og.main(["--explain", "--project-root", str(layered.project_root)]) == 0
        explained = capsys.readouterr().out
        assert "no drivable dispatch mechanics" in explained


class TestNoBareCodenames:
    """Registry entry ids are the public routing names."""

    def test_shipped_routing_uses_entry_ids_not_provider_slugs(self):
        data = shipped()
        models = [model for row in data["routing"] for model in row["models"]]
        assert "sol" in models and "luna" in models
        assert all("gpt-5.6-" not in model for model in models)


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

    def test_fallthrough_attribution_uses_each_immediately_preceding_model(self, layered):
        layered(
            "shipped",
            cfg(
                routing=[
                    {
                        "shape": ["novel"],
                        "models": ["agent:fable", "agent:sonnet", "agent:haiku"],
                    }
                ]
            ),
        )
        config, provenance = og.resolve_config(layered.project_root)
        text = og.render(config, provenance)
        assert "failed model from `fable`." in text
        assert "failed model from `sonnet`." in text

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
        assert "delegating <what> to <target> (<the matched row's shape terms>)" in text
        assert "delegating rename across 30 files to sonnet (default)" in text

    def test_announcement_examples_use_only_skill_terms(self):
        data = shipped()
        skills = {t["id"] for t in data["lexicon"] if t.get("kind") == "skill"}
        for example in data["announce"]["examples"]:
            inside = re.search(r"\(([^)]*)\)$", example["text"]).group(1)
            terms = inside.split(";", 1)[0]
            assert {t.strip() for t in terms.split(",")} <= skills, example["id"]

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

    def test_a_project_routing_list_wins_over_a_machine_list(self, layered):
        layered("shipped", cfg())
        machine_routing = [{"shape": ["open"], "models": ["agent:haiku"]}]
        project_routing = [{"shape": ["novel"], "models": ["agent:fable"]}]
        layered("machine", {"routing": machine_routing})
        layered("project", {"routing": project_routing})

        config, provenance = og.resolve_config(layered.project_root)
        assert config["routing"] == project_routing
        text = og.render(config, provenance)
        assert "try **fable**." in text
        assert "If `open`: try **haiku**." not in text

    def test_a_user_layer_patches_a_lexicon_gloss(self, layered):
        layered("shipped", cfg())
        layered("user", {"lexicon": [{"id": "known", "gloss": "my own gloss"}]})
        config, provenance = og.resolve_config(layered.project_root)
        assert "`known` (my own gloss)" in og.render(config, provenance)

    def test_a_user_layer_tunes_only_the_parallel_development_razor(self, layered):
        base = yaml.safe_load(_shipped_path().read_text(encoding="utf-8"))
        layered("shipped", base)
        layered(
            "user",
            {
                "shape": {
                    "tests": [
                        {
                            "id": "parallel-development-razor",
                            "text": "CUSTOM PARALLEL THRESHOLD",
                        }
                    ]
                }
            },
        )
        config, provenance = og.resolve_config(layered.project_root)
        assert len(config["shape"]["tests"]) == len(base["shape"]["tests"])
        text = og.render(config, provenance)
        assert "CUSTOM PARALLEL THRESHOLD" in text
        assert "Split implementation only when EVERY" not in text



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

    def test_retired_capacity_override_warns_and_has_no_effect(self, monkeypatch, tmp_path):
        monkeypatch.setattr(og, "user_config_path", lambda: tmp_path / "none.yaml")
        config, provenance = og.resolve_config(tmp_path / "no-project")
        config["capacity"]["tier_overrides"] = {"top": "unavailable"}
        text = og.render(config, provenance)
        assert "`capacity.tier_overrides`" in text
        assert "does not affect routing or capacity" in text


class TestSelectionRendersForAnOverrideSuppliedBackend:
    """`selection` is live machinery no SHIPPED record reaches any more.

    The two request-only backends that carried it were deleted, but the
    renderer still emits it and SKILL.md still instructs an agent on how to
    obey it ("a backend whose block opens with a `**Selection.**` line is not a
    routing target"). A user or project override can still supply one, so the
    path is reachable and must stay pinned -- otherwise the feature can be
    deleted with every test still green while the shipped SKILL.md keeps
    promising it.
    """

    def test_a_backend_carrying_selection_renders_the_line(self):
        backend = {
            "id": "example-request-only",
            "label": "Example request-only backend",
            "selection": "ON EXPLICIT USER REQUEST ONLY.",
        }
        out: list = []
        og.render_backends(
            {"backends": [backend]},
            [(backend, True, "detected")],
            out,
            command_text_provider=lambda _b: None,
        )
        text = "\n".join(out)
        assert "**Selection.**" in text
        assert "ON EXPLICIT USER REQUEST ONLY." in text


class TestRenderedCodexCommandKeepsItsSilentFailureFlags:
    """The rendered command must not quietly lose a flag whose absence is silent.

    references/codex-dispatch.md states the session scratchpad sits outside the
    writable root under `-s workspace-write`, so a unit told to write there
    exits 0 having written nothing. The prose beneath the rendered command
    promises "every element is there because a probe showed its absence fails
    silently" -- a rendered command missing --add-dir makes that promise false.
    """

    @pytest.fixture(autouse=True)
    def _shared_libs_on_path(self, monkeypatch):
        # The provider degrades to the config fallback when the shared libs are
        # absent, and a test that accepted the fallback would pass while the
        # adapter dropped the flag. Put the libs on the path so the ADAPTER
        # path is the one under test.
        repo = Path(__file__).resolve().parents[2]
        for lib in (repo / "plugins" / "llm-scripting-kit" / "lib", repo / "plugins" / "bootstrap"):
            monkeypatch.syspath_prepend(str(lib))

    def test_the_adapter_rendered_command_grants_the_scratchpad(self):
        rendered = og.adapter_command_text_provider(
            {"id": "codex", "command": "codex exec ... -"},
            model_entries={
                "sol": {
                    "id": "sol",
                    "kind": "harness",
                    "harness": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                }
            },
            notes=[],
        )
        assert rendered is not None
        assert "--add-dir" in rendered, (
            "the rendered codex command dropped --add-dir; the scratchpad is "
            "outside the writable root, so a write there fails silently"
        )


# --------------------------------------------------------------------------
# User-layer location
# --------------------------------------------------------------------------


class TestUserConfigLocation:
    """Portable user and machine-local configuration have separate layers.

    ``~/.claude/config/`` is the conventional home for portable user
    configuration; a plugin's data directory is chartered for machine-global
    values that stay out of version control.
    """

    def test_conventional_path_is_the_tracked_config_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(og.Path, "home", staticmethod(lambda: tmp_path))
        assert og.user_config_dir_path() == tmp_path / ".claude" / "config" / og.CONFIG_NAME

    def test_machine_path_is_the_plugin_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(og.Path, "home", staticmethod(lambda: tmp_path))
        expected = (
            tmp_path / ".claude" / "plugins" / "data"
            / og.MARKETPLACE / og.PLUGIN / og.CONFIG_NAME
        )
        assert og.machine_config_path() == expected
        assert og.legacy_user_config_path() == expected

    def test_prefers_conventional_when_present(self, monkeypatch, tmp_path):
        monkeypatch.setattr(og.Path, "home", staticmethod(lambda: tmp_path))
        conventional = og.user_config_dir_path()
        conventional.parent.mkdir(parents=True)
        conventional.write_text("resolution: from-config-dir\n", encoding="utf-8")
        assert og.user_config_path() == conventional

    def test_portable_user_layer_overrides_machine_layer(self, monkeypatch, tmp_path):
        monkeypatch.setattr(og.Path, "home", staticmethod(lambda: tmp_path))
        conventional = og.user_config_dir_path()
        conventional.parent.mkdir(parents=True)
        conventional.write_text("resolution: from-config-dir\n", encoding="utf-8")
        machine = og.machine_config_path()
        machine.parent.mkdir(parents=True)
        machine.write_text("resolution: from-data-dir\n", encoding="utf-8")

        project_root = tmp_path / "project"
        (project_root / ".claude").mkdir(parents=True)
        assert og.user_config_path() == conventional
        config, provenance = og.resolve_config(project_root)
        assert config["resolution"] == "from-config-dir"
        assert [layer for layer, _path, status in provenance if og.status_is_applied(status)] == [
            "shipped", "machine", "user"
        ]

    def test_defaults_to_conventional_when_neither_exists(self, monkeypatch, tmp_path):
        """An absent portable layer reports its own path."""
        monkeypatch.setattr(og.Path, "home", staticmethod(lambda: tmp_path))
        assert og.user_config_path() == og.user_config_dir_path()

    def test_machine_use_is_flagged_in_provenance(self, monkeypatch, tmp_path, capsys):
        """--explain identifies the machine-local layer and portable alternative."""
        monkeypatch.setattr(og.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(og, "DEFAULTS_PATH", tmp_path / "absent-shipped.yaml")
        machine = og.machine_config_path()
        machine.parent.mkdir(parents=True)
        machine.write_text("resolution: from-data-dir\n", encoding="utf-8")

        project_root = tmp_path / "project"
        (project_root / ".claude").mkdir(parents=True)
        _config, provenance = og.resolve_config(project_root)

        machine_entries = [row for row in provenance if row[0] == "machine"]
        assert len(machine_entries) == 1
        _layer, path, status = machine_entries[0]
        assert path == machine
        assert "machine-local" in status
        assert "NOTE: decision-half keys found" in status
        assert "keep portable policy in" in status
        assert str(og.user_config_dir_path()) in status

        assert og.main(["--explain", "--project-root", str(project_root)]) == 0
        explained = capsys.readouterr().out
        assert "NOTE: decision-half keys found" in explained
        assert str(og.user_config_dir_path()) in explained

    def test_machine_only_data_does_not_trigger_portable_policy_note(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(og.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(og, "DEFAULTS_PATH", tmp_path / "absent-shipped.yaml")
        machine = og.machine_config_path()
        machine.parent.mkdir(parents=True)
        machine.write_text("capacity:\n  source: none\n", encoding="utf-8")

        project_root = tmp_path / "project"
        (project_root / ".claude").mkdir(parents=True)
        _config, provenance = og.resolve_config(project_root)

        machine_entries = [row for row in provenance if row[0] == "machine"]
        assert machine_entries[0][2] == "applied (machine-local)"


class TestAppliedStatusDecoration:
    """A decorated 'applied' status still counts as applied.

    Two provenance statuses carry a suffix -- the project layer's
    stripped-executable-fields note and the machine layer's provenance note.
    An exact match on "applied" reported those layers as NOT applied, so a
    user's in-force policy vanished from the rendered "Layers applied" line.
    """

    def test_bare_applied_counts(self):
        assert og.status_is_applied("applied")

    def test_absent_and_empty_do_not_count(self):
        assert not og.status_is_applied("absent")
        assert not og.status_is_applied("empty")

    def test_machine_provenance_decoration_counts(self):
        assert og.status_is_applied("applied (machine-local; keep portable policy in /x/y.yaml)")

    def test_executable_stripped_decoration_counts(self):
        assert og.status_is_applied("applied (1 executable field(s) ignored: command)")

    def test_machine_layer_is_listed_as_applied(self, monkeypatch, tmp_path, capsys):
        """End to end: the machine-local layer appears in 'Layers applied'."""
        monkeypatch.setattr(og.Path, "home", staticmethod(lambda: tmp_path))
        machine = og.machine_config_path()
        machine.parent.mkdir(parents=True)
        machine.write_text("resolution: from-data-dir\n", encoding="utf-8")

        project_root = tmp_path / "project"
        (project_root / ".claude").mkdir(parents=True)
        _config, provenance = og.resolve_config(project_root)

        applied = [layer for layer, _p, status in provenance if og.status_is_applied(status)]
        assert "machine" in applied
