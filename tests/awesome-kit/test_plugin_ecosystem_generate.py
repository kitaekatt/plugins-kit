"""Tests for the plugin-ecosystem generator's pure core (publish-flow load-bearing).

generate.py is imported by file path (the test_html_pdf.py pattern) so the test
does not depend on the plugin being on sys.path. Covered here: the minimal YAML
reader (the single parser shared by poster.yaml and SKILL.md frontmatter),
normalize_state, the compute_state 5-level precedence, and collect_plugins'
phantom-install filtering. HTML rendering and the browser-open path are not
unit-tested.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "awesome-kit" / "skills" / "plugin-ecosystem" / "scripts" / "generate.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("plugin_ecosystem_generate", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


generate = _load_module()


class TestParseYaml:
    def test_scalar_keys(self):
        out = generate.parse_yaml("title: My Poster\nurl: https://x.example\n")
        assert out == {"title": "My Poster", "url": "https://x.example"}

    def test_quotes_stripped(self):
        out = generate.parse_yaml('a: "double"\nb: \'single\'\n')
        assert out == {"a": "double", "b": "single"}

    def test_one_level_nested_map(self):
        out = generate.parse_yaml("states:\n  bootstrap: required\n  git-kit: on\n")
        assert out == {"states": {"bootstrap": "required", "git-kit": "on"}}

    def test_comments_and_blank_lines_skipped(self):
        out = generate.parse_yaml("# header\n\ntitle: T\n")
        assert out == {"title": "T"}

    def test_value_with_colon_preserved(self):
        out = generate.parse_yaml("desc: Use when X: do Y\n")
        assert out["desc"] == "Use when X: do Y"

    def test_booleans_kept_as_strings(self):
        out = generate.parse_yaml("flag: true\n")
        assert out["flag"] == "true"


class TestParseFrontmatter:
    def test_extracts_block(self):
        text = "---\nname: my-skill\ndescription: Does things\n---\n\n# Body\n"
        out = generate.parse_frontmatter(text)
        assert out["name"] == "my-skill"
        assert out["description"] == "Does things"

    def test_no_frontmatter_returns_empty(self):
        assert generate.parse_frontmatter("# Just a heading\n") == {}

    def test_unterminated_frontmatter_returns_empty(self):
        assert generate.parse_frontmatter("---\nname: x\n") == {}

    def test_parse_skill_frontmatter_reads_file(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text("---\nname: s\nauthor: christina\n---\nbody\n", encoding="utf-8")
        out = generate.parse_skill_frontmatter(md)
        assert out == {"name": "s", "author": "christina"}

    def test_parse_skill_frontmatter_missing_file(self, tmp_path):
        assert generate.parse_skill_frontmatter(tmp_path / "nope.md") == {}


class TestNormalizeState:
    def test_mappings(self):
        ns = generate.normalize_state
        assert ns("on") == ns("true") == ns("enabled") == ns("yes") == "on"
        assert ns("off") == ns("false") == ns("disabled") == ns("no") == "off"
        assert ns("opt-in") == ns("optin") == ns("manual") == "opt-in"
        assert ns("required") == ns("mandatory") == "required"
        assert ns(" ON ") == "on"  # trim + case-fold
        assert ns("weird") == "weird"  # passthrough


class TestComputeStatePrecedence:
    REF = "mkt:plug"

    def _state(self, settings=None, bs=None, overrides=None, m_states=None,
               defaults_mode=False):
        return generate.compute_state(
            self.REF, settings or {}, bs or {}, overrides or {},
            m_states or {}, defaults_mode)

    def test_level1_user_override_wins_over_everything(self):
        state = self._state(
            settings={"plug@mkt": True},
            bs={self.REF: {"enabled": True}},
            overrides={self.REF: "off"},
            m_states={"mkt": {"plug": "required"}})
        assert state == "off"

    def test_level1_short_name_override(self):
        assert self._state(overrides={"plug": "opt-in"}) == "opt-in"

    def test_level2_marketplace_states_beat_settings(self):
        state = self._state(
            settings={"plug@mkt": False},
            m_states={"mkt": {"plug": "required"}})
        assert state == "required"

    def test_level3_settings_enabled_plugins(self):
        assert self._state(settings={"plug@mkt": True}) == "on"
        assert self._state(settings={"plug@mkt": False}) == "off"

    def test_level4_bootstrap_declaration(self):
        assert self._state(bs={self.REF: {"install": "manual"}}) == "opt-in"
        assert self._state(bs={self.REF: {"enabled": True}}) == "on"
        assert self._state(bs={self.REF: {"enabled": False}}) == "off"

    def test_level5_default(self):
        assert self._state() == "unmanaged"
        assert self._state(defaults_mode=True) == "opt-in"


class TestCollectPluginsPhantomFiltering:
    def _make_install(self, tmp_path, name, description="desc"):
        root = tmp_path / name
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "description": description}), encoding="utf-8")
        return root

    def test_filters_phantoms_and_non_opted_in(self, tmp_path):
        real = self._make_install(tmp_path, "real")
        ghost = self._make_install(tmp_path, "ghost")
        other = self._make_install(tmp_path, "other")
        installed = {"plugins": {
            "real@mkt": [{"installPath": str(real), "version": "1.0.0"}],
            # phantom: still installed locally, removed from marketplace.json
            "ghost@mkt": [{"installPath": str(ghost), "version": "1.0.0"}],
            # marketplace that did not opt in (no poster.yaml)
            "other@silent": [{"installPath": str(other), "version": "1.0.0"}],
            # malformed key: no @
            "noatsign": [{"installPath": str(real), "version": "1.0.0"}],
            # empty entries list
            "empty@mkt": [],
        }}
        marketplaces = {"mkt": {"poster": {}, "plugin_names": {"real", "empty"}}}
        out = generate.collect_plugins(installed, marketplaces, {}, {}, {}, {})
        assert [p["name"] for p in out] == ["real"]
        assert out[0]["marketplace"] == "mkt"
        assert out[0]["version"] == "1.0.0"

    def test_hidden_plugin_excluded(self, tmp_path):
        """`hidden: true` in the plugin's own poster.yaml drops it from the
        poster even though it is installed and listed in marketplace.json."""
        shown = self._make_install(tmp_path, "shown")
        hidden = self._make_install(tmp_path, "hidden")
        (hidden / ".claude-plugin" / "poster.yaml").write_text(
            "hidden: true\n", encoding="utf-8")
        installed = {"plugins": {
            "shown@mkt": [{"installPath": str(shown), "version": "1.0.0"}],
            "hidden@mkt": [{"installPath": str(hidden), "version": "1.0.0"}],
        }}
        marketplaces = {"mkt": {"poster": {}, "plugin_names": {"shown", "hidden"}}}
        out = generate.collect_plugins(installed, marketplaces, {}, {}, {}, {})
        assert [p["name"] for p in out] == ["shown"]

    def test_hidden_false_still_shown(self, tmp_path):
        root = self._make_install(tmp_path, "p")
        (root / ".claude-plugin" / "poster.yaml").write_text(
            "hidden: false\n", encoding="utf-8")
        installed = {"plugins": {"p@mkt": [{"installPath": str(root), "version": "0.1"}]}}
        marketplaces = {"mkt": {"poster": {}, "plugin_names": {"p"}}}
        out = generate.collect_plugins(installed, marketplaces, {}, {}, {}, {})
        assert [p["name"] for p in out] == ["p"]

    def test_poster_yaml_overrides_card_copy(self, tmp_path):
        root = self._make_install(tmp_path, "p", description="from plugin.json")
        (root / ".claude-plugin" / "poster.yaml").write_text(
            "description: from poster\n", encoding="utf-8")
        installed = {"plugins": {"p@mkt": [{"installPath": str(root), "version": "0.1"}]}}
        marketplaces = {"mkt": {"poster": {}, "plugin_names": {"p"}}}
        out = generate.collect_plugins(installed, marketplaces, {}, {}, {}, {})
        assert out[0]["description"] == "from poster"


class TestMergeCacheFallback:
    """Registry-v2 fallback: with installed_plugins.json at {"plugins": {}}
    (newer Claude Code), the poster rendered empty. merge_cache_fallback
    synthesizes entries from ~/.claude/plugins/cache/<mkt>/<plugin>/<version>/;
    registry entries keep precedence."""

    def _home(self, tmp_path, monkeypatch, cache=None):
        home = tmp_path / "home" / ".claude"
        for ref, versions in (cache or {}).items():
            name, _, mkt = ref.partition("@")
            for v in versions:
                (home / "plugins" / "cache" / mkt / name / v).mkdir(parents=True)
        monkeypatch.setattr(generate, "home_claude", lambda: home)
        return home

    def test_empty_registry_synthesizes_from_cache(self, tmp_path, monkeypatch):
        home = self._home(tmp_path, monkeypatch, cache={"pluga@mkt": ["1.2.0"]})
        merged = generate.merge_cache_fallback({"version": 2, "plugins": {}})
        entry = merged["plugins"]["pluga@mkt"][0]
        assert entry["version"] == "1.2.0"
        assert entry["installPath"] == str(home / "plugins" / "cache" / "mkt" / "pluga" / "1.2.0")

    def test_registry_entry_takes_precedence(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch, cache={"pluga@mkt": ["9.9.9"]})
        installed = {"plugins": {"pluga@mkt": [{"installPath": "/x", "version": "1.0.0"}]}}
        merged = generate.merge_cache_fallback(installed)
        assert merged["plugins"]["pluga@mkt"][0]["version"] == "1.0.0"

    def test_highest_version_wins_numerically(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch, cache={"pluga@mkt": ["0.9.0", "0.10.0"]})
        merged = generate.merge_cache_fallback({"plugins": {}})
        assert merged["plugins"]["pluga@mkt"][0]["version"] == "0.10.0"

    def test_no_cache_dir_is_noop(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch)
        merged = generate.merge_cache_fallback({"plugins": {}})
        assert merged["plugins"] == {}


class TestPublicMode:
    """--public renders the checked-in/published variant: no machine-local state
    badges, and the page flows instead of scrolling inside a fixed 16:9 frame."""

    PLUGINS = [{
        "marketplace": "mkt", "name": "pluga", "version": "1.0.0",
        "description": "d", "razor": "", "skills": [],
    }]

    def _render(self, public, plugins=None):
        return generate.render_html(
            "T", "tag", plugins if plugins is not None else self.PLUGINS,
            ["mkt"], {}, {}, public=public)

    def test_public_css_appended_only_in_public_mode(self):
        assert "min-height: 100vh" in self._render(True)
        assert "min-height: 100vh" not in self._render(False)

    def test_public_page_flows_instead_of_clipping(self):
        html = self._render(True)
        # The default poster sets html/body overflow:hidden and scrolls inside
        # .col-body; the public override hands scrolling back to the window.
        assert "html, body { overflow: visible; }" in html
        assert ".col-body { overflow-y: visible; }" in html

    def test_state_absent_from_embedded_data(self):
        """State is stripped from the DATA, not merely hidden -- a published page
        must not embed which plugins the generating machine had enabled."""
        stateful = [dict(self.PLUGINS[0], state="on")]
        assert '"state": "on"' in self._render(False, stateful)

    def test_badge_helper_tolerates_missing_state(self):
        assert "if (!state) return null;" in self._render(True)


class TestMarketplaceMetadataOverrides:
    """--marketplace-json and --poster redirect the two per-marketplace inputs at
    a source tree. Both cached copies lag the source by one publish, so a
    marketplace generating its own landing page must not read either of them --
    and --poster additionally opts a marketplace in with no clone installed."""

    def _home(self, tmp_path, monkeypatch, clones=None):
        home = tmp_path / "home" / ".claude"
        for name, poster in (clones or {}).items():
            d = home / "plugins" / "marketplaces" / name / ".claude-plugin"
            d.mkdir(parents=True)
            if poster is not None:
                (d / "poster.yaml").write_text(poster, encoding="utf-8")
            (d / "marketplace.json").write_text(
                json.dumps({"plugins": [{"name": "cached-only"}]}), encoding="utf-8")
        monkeypatch.setattr(generate, "home_claude", lambda: home)
        return home

    def _source(self, tmp_path, subtitle="from source", plugins=("fresh",)):
        src = tmp_path / "src"
        src.mkdir(exist_ok=True)
        poster = src / "poster.yaml"
        poster.write_text(f"subtitle: {subtitle}\n", encoding="utf-8")
        listing = src / "marketplace.json"
        listing.write_text(
            json.dumps({"plugins": [{"name": n} for n in plugins]}), encoding="utf-8")
        return poster, listing

    def test_poster_override_beats_the_cached_clone(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch, clones={"mkt": "subtitle: from cache\n"})
        poster, listing = self._source(tmp_path)
        out = generate.collect_marketplace_metadata({"mkt": listing}, {"mkt": poster})
        assert out["mkt"]["poster"]["subtitle"] == "from source"
        assert out["mkt"]["plugin_names"] == {"fresh"}

    def test_poster_override_opts_in_a_marketplace_with_no_clone(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch)
        poster, listing = self._source(tmp_path)
        out = generate.collect_marketplace_metadata({"mkt": listing}, {"mkt": poster})
        assert set(out) == {"mkt"}
        assert out["mkt"]["plugin_names"] == {"fresh"}

    def test_other_installed_marketplaces_still_collected(self, tmp_path, monkeypatch):
        """The override adds and replaces; it does not scope. Scoping is
        --marketplace's job, which is why publish.py passes both."""
        self._home(tmp_path, monkeypatch, clones={"private": "subtitle: theirs\n"})
        poster, listing = self._source(tmp_path)
        out = generate.collect_marketplace_metadata({"mkt": listing}, {"mkt": poster})
        assert set(out) == {"mkt", "private"}

    def test_clone_without_poster_yaml_still_skipped(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch, clones={"silent": None})
        assert generate.collect_marketplace_metadata() == {}

    def test_missing_override_path_exits(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch)
        with pytest.raises(SystemExit) as exc:
            generate.collect_marketplace_metadata({}, {"mkt": tmp_path / "nope.yaml"})
        assert "does not exist" in str(exc.value)

    def test_override_without_any_listing_exits(self, tmp_path, monkeypatch):
        """A poster override with no clone AND no --marketplace-json has no plugin
        listing at all, which would silently render an empty column."""
        self._home(tmp_path, monkeypatch)
        poster, _ = self._source(tmp_path)
        with pytest.raises(SystemExit) as exc:
            generate.collect_marketplace_metadata({}, {"mkt": poster})
        assert "--marketplace-json" in str(exc.value)


class TestParseNamePath:
    def test_parses_pairs(self, tmp_path):
        out = generate._parse_name_path(["a=/x/y", "b=/z"], "--poster")
        assert out == {"a": Path("/x/y"), "b": Path("/z")}

    def test_rejects_missing_separator(self):
        with pytest.raises(SystemExit) as exc:
            generate._parse_name_path(["justaname"], "--poster")
        assert "--poster expects NAME=PATH" in str(exc.value)

    def test_rejects_empty_half(self):
        with pytest.raises(SystemExit):
            generate._parse_name_path(["=/x"], "--marketplace-json")
