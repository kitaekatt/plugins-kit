"""Tests for the code-references scanner.

The scanner only emits content paths that resolve to a real on-disk asset
under a discovered mount point. Anything else (test fixtures, /Script/...
class paths, third-party include paths) is filtered out.
"""

import os
import sys
import builtins
from pathlib import Path

import pytest

_LIB_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins"
    / "unreal-kit"
    / "skills"
    / "fix-up-redirectors"
    / "lib"
)
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import code_refs
from code_refs import IncompleteScanError, discover_mount_points, scan


def _make_project(tmp_path: Path):
    """Build a minimal fake UE project layout under tmp_path:

        Game/
          MyGame.uproject
          Content/                       -> /Game
            UI/WBP_Real.uasset
            Maps/Level_Real.umap
        Engine/
          Content/                       -> /Engine
            Slate/Foo.uasset
        Plugins/MyPlugin/
          MyPlugin.uplugin
          Content/                       -> /MyPlugin
            Bar.uasset
        Source/
          (populated by tests)
    """
    proj = tmp_path / "Game"
    (proj / "Content" / "UI").mkdir(parents=True)
    (proj / "Content" / "UI" / "WBP_Real.uasset").write_bytes(b"\x00")
    (proj / "Content" / "Maps").mkdir(parents=True)
    (proj / "Content" / "Maps" / "Level_Real.umap").write_bytes(b"\x00")
    (proj / "MyGame.uproject").write_text("{}")

    engine_content = tmp_path / "Engine" / "Content" / "Slate"
    engine_content.mkdir(parents=True)
    (engine_content / "Foo.uasset").write_bytes(b"\x00")

    plugin = tmp_path / "Plugins" / "MyPlugin"
    (plugin / "Content").mkdir(parents=True)
    (plugin / "Content" / "Bar.uasset").write_bytes(b"\x00")
    (plugin / "MyPlugin.uplugin").write_text("{}")

    src = tmp_path / "Source"
    src.mkdir()
    return tmp_path, src


class TestDiscoverMountPoints:
    def test_discovers_game_engine_and_plugin(self, tmp_path):
        root, _ = _make_project(tmp_path)
        mounts = discover_mount_points(str(root))

        assert "/Game" in mounts
        assert "/Engine" in mounts
        assert "/MyPlugin" in mounts
        assert mounts["/Game"].endswith("Content")
        assert mounts["/Engine"].endswith("Content")
        assert mounts["/MyPlugin"].endswith("Content")

    def test_no_uproject_means_no_game_mount(self, tmp_path):
        # Bare directory, no .uproject anywhere.
        (tmp_path / "Source").mkdir()
        mounts = discover_mount_points(str(tmp_path))
        assert "/Game" not in mounts

    def test_prefers_shallowest_uproject_for_game_mount(self, tmp_path):
        # Real UE projects have engine sub-tools (UnrealLightmass etc.)
        # with their own .uproject files. /Game should map to the project's
        # uproject, not whichever one os.walk happens to visit first.
        root, _ = _make_project(tmp_path)

        deep_tool = (
            root / "Engine" / "Programs" / "UnrealLightmass"
        )
        (deep_tool / "Content").mkdir(parents=True)
        (deep_tool / "Content" / "Tool.uasset").write_bytes(b"\x00")
        (deep_tool / "UnrealLightmass.uproject").write_text("{}")

        mounts = discover_mount_points(str(root))
        assert mounts["/Game"].endswith(
            os.path.join("Game", "Content")
        )


class TestScan:
    def test_keeps_real_asset_references(self, tmp_path):
        root, src = _make_project(tmp_path)
        (src / "main.cpp").write_text(
            'static const char* kWidget = "/Game/UI/WBP_Real";\n'
            'static const char* kMap = "/Game/Maps/Level_Real";\n'
        )

        refs, _, _, _ = scan(str(root))
        assert "/Game/UI/WBP_Real" in refs
        assert "/Game/Maps/Level_Real" in refs

    def test_drops_paths_under_unknown_mount(self, tmp_path):
        root, src = _make_project(tmp_path)
        (src / "test_fixtures.cpp").write_text(
            'TEST("/A/B/C");\n'
            'TEST("/A/../A/./B");\n'
            'static const char* k = "/KhronosGroup/glTF/Foo";\n'
        )

        refs, _, _, _ = scan(str(root))
        assert refs == set()

    def test_drops_script_paths(self, tmp_path):
        # /Script/<Module>.<Class> are class refs, not asset refs. Mount
        # discovery never includes /Script, so they're filtered out.
        root, src = _make_project(tmp_path)
        (src / "ini.ini").write_text(
            'GameClass=/Script/Engine.GameMode\n'
            'WidgetClass=/Script/UMG.UserWidget\n'
        )

        refs, _, _, _ = scan(str(root))
        assert refs == set()

    def test_drops_paths_to_missing_assets(self, tmp_path):
        # /Game/UI/WBP_Real exists; /Game/UI/WBP_Ghost does not.
        root, src = _make_project(tmp_path)
        (src / "stale.cpp").write_text(
            'static const char* kReal = "/Game/UI/WBP_Real";\n'
            'static const char* kGhost = "/Game/UI/WBP_Ghost";\n'
        )

        refs, _, _, _ = scan(str(root))
        assert refs == {"/Game/UI/WBP_Real"}

    def test_normalizes_dotted_object_paths(self, tmp_path):
        # `/Game/UI/WBP_Real.WBP_Real` and `/Game/UI/WBP_Real.WBP_Real_C`
        # both refer to the same package.
        root, src = _make_project(tmp_path)
        (src / "dotted.cpp").write_text(
            'A = "/Game/UI/WBP_Real.WBP_Real";\n'
            'B = "/Game/UI/WBP_Real.WBP_Real_C";\n'
        )

        refs, _, _, _ = scan(str(root))
        assert refs == {"/Game/UI/WBP_Real"}

    def test_keeps_engine_and_plugin_references(self, tmp_path):
        root, src = _make_project(tmp_path)
        (src / "main.cpp").write_text(
            'A = "/Engine/Slate/Foo";\n'
            'B = "/MyPlugin/Bar";\n'
        )

        refs, _, _, _ = scan(str(root))
        assert "/Engine/Slate/Foo" in refs
        assert "/MyPlugin/Bar" in refs

    def test_verify_on_disk_false_keeps_unverified(self, tmp_path):
        # When verification is disabled, paths under known mounts are kept
        # even if no asset exists on disk.
        root, src = _make_project(tmp_path)
        (src / "stale.cpp").write_text('K = "/Game/UI/WBP_Ghost";\n')

        refs, _, _, _ = scan(str(root), verify_on_disk=False)
        assert "/Game/UI/WBP_Ghost" in refs

    def test_returns_discovered_mounts(self, tmp_path):
        root, _ = _make_project(tmp_path)
        _, _, _, mounts = scan(str(root))
        assert set(mounts) >= {"/Game", "/Engine", "/MyPlugin"}

    @pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
    def test_scans_utf16_source_files(self, tmp_path, encoding):
        root, src = _make_project(tmp_path)
        (src / "ref.ini").write_bytes(
            'Redirector="/Game/UI/WBP_Real"'.encode(encoding)
        )

        refs, _, _, _ = scan(str(root))

        assert "/Game/UI/WBP_Real" in refs

    def test_lowercase_plugin_mount_is_scanned(self, tmp_path):
        root, src = _make_project(tmp_path)
        plugin = root / "Plugins" / "lowerplugin"
        (plugin / "Content").mkdir(parents=True)
        (plugin / "Content" / "Bar.uasset").write_bytes(b"\x00")
        (plugin / "lowerplugin.uplugin").write_text("{}")
        (src / "ref.cpp").write_text('K = "/lowerplugin/Bar";')

        refs, _, _, mounts = scan(str(root))

        assert "/lowerplugin" in mounts
        assert "/lowerplugin/Bar" in refs

    def test_oversized_eligible_file_fails_closed(self, tmp_path):
        root, src = _make_project(tmp_path)
        (src / "huge.cpp").write_bytes(
            b"x" * (code_refs._MAX_FILE_BYTES + 1)
            + b'\nK = "/Game/UI/WBP_Real";'
        )

        with pytest.raises(IncompleteScanError, match="oversized"):
            scan(str(root))

    def test_stat_failure_fails_closed(self, tmp_path, monkeypatch):
        root, src = _make_project(tmp_path)
        target = src / "ref.cpp"
        target.write_text('K = "/Game/UI/WBP_Real";')
        original_getsize = code_refs.os.path.getsize

        def fail_for_target(path):
            if os.path.abspath(path) == os.path.abspath(target):
                raise OSError("stat denied")
            return original_getsize(path)

        monkeypatch.setattr(code_refs.os.path, "getsize", fail_for_target)

        with pytest.raises(IncompleteScanError, match="stat"):
            scan(str(root))

    def test_open_failure_fails_closed(self, tmp_path, monkeypatch):
        root, src = _make_project(tmp_path)
        target = src / "ref.cpp"
        target.write_text('K = "/Game/UI/WBP_Real";')
        original_open = builtins.open

        def fail_for_target(path, *args, **kwargs):
            if os.path.abspath(path) == os.path.abspath(target):
                raise OSError("open denied")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fail_for_target)

        with pytest.raises(IncompleteScanError, match="open"):
            scan(str(root))

    def test_directory_enumeration_failure_fails_closed(self, tmp_path, monkeypatch):
        root, src = _make_project(tmp_path)
        (src / "ref.cpp").write_text('K = "/Game/UI/WBP_Real";')
        original_walk = code_refs.os.walk

        def walk_with_error(path, *args, **kwargs):
            onerror = kwargs.get("onerror")
            for item in original_walk(path, *args, **kwargs):
                yield item
            if onerror and os.path.abspath(path) == os.path.abspath(root):
                onerror(OSError("unreadable subtree"))

        monkeypatch.setattr(code_refs.os, "walk", walk_with_error)

        with pytest.raises(IncompleteScanError, match="directory"):
            scan(str(root))


class TestCacheAge:
    """U14 regression: save()/get_age_hours() must work without the deprecated
    datetime.utcnow() and agree on the timestamp format (trailing-Z UTC)."""

    def test_fresh_cache_age_is_near_zero(self, tmp_path):
        import warnings

        from code_refs import get_age_hours, load, save

        cache = tmp_path / "code_references.yaml"
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            save(str(cache), {"/Game/UI/WBP_X"}, str(tmp_path), 1, 1, (".cpp",))
            age = get_age_hours(str(cache))

        assert age is not None
        assert 0 <= age < 0.1

        doc = load(str(cache))
        assert doc["generated_at"].endswith("Z")
        assert "+00:00" not in doc["generated_at"]

    def test_missing_cache_age_is_none(self, tmp_path):
        from code_refs import get_age_hours

        assert get_age_hours(str(tmp_path / "nope.yaml")) is None
