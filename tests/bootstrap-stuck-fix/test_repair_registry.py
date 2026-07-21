"""Tests for the bootstrap-stuck-fix registry repair.

The happy path is the least interesting property here. This remediation runs
unattended on every session start of every known user, so the tests that matter
are the ones asserting it REFUSES to act: on legitimate project-scope installs,
on a registry where no healthy record would survive, and on anything it does not
recognize.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "bootstrap-stuck-fix" / "scripts" / "repair_registry.py"
)


def _load_module():
    """Load by file path -- per the pythonpath note in pyproject.toml, per-plugin
    script dirs share one flat namespace and must not go on sys.path."""
    spec = importlib.util.spec_from_file_location("bsf_repair_registry", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_repair = _load_module()
apply_repair = _repair.apply_repair
plan_repair = _repair.plan_repair

REF = "bootstrap@plugins-kit"


def reg(*records, ref=REF):
    """Build a registry document holding *records* under *ref*."""
    return {"plugins": {ref: list(records)}}


def rec(version, scope="user", project_path=None, **extra):
    r = {"version": version, "scope": scope, **extra}
    if project_path:
        r["projectPath"] = project_path
    return r


# --- the wedge itself ----------------------------------------------------


def test_drops_the_malformed_user_record():
    data = reg(
        rec("0.45.0", project_path=r"D:\dev\env-config"),
        rec("0.52.0"),
    )
    keep, dropped = plan_repair(data)
    assert [r["version"] for r in dropped] == ["0.45.0"]
    assert [r["version"] for r in keep] == ["0.52.0"]


def test_drops_regardless_of_version_ordering():
    """Version is the symptom; the projectPath is the disease.

    The malformed record is selected by its shape, never by comparing versions
    -- that would encode assumptions about a schema we do not own.
    """
    data = reg(
        rec("9.9.9", project_path=r"D:\proj"),  # malformed but NEWER
        rec("0.1.0"),
    )
    _, dropped = plan_repair(data)
    assert [r["version"] for r in dropped] == ["9.9.9"]


# --- refusals: the properties that keep this safe ------------------------


def test_never_touches_project_scope_records():
    """A genuine per-project install is legitimate state, not damage."""
    data = reg(
        rec("0.45.0", scope="project", project_path=r"D:\dev\thing"),
        rec("0.52.0"),
    )
    _, dropped = plan_repair(data)
    assert dropped == []


def test_refuses_when_no_healthy_record_would_survive():
    """Better to leave a machine wedged than to deregister its bootstrap."""
    data = reg(
        rec("0.45.0", project_path=r"D:\a"),
        rec("0.46.0", project_path=r"D:\b"),
    )
    _, dropped = plan_repair(data)
    assert dropped == []


def test_single_healthy_record_is_a_noop():
    _, dropped = plan_repair(reg(rec("0.52.0")))
    assert dropped == []


def test_other_refs_are_untouched():
    data = reg(rec("0.45.0", project_path=r"D:\x"), rec("0.52.0"))
    data["plugins"]["other@mkt"] = [
        rec("1.0.0", project_path=r"D:\x"),
        rec("2.0.0"),
    ]
    keep, dropped = plan_repair(data)
    assert [r["version"] for r in dropped] == ["0.45.0"]
    assert len(data["plugins"]["other@mkt"]) == 2


def test_unrecognized_shape_is_a_noop():
    for data in ({}, {"plugins": []}, {"plugins": {REF: "nonsense"}}, []):
        assert plan_repair(data) == (None, [])


# --- file-level behavior -------------------------------------------------


def test_apply_is_idempotent_and_preserves_everything_else(tmp_path):
    path = tmp_path / "installed_plugins.json"
    data = reg(rec("0.45.0", project_path=r"D:\dev\env-config"), rec("0.52.0"))
    data["plugins"]["keep@mkt"] = [rec("3.0.0")]
    data["someOtherTopLevelKey"] = {"preserved": True}
    path.write_text(json.dumps(data), encoding="utf-8")

    assert len(apply_repair(str(path))) == 1
    assert apply_repair(str(path)) == []  # second run is a no-op

    after = json.loads(path.read_text(encoding="utf-8"))
    assert [r["version"] for r in after["plugins"][REF]] == ["0.52.0"]
    assert after["plugins"]["keep@mkt"] == [rec("3.0.0")]
    assert after["someOtherTopLevelKey"] == {"preserved": True}


def test_unparseable_and_missing_files_are_silent_noops(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert apply_repair(str(bad)) == []
    assert bad.read_text(encoding="utf-8") == "{not json"  # left alone

    assert apply_repair(str(tmp_path / "does-not-exist.json")) == []


def test_backup_is_written_only_when_repairing(tmp_path):
    path = tmp_path / "installed_plugins.json"
    backup = tmp_path / "installed_plugins.json.bootstrap-stuck-fix.bak"

    path.write_text(json.dumps(reg(rec("0.52.0"))), encoding="utf-8")
    apply_repair(str(path))
    assert not backup.exists()

    path.write_text(
        json.dumps(reg(rec("0.45.0", project_path=r"D:\x"), rec("0.52.0"))),
        encoding="utf-8",
    )
    apply_repair(str(path))
    assert backup.exists()


@pytest.mark.parametrize("scope", ["user", "project", None, "", "weird"])
def test_never_empties_a_ref(tmp_path, scope):
    """No input may ever leave a ref with zero records."""
    path = tmp_path / "installed_plugins.json"
    path.write_text(
        json.dumps(reg(rec("0.45.0", scope=scope, project_path=r"D:\x"))),
        encoding="utf-8",
    )
    apply_repair(str(path))
    after = json.loads(path.read_text(encoding="utf-8"))
    assert len(after["plugins"][REF]) >= 1
