from pathlib import Path

import pytest

from bootstrap_lib.code_review import mechanical
from bootstrap_lib.code_review import mechanical_config as config
from bootstrap_lib.code_review import pipeline
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
