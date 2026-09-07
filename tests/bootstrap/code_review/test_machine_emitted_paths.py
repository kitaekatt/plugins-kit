"""Tests for the declared-path axis of machine-emitted-artifact detection.

The motivating case for this axis is a real multi-megabyte generated API stub
that carries NO banner of any kind -- it begins directly at its imports. The
content axis cannot see it, and no signature list ever could. Its LOCATION is
what says a tool wrote it.
"""

from bootstrap_lib.code_review.machine_emitted_paths import (
    LABEL_DURABLE,
    LABEL_EPHEMERAL,
    LABEL_MANIFEST,
    LABEL_RELOCATED,
    declared_generated_rules,
    durable_container,
    ephemeral_container,
    match_declared_path,
)

# The shape of the motivating file: imports, no banner, no marker.
UNBANNERED = "from __future__ import annotations\nimport sys as _sys\n"


def _labels(rules):
    return {label for label, _ in rules}


class TestContainersAreDerived:
    def test_durable_container_under_workspace(self, tmp_path):
        assert durable_container(tmp_path) == tmp_path / ".plugin-data"

    def test_ephemeral_container_under_workspace(self, tmp_path):
        assert ephemeral_container(tmp_path) == tmp_path / ".local-data"


class TestDeclaredGeneratedRules:
    def test_no_workspace_root_yields_no_rules(self):
        assert declared_generated_rules(None) == []

    def test_bare_workspace_still_declares_the_containers(self, tmp_path):
        assert _labels(declared_generated_rules(tmp_path)) == {
            LABEL_DURABLE,
            LABEL_EPHEMERAL,
        }

    def test_project_config_relocation_is_followed(self, tmp_path):
        cfg = tmp_path / ".local-data" / "some-marketplace" / "some-plugin"
        cfg.mkdir(parents=True)
        (cfg / "config.yaml").write_text(
            "plugin_data_dir: Generated/PluginData\n", encoding="utf-8"
        )
        rules = declared_generated_rules(tmp_path)
        relocated = [root for label, root in rules if label == LABEL_RELOCATED]
        assert relocated == [(tmp_path / "Generated" / "PluginData").resolve()]

    def test_manifest_write_target_under_the_workspace(self, tmp_path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "bootstrap.json").write_text(
            '{"pypi_packages": [{"extract_to": "${cwd}/Generated/stub.py"}]}',
            encoding="utf-8",
        )
        rules = declared_generated_rules(tmp_path)
        targets = [root for label, root in rules if label == LABEL_MANIFEST]
        assert targets == [tmp_path / "Generated" / "stub.py"]

    def test_json_reference_is_source_and_target_is_the_only_rule(self, tmp_path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "bootstrap.json").write_text(
            '{"json_entries": [{"reference": "bundled.json", '
            '"target": "${cwd}/Generated/known.json"}]}',
            encoding="utf-8",
        )
        targets = [root for label, root in declared_generated_rules(tmp_path)
                   if label == LABEL_MANIFEST]
        assert targets == [tmp_path / "Generated" / "known.json"]

    def test_target_outside_the_workspace_is_dropped(self, tmp_path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "bootstrap.json").write_text(
            '{"pypi_packages": [{"extract_to": "${plugin_root}/stubs/x.py"}]}',
            encoding="utf-8",
        )
        assert LABEL_MANIFEST not in _labels(declared_generated_rules(tmp_path))

    def test_broken_manifest_does_not_raise(self, tmp_path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "bootstrap.json").write_text("{not json", encoding="utf-8")
        assert _labels(declared_generated_rules(tmp_path)) == {
            LABEL_DURABLE,
            LABEL_EPHEMERAL,
        }


class TestMatchDeclaredPath:
    def test_file_under_the_durable_container_matches(self, tmp_path):
        rules = declared_generated_rules(tmp_path)
        target = tmp_path / ".plugin-data" / "a-marketplace" / "a-plugin" / "api.py"
        assert match_declared_path(str(target), rules) == LABEL_DURABLE

    def test_ordinary_source_does_not_match(self, tmp_path):
        rules = declared_generated_rules(tmp_path)
        assert match_declared_path(str(tmp_path / "src" / "app.py"), rules) is None

    def test_sibling_prefix_is_not_a_match(self, tmp_path):
        """`.plugin-data-notes/` must not be swallowed by `.plugin-data`."""
        rules = declared_generated_rules(tmp_path)
        target = tmp_path / ".plugin-data-notes" / "x.py"
        assert match_declared_path(str(target), rules) is None

    def test_no_local_path_cannot_match(self, tmp_path):
        assert match_declared_path(None, declared_generated_rules(tmp_path)) is None

    def test_empty_rules_never_match(self, tmp_path):
        assert match_declared_path(str(tmp_path / "x.py"), []) is None

    def test_unbannered_stub_at_a_durable_path_is_detected(self, tmp_path):
        """Regression guard for the case that motivated this axis.

        A generated API stub with no banner, no `@generated`, no DO-NOT-EDIT --
        content detection returns nothing, and the declared path is the only
        evidence there is.
        """
        from bootstrap_lib.code_review.machine_emitted import detect_signature

        stub = tmp_path / ".plugin-data" / "a-marketplace" / "a-plugin" / "api.py"
        stub.parent.mkdir(parents=True)
        stub.write_text(UNBANNERED, encoding="utf-8")

        assert detect_signature(UNBANNERED) is None  # content axis is blind here
        rules = declared_generated_rules(tmp_path)
        assert match_declared_path(str(stub), rules) == LABEL_DURABLE
