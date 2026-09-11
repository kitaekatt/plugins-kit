from pathlib import Path

import pytest

from bootstrap_lib.code_review import mechanical
from bootstrap_lib.code_review import mechanical_config as config
from bootstrap_lib.code_review import pipeline
from bootstrap_lib.code_review.lane_prompts import build_user_message
from bootstrap_lib.code_review.pipeline import assemble_bundle


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _record(check_id: str = "todo", pattern: str = "TODO") -> str:
    return f"""checks:
  - id: {check_id}
    phrase: todo markers
    pattern: '{pattern}'
    applies_to: ['src/*.py']
"""


def test_defaults_exclude_personal_conventions_but_legacy_facade_keeps_them(tmp_path: Path) -> None:
    diff = "@@ -0,0 +1 @@\n+example \u2014 /opt/app/data\n"
    checks = mechanical.resolve_checks(tmp_path)
    assert {check.check_id for check in checks} == {
        "structured_parse", "duplicate_keys", "column_counts",
    }
    assert mechanical.scan_file("example.txt", diff)["checks_run"] == []
    assert mechanical.scan_file("example.txt", diff, checks=checks)["findings"] == []
    assert {finding["check"] for finding in mechanical.mechanical_findings(diff)} == {
        "non_ascii", "abs_path",
    }


@pytest.mark.parametrize("text", [
    "plain text", "example \u2014 text", "\u2500 diagram", "\u00e9\u4e2d",
    r"C:\work\file", "D:/work/file", "/opt/app/file", "use '/opt/app/file'",
    "use `/opt/app/file`", "relative/file", "https://example.com/file",
    "/single-component", "example \u2014 /opt/app/file and C:/work/file",
    "a" * 10001 + " \u2014 /opt/app/file",
    "a" * 10001 + " \u2500 C:/work/file",
])
def test_user_conventions_detect_same_lines_as_legacy(
    tmp_path: Path, personal_conventions: Path, text: str,
) -> None:
    diff = f"@@ -1,1 +1,1 @@\n-old \u2014 /opt/old/file\n+{text}\n"
    checks = mechanical.resolve_checks(tmp_path)
    personal = [check for check in checks if check.check_id in mechanical.LEGACY_CHECK_IDS]
    assert [check.check_id for check in personal] == ["non_ascii", "abs_path"]
    assert all(check.source_layer == str(personal_conventions) for check in personal)
    result = mechanical.scan_file("docs/example.txt", diff, checks=checks)
    assert result["checks_run"] == ["non_ascii", "abs_path"]
    assert result["findings"] == mechanical.mechanical_findings(diff)


def test_builtin_details_preserve_codepoint_and_windows_match_priority(
    tmp_path: Path, personal_conventions: Path,
) -> None:
    diff = "@@ -0,0 +1 @@\n+\u2500 /opt/app/file C:/work/file\n"
    scan = mechanical.scan_file("example.md", diff, checks=mechanical.resolve_checks(tmp_path))
    assert scan["findings"] == [
        {"check": "non_ascii", "line": 1,
         "detail": "U+2500 ('\u2500') in: \u2500 /opt/app/file C:/work/file"},
        {"check": "abs_path", "line": 1,
         "detail": "'C:/' in: \u2500 /opt/app/file C:/work/file"},
    ]


def test_builtin_finds_both_hits_after_ten_thousand_characters(
    tmp_path: Path, personal_conventions: Path,
) -> None:
    diff = "@@ -0,0 +1 @@\n+" + "x" * 10001 + " \u2014 /opt/app/file\n"
    scan = mechanical.scan_file("example.md", diff, checks=mechanical.resolve_checks(tmp_path))
    assert scan["findings"] == [
        {"check": "non_ascii", "line": 1,
         "detail": "U+2014 ('\u2014') in: " + "x" * 120},
        {"check": "abs_path", "line": 1,
         "detail": "'/opt/' in: " + "x" * 120},
    ]


@pytest.mark.parametrize("selection", ["[]", "[abs_path]", "[abs_path, non_ascii]"])
def test_builtin_selection_is_ordered_and_additive(
    tmp_path: Path, personal_conventions: Path, selection: str,
) -> None:
    import yaml

    personal_conventions.write_text(f"checks: {selection}\n", encoding="utf-8")
    _write(tmp_path / ".claude/mechanical_checks.yaml", _record())
    checks = mechanical.resolve_checks(tmp_path)
    assert [check.check_id for check in checks] == (
        [check.check_id for check in mechanical.REGISTRY] + yaml.safe_load(selection) + ["todo"]
    )


@pytest.mark.parametrize("yaml_text", [
    "checks: [non_ascii, non_ascii]\n", "checks: [structured_parse]\n",
    "checks: [unknown]\n", "checks: [null]\n", "checks: [{id: abs_path}]\n",
    "checks: abs_path\n", "checks: []\nextra: true\n", "{}\n", "\n",
])
def test_builtin_selection_rejects_invalid_schema(
    tmp_path: Path, personal_conventions: Path, yaml_text: str,
) -> None:
    personal_conventions.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(config.MechanicalConfigError, match="mechanical_builtin_checks.yaml"):
        mechanical.resolve_checks(tmp_path)


@pytest.mark.parametrize("layer", ["shipped", "user", "project"])
def test_builtin_id_cannot_be_redefined_by_any_pattern_layer(
    tmp_path: Path, personal_conventions: Path, monkeypatch: pytest.MonkeyPatch, layer: str,
) -> None:
    monkeypatch.setattr(config, "DEFAULTS_PATH", tmp_path / "defaults.yaml")
    _write(config.DEFAULTS_PATH, "checks: []\n")
    paths = dict(config.layer_paths(tmp_path))
    _write(paths[layer], _record("non_ascii"))
    with pytest.raises(config.MechanicalConfigError, match="duplicate id") as error:
        mechanical.resolve_checks(tmp_path)
    assert str(personal_conventions) in str(error.value)
    assert str(paths[layer]) in str(error.value)


def test_default_registry_id_cannot_be_redefined_by_pattern_config(tmp_path: Path) -> None:
    _write(tmp_path / ".claude/mechanical_checks.yaml", _record("structured_parse"))
    with pytest.raises(config.MechanicalConfigError, match="duplicate id"):
        mechanical.resolve_checks(tmp_path)


def test_legacy_pattern_loader_ignores_builtin_selector(
    tmp_path: Path, personal_conventions: Path,
) -> None:
    # Older versions call only this existing generic loader, so the selector
    # cannot collide with their still-shipped personal checks.
    assert config.resolve_config(tmp_path) == ()
    assert not personal_conventions.with_name(config.CONFIG_NAME).exists()


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("claimed", [False, True])
def test_personal_conventions_reach_lanes_only_when_configured(
    tmp_path: Path, request: pytest.FixtureRequest, configured: bool, claimed: bool,
) -> None:
    if configured:
        request.getfixturevalue("personal_conventions")
    diff = "@@ -0,0 +1 @@\n+example \u2014 /opt/app/file\n"
    bundle = assemble_bundle(
        "", [{"identifier": "docs/example.md", "text": diff}],
        [{"identifier": "docs/example.md", "local": None}], tmp_path / "bundle",
        10000, tmp_path, claim_globs=["**/*.md"] if claimed else [],
    )
    entry = bundle["claimed_files" if claimed else "diff_chunks"][0]
    scan = entry["mechanical_scan"]
    assert scan["files"][0]["checks_run"] == (
        ["non_ascii", "abs_path"] if configured else []
    )
    assert len(scan["files"][0]["findings"]) == (2 if configured else 0)
    if claimed:
        # The claimed-file transport is consumed by md-domain, whose criteria
        # are distinct from generic reviewer A/B prompts.
        return
    for lane in ("reviewer_a_claude_md_compliance", "reviewer_b_diff_only_bugs"):
        message = build_user_message(
            lane, diff_text=diff, mechanical_findings=scan,
            mechanical_check_phrases=bundle["mechanical_check_phrases"],
        )
        assert ("non_ascii (non-ASCII characters)" in message) is configured
        assert ("abs_path (absolute paths)" in message) is configured
        assert "do not run that check again" in message


def test_layers_are_additive_in_precedence_order(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULTS_PATH", tmp_path / "defaults.yaml")
    _write(config.DEFAULTS_PATH, _record("shipped"))
    _write(tmp_path / "home/.claude/config/mechanical_checks.yaml", _record("user"))
    _write(tmp_path / "project/.claude/mechanical_checks.yaml", _record("project"))
    records = config.resolve_config(tmp_path / "project", home=tmp_path / "home")
    assert [record["id"] for record in records] == ["shipped", "user", "project"]


@pytest.mark.parametrize("lower,higher", [("user", "project"), ("shipped", "user"), ("shipped", "project")])
def test_duplicate_id_names_both_layers(tmp_path, monkeypatch, lower, higher):
    monkeypatch.setattr(config, "DEFAULTS_PATH", tmp_path / "defaults.yaml")
    _write(config.DEFAULTS_PATH, _record("same") if lower == "shipped" or higher == "shipped" else "checks: []\n")
    if lower == "user" or higher == "user":
        _write(tmp_path / "home/.claude/config/mechanical_checks.yaml", _record("same"))
    if lower == "project" or higher == "project":
        _write(tmp_path / "project/.claude/mechanical_checks.yaml", _record("same"))
    with pytest.raises(config.MechanicalConfigError, match="same") as exc:
        config.resolve_config(tmp_path / "project", home=tmp_path / "home")
    assert "layer" in str(exc.value)


@pytest.mark.parametrize("yaml_text,needle", [
    ("other: 1\n", "top-level"),
    ("checks: nope\n", "checks"),
    ("checks:\n  - id: Bad\n    phrase: x\n    pattern: x\n    applies_to: ['*']\n", "Bad"),
    ("checks:\n  - id: x\n    phrase: x\n    pattern: '['\n    applies_to: ['*']\n", "pattern"),
    ("checks:\n  - id: x\n    phrase: x\n    pattern: x\n    applies_to: ['!*.py']\n", "positive"),
    ("checks:\n  - id: x\n    phrase: x\n    pattern: x\n    applies_to: ['*']\n    detail: '{bad}'\n", "detail"),
])
def test_validation_rejects_bad_records(tmp_path, monkeypatch, yaml_text, needle):
    monkeypatch.setattr(config, "DEFAULTS_PATH", tmp_path / "defaults.yaml")
    _write(config.DEFAULTS_PATH, yaml_text)
    with pytest.raises(config.MechanicalConfigError, match=needle):
        config.resolve_config(tmp_path / "project", home=tmp_path / "home")


def test_project_relative_and_depot_paths_normalize():
    assert config.normalize_project_path("src/x.py", "/repo") == "src/x.py"
    assert config.normalize_project_path("//depot/project/src/x.py", "/repo") == "src/x.py"


def test_config_check_scans_added_lines_and_is_transactional(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULTS_PATH", tmp_path / "defaults.yaml")
    _write(config.DEFAULTS_PATH, _record("todo"))
    checks = mechanical.resolve_checks(tmp_path)
    diff = "diff --git a/src/x.py b/src/x.py\n@@ -0,0 +1 @@\n+TODO now\n"
    result = mechanical.scan_file("src/x.py", diff, checks=checks)
    assert "todo" in result["checks_run"]
    assert result["findings"][-1]["check"] == "todo"

    _write(config.DEFAULTS_PATH, _record("slow", r"(a+)+$"))
    checks = mechanical.resolve_checks(tmp_path)
    result = mechanical.scan_file("src/x.py", "diff --git a/src/x.py b/src/x.py\n@@ -0,0 +1 @@\n+" + "a" * 1000 + "!\n", checks=checks)
    assert "slow" not in result["checks_run"]
    assert not [f for f in result["findings"] if f["check"] == "slow"]
    assert "slow" in result["diagnostics"][0]


def test_pipeline_resolves_once_and_publishes_config_phrase(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULTS_PATH", tmp_path / "defaults.yaml")
    _write(config.DEFAULTS_PATH, _record("todo"))
    diff = "diff --git a/src/x.py b/src/x.py\n@@ -0,0 +1 @@\n+TODO now\n"
    bundle = assemble_bundle(
        "", [{"identifier": "src/x.py", "text": diff}],
        [{"identifier": "src/x.py", "local": None}], tmp_path / "bundle", 10000,
        tmp_path,
    )
    assert bundle["mechanical_check_phrases"]["todo"] == "todo markers"
    assert "todo" in bundle["diff_chunks"][0]["mechanical_scan"]["files"][0]["checks_run"]


def test_pipeline_applies_effective_checks_to_claimed_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULTS_PATH", tmp_path / "defaults.yaml")
    _write(
        config.DEFAULTS_PATH,
        "checks:\n"
        "  - id: todo\n"
        "    phrase: todo markers\n"
        "    pattern: 'TODO'\n"
        "    applies_to: ['docs/*.md']\n",
    )
    diff = "diff --git a/docs/x.md b/docs/x.md\n@@ -0,0 +1 @@\n+TODO now\n"
    bundle = assemble_bundle(
        "", [{"identifier": "docs/x.md", "text": diff}],
        [{"identifier": "docs/x.md", "local": None}], tmp_path / "bundle", 10000,
        tmp_path, claim_globs=["**/*.md"],
    )
    scan = bundle["claimed_files"][0]["mechanical_scan"]
    assert scan["schema_version"] == 2
    assert len(scan["files"]) == 1
    assert scan["files"][0]["file"] == "docs/x.md"
    assert "todo" in scan["files"][0]["checks_run"]
    assert scan["files"][0]["findings"][-1]["check"] == "todo"


def test_claimed_scan_diagnostic_leaves_failed_check_uncovered(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULTS_PATH", tmp_path / "defaults.yaml")
    _write(config.DEFAULTS_PATH, _record("slow", r"(a+)+$"))
    diff = (
        "diff --git a/src/x.py b/src/x.py\n@@ -0,0 +1 @@\n+"
        + "a" * 1000
        + "!\n"
    )
    bundle = assemble_bundle(
        "", [{"identifier": "src/x.py", "text": diff}],
        [{"identifier": "src/x.py", "local": None}], tmp_path / "bundle", 10000,
        tmp_path, claim_globs=["**/*.py"],
    )
    record = bundle["claimed_files"][0]["mechanical_scan"]["files"][0]
    assert "slow" not in record["checks_run"]
    assert not [row for row in record["findings"] if row["check"] == "slow"]
    assert "slow" in record["diagnostics"][0]


def test_pipeline_resolves_once_for_claimed_and_generic_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULTS_PATH", tmp_path / "defaults.yaml")
    _write(
        config.DEFAULTS_PATH,
        "checks:\n"
        "  - id: todo\n"
        "    phrase: todo markers\n"
        "    pattern: 'TODO'\n"
        "    applies_to: ['**/*']\n",
    )
    real_resolve = pipeline.resolve_checks
    calls = 0

    def counting_resolve(root):
        nonlocal calls
        calls += 1
        return real_resolve(root)

    monkeypatch.setattr(pipeline, "resolve_checks", counting_resolve)
    sections = [
        {"identifier": path, "text": f"@@ -0,0 +1 @@\n+TODO in {path}\n"}
        for path in ("docs/x.md", "src/y.py")
    ]
    bundle = pipeline.assemble_bundle(
        "", sections,
        [{"identifier": row["identifier"], "local": None} for row in sections],
        tmp_path / "bundle", 10000, tmp_path, claim_globs=["**/*.md"],
    )
    assert calls == 1
    claimed = bundle["claimed_files"][0]["mechanical_scan"]["files"][0]
    generic = bundle["diff_chunks"][0]["mechanical_scan"]["files"][0]
    assert "todo" in claimed["checks_run"]
    assert "todo" in generic["checks_run"]


def test_config_check_declares_no_snapshot_input(tmp_path, monkeypatch):
    """Pin the PROPERTY, not the live registry's current answer.

    `requires_pre_image()` takes no argument, so asserting it is unchanged by
    config cannot fail and pins nothing. What actually keeps the pre-image gate
    safe is that a config-built check declares only `added_lines` -- assert
    that, and this goes red the moment a config check can ask for an image.
    """
    monkeypatch.setattr(config, "DEFAULTS_PATH", tmp_path / "defaults.yaml")
    _write(config.DEFAULTS_PATH, _record("todo"))
    built = mechanical.resolve_checks(tmp_path)[len(mechanical.REGISTRY):]
    assert built, "config check was not built"
    snapshot_inputs = {"pre_image_text", "post_image_text"}
    for check in built:
        assert check.required_inputs == frozenset({"added_lines"})
        assert not (check.required_inputs & snapshot_inputs)


def test_out_of_scope_file_is_omitted_from_coverage(tmp_path, monkeypatch):
    """An out-of-scope file must be UNCOVERED, not covered-and-empty.

    Goes red if `applies_to` is tested inside `scan` instead of in the
    precondition: the check would then run, find nothing, and still be listed
    in `checks_run` -- telling the lane the file was scanned for TODO markers
    when the check never looked at it.
    """
    monkeypatch.setattr(config, "DEFAULTS_PATH", tmp_path / "defaults.yaml")
    _write(config.DEFAULTS_PATH, _record("todo"))
    checks = mechanical.resolve_checks(tmp_path)
    diff = "diff --git a/docs/x.md b/docs/x.md\n@@ -0,0 +1 @@\n+TODO now\n"
    result = mechanical.scan_file("docs/x.md", diff, checks=checks)
    assert "todo" not in result["checks_run"]
    assert not [f for f in result["findings"] if f["check"] == "todo"]

    in_scope = mechanical.scan_file(
        "src/x.py",
        "diff --git a/src/x.py b/src/x.py\n@@ -0,0 +1 @@\n+TODO now\n",
        checks=checks,
    )
    assert "todo" in in_scope["checks_run"]
