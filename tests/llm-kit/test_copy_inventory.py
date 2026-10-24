"""Inventory of the legacy test mirror: the disposition ledgers and the name map.

Two things are reconciled here without running the legacy suites:

* every legacy test file has exactly one ledger row (``rewrite`` or
  ``legacy-only``, each with a reason), and the rows' collection counts add up to
  the 5,276 tests the three suites collect;
* every legacy module has a row in the repoint map that says where its code lives
  in the final package (or why it has no home), every recorded home exists, and a
  legacy edit made after the map recorded a module is accounted for in the
  porting ledger.

The quarantined copy no longer exists; ``test_the_quarantined_copy_is_gone``
holds that line. The differential itself is test_repointed_suites.py.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import repoint

GEN = repoint.load_generator()
REPO = repoint.REPO
SUITES = repoint.SUITES
MAP = GEN.load_map()
LEDGERS = {suite: repoint.load_ledger(suite) for suite in SUITES}
FINAL_LIB = Path(__import__("llm_kit").__file__).resolve().parent


def _legacy_test_files(suite: str) -> list[str]:
    return [p for p in GEN.git_files(f"tests/{suite}") if p.rsplit("/", 1)[1].startswith("test_")
            and p.endswith(".py")]


# --- the disposition ledgers ---------------------------------------------

@pytest.mark.parametrize("suite", SUITES)
def test_every_legacy_test_file_has_exactly_one_row(suite):
    rows = [r["path"] for r in LEDGERS[suite]["rows"]]
    assert len(rows) == len(set(rows)), "duplicate ledger rows: " + ", ".join(
        sorted({p for p in rows if rows.count(p) > 1}))
    legacy = set(_legacy_test_files(suite))
    assert not legacy - set(rows), f"legacy test files with no row: {sorted(legacy - set(rows))}"
    assert not set(rows) - legacy, f"rows naming no legacy test file: {sorted(set(rows) - legacy)}"


@pytest.mark.parametrize("suite", SUITES)
def test_rows_are_classified_and_explained(suite):
    for row in LEDGERS[suite]["rows"]:
        assert row["disposition"] in GEN.DISPOSITIONS, row["path"]
        assert row["reason"].strip(), f"{row['path']}: no reason recorded"
        assert row["collection_count"] >= 0
        if row["disposition"] != "rewrite":
            assert row.get("declared_differences", {}) == {}, (
                f"{row['path']}: declared differences belong to mirrored rows")
            assert row["rewrite_classes"] == []


def test_no_row_is_left_deferred():
    """Every file has a final disposition: re-pointed, or legacy-only with its reason."""
    assert GEN.DISPOSITIONS == ("rewrite", "legacy-only")
    for suite in SUITES:
        for row in LEDGERS[suite]["rows"]:
            assert row["disposition"] in ("rewrite", "legacy-only"), row["path"]
            assert "I9" not in row["reason"] and "deferred-to" not in row["reason"].lower(), row["path"]
            for node, entry in row.get("declared_differences", {}).items():
                assert "I9" not in entry["reason"] and "quarantine" not in entry["reason"].lower(), node


@pytest.mark.parametrize("suite", SUITES)
def test_declared_differences_name_their_own_file(suite):
    for row in LEDGERS[suite]["rows"]:
        file_name = row["path"].rsplit("/", 1)[1]
        for node, entry in row.get("declared_differences", {}).items():
            assert node.split("::", 1)[0] == f"{suite}/{file_name}", node
            assert entry["mirror_status"] in ("skipped", "failed"), node
            assert entry["reason"].strip(), node
        for node in row.get("baseline_failures", []):
            assert node.split("::", 1)[0] == f"{suite}/{file_name}", node


@pytest.mark.parametrize("suite", SUITES)
def test_flaky_nodes_name_their_own_file_and_a_reason(suite):
    for row in LEDGERS[suite]["rows"]:
        file_name = row["path"].rsplit("/", 1)[1]
        for node, reason in row.get("flaky_nodes", {}).items():
            assert row["disposition"] == "rewrite", node
            assert node.split("::", 1)[0] == f"{suite}/{file_name}", node
            assert "[" not in node, f"{node}: flaky nodes are function-level"
            assert reason.strip(), node
            assert node not in row.get("baseline_failures", []), node


def test_a_failing_flaky_node_is_rerun_and_counts_only_when_its_own_rerun_passes(tmp_path, monkeypatch):
    """The differential tolerates a timing-dependent failure once, and nothing else."""
    outcome = repoint.Outcome
    flaky = {"s/test_x.py::test_race": "timing"}
    run = repoint.SuiteRun("s", {"s/test_x.py::test_race[1]": outcome("failed", "AssertionError", "m"),
                                  "s/test_x.py::test_other": outcome("failed", "AssertionError", "m")}, 1, "", "")
    calls = []

    def fake_pytest(target, junit, *, mirror, extra_args=(), timeout=0):
        calls.append(target)
        junit.write_text("<testsuite/>", encoding="utf-8")
        return None

    results = iter([{"s/test_x.py::test_race[1]": outcome("passed")}])
    monkeypatch.setattr(repoint, "run_pytest", fake_pytest)
    monkeypatch.setattr(repoint, "parse_junit", lambda path, suite: next(results))
    recovered = repoint.rerun_flaky("s", run, flaky, tmp_path, mirror=False)
    assert recovered == ["s/test_x.py::test_race[1]"]
    assert calls == ["tests/s/test_x.py::test_race"]                         # only the flaky node is re-run
    assert run.outcomes["s/test_x.py::test_race[1]"].status == "passed"
    assert run.outcomes["s/test_x.py::test_other"].status == "failed"        # a non-flaky failure stays

    run2 = repoint.SuiteRun("s", {"s/test_x.py::test_race[1]": outcome("failed", "AssertionError", "m")}, 1, "", "")
    again = iter([{"s/test_x.py::test_race[1]": outcome("failed", "AssertionError", "m")}] * 2)
    monkeypatch.setattr(repoint, "parse_junit", lambda path, suite: next(again))
    assert repoint.rerun_flaky("s", run2, flaky, tmp_path, mirror=False) == []
    assert run2.outcomes["s/test_x.py::test_race[1]"].status == "failed"     # a failure that repeats stays a failure


def test_collection_counts_reconcile_to_every_collected_test():
    totals = {suite: sum(r["collection_count"] for r in LEDGERS[suite]["rows"]) for suite in SUITES}
    assert totals == repoint.SUITE_TOTALS
    assert sum(totals.values()) == repoint.TOTAL_TESTS
    for suite in SUITES:
        assert LEDGERS[suite]["collection_total"] == totals[suite]


def test_the_version_test_is_green_so_no_baseline_failure_is_recorded():
    """The legacy __version__ drift (0.36.0 against manifest 0.37.0) was fixed by a patch bump."""
    rows = {r["path"]: r for r in LEDGERS["content-pipeline-kit"]["rows"]}
    row = rows["tests/content-pipeline-kit/test_version_matches_plugin_json.py"]
    assert row["disposition"] == "legacy-only"
    assert row["baseline_failures"] == []


def test_rewrite_classes_match_the_current_rewrite():
    """The classes a row records are what the rewriter applies today."""
    mirror = GEN.build_mirror(MAP, LEDGERS)
    for suite in SUITES:
        for row in LEDGERS[suite]["rows"]:
            if row["disposition"] != "rewrite":
                continue
            name = row["path"].rsplit("/", 1)[1]
            assert mirror[f"{suite}/{name}"][1] == row["rewrite_classes"], row["path"]


def test_every_rewrite_file_maps_to_final_names():
    """No rewrite file names a legacy module that has no final home."""
    unmapped: list[str] = []
    GEN.build_mirror(MAP, LEDGERS, unmapped)
    assert unmapped == []


# --- the map ---------------------------------------------------------------

def test_the_map_matches_the_legacy_trees_and_the_final_tree():
    assert GEN.check_problems() == []


def test_the_quarantined_copy_is_gone():
    assert not (FINAL_LIB / "_copy").exists()
    assert "copy_dir" not in json.dumps(MAP) and "_copy" not in json.dumps(MAP["modules"])


def test_every_legacy_package_file_is_classified():
    states = {}
    for row in GEN.legacy_package_files(MAP):
        states.setdefault(row["state"], []).append(row["legacy"])
    assert set(states) <= {"module", "dropped", "excluded"}
    assert sorted(states["dropped"]) == sorted(MAP["dropped"])
    assert sorted(states["excluded"]) == sorted(MAP["excluded"])
    assert sorted(states["module"]) == sorted(MAP["modules"])
    assert len(states["module"]) + len(states["dropped"]) + len(states["excluded"]) == sum(
        len(GEN.git_files(spec["source_dir"])) for spec in MAP["packages"].values())


def test_every_module_row_has_a_known_kind_and_a_home():
    for path, row in MAP["modules"].items():
        assert row["kind"] in GEN.KINDS, path
        if row["kind"] in ("module", "facade-package"):
            assert GEN.final_file(row["final"]) is not None, (path, row["final"])
        elif row["kind"] == "facade-names":
            assert isinstance(row["names"], dict), path
        elif row["kind"] == "forwarded":
            assert row["homes"] and all(GEN.final_file(h) for h in row["homes"]), path
        elif row["kind"] == "data":
            assert row["final"] is None, path


def test_the_cli_modules_have_their_group_homes():
    kinds = {p.rsplit("/lib/", 1)[1]: r for p, r in MAP["modules"].items()
             if p.endswith(("llm_scripting_kit/cli.py", "job_kit/cli.py", "content_pipeline/cli/run.py"))}
    lsk = kinds["llm_scripting_kit/cli.py"]
    assert lsk["kind"] == "forwarded"
    assert set(lsk["homes"]) == {"llm_kit.cli._common", "llm_kit.cli.account", "llm_kit.cli.completion",
                                 "llm_kit.cli.models", "llm_kit.cli.local"}
    assert kinds["job_kit/cli.py"]["kind"] == "forwarded" and kinds["job_kit/cli.py"]["homes"] == ["llm_kit.cli.jobs"]
    assert (kinds["content_pipeline/cli/run.py"]["kind"], kinds["content_pipeline/cli/run.py"]["final"]) == (
        "module", "llm_kit.cli.run")


def test_root_initializers_are_empty():
    assert GEN._read(FINAL_LIB / "__init__.py") == b""


def test_guard_is_the_canonical_bytes():
    canonical = REPO / MAP["guard"]["canonical"]
    assert GEN._read(FINAL_LIB / "bootstrap_guard.py") == GEN._read(canonical)


def test_the_final_tree_holds_no_legacy_import():
    """No final module imports a legacy package or a vendored guard by its old name."""
    offenders = {}
    roots = set(MAP["packages"]) | {"bootstrap_guard"}
    for path in sorted(FINAL_LIB.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        found = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found += [a.name for a in node.names if a.name.split(".")[0] in roots]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                if node.module.split(".")[0] in roots:
                    found.append(node.module)
        if found:
            offenders[path.relative_to(REPO).as_posix()] = found
    assert offenders == {}


def test_names_resolve_every_legacy_module_a_ledger_row_can_name():
    """Every legacy package a rewrite test imports from has a row to resolve through."""
    names = GEN.Names(MAP)
    for root in MAP["packages"]:
        assert root in names.rows, root


# --- the import-root guard --------------------------------------------------

def _child(code: str, tmp_path: Path, extra_env=None) -> subprocess.CompletedProcess:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(GEN.render_sitecustomize(MAP), encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": os.pathsep.join([str(site), str(Path(__import__("llm_kit").__file__).resolve().parent.parent),
                                       str(REPO / "plugins/bootstrap")]),
        "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1",
        "PYTHONNOUSERSITE": "1",
    })
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, encoding="utf-8", errors="replace", cwd=str(REPO))


def test_every_final_module_imports_under_the_guard(tmp_path):
    code = (
        "import importlib, pkgutil\n"
        "import llm_kit as root\n"
        "n = 0\n"
        "for m in pkgutil.walk_packages(root.__path__, 'llm_kit.'):\n"
        "    if m.name.endswith('.__main__'):\n"
        "        continue\n"
        "    importlib.import_module(m.name); n += 1\n"
        "print(n)\n"
    )
    result = _child(code, tmp_path)
    assert result.returncode == 0, result.stderr[-2000:]
    assert int(result.stdout.strip()) >= 150


@pytest.mark.parametrize("legacy", ["llm_scripting_kit", "content_pipeline", "job_kit"])
def test_the_guard_rejects_a_legacy_top_level_import(tmp_path, legacy):
    result = _child(f"import {legacy}", tmp_path)
    assert result.returncode != 0
    assert "legacy package imported in a mirrored run" in result.stderr


def test_the_guard_rejects_a_legacy_library_on_sys_path(tmp_path):
    code = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import json\n"
        "import xml.dom\n"
    ) % str(REPO / "plugins/job-kit/lib")
    result = _child(code, tmp_path)
    assert result.returncode != 0
    assert "legacy plugin directory on sys.path" in result.stderr


def test_the_guard_rejects_a_legacy_path_handed_to_a_child(tmp_path):
    code = (
        "import os, subprocess, sys\n"
        "env = dict(os.environ, PYTHONPATH=%r)\n"
        "subprocess.run([sys.executable, '-c', 'pass'], env=env)\n"
    ) % str(REPO / "plugins/content-pipeline-kit/lib")
    result = _child(code, tmp_path)
    assert result.returncode != 0
    assert "legacy plugin path handed to a child process" in result.stderr


def test_the_guard_keeps_the_mirror_on_a_child_environment(tmp_path):
    code = (
        "import os, subprocess, sys\n"
        "out = subprocess.run([sys.executable, '-c', 'import os; print(os.environ[\"PYTHONPATH\"])'],\n"
        "                     env={'PYTHONPATH': 'x', 'SystemRoot': os.environ.get('SystemRoot', '')},\n"
        "                     capture_output=True, text=True)\n"
        "print(out.stdout.strip())\n"
    )
    result = _child(code, tmp_path)
    assert result.returncode == 0, result.stderr
    assert os.path.normcase(str(REPO / "tmp")) in os.path.normcase(result.stdout)


def test_the_alias_tree_imports_without_a_legacy_package(tmp_path):
    """The generated legacy_names tree resolves names to llm_kit objects in a guarded child."""
    GEN.cmd_mirror(None)
    code = (
        "import legacy_names.llm_scripting_kit.completion as c\n"
        "import llm_kit.adapters.backends as b\n"
        "assert c.OpenRouterBackend is b.OpenRouterBackend\n"
        "import legacy_names.job_kit as j\n"
        "import llm_kit.jobs as final\n"
        "assert j.JobStore is final.JobStore\n"
        "print('ok')\n"
    )
    result = _child(code, tmp_path, {"PYTHONPATH": os.pathsep.join([
        str(tmp_path / "site"), str(REPO / "tmp"), str(Path(__import__("llm_kit").__file__).resolve().parent.parent),
        str(REPO / "plugins/bootstrap")])})
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "ok"


# --- the porting ledger -----------------------------------------------------

def test_porting_ledger_is_well_formed_and_only_names_mapped_modules():
    ledger = json.loads((REPO / "tests/llm-kit/porting-ledger.json").read_text(encoding="utf-8"))
    assert ledger["schema"] == 1
    for entry in ledger["entries"]:
        assert set(ledger["entry_fields"]) <= set(entry), entry
        assert entry["disposition"] in ledger["dispositions"], entry
        assert entry["legacy_path"] in MAP["modules"], (
            f"{entry['legacy_path']}: a legacy edit is recorded for a module the map does not know")


def test_a_legacy_edit_is_refused_until_it_is_recorded(tmp_path, monkeypatch):
    """check_problems keeps the map's baseline and refuses an unrecorded legacy edit."""
    import hashlib

    def sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    legacy_path = "plugins/x/lib/pkg/mod.py"
    legacy = tmp_path / legacy_path
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"A = 1\n")
    final = tmp_path / "packages/katsoft-llm-kit/src/llm_kit/pkg/mod.py"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"A = 1\n")
    ledger = tmp_path / "porting-ledger.json"
    ledger.write_text(json.dumps({"schema": 1, "entries": []}), encoding="utf-8")
    mapping = {"schema": 2, "modules": {legacy_path: {
        "kind": "module", "final": "llm_kit.pkg.mod", "legacy_sha256": sha(b"OLD\n")}}}
    monkeypatch.setattr(GEN, "REPO", tmp_path)
    monkeypatch.setattr(GEN, "LLM_KIT_LIB", tmp_path / "packages/katsoft-llm-kit/src")
    monkeypatch.setattr(GEN, "PORTING_LEDGER", ledger)
    monkeypatch.setattr(GEN, "load_map", lambda: mapping)
    monkeypatch.setattr(GEN, "legacy_package_files", lambda _m: [
        {"legacy": legacy_path, "package": "pkg", "state": "module"}])

    problems = GEN.check_problems()
    assert any("legacy module edited since the map recorded it" in p for p in problems), problems

    ledger.write_text(json.dumps({"schema": 1, "entries": [{
        "legacy_path": legacy_path, "legacy_sha256": sha(b"A = 1\n")}]}), encoding="utf-8")
    assert GEN.check_problems() == []
