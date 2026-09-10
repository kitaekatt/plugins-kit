"""Tests for the layered standards resolver (standards_resolve.py) and audit.py's
consumption of a ResolvedStandards object.

Everything is hermetic: CLAUDE_CONFIG_DIR is monkeypatched to a tmp_path so no
test ever reads the real ~/.claude, and project layers are passed explicitly as
tmp_path subtrees. Config precedence, the reject-architectural / unknown-key
guards, malformed-config loudness, standards-file parsing + grouping, and the
audit-side disable/threshold-override behavior are each pinned.
"""

import textwrap

import pytest
import yaml

from skills_kit_lib import standards_resolve
from skills_kit_lib.standards_resolve import (
    ResolvedStandards,
    StandardsConfigError,
    resolve,
)
from skills_kit_lib.audit import audit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_layer(tmp_path, monkeypatch):
    """Point CLAUDE_CONFIG_DIR at tmp_path/config and return the skills-kit dir."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    layer = config_dir / "skills-kit"
    layer.mkdir(parents=True)
    return layer


def _project_layer(tmp_path):
    """Return a project root whose .claude/skills-kit dir exists."""
    project_root = tmp_path / "project"
    layer = project_root / ".claude" / "skills-kit"
    layer.mkdir(parents=True)
    return project_root, layer


def _write_yaml(path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


_VALID_STANDARDS_MD = textwrap.dedent(
    """\
    # SKILL standards

    Prose for a human reader.

    ```yaml
    standards_set:
      identity: Optional description-hygiene standards for SKILL.md files.
      applies_to: skill_md
      criteria:
        - id: desc-160-char
          statement: The description frontmatter field is at most 160 characters.
          severity: fail
          keywords: [description length, 160 char, hygiene]
          enforcement: mechanical
    ```
    """
)


def _write_skill(tmp_path, fixture, name="example-skill", description=None):
    """Materialize a SKILL.md from a minimal_* fixture dict."""
    root = next(iter(fixture))
    skill_type = root.replace("_", "-")
    if description is None:
        description = "Use when doing X. Do NOT use for Y."
    skill_dir = tmp_path / name
    skill_dir.mkdir()
    body = (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"skill-type: {skill_type}\n"
        f"---\n\n"
        f"# {name}\n\n"
        f"Orientation paragraph.\n\n"
        f"```yaml\n{yaml.safe_dump(fixture, sort_keys=False)}```\n"
    )
    path = skill_dir / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


def _find_row(report, rule_id):
    for key in ("universal", "yaml_contract", "type_specific"):
        for r in report.get(key, []):
            if r["rule"] == rule_id:
                return r
    if report.get("mixed_type", {}).get("rule") == rule_id:
        return report["mixed_type"]
    return None


# ---------------------------------------------------------------------------
# Resolver: config precedence
# ---------------------------------------------------------------------------


def test_absent_everything_returns_empty_defaults(tmp_path, monkeypatch):
    # CLAUDE_CONFIG_DIR points at a dir with no skills-kit layer; no project.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-config"))
    r = resolve(None)
    assert isinstance(r, ResolvedStandards)
    assert r.disabled_rules == set()
    assert r.thresholds == {}
    assert r.standards_by_primitive == {}


def test_user_layer_disable_flows_through(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    _write_yaml(layer / "config.yaml", {"rules": {"desc-160-char": "off"}})
    r = resolve(None)
    assert "desc-160-char" in r.disabled_rules


def test_project_layer_overrides_user_later_wins(tmp_path, monkeypatch):
    user_layer = _user_layer(tmp_path, monkeypatch)
    _write_yaml(user_layer / "config.yaml", {"thresholds": {"desc_max_chars": 100}})
    project_root, proj_layer = _project_layer(tmp_path)
    _write_yaml(proj_layer / "config.yaml", {"thresholds": {"desc_max_chars": 80}})
    r = resolve(project_root)
    assert r.thresholds["desc_max_chars"] == 80


def test_config_local_wins_over_config_yaml_same_scope(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    _write_yaml(layer / "config.yaml", {"thresholds": {"desc_max_chars": 100}})
    _write_yaml(layer / "config.local.yaml", {"thresholds": {"desc_max_chars": 50}})
    r = resolve(None)
    assert r.thresholds["desc_max_chars"] == 50


# ---------------------------------------------------------------------------
# Resolver: validation guards (loud, never silent {})
# ---------------------------------------------------------------------------


def test_disabling_architectural_rule_raises_with_bucket(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    _write_yaml(layer / "config.yaml", {"rules": {"yaml-contract": "off"}})
    with pytest.raises(StandardsConfigError) as exc:
        resolve(None)
    msg = str(exc.value)
    assert "yaml-contract" in msg
    assert "architectural" in msg


def test_disabling_inoffensive_rule_raises_with_bucket(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    _write_yaml(layer / "config.yaml", {"rules": {"frontmatter-present": "off"}})
    with pytest.raises(StandardsConfigError) as exc:
        resolve(None)
    assert "inoffensive" in str(exc.value)


def test_unknown_rule_id_raises_unknown_bucket(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    _write_yaml(layer / "config.yaml", {"rules": {"no-such-rule": "off"}})
    with pytest.raises(StandardsConfigError) as exc:
        resolve(None)
    assert "unknown" in str(exc.value)


def test_non_off_rule_value_raises(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    # `on` parses to True in YAML -- not a disable value.
    _write_yaml(layer / "config.yaml", {"rules": {"desc-160-char": "on"}})
    with pytest.raises(StandardsConfigError):
        resolve(None)


def test_unknown_threshold_key_raises(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    _write_yaml(layer / "config.yaml", {"thresholds": {"bogus_key": 5}})
    with pytest.raises(StandardsConfigError) as exc:
        resolve(None)
    msg = str(exc.value)
    assert "bogus_key" in msg
    # names the valid keys
    assert "desc_max_chars" in msg


def test_non_positive_threshold_raises(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    _write_yaml(layer / "config.yaml", {"thresholds": {"desc_max_chars": 0}})
    with pytest.raises(StandardsConfigError):
        resolve(None)


def test_malformed_yaml_raises(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    (layer / "config.yaml").write_text("rules: [unterminated\n", encoding="utf-8")
    with pytest.raises(StandardsConfigError) as exc:
        resolve(None)
    assert "config.yaml" in str(exc.value)


def test_non_mapping_config_root_raises(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    (layer / "config.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(StandardsConfigError):
        resolve(None)


# ---------------------------------------------------------------------------
# Resolver: standards files
# ---------------------------------------------------------------------------


def test_valid_standards_file_parsed_and_grouped(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    md = layer / "SKILL-standards.md"
    md.write_text(_VALID_STANDARDS_MD, encoding="utf-8")
    r = resolve(None)
    assert "skill_md" in r.standards_by_primitive
    files = r.standards_by_primitive["skill_md"]
    assert len(files) == 1
    sf = files[0]
    assert sf.path == md
    assert sf.applies_to == "skill_md"
    assert sf.criteria and sf.criteria[0]["id"] == "desc-160-char"


def test_standards_files_union_across_layers(tmp_path, monkeypatch):
    user_layer = _user_layer(tmp_path, monkeypatch)
    (user_layer / "SKILL-standards.md").write_text(_VALID_STANDARDS_MD, encoding="utf-8")
    project_root, proj_layer = _project_layer(tmp_path)
    (proj_layer / "SKILL-standards.md").write_text(_VALID_STANDARDS_MD, encoding="utf-8")
    r = resolve(project_root)
    # Both layers append under skill_md -- union, not replace.
    assert len(r.standards_by_primitive["skill_md"]) == 2


def test_invalid_standards_file_raises(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    bad = textwrap.dedent(
        """\
        # bad standards

        ```yaml
        standards_set:
          identity: Missing severity on the one criterion.
          applies_to: skill_md
          criteria:
            - id: desc-160-char
              statement: Something.
              keywords: [a, b, c]
        ```
        """
    )
    md = layer / "SKILL-standards.md"
    md.write_text(bad, encoding="utf-8")
    with pytest.raises(StandardsConfigError) as exc:
        resolve(None)
    assert "SKILL-standards.md" in str(exc.value)


def test_standards_file_without_block_raises(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    md = layer / "SKILL-standards.md"
    md.write_text("# no block here\n\nJust prose.\n", encoding="utf-8")
    with pytest.raises(StandardsConfigError):
        resolve(None)


_AUTHORED_CRITERION_STANDARDS_MD = textwrap.dedent(
    """\
    # SKILL standards

    Prose for a human reader.

    ```yaml
    standards_set:
      identity: A project-authored standard for SKILL.md files.
      applies_to: skill_md
      criteria:
        - id: no-emoji
          statement: A SKILL.md body carries no emoji.
          severity: fail
          keywords: [emoji, glyph, ascii]
          enforcement: mechanical
    ```
    """
)


def test_authored_criterion_id_is_a_valid_rules_off_knob(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    (layer / "SKILL-standards.md").write_text(
        _AUTHORED_CRITERION_STANDARDS_MD, encoding="utf-8"
    )
    _write_yaml(layer / "config.local.yaml", {"rules": {"no-emoji": "off"}})
    r = resolve(None)
    assert "no-emoji" in r.disabled_rules


def test_unauthored_unknown_id_still_raises(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    (layer / "SKILL-standards.md").write_text(
        _AUTHORED_CRITERION_STANDARDS_MD, encoding="utf-8"
    )
    _write_yaml(layer / "config.local.yaml", {"rules": {"bogus": "off"}})
    with pytest.raises(StandardsConfigError) as exc:
        resolve(None)
    assert "unknown" in str(exc.value)


# ---------------------------------------------------------------------------
# audit.py consumption
# ---------------------------------------------------------------------------


def test_audit_drops_disabled_optional_rule(tmp_path, minimal_reference_skill):
    path = _write_skill(tmp_path, minimal_reference_skill)
    baseline = audit(path)
    assert _find_row(baseline, "desc-160-char") is not None

    resolved = ResolvedStandards(disabled_rules={"desc-160-char"})
    report = audit(path, resolved)
    # The disabled optional row is gone...
    assert _find_row(report, "desc-160-char") is None
    # ...while an architectural row (never disableable) survives.
    assert _find_row(report, "yaml-contract") is not None


def test_audit_threshold_override_changes_desc_verdict(tmp_path, minimal_reference_skill):
    # A 34-char description passes the 160-char default.
    desc = "Use when doing X. Do NOT use Y."
    assert len(desc) <= 160
    path = _write_skill(tmp_path, minimal_reference_skill, description=desc)

    baseline = audit(path)
    assert _find_row(baseline, "desc-160-char")["verdict"] == "pass"

    # Lower desc_max_chars below the description length -> the same row fails.
    resolved = ResolvedStandards(thresholds={"desc_max_chars": 20})
    report = audit(path, resolved)
    assert _find_row(report, "desc-160-char")["verdict"] == "fail"


def test_audit_default_none_is_unchanged_behavior(tmp_path, minimal_reference_skill):
    path = _write_skill(tmp_path, minimal_reference_skill)
    # No resolved object -> identical to the historical call shape.
    assert audit(path) == audit(path, None)


def test_module_resolve_symbol_is_public(tmp_path, monkeypatch):
    # standards_resolve.resolve is the documented public entry point.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "none"))
    assert standards_resolve.resolve(None).disabled_rules == set()
