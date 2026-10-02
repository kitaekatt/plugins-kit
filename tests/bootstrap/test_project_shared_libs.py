"""Step 4c2: ``project_venv.shared_lib_imports`` links published shared libs
into the PROJECT venv.

Integration tests drive ``engine._main`` in-process (the harness of
test_engine_python_export.py) against a real, stdlib-created venv, because
``link_shared_lib`` verifies with ``<python> -c "import <name>"`` and a fake
interpreter cannot answer that. Only the uv sync step is faked.

Each test's docstring names the production change that turns it red. Per
docs/reference/vacuous-checks.md shape 7, a property stated as an absence is
broken by an INSERTION, not a removal.
"""

import json
import os
import venv
from pathlib import Path

from bootstrap_lib import engine, venv_check
from bootstrap_lib.interpreter_env import PROJECT_VAR
from bootstrap_lib.records import EVENTS_FILENAME

from bootstrap.test_engine_python_export import (  # noqa: F401
    PROJECT_VENV, _expected_project_python, _home, _no_ambient_interpreter_env,
    _project)
from bootstrap.test_engine_ran_version import _fake_root

MKT = "plugins-kit"


def _roots(tmp_path):
    """data_root and the engine data_dir (<data_root>/<mkt>/bootstrap)."""
    data_root = tmp_path / "droot"
    data_dir = data_root / MKT / "bootstrap"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_root, data_dir


def _publish(data_root, name, mkt=MKT):
    pkg = data_root / mkt / "_shared_libs" / name / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")


def _real_venv(project):
    venv.create(project / ".venv", with_pip=False)
    return venv_check._find_python(str(project / ".venv"))


def _site(python):
    from bootstrap_lib.shared_lib import purelib_of
    return Path(purelib_of(python))


def _noop_sync(*_a, **_k):
    """Stand-in for the uv sync: the venv already exists and is healthy."""


def _failing_sync(venv_def, data_dir, plugin_root, prefix, label, action_entries,
                  ok_entries, failures, **_kw):
    action_entries.append("project_venv: FAILED - fake sync failure")
    failures.append({"type": "project_venv", "plugin": "config",
                     "message": "fake sync failure"})


def _run(tmp_path, monkeypatch, project):
    """One console pass; returns (record texts, exit code)."""
    _data_root, data_dir = _roots(tmp_path)
    root = str(tmp_path / "plugin_root") if (tmp_path / "plugin_root").exists() \
        else _fake_root(tmp_path, "0.120.0")
    home = _home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setattr("sys.argv", [
        "bootstrap_engine.py", "--plugin-root", root, "--data-dir", str(data_dir),
        "--project-dir", str(project), "--project-key", "_global_",
        "--console", "--exit-status"])
    recorders = []
    real = engine._new_recorder
    monkeypatch.setattr(engine, "_new_recorder",
                        lambda *a: recorders.append(real(*a)) or recorders[-1])
    events = data_dir / EVENTS_FILENAME
    already = len(events.read_text(encoding="utf-8").splitlines()) if events.exists() else 0
    code = 0
    try:
        engine._main()
    except SystemExit as exc:
        code = exc.code or 0
    finally:
        for recorder in recorders:
            recorder.flush()
    lines = events.read_text(encoding="utf-8").splitlines() if events.exists() else []
    return [json.loads(x).get("text") or "" for x in lines[already:] if x.strip()], code


def _declared(tmp_path, monkeypatch, imports, *, sync=_noop_sync):
    monkeypatch.setattr(engine, "_process_venv_def", sync)
    vdef = dict(PROJECT_VENV, shared_lib_imports=imports)
    return _project(tmp_path, {"project_venv": vdef})


class TestLinkSite:
    def test_first_pass_links_second_pass_is_cached(self, tmp_path, monkeypatch):
        """Removing the Step 4c2 call block in _main turns this red."""
        data_root, _ = _roots(tmp_path)
        _publish(data_root, "fakelib")
        project = _declared(tmp_path, monkeypatch, ["fakelib"])
        pth = _site(_real_venv(project)) / "fakelib.pth"

        texts, code = _run(tmp_path, monkeypatch, project)
        assert pth.is_file(), texts
        assert texts.count("linked fakelib (project)") == 1, texts
        assert code == 0, texts

        before = pth.stat().st_mtime_ns
        texts, code = _run(tmp_path, monkeypatch, project)
        assert "linked fakelib (project)" not in texts, texts
        assert any("shared-lib fakelib: linked (cached" in t for t in texts), texts
        assert pth.stat().st_mtime_ns == before
        assert code == 0

    def test_owner_not_published_is_a_soft_skip(self, tmp_path, monkeypatch):
        """Routing absent to a failure (an insertion) turns this red."""
        _roots(tmp_path)
        project = _declared(tmp_path, monkeypatch, ["fakelib"])
        python = _real_venv(project)
        texts, code = _run(tmp_path, monkeypatch, project)
        assert code == 0, texts
        assert any("shared-lib fakelib: shared lib fakelib not published" in t
                   for t in texts), texts
        assert not (_site(python) / "fakelib.pth").exists()

    def test_ambiguous_is_a_config_failure_with_the_resolver_message(
            self, tmp_path, monkeypatch):
        """Routing ambiguous like absent (to oks) turns this red."""
        data_root, _ = _roots(tmp_path)
        _publish(data_root, "fakelib", "mkt-a")
        _publish(data_root, "fakelib", "mkt-b")
        project = _declared(tmp_path, monkeypatch, ["fakelib"])
        python = _real_venv(project)
        texts, code = _run(tmp_path, monkeypatch, project)
        assert code == 1, texts
        assert any("FAILED" in t and "mkt-a, mkt-b" in t and '"marketplace"' in t
                   for t in texts), texts
        assert not (_site(python) / "fakelib.pth").exists()

    def test_unwritable_target_is_a_config_failure(self, tmp_path, monkeypatch):
        """A directory squatting on <name>.pth makes the write fail; routing
        failed to oks turns this red."""
        data_root, _ = _roots(tmp_path)
        _publish(data_root, "fakelib")
        project = _declared(tmp_path, monkeypatch, ["fakelib"])
        (_site(_real_venv(project)) / "fakelib.pth").mkdir()
        texts, code = _run(tmp_path, monkeypatch, project)
        assert code == 1, texts
        assert any("shared-lib fakelib: FAILED" in t for t in texts), texts

    def test_qualified_entry_selects_its_marketplace(self, tmp_path, monkeypatch):
        data_root, _ = _roots(tmp_path)
        _publish(data_root, "fakelib", "mkt-a")
        _publish(data_root, "fakelib", "mkt-b")
        project = _declared(tmp_path, monkeypatch,
                            [{"name": "fakelib", "marketplace": "mkt-b"}])
        python = _real_venv(project)
        texts, code = _run(tmp_path, monkeypatch, project)
        assert code == 0, texts
        assert "mkt-b" in (_site(python) / "fakelib.pth").read_text(encoding="utf-8")

    def test_existing_sitecustomize_is_reported_and_left_alone(
            self, tmp_path, monkeypatch):
        """Dropping the detection turns this red."""
        data_root, _ = _roots(tmp_path)
        _publish(data_root, "fakelib")
        project = _declared(tmp_path, monkeypatch, ["fakelib"])
        shim = _site(_real_venv(project)) / "sitecustomize.py"
        shim.write_text("# consumer shim\n", encoding="utf-8")
        texts, _code = _run(tmp_path, monkeypatch, project)
        assert any(str(shim) in t and "prepends the same generation twice" in t
                   for t in texts), texts
        assert shim.read_text(encoding="utf-8") == "# consumer shim\n"

    def test_clean_venv_has_no_shim_entry(self, tmp_path, monkeypatch):
        data_root, _ = _roots(tmp_path)
        _publish(data_root, "fakelib")
        project = _declared(tmp_path, monkeypatch, ["fakelib"])
        _real_venv(project)
        texts, _code = _run(tmp_path, monkeypatch, project)
        assert not any("sitecustomize" in t for t in texts), texts


class TestMalformedEntryDoesNotWithholdExport:
    def test_bad_entry_is_reported_and_export_still_happens(self, tmp_path, monkeypatch):
        """Half 1. Removing the "blocks_venv": False flag (the failure gates the
        export again) turns this red."""
        _roots(tmp_path)
        project = _declared(tmp_path, monkeypatch, ["ok", 7])
        _real_venv(project)
        texts, code = _run(tmp_path, monkeypatch, project)
        assert code == 1, texts  # the pass still reports failure
        assert any("bad shared_lib_imports" in t for t in texts), texts
        assert os.environ.get(PROJECT_VAR) == _expected_project_python(str(project))
        assert any(f"exported {PROJECT_VAR}=" in t for t in texts), texts

    def test_genuine_venv_failure_still_suppresses_the_export(self, tmp_path, monkeypatch):
        """Half 2, the guard on behaviour that must be preserved. Making the
        Step 3d gate unconditional turns this red."""
        _roots(tmp_path)
        project = _declared(tmp_path, monkeypatch, ["ok"], sync=_failing_sync)
        _real_venv(project)
        texts, code = _run(tmp_path, monkeypatch, project)
        assert code == 1, texts
        assert not any(f"exported {PROJECT_VAR}=" in t for t in texts), texts

    def test_bad_entry_plus_venv_failure_still_suppresses(self, tmp_path, monkeypatch):
        """A bad entry riding along with a genuine venv failure must not
        launder it: the exemption is per failure, not per pass."""
        _roots(tmp_path)
        project = _declared(tmp_path, monkeypatch, [7], sync=_failing_sync)
        _real_venv(project)
        texts, _code = _run(tmp_path, monkeypatch, project)
        assert not any(f"exported {PROJECT_VAR}=" in t for t in texts), texts


class TestQualifiedSupersedesBare:
    def test_normalizer_drops_the_bare_twin(self):
        """Removing the supersede filter turns this red."""
        entries, failures = engine._normalize_project_shared_lib_imports({
            "shared_lib_imports": ["x", {"name": "x", "marketplace": "m"}, "y"]})
        assert failures == []
        assert entries == [{"name": "x", "marketplace": "m"},
                           {"name": "y", "marketplace": None}]

    def test_normalizer_drops_the_bare_twin_in_either_order(self):
        entries, _ = engine._normalize_project_shared_lib_imports({
            "shared_lib_imports": [{"name": "x", "marketplace": "m"}, "x"]})
        assert entries == [{"name": "x", "marketplace": "m"}]

    def test_no_spurious_ambiguity_when_both_forms_declared(self, tmp_path, monkeypatch):
        """End to end: with the bare twin kept, two marketplaces make it
        ambiguous and the pass fails although the qualified entry linked."""
        data_root, _ = _roots(tmp_path)
        _publish(data_root, "fakelib", "mkt-a")
        _publish(data_root, "fakelib", "mkt-b")
        project = _declared(tmp_path, monkeypatch,
                            ["fakelib", {"name": "fakelib", "marketplace": "mkt-a"}])
        _real_venv(project)
        texts, code = _run(tmp_path, monkeypatch, project)
        assert code == 0, texts


class TestLinkRunsAfterOwnersPublish:
    def test_consumer_before_owner_converges_in_one_pass(self, tmp_path, monkeypatch):
        """Moving the ``_link_project_shared_libs`` call from Step 4c2 up into
        Step 3d turns this red: the library does not exist until the owner
        publishes during Step 4, so an inline link soft-skips and the project
        waits for the next session.

        The owner is simulated by publishing from inside ``_phase2_new_plugins``,
        the first call made after the Step 4 plugin loop. The link must have
        succeeded in THIS pass, not been skipped with a promise to retry.
        """
        data_root, _ = _roots(tmp_path)
        project = _declared(tmp_path, monkeypatch, ["lateowner"])
        pth = _site(_real_venv(project)) / "lateowner.pth"
        order = []
        real_link = engine._link_project_shared_libs
        real_phase2 = engine._phase2_new_plugins

        def publishing_phase2(*a, **k):
            assert not pth.exists(), "link ran before the owner published"
            _publish(data_root, "lateowner")
            order.append("owner_published")
            return real_phase2(*a, **k)

        def spying_link(*a, **k):
            order.append("project_link")
            return real_link(*a, **k)

        monkeypatch.setattr(engine, "_phase2_new_plugins", publishing_phase2)
        monkeypatch.setattr(engine, "_link_project_shared_libs", spying_link)

        texts, code = _run(tmp_path, monkeypatch, project)

        assert order == ["owner_published", "project_link"], order
        assert pth.is_file(), texts
        assert texts.count("linked lateowner (project)") == 1, texts
        assert code == 0, texts


class TestOwnCopyShadowWarning:
    """The shared copy is prepended, so a copy the venv already holds is
    shadowed with no ImportError. The warning is the only signal."""

    def _setup(self, tmp_path, monkeypatch):
        data_root, _ = _roots(tmp_path)
        _publish(data_root, "shadowlib")
        project = _declared(tmp_path, monkeypatch, ["shadowlib"])
        return project, _site(_real_venv(project))

    @staticmethod
    def _shadow(texts):
        return [t for t in texts if t.startswith(
            "config: shared-lib shadowlib: the project venv already holds its own")]

    def test_own_copy_with_dist_info_is_named_with_its_version(
            self, tmp_path, monkeypatch):
        """Check 1. Removing the _project_venv_own_copy call in
        _link_project_shared_libs turns this red."""
        project, site = self._setup(tmp_path, monkeypatch)
        (site / "shadowlib").mkdir()
        (site / "shadowlib" / "__init__.py").write_text("VALUE = 9\n", encoding="utf-8")
        (site / "shadowlib-2.3.4.dist-info").mkdir()
        texts, code = _run(tmp_path, monkeypatch, project)
        hits = self._shadow(texts)
        assert len(hits) == 1 and "shadowlib 2.3.4" in hits[0], texts
        assert "shared_lib_imports" in hits[0], hits
        assert code == 0, texts  # check 3: a warning, not a failure
        assert (site / "shadowlib" / "__init__.py").read_text(
            encoding="utf-8") == "VALUE = 9\n"
        assert os.environ.get(PROJECT_VAR) == _expected_project_python(str(project))

    def test_own_module_file_without_dist_info_is_named_without_a_version(
            self, tmp_path, monkeypatch):
        """Dropping the package/module presence test turns this red."""
        project, site = self._setup(tmp_path, monkeypatch)
        (site / "shadowlib.py").write_text("VALUE = 9\n", encoding="utf-8")
        texts, code = _run(tmp_path, monkeypatch, project)
        hits = self._shadow(texts)
        assert len(hits) == 1 and "its own shadowlib;" in hits[0], texts
        assert code == 0, texts

    def test_clean_venv_has_no_shadow_warning(self, tmp_path, monkeypatch):
        """Check 2. Making the helper always return a name (an insertion)
        turns this red."""
        project, _site_dir = self._setup(tmp_path, monkeypatch)
        texts, code = _run(tmp_path, monkeypatch, project)
        assert self._shadow(texts) == [], texts
        assert code == 0, texts

    def test_a_shadow_appearing_after_the_first_link_is_still_reported(
            self, tmp_path, monkeypatch):
        """The cached branch. Restricting the warning to status == linked
        turns this red."""
        project, site = self._setup(tmp_path, monkeypatch)
        texts, _ = _run(tmp_path, monkeypatch, project)
        assert self._shadow(texts) == [], texts
        (site / "shadowlib-1.0.dist-info").mkdir()
        texts, code = _run(tmp_path, monkeypatch, project)
        assert any("shadowlib 1.0" in t for t in self._shadow(texts)), texts
        assert code == 0, texts
