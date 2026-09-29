"""Tests for the model-endpoints registry reader.

Every test redirects the home directory, so the CONVENTION path resolves inside
a tmp_path and no test can read (or create) the developer's real
``~/.claude/config/model-endpoints.yaml``.
"""

import pytest

from llm_scripting_kit.model_endpoints import (
    REGISTRY_ENV,
    EndpointRegistryError,
    default_registry_path,
    load_endpoint_registry,
    resolve_registry_entry,
)


VALID_YAML = """\
version: 1
default: alpha
models:
  alpha:
    name: Alpha on a box
    base_url: http://alpha.invalid:8080/v1
    model: alpha-27b
    context_window: 262144
    reasoning_effort: medium
    effort_style: top-level
  beta:
    base_url: http://beta.invalid:8080/v1
    model: beta-9b
    key_env: BETA_API_KEY
"""

MIXED_YAML = """\
version: 1
default: alpha
models:
  alpha:
    base_url: http://alpha.invalid/v1
    model: alpha-1
  sol:
    harness: codex
    model: gpt-5.6-sol
    effort: high
    name: Sol
"""


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect ``Path.home()`` at a tmp dir on every platform.

    Both variables are set because ``expanduser`` reads HOME on POSIX and
    USERPROFILE on Windows, and this suite runs on both.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv(REGISTRY_ENV, raising=False)
    return home


def _write_convention(home, text):
    path = home / ".claude" / "config" / "model-endpoints.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestPathResolution:
    def test_convention_path_is_home_relative(self, fake_home):
        assert default_registry_path() == (
            fake_home / ".claude" / "config" / "model-endpoints.yaml"
        )

    def test_no_override_no_file_is_an_empty_registry(self, fake_home):
        reg = load_endpoint_registry()
        assert reg.entries == {}
        assert reg.default_id is None
        assert reg.path is None

    def test_override_wins_over_an_existing_convention_file(
        self, fake_home, tmp_path, monkeypatch
    ):
        _write_convention(fake_home, VALID_YAML)
        other = tmp_path / "elsewhere.yaml"
        other.write_text(
            "models:\n  only:\n    base_url: http://only.invalid/v1\n    model: only-1\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(REGISTRY_ENV, str(other))
        reg = load_endpoint_registry()
        assert sorted(reg.entries) == ["only"]
        assert reg.path == other

    def test_override_expands_a_tilde(self, fake_home, monkeypatch):
        target = fake_home / "reg.yaml"
        target.write_text(
            "models:\n  a:\n    base_url: http://a.invalid/v1\n    model: a-1\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(REGISTRY_ENV, "~/reg.yaml")
        reg = load_endpoint_registry()
        assert sorted(reg.entries) == ["a"]

    def test_dangling_override_is_loud(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.setenv(REGISTRY_ENV, str(tmp_path / "nope.yaml"))
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert REGISTRY_ENV in str(exc.value)
        assert "nope.yaml" in str(exc.value)

    def test_explicit_environ_mapping_is_honored(self, fake_home, tmp_path):
        target = tmp_path / "explicit.yaml"
        target.write_text(
            "models:\n  x:\n    base_url: http://x.invalid/v1\n    model: x-1\n",
            encoding="utf-8",
        )
        reg = load_endpoint_registry({REGISTRY_ENV: str(target)})
        assert sorted(reg.entries) == ["x"]


class TestSchema:
    def test_valid_file_parses_every_field(self, fake_home):
        path = _write_convention(fake_home, VALID_YAML)
        reg = load_endpoint_registry()
        assert reg.default_id == "alpha"
        assert reg.path == path
        alpha = reg.entries["alpha"]
        assert alpha.id == "alpha"
        assert alpha.base_url == "http://alpha.invalid:8080/v1"
        assert alpha.model == "alpha-27b"
        assert alpha.name == "Alpha on a box"
        assert alpha.context_window == 262144
        assert alpha.reasoning_effort == "medium"
        assert alpha.key_env is None  # omitted = keyless
        assert reg.entries["beta"].key_env == "BETA_API_KEY"

    def test_routing_parses_for_transport_and_unknown_keys_are_noted(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n  alpha:\n    base_url: http://alpha/v1\n    model: alpha\n"
            "    routing:\n      group: qwen\n      order: 2\n"
            "      max_parallel: 4\n      effort_style: chat_template_kwargs\n"
            "      future: ignored\n",
        )
        reg = load_endpoint_registry()
        assert reg.entries["alpha"].routing.group == "qwen"
        assert reg.entries["alpha"].routing.max_parallel == 4
        assert reg.entries["alpha"].routing.effort_style == "chat_template_kwargs"
        assert any("future" in note for note in reg.notes)

    def test_bad_max_parallel_keeps_transport_usable(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n  alpha:\n    base_url: http://alpha/v1\n    model: alpha\n"
            "    routing: {group: qwen, max_parallel: many}\n",
        )
        reg = load_endpoint_registry()
        assert reg.entries["alpha"].routing.max_parallel is None
        assert any("max_parallel" in note for note in reg.notes)

    def test_missing_routing_is_none(self, fake_home):
        _write_convention(fake_home, "models:\n  alpha:\n    base_url: http://alpha/v1\n    model: alpha\n")
        assert load_endpoint_registry().entries["alpha"].routing is None

    def test_unknown_keys_are_ignored(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n  a:\n    base_url: http://a.invalid/v1\n"
            "    model: a-1\n    someday: whatever\n",
        )
        assert load_endpoint_registry().entries["a"].model == "a-1"

    def test_malformed_yaml_at_the_convention_path_raises(self, fake_home):
        path = _write_convention(fake_home, "models: [unclosed\n")
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert str(path) in str(exc.value)

    def test_a_non_mapping_document_raises(self, fake_home):
        _write_convention(fake_home, "- a\n- b\n")
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert "not a YAML mapping" in str(exc.value)

    def test_missing_models_map_raises(self, fake_home):
        _write_convention(fake_home, "version: 1\ndefault: alpha\n")
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert "no 'models' map" in str(exc.value)

    def test_mixed_transport_and_harness_entries_load(self, fake_home):
        _write_convention(fake_home, MIXED_YAML)
        reg = load_endpoint_registry()

        assert reg.entries["alpha"].kind == "transport"
        assert reg.entries["alpha"].base_url == "http://alpha.invalid/v1"
        sol = reg.entries["sol"]
        assert sol.kind == "harness"
        assert sol.base_url is None
        assert sol.harness == "codex"
        assert sol.model == "gpt-5.6-sol"
        assert sol.effort == "high"
        assert sol.name == "Sol"

    def test_unknown_kind_is_skipped_and_noted(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n"
            "  alpha:\n"
            "    base_url: http://alpha.invalid/v1\n"
            "    model: alpha-27b\n"
            "  sol:\n"
            "    harness: codex\n"
            "    model: gpt-5.6-sol\n"
            "  mystery:\n"
            "    model: mystery-1\n",
        )
        reg = load_endpoint_registry()
        assert sorted(reg.entries) == ["alpha", "sol"]
        assert len(reg.notes) == 1
        assert "mystery" in reg.notes[0]
        assert "unknown kind" in reg.notes[0]

    def test_both_addresses_are_skipped_and_conflict_is_noted(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n  alpha:\n    base_url: http://a.invalid/v1\n"
            "    harness: codex\n    model: alpha-27b\n",
        )
        reg = load_endpoint_registry()
        assert reg.entries == {}
        assert "alpha" in reg.notes[0]
        assert "both 'base_url' and 'harness'" in reg.notes[0]

    def test_transport_entry_without_model_is_skipped_and_noted(self, fake_home):
        _write_convention(
            fake_home, "models:\n  alpha:\n    base_url: http://a.invalid/v1\n"
        )
        reg = load_endpoint_registry()
        assert reg.entries == {}
        assert "alpha" in reg.notes[0]
        assert "'model'" in reg.notes[0]

    def test_entry_that_is_not_a_mapping_is_skipped_and_noted(self, fake_home):
        _write_convention(fake_home, "models:\n  alpha: http://a.invalid/v1\n")
        reg = load_endpoint_registry()
        assert reg.entries == {}
        assert "alpha" in reg.notes[0]
        assert "not a mapping" in reg.notes[0]

    def test_non_integer_context_window_is_skipped_and_noted(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n  alpha:\n    base_url: http://a.invalid/v1\n"
            "    model: a-1\n    context_window: lots\n",
        )
        reg = load_endpoint_registry()
        assert reg.entries == {}
        assert "alpha" in reg.notes[0]
        assert "context_window" in reg.notes[0]

    def test_default_naming_no_entry_raises(self, fake_home):
        _write_convention(
            fake_home,
            "default: missing\nmodels:\n  alpha:\n"
            "    base_url: http://a.invalid/v1\n    model: a-1\n",
        )
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert "missing" in str(exc.value)
        assert "alpha" in str(exc.value)

    def test_default_naming_skipped_entry_reports_the_skip_not_absence(self, fake_home):
        # The entry IS in the file; it was skipped. Saying "names no entry"
        # would be false and would send the reader to the `default:` line
        # instead of the defective entry, so the raise carries the skip note.
        _write_convention(
            fake_home,
            "default: mystery\nmodels:\n  mystery:\n"
            "    model: mystery-1\n  alpha:\n"
            "    base_url: http://a.invalid/v1\n    model: a-1\n",
        )
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        message = str(exc.value)
        assert "mystery" in message
        assert "could not be loaded" in message
        assert "unknown kind" in message
        assert "names no entry" not in message

    def test_default_naming_an_absent_id_still_says_names_no_entry(self, fake_home):
        # The other half of the same branch: a `default:` typo names nothing in
        # the file, and there the original wording is the accurate one.
        _write_convention(
            fake_home,
            "default: nosuch\nmodels:\n  alpha:\n"
            "    base_url: http://a.invalid/v1\n    model: a-1\n",
        )
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        message = str(exc.value)
        assert "names no entry" in message
        assert "alpha" in message

    def test_no_default_is_allowed(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n  alpha:\n    base_url: http://a.invalid/v1\n    model: a-1\n",
        )
        assert load_endpoint_registry().default_id is None


class TestResolveRegistryEntry:
    def test_none_resolves_the_default_entry(self, fake_home):
        _write_convention(fake_home, VALID_YAML)
        assert resolve_registry_entry(None).id == "alpha"

    def test_explicit_id_resolves_that_entry(self, fake_home):
        _write_convention(fake_home, VALID_YAML)
        assert resolve_registry_entry("beta").model == "beta-9b"

    def test_default_harness_entry_is_refused_by_transport_resolution(self, fake_home):
        _write_convention(fake_home, MIXED_YAML.replace("default: alpha", "default: sol"))
        with pytest.raises(EndpointRegistryError) as exc:
            resolve_registry_entry(None)
        msg = str(exc.value)
        assert "sol" in msg
        assert "harness" in msg
        assert "codex" in msg

    def test_unknown_id_lists_the_known_ids(self, fake_home):
        _write_convention(fake_home, VALID_YAML)
        with pytest.raises(EndpointRegistryError) as exc:
            resolve_registry_entry("gamma")
        assert "gamma" in str(exc.value)
        assert "alpha, beta" in str(exc.value)

    def test_no_default_declared_raises(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n  alpha:\n    base_url: http://a.invalid/v1\n    model: a-1\n",
        )
        with pytest.raises(EndpointRegistryError) as exc:
            resolve_registry_entry(None)
        assert "default" in str(exc.value)

    def test_an_injected_registry_skips_the_file_read(self, fake_home):
        _write_convention(fake_home, VALID_YAML)
        reg = load_endpoint_registry()
        (fake_home / ".claude" / "config" / "model-endpoints.yaml").unlink()
        assert resolve_registry_entry("beta", registry=reg).id == "beta"


def test_routing_effort_style_accepts_ninfer(tmp_path):
    from llm_scripting_kit.model_endpoints import load_endpoint_registry

    path = tmp_path / "m.yaml"
    path.write_text(
        "models:\n  a:\n    base_url: http://h/v1\n    model: m\n"
        "    routing: {group: g, effort_style: ninfer}\n",
        encoding="utf-8",
    )
    registry = load_endpoint_registry({"MODEL_ENDPOINTS_REGISTRY": str(path)})
    assert registry.entries["a"].routing.effort_style == "ninfer"
    assert registry.notes == []


# ---------------------------------------------------------------------------
# Migration step 3 (L2): the shipped registry carries every core id
# ---------------------------------------------------------------------------


class TestShippedCoreEntries:
    """Every core id is a shipped ``harness: claude`` entry, haiku included."""

    def test_haiku_ships_as_a_tier_one_claude_entry(self):
        from llm_scripting_kit import DEFAULT_MODEL_CONFIG, discover_model_entries

        haiku = discover_model_entries(config=DEFAULT_MODEL_CONFIG)["haiku"]
        assert haiku.kind == "harness"
        assert haiku.harness == "claude"
        assert haiku.model == "haiku"
        assert haiku.tier == 1
        assert haiku.family == "anthropic"
        assert haiku.base_url is None

    def test_every_core_id_is_shipped_on_the_claude_harness(self):
        from bootstrap_lib.model_declaration import CORE_IDS
        from llm_scripting_kit import DEFAULT_MODEL_CONFIG, discover_model_entries
        from llm_scripting_kit.declaration import check_registry_entry

        found = discover_model_entries(config=DEFAULT_MODEL_CONFIG)
        for core in sorted(CORE_IDS):
            assert core in found, core
            assert check_registry_entry(core, found[core]) is None

    def test_haiku_describes_as_an_agent_entry_in_session(self, fake_home):
        from llm_scripting_kit import DEFAULT_MODEL_CONFIG, discover_model_entries
        from llm_scripting_kit.declaration import describe
        from llm_scripting_kit.reachability import Reachability

        entries = discover_model_entries(config=DEFAULT_MODEL_CONFIG)
        ranking = describe(
            ["haiku"], caller="session", entries=entries,
            reachability_cache={"haiku": Reachability("reachable", "cli-version", "ok")},
        )
        assert ranking.default.id == "haiku"
        assert ranking.default.drive == "agent"
        assert ranking.default.tier == 1


class TestDeclarationRenderAgainstTheRegistry:
    """Filtered render and floor over a real registry file (R18-R21, R23)."""

    REGISTRY = """\
version: 1
models:
  local-box:
    base_url: http://local-box.invalid/v1
    model: local-27b
  opencode-pro:
    harness: opencode
    model: provider/pro
"""

    def _entries(self, fake_home):
        from llm_scripting_kit import DEFAULT_MODEL_CONFIG, discover_model_entries

        _write_convention(fake_home, self.REGISTRY)
        return discover_model_entries(config=DEFAULT_MODEL_CONFIG)

    def test_session_render_hides_the_transport_and_the_typo(self, fake_home):
        from llm_scripting_kit.declaration import describe
        from llm_scripting_kit.reachability import Reachability

        entries = self._entries(fake_home)
        ranking = describe(
            ["local-box", "sol", "sonet", "opencode-pro"], caller="session", entries=entries,
            reachability_cache={
                "sol": Reachability("reachable", "cli-version", "ok"),
                "opencode-pro": Reachability("unreachable", "cli-version", "missing"),
            },
        )
        assert [e.id for e in ranking.rendered_entries] == ["sol", "opencode-pro"]
        assert "local-box" not in ranking.render() and "sonet" not in ranking.render()
        assert [d.disposition for d in ranking.dispositions] == [
            "unroutable", "usable", "unresolved", "unreachable",
        ]

    def test_process_render_routes_the_transport(self, fake_home):
        from llm_scripting_kit.declaration import describe
        from llm_scripting_kit.reachability import Reachability

        entries = self._entries(fake_home)
        ranking = describe(
            ["local-box", "sol"], caller="process", entries=entries,
            reachability_cache={
                "local-box": Reachability("reachable", "models-endpoint", "ok"),
                "sol": Reachability("reachable", "cli-version", "ok"),
            },
        )
        assert ranking.default.id == "local-box"

    def test_floor_itemises_hidden_entries_from_the_registry(self, fake_home):
        from llm_scripting_kit.declaration import NoUsableRoutingTarget, describe

        entries = self._entries(fake_home)
        with pytest.raises(NoUsableRoutingTarget) as excinfo:
            describe(["local-box", "sonet"], caller="session", entries=entries,
                     reachability_cache={})
        assert [(d.id, d.disposition) for d in excinfo.value.dispositions] == [
            ("local-box", "unroutable"), ("sonet", "unresolved"),
        ]


class TestFrontdoorAndBillingMarkers:
    def test_invalid_frontdoor_is_noted_false_and_keeps_default_entry(self, fake_home):
        _write_convention(
            fake_home,
            "default: alpha\nmodels:\n  alpha:\n    base_url: http://alpha/v1\n"
            "    model: alpha\n    frontdoor: 'yes'\n",
        )
        reg = load_endpoint_registry()
        assert reg.default_id == "alpha"
        assert reg.entries["alpha"].frontdoor is False
        assert any("frontdoor" in n and "alpha" in n for n in reg.notes)

    def test_frontdoor_true_and_omitted(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n  fd:\n    base_url: http://fd/v1\n    model: grp\n    frontdoor: true\n"
            "  plain:\n    base_url: http://p/v1\n    model: m\n",
        )
        reg = load_endpoint_registry()
        assert reg.entries["fd"].frontdoor is True
        assert reg.entries["plain"].frontdoor is False
        assert reg.notes == []

    def test_billing_modes_parse(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n"
            "  a:\n    base_url: http://a/v1\n    model: m\n    billing: {mode: unmetered}\n"
            "  b:\n    base_url: http://b/v1\n    model: m\n    billing: {mode: provider-reported}\n"
            "  c:\n    base_url: http://c/v1\n    model: m\n",
        )
        reg = load_endpoint_registry()
        assert reg.entries["a"].billing_mode == "unmetered"
        assert reg.entries["b"].billing_mode == "provider-reported"
        assert reg.entries["c"].billing_mode is None

    def test_invalid_billing_is_noted_and_unset(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n"
            "  a:\n    base_url: http://a/v1\n    model: m\n    billing: {mode: free}\n"
            "  b:\n    base_url: http://b/v1\n    model: m\n    billing: nope\n"
            "  c:\n    base_url: http://c/v1\n    model: m\n    billing: {}\n",
        )
        reg = load_endpoint_registry()
        assert set(reg.entries) == {"a", "b", "c"}  # entries retained
        assert reg.entries["a"].billing_mode is None
        assert reg.entries["b"].billing_mode is None
        assert reg.entries["c"].billing_mode is None
        assert any("'a'" in n and "billing" in n for n in reg.notes)
        assert any("'b'" in n and "billing" in n for n in reg.notes)


# ---------------------------------------------------------------------------
# Effort delivery: entry-level effort_style, declared vs defaulted routing
# style, the direct-call resolution order, and the registry warnings.
# ---------------------------------------------------------------------------


def _load_text(tmp_path, text):
    path = tmp_path / "m.yaml"
    path.write_text(text, encoding="utf-8")
    return load_endpoint_registry({REGISTRY_ENV: str(path)})


def _entry_yaml(name, extra=""):
    return f"  {name}:\n    base_url: http://{name}.invalid/v1\n    model: {name}-m\n{extra}"


class TestEffortStyleSchema:
    def test_entry_level_effort_style_parses(self, tmp_path):
        reg = _load_text(tmp_path, "models:\n" + _entry_yaml("a", "    effort_style: ninfer\n"))
        entry = reg.entries["a"]
        assert (entry.effort_style, entry.effort_style_declared) == ("ninfer", True)
        assert reg.notes == []

    def test_entry_level_unsupported_is_valid(self, tmp_path):
        reg = _load_text(tmp_path, "models:\n" + _entry_yaml("a", "    effort_style: unsupported\n"))
        assert reg.entries["a"].effort_style == "unsupported"
        assert reg.notes == []

    def test_invalid_entry_level_style_is_noted_and_resolves_to_none(self, tmp_path):
        from llm_scripting_kit.model_endpoints import resolve_effort_style

        reg = _load_text(
            tmp_path,
            "models:\n"
            + _entry_yaml(
                "a",
                "    effort_style: toplevel\n    frontdoor: true\n"
                "    routing: {group: g, effort_style: ninfer}\n",
            ),
        )
        entry = reg.entries["a"]
        assert (entry.effort_style, entry.effort_style_declared) == (None, True)
        assert any("'a'" in n and "invalid 'effort_style'" in n for n in reg.notes)
        # Declared-invalid does NOT fall through to frontdoor or routing.
        delivery = resolve_effort_style(entry)
        assert (delivery.style, delivery.source, delivery.deliverable) == (None, "endpoint", False)

    def test_routing_style_declared_vs_defaulted(self, tmp_path):
        reg = _load_text(
            tmp_path,
            "models:\n"
            + _entry_yaml("declared", "    routing: {group: g, effort_style: chat_template_kwargs}\n")
            + _entry_yaml("defaulted", "    routing: {group: g}\n"),
        )
        declared = reg.entries["declared"].routing
        defaulted = reg.entries["defaulted"].routing
        assert (declared.effort_style, declared.effort_style_declared) == ("chat_template_kwargs", True)
        assert (defaulted.effort_style, defaulted.effort_style_declared) == ("top-level", False)
        assert reg.notes == []

    def test_routing_accepts_unsupported(self, tmp_path):
        reg = _load_text(
            tmp_path, "models:\n" + _entry_yaml("a", "    routing: {group: g, effort_style: unsupported}\n")
        )
        assert reg.entries["a"].routing.effort_style == "unsupported"
        assert reg.notes == []

    def test_invalid_routing_style_is_noted_and_is_none(self, tmp_path):
        from llm_scripting_kit.model_endpoints import deployment_effort_style, resolve_effort_style

        reg = _load_text(
            tmp_path, "models:\n" + _entry_yaml("a", "    routing: {group: g, effort_style: nope}\n")
        )
        routing = reg.entries["a"].routing
        assert (routing.effort_style, routing.effort_style_declared) == (None, True)
        assert any("routing has invalid 'effort_style'" in n for n in reg.notes)
        # Neither a direct call nor the front door guesses a wire format.
        assert resolve_effort_style(reg.entries["a"]).deliverable is False
        assert deployment_effort_style(reg.entries["a"]) is None


class TestEffortResolutionOrder:
    def _resolve(self, tmp_path, extra):
        from llm_scripting_kit.model_endpoints import resolve_effort_style

        reg = _load_text(tmp_path, "models:\n" + _entry_yaml("a", extra))
        return resolve_effort_style(reg.entries["a"])

    def test_endpoint_beats_frontdoor_and_routing(self, tmp_path):
        d = self._resolve(
            tmp_path,
            "    effort_style: chat_template_kwargs\n    frontdoor: true\n"
            "    routing: {group: g, effort_style: ninfer}\n",
        )
        assert (d.style, d.source) == ("chat_template_kwargs", "endpoint")

    def test_frontdoor_beats_declared_routing(self, tmp_path):
        d = self._resolve(
            tmp_path, "    frontdoor: true\n    routing: {group: g, effort_style: ninfer}\n"
        )
        assert (d.style, d.source) == ("top-level", "frontdoor")

    def test_declared_routing_is_the_fallback(self, tmp_path):
        d = self._resolve(tmp_path, "    routing: {group: g, effort_style: ninfer}\n")
        assert (d.style, d.source, d.deliverable) == ("ninfer", "routing", True)

    def test_nothing_declared_is_none(self, tmp_path):
        d = self._resolve(tmp_path, "")
        assert (d.style, d.source, d.deliverable) == (None, "none", False)

    def test_routing_without_style_is_not_deliverable_directly_but_is_top_level_at_the_front_door(
        self, tmp_path
    ):
        """The paid-spillover shape: routing with no effort_style."""
        from llm_scripting_kit.model_endpoints import deployment_effort_style, resolve_effort_style

        reg = _load_text(tmp_path, "models:\n" + _entry_yaml("a", "    routing: {group: g, order: 3}\n"))
        entry = reg.entries["a"]
        assert resolve_effort_style(entry).deliverable is False
        assert resolve_effort_style(entry).source == "none"
        assert deployment_effort_style(entry) == "top-level"

    def test_deployment_style_prefers_the_entry_level_style(self, tmp_path):
        from llm_scripting_kit.model_endpoints import deployment_effort_style

        reg = _load_text(
            tmp_path,
            "models:\n" + _entry_yaml("a", "    effort_style: unsupported\n    routing: {group: g}\n"),
        )
        assert deployment_effort_style(reg.entries["a"]) == "unsupported"

    def test_harness_entry_resolves_to_none(self, tmp_path):
        from llm_scripting_kit.model_endpoints import resolve_effort_style

        reg = _load_text(tmp_path, "models:\n  h:\n    harness: codex\n    model: m\n")
        assert resolve_effort_style(reg.entries["h"]).source == "none"


class TestEffortValidation:
    def test_reasoning_effort_without_a_deliverable_style_is_an_error(self, tmp_path):
        with pytest.raises(EndpointRegistryError) as exc:
            _load_text(tmp_path, "models:\n" + _entry_yaml("a", "    reasoning_effort: medium\n"))
        message = str(exc.value)
        assert "entry 'a'" in message
        assert "reasoning_effort 'medium'" in message
        assert "declare an effort_style" in message
        assert "remove reasoning_effort" in message

    def test_reasoning_effort_with_a_delivering_style_loads(self, tmp_path):
        reg = _load_text(
            tmp_path,
            "models:\n" + _entry_yaml(
                "a", "    reasoning_effort: medium\n    effort_style: top-level\n"
            ),
        )
        assert reg.entries["a"].reasoning_effort == "medium"
        assert reg.notes == []

    def test_conflicting_declared_styles_warn(self, tmp_path):
        reg = _load_text(
            tmp_path,
            "models:\n"
            + _entry_yaml(
                "a", "    effort_style: chat_template_kwargs\n    routing: {group: g, effort_style: ninfer}\n"
            ),
        )
        assert len(reg.notes) == 1
        assert "overrides its routing effort_style 'ninfer'" in reg.notes[0]

    def test_current_fleet_shaped_registry_loads_with_deliverable_effort(self, tmp_path):
        """Mirrors the fleet registry's shapes: only the 64K entry, which
        declares reasoning_effort with an explicit delivering style."""
        reg = _load_text(
            tmp_path,
            "default: fd\nmodels:\n"
            + _entry_yaml(
                "m5",
                "    reasoning_effort: medium\n"
                "    routing: {group: q, order: 2, max_parallel: 1, effort_style: chat_template_kwargs}\n",
            )
            + _entry_yaml(
                "m5-64k",
                "    reasoning_effort: medium\n    effort_style: chat_template_kwargs\n",
            )
            + _entry_yaml(
                "gpu",
                "    reasoning_effort: medium\n"
                "    routing: {group: q, order: 1, max_parallel: 4, effort_style: ninfer}\n",
            )
            + _entry_yaml("small-m5", "    routing: {group: s, order: 2, effort_style: chat_template_kwargs}\n")
            + _entry_yaml("paid", "    key_env: K\n    routing: {group: q, order: 3}\n")
            + _entry_yaml("fd", "    frontdoor: true\n    reasoning_effort: medium\n")
            + "  h:\n    harness: opencode\n    model: p/m\n",
        )
        assert reg.notes == []
