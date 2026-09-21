"""Fake UE/P4 boundaries for exercising the real apply_fixups entrypoint."""

from __future__ import annotations

import contextlib
import io
import json
import runpy
import sys
import types
from pathlib import Path
from typing import Any, Iterator

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "unreal-kit"
    / "skills"
    / "fix-up-redirectors"
    / "scripts"
    / "apply_fixups.py"
)


class ApplyHarness:
    """Run apply_fixups with explicit, fail-fast fake side effects."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        mode: str = "fixup",
        load_results: list[Any] | None = None,
        save_result: bool = True,
        setter_error: Exception | None = None,
        soft_rewrite_error: Exception | None = None,
        fail_manifest: bool = False,
    ) -> None:
        self.project = tmp_path / "project"
        self.project.mkdir()
        (self.project / "Saved" / "PythonOutput").mkdir(parents=True)
        self.redirector_file = self.project / "Content" / "R.uasset"
        self.redirector_file.parent.mkdir(parents=True)
        self.redirector_file.write_bytes(b"redirector")
        self.referencer_file = self.project / "Content" / "A.uasset"
        self.referencer_file.write_bytes(b"referencer")
        self.safe_json = tmp_path / "safe.json"
        record = {
            "pkg": "/Game/R",
            "file": str(self.redirector_file),
            "target_pkg": "/Game/Target",
            "target_exists": True,
            "referencer_pkgs": ["/Game/A"],
            "referencer_files": [str(self.referencer_file)],
        }
        self.safe_json.write_text(
            json.dumps({"scope": "/Game", "redirectors": [record]}),
            encoding="utf-8",
        )
        self.mode = mode
        self.load_results = list(load_results or [object(), object()])
        self.save_result = save_result
        self.setter_error = setter_error
        self.soft_rewrite_error = soft_rewrite_error
        self.fail_manifest = fail_manifest
        self.events: list[tuple[str, Any]] = []
        self._load_count = 0
        self._manifest_writer = None

    def _unreal_module(self) -> types.ModuleType:
        harness = self
        module = types.ModuleType("unreal")

        class _Paths:
            @staticmethod
            def project_dir() -> str:
                return str(harness.project)

        class _CollectionSettings:
            pass

        class _Collection:
            def set_editor_property(self, name: str, value: Any) -> None:
                harness.events.append(("collection_set", (name, value)))
                if harness.setter_error:
                    raise harness.setter_error

        class _AssetTools:
            def rename_referencing_soft_object_paths(
                self, packages: Any, mapping: Any
            ) -> None:
                harness.events.append(("soft_rewrite", list(packages)))
                if harness.soft_rewrite_error:
                    raise harness.soft_rewrite_error

        class _AssetToolsHelpers:
            @staticmethod
            def get_asset_tools() -> _AssetTools:
                harness.events.append(("asset_tools", None))
                return _AssetTools()

        class _EditorAssetLibrary:
            @staticmethod
            def load_asset(pkg: str) -> Any:
                harness.events.append(("load", pkg))
                if harness._load_count >= len(harness.load_results):
                    return object()
                result = harness.load_results[harness._load_count]
                harness._load_count += 1
                return result

            @staticmethod
            def save_loaded_asset(asset: Any, only_if_is_dirty: bool = False) -> bool:
                harness.events.append(("save", only_if_is_dirty))
                return harness.save_result

            @staticmethod
            def does_asset_exist(pkg: str) -> bool:
                harness.events.append(("exists", pkg))
                return True

            @staticmethod
            def delete_asset(pkg: str) -> bool:
                harness.events.append(("ue_delete", pkg))
                return True

        class _SystemLibrary:
            @staticmethod
            def collect_garbage(_purge: int) -> None:
                harness.events.append(("gc", None))

        module.Paths = _Paths
        module.CollectionSettings = _CollectionSettings
        module.AssetToolsHelpers = _AssetToolsHelpers
        module.EditorAssetLibrary = _EditorAssetLibrary
        module.SystemLibrary = _SystemLibrary
        module.SoftObjectPath = lambda value: value
        module.Name = lambda value: value
        module.Array = lambda _type: []
        module.get_default_object = lambda _kind: _Collection()
        return module

    def _p4_module(self) -> types.ModuleType:
        harness = self
        module = types.ModuleType("p4cli")

        def create_pending_cl(description: str, client: str | None = None) -> str:
            harness.events.append(("create_cl", description))
            return "123"

        def edit_files(cl: str, files: list[str]) -> None:
            harness.events.append(("edit", (cl, list(files))))

        def get_p4_user() -> str:
            harness.events.append(("p4_user", None))
            return "alice"

        def reopen_files(cl: str, files: list[str]) -> None:
            harness.events.append(("reopen", (cl, list(files))))

        def run_p4(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
            harness.events.append(("run_p4", (list(args), stdin)))
            if args[:2] == ["opened", "-c"]:
                return 0, "", ""
            if "fstat" in args:
                return 0, "\n".join(
                    f"... depotFile {harness.redirector_file}\n"
                    for _ in [0]
                ), ""
            if args and args[0] == "delete":
                harness.events.append(("p4_delete", args[-1]))
                return 0, "", ""
            raise AssertionError(f"unexpected run_p4 call: {args!r}")

        def run_p4_or_die(args: list[str], **_kwargs: Any) -> str:
            harness.events.append(("run_p4_or_die", list(args)))
            return ""

        module.create_pending_cl = create_pending_cl
        module.delete_files = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("delete_files should not be called by apply_fixups")
        )
        module.edit_files = edit_files
        module.get_p4_user = get_p4_user
        module.reopen_files = reopen_files
        module.run_p4 = run_p4
        module.run_p4_or_die = run_p4_or_die
        return module

    @contextlib.contextmanager
    def _modules(self) -> Iterator[None]:
        names = ("bootstrap", "path_repair", "unreal", "p4cli", "redirector_record")
        saved = {name: sys.modules.get(name) for name in names}
        bootstrap = types.ModuleType("bootstrap")
        bootstrap.ensure_dependencies = lambda: None
        path_repair = types.ModuleType("path_repair")
        path_repair.repair_path = lambda: None
        sys.modules.update(
            {
                "bootstrap": bootstrap,
                "path_repair": path_repair,
                "unreal": self._unreal_module(),
                "p4cli": self._p4_module(),
            }
        )
        import importlib

        lib_dir = str(SCRIPT.parent.parent / "lib")
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        record_module = importlib.import_module("redirector_record")
        original_writer = record_module.save_apply_manifest
        if self.fail_manifest:
            record_module.save_apply_manifest = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("manifest write failed")
            )
        try:
            yield
        finally:
            record_module.save_apply_manifest = original_writer
            for name, old in saved.items():
                if old is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old

    def run(self) -> tuple[int, str, str, Path]:
        old_env = {
            "SAFE_JSON": __import__("os").environ.get("SAFE_JSON"),
            "CL_DESC_SUFFIX": __import__("os").environ.get("CL_DESC_SUFFIX"),
            "HOME": __import__("os").environ.get("HOME"),
            "CLAUDE_BOOTSTRAP_DATA_ROOT": __import__("os").environ.get(
                "CLAUDE_BOOTSTRAP_DATA_ROOT"
            ),
        }
        old_argv = sys.argv
        stdout = io.StringIO()
        stderr = io.StringIO()
        import os

        os.environ["SAFE_JSON"] = str(self.safe_json)
        os.environ.pop("CL_DESC_SUFFIX", None)
        os.environ["HOME"] = str(self.project / "home")
        os.environ["CLAUDE_BOOTSTRAP_DATA_ROOT"] = str(self.project / "bootstrap")
        sys.argv = [str(SCRIPT), f"--mode={self.mode}"]
        code = 0
        with self._modules(), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                runpy.run_path(str(SCRIPT), run_name="__main__")
            except SystemExit as exc:
                code = int(exc.code or 0)
            except BaseException as exc:
                code = 1
                stderr.write(f"{type(exc).__name__}: {exc}\n")
            finally:
                sys.argv = old_argv
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        manifest = self.project / "Saved" / "PythonOutput" / "redirectors_apply_123.yaml"
        return code, stdout.getvalue(), stderr.getvalue(), manifest


def make_harness(tmp_path: Path, **kwargs: Any) -> ApplyHarness:
    return ApplyHarness(tmp_path, **kwargs)
