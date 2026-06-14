"""Unit + integration tests for bootstrap_lib.shared_lib.

Covers the owner source-publish (sync), the consumer/standalone .pth link, content
caching, stale-module pruning, soft-skips, and an end-to-end real-venv import.
"""

import json
import os
import subprocess
import sys

import pytest

from bootstrap_lib import shared_lib
from bootstrap_lib.engine import _process_manifest


def _make_pkg(src_dir, name, modules=None, value=1):
    """Create a fake first-party package <src_dir>/<name>/ with given modules."""
    pkg = os.path.join(src_dir, name)
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8") as f:
        f.write(f"VALUE = {value}\n")
    for mod in (modules or []):
        with open(os.path.join(pkg, mod), "w", encoding="utf-8") as f:
            f.write("# module\n")
    return pkg


# --- sync_shared_lib (owner publish) -------------------------------------

class TestSync:
    def test_publishes_then_caches(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        _make_pkg(str(plugin_root / "lib"), "mylib")
        shared_root = str(tmp_path / "_shared_libs")

        r1 = shared_lib.sync_shared_lib("mylib", "lib", str(plugin_root), shared_root)
        assert r1.status == "published"
        # Package lands at <shared_root>/<name>/<name>/
        assert os.path.isfile(os.path.join(shared_root, "mylib", "mylib", "__init__.py"))

        r2 = shared_lib.sync_shared_lib("mylib", "lib", str(plugin_root), shared_root)
        assert r2.status == "cached"

    def test_resyncs_on_content_change(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        pkg = _make_pkg(str(plugin_root / "lib"), "mylib", value=1)
        shared_root = str(tmp_path / "_shared_libs")
        shared_lib.sync_shared_lib("mylib", "lib", str(plugin_root), shared_root)

        with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8") as f:
            f.write("VALUE = 99\n")
        r = shared_lib.sync_shared_lib("mylib", "lib", str(plugin_root), shared_root)
        assert r.status == "published"

    def test_prunes_stale_modules(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        pkg = _make_pkg(str(plugin_root / "lib"), "mylib", modules=["extra.py"])
        shared_root = str(tmp_path / "_shared_libs")
        shared_lib.sync_shared_lib("mylib", "lib", str(plugin_root), shared_root)
        dest_extra = os.path.join(shared_root, "mylib", "mylib", "extra.py")
        assert os.path.isfile(dest_extra)

        os.remove(os.path.join(pkg, "extra.py"))  # rename/delete in source
        shared_lib.sync_shared_lib("mylib", "lib", str(plugin_root), shared_root)
        assert not os.path.exists(dest_extra)  # clean re-sync pruned it

    def test_missing_source_fails(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        shared_root = str(tmp_path / "_shared_libs")
        r = shared_lib.sync_shared_lib("mylib", "lib", str(plugin_root), shared_root)
        assert r.status == "failed"


# --- link_shared_lib (.pth registration) ---------------------------------

class TestLink:
    def test_skipped_when_not_published(self, tmp_path):
        shared_root = str(tmp_path / "_shared_libs")
        r = shared_lib.link_shared_lib("mylib", sys.executable, shared_root)
        assert r.status == "skipped"
        assert "not yet published" in r.message

    def test_skipped_when_no_interpreter(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        _make_pkg(str(plugin_root / "lib"), "mylib")
        shared_root = str(tmp_path / "_shared_libs")
        shared_lib.sync_shared_lib("mylib", "lib", str(plugin_root), shared_root)

        r = shared_lib.link_shared_lib("mylib", None, shared_root)
        assert r.status == "skipped"
        r2 = shared_lib.link_shared_lib("mylib", str(tmp_path / "nope" / "python"), shared_root)
        assert r2.status == "skipped"

    def test_writes_pth_then_caches(self, tmp_path, monkeypatch):
        plugin_root = tmp_path / "plugin"
        _make_pkg(str(plugin_root / "lib"), "mylib")
        shared_root = str(tmp_path / "_shared_libs")
        shared_lib.sync_shared_lib("mylib", "lib", str(plugin_root), shared_root)

        site = tmp_path / "site"
        site.mkdir()
        monkeypatch.setattr(shared_lib, "purelib_of", lambda py: str(site))
        monkeypatch.setattr(shared_lib, "_verify_import", lambda py, name: True)

        r1 = shared_lib.link_shared_lib("mylib", sys.executable, shared_root)
        assert r1.status == "linked"
        pth = site / "mylib.pth"
        # Executable prepend .pth (wins over any stale installed shadow).
        entry = os.path.join(shared_root, "mylib")
        assert pth.read_text(encoding="utf-8").strip() == 'import sys; sys.path.insert(0, r"%s")' % entry

        r2 = shared_lib.link_shared_lib("mylib", sys.executable, shared_root)
        assert r2.status == "cached"

    def test_failed_when_import_fails(self, tmp_path, monkeypatch):
        plugin_root = tmp_path / "plugin"
        _make_pkg(str(plugin_root / "lib"), "mylib")
        shared_root = str(tmp_path / "_shared_libs")
        shared_lib.sync_shared_lib("mylib", "lib", str(plugin_root), shared_root)

        site = tmp_path / "site"
        site.mkdir()
        monkeypatch.setattr(shared_lib, "purelib_of", lambda py: str(site))
        monkeypatch.setattr(shared_lib, "_verify_import", lambda py, name: False)

        r = shared_lib.link_shared_lib("mylib", sys.executable, shared_root)
        assert r.status == "failed"


# --- end-to-end with a real venv -----------------------------------------

class TestRealVenv:
    def test_pth_makes_package_importable(self, tmp_path):
        venv_dir = tmp_path / "venv"
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
                capture_output=True, timeout=120, check=True,
            )
        except (subprocess.SubprocessError, OSError) as e:
            pytest.skip(f"could not create venv: {e}")

        from bootstrap_lib.venv_check import _find_python
        venv_python = _find_python(str(venv_dir))
        assert venv_python, "venv python not found"

        plugin_root = tmp_path / "plugin"
        _make_pkg(str(plugin_root / "lib"), "mylib", value=42)
        shared_root = str(tmp_path / "_shared_libs")
        shared_lib.sync_shared_lib("mylib", "lib", str(plugin_root), shared_root)

        r = shared_lib.link_shared_lib("mylib", venv_python, shared_root)
        assert r.status == "linked", r.message

        proc = subprocess.run(
            [venv_python, "-c", "import mylib; print(mylib.VALUE, mylib.__file__)"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert "42" in proc.stdout
        assert shared_root.replace("/", os.sep) in proc.stdout or "mylib" in proc.stdout

    def test_pth_wins_over_stale_installed_shadow(self, tmp_path):
        """Regression: a plain-path .pth only appends, so a pip-installed copy of
        the package in site-packages (e.g. left from a former git-dependency that
        uv sync didn't prune) would shadow the shared copy. The executable prepend
        .pth must win regardless."""
        venv_dir = tmp_path / "venv"
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
                capture_output=True, timeout=120, check=True,
            )
        except (subprocess.SubprocessError, OSError) as e:
            pytest.skip(f"could not create venv: {e}")

        from bootstrap_lib.venv_check import _find_python
        venv_python = _find_python(str(venv_dir))
        assert venv_python

        # Plant a STALE shadow copy directly in the venv's site-packages.
        site = subprocess.run(
            [venv_python, "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        shadow = os.path.join(site, "mylib")
        os.makedirs(shadow, exist_ok=True)
        with open(os.path.join(shadow, "__init__.py"), "w", encoding="utf-8") as f:
            f.write("VALUE = 'STALE'\n")  # no SHARED marker; missing newer API

        # Publish the real shared copy and link it.
        plugin_root = tmp_path / "plugin"
        _make_pkg(str(plugin_root / "lib"), "mylib", value=99)
        shared_root = str(tmp_path / "_shared_libs")
        shared_lib.sync_shared_lib("mylib", "lib", str(plugin_root), shared_root)
        r = shared_lib.link_shared_lib("mylib", venv_python, shared_root)
        assert r.status == "linked", r.message

        # The SHARED copy (VALUE=99) must win over the stale shadow (VALUE='STALE').
        proc = subprocess.run(
            [venv_python, "-c", "import mylib; print(mylib.VALUE)"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "99", f"stale shadow won: {proc.stdout!r}"


# --- engine wiring via _process_manifest ---------------------------------

class TestEngineWiring:
    def _dirs(self, tmp_path):
        # data_dir's parent is the marketplace data root; shared libs land in
        # <parent>/_shared_libs. Mirror that layout so shared_root is derivable.
        data_dir = tmp_path / "data" / "myplugin"
        data_dir.mkdir(parents=True)
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        shared_root = tmp_path / "data" / "_shared_libs"
        return data_dir, plugin_root, shared_root

    def test_owner_publish_via_process_manifest(self, tmp_path, monkeypatch):
        data_dir, plugin_root, shared_root = self._dirs(tmp_path)
        _make_pkg(str(plugin_root / "lib"), "mylib")
        # Don't touch the real standalone Python: the owner phase links to it via
        # find_standalone_python(); stub it out so the test stays hermetic.
        monkeypatch.setattr(shared_lib, "find_standalone_python", lambda: None)

        manifest = {"shared_libs": [{"name": "mylib", "src": "lib"}]}
        action_entries, ok_entries = [], []
        failures = _process_manifest(
            manifest, "windows", str(data_dir), str(plugin_root),
            action_entries, ok_entries, plugin_name="myplugin",
        )

        assert failures == []
        assert (shared_root / "mylib" / "mylib" / "__init__.py").exists()
        assert any("shared-lib mylib" in e for e in action_entries + ok_entries)

    def test_consumer_link_skips_without_venv(self, tmp_path):
        data_dir, plugin_root, shared_root = self._dirs(tmp_path)
        # Pre-publish the lib so the consumer has something to link to.
        pkg = shared_root / "mylib" / "mylib"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

        manifest = {"shared_lib_imports": ["mylib"]}
        action_entries, ok_entries = [], []
        failures = _process_manifest(
            manifest, "windows", str(data_dir), str(plugin_root),
            action_entries, ok_entries, plugin_name="myplugin",
        )

        # No venv at <data_dir>/.venv -> soft skip, no failure.
        assert failures == []
        assert any("shared-lib mylib" in e and "skip" in e.lower() for e in ok_entries)


# --- single-pass convergence sweep ---------------------------------------

class TestConvergenceSweep:
    """_shared_lib_convergence_sweep re-links consumers after every owner has
    published, so a consumer processed before its owner converges in the SAME
    pass instead of deferring to 'next session' (an avoidable restart)."""

    def _consumer_plugin(self, tmp_path, name, imports):
        """A plugin install dir whose bootstrap.json declares shared_lib_imports,
        plus a real (pip-less) venv at <data>/<name>/.venv so links can land."""
        install = tmp_path / "plugins" / name
        install.mkdir(parents=True)
        (install / "bootstrap.json").write_text(
            json.dumps({"shared_lib_imports": imports}), encoding="utf-8"
        )
        return install

    def test_converges_consumer_linked_in_same_pass(self, tmp_path):
        from bootstrap_lib.engine import _shared_lib_convergence_sweep
        from bootstrap_lib.plugin_resolve import PluginInfo

        # data_dir parent is the marketplace data root; _shared_libs lives beside it.
        data_root = tmp_path / "data"
        bootstrap_data = data_root / "mkt" / "bootstrap"
        bootstrap_data.mkdir(parents=True)
        shared_root = data_root / "mkt" / "_shared_libs"

        # Owner already published the lib (the sweep runs AFTER the owner loop).
        pkg = shared_root / "mylib" / "mylib"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("VALUE = 7\n", encoding="utf-8")

        # Consumer plugin + a real venv at <data_root>/consumer/.venv.
        install = self._consumer_plugin(tmp_path, "consumer", ["mylib"])
        venv_dir = data_root / "mkt" / "consumer" / ".venv"
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
                capture_output=True, timeout=120, check=True,
            )
        except (subprocess.SubprocessError, OSError) as e:
            pytest.skip(f"could not create venv: {e}")

        plugins = [PluginInfo(name="consumer", install_path=str(install), version="1.0", marketplace="mkt")]
        actions, oks, failures = _shared_lib_convergence_sweep(plugins, str(bootstrap_data))

        assert failures == []
        assert any("shared-lib mylib" in a and "linked" in a.lower() for a in actions), (
            f"sweep should have linked the consumer; actions={actions} oks={oks}"
        )

        # The link is real: the consumer venv can now import the shared package.
        from bootstrap_lib.venv_check import _find_python
        venv_python = _find_python(str(venv_dir))
        proc = subprocess.run(
            [venv_python, "-c", "import mylib; print(mylib.VALUE)"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "7"

    def test_idempotent_second_sweep_is_cached(self, tmp_path):
        from bootstrap_lib.engine import _shared_lib_convergence_sweep
        from bootstrap_lib.plugin_resolve import PluginInfo

        data_root = tmp_path / "data"
        bootstrap_data = data_root / "mkt" / "bootstrap"
        bootstrap_data.mkdir(parents=True)
        shared_root = data_root / "mkt" / "_shared_libs"
        pkg = shared_root / "mylib" / "mylib"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

        install = self._consumer_plugin(tmp_path, "consumer", ["mylib"])
        venv_dir = data_root / "mkt" / "consumer" / ".venv"
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
                capture_output=True, timeout=120, check=True,
            )
        except (subprocess.SubprocessError, OSError) as e:
            pytest.skip(f"could not create venv: {e}")

        plugins = [PluginInfo(name="consumer", install_path=str(install), version="1.0", marketplace="mkt")]
        _shared_lib_convergence_sweep(plugins, str(bootstrap_data))
        # Second sweep: already linked -> "cached" (verbose-only), no new actions.
        actions, oks, failures = _shared_lib_convergence_sweep(plugins, str(bootstrap_data))
        assert failures == []
        assert actions == [], f"second sweep should be a silent no-op, got {actions}"
        assert any("cached" in o.lower() for o in oks)

    def test_no_imports_is_noop(self, tmp_path):
        from bootstrap_lib.engine import _shared_lib_convergence_sweep
        from bootstrap_lib.plugin_resolve import PluginInfo

        data_root = tmp_path / "data"
        (data_root / "bootstrap").mkdir(parents=True)
        # Plugin with a bootstrap.json but no shared_lib_imports.
        install = tmp_path / "plugins" / "plain"
        install.mkdir(parents=True)
        (install / "bootstrap.json").write_text(json.dumps({"tools": []}), encoding="utf-8")

        plugins = [PluginInfo(name="plain", install_path=str(install), version="1.0", marketplace="mkt")]
        actions, oks, failures = _shared_lib_convergence_sweep(plugins, str(data_root / "bootstrap"))
        assert (actions, oks, failures) == ([], [], [])
