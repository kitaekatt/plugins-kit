"""Runtime mode of llm_kit: legacy (plugin .pth) versus package (katsoft-llm-kit wheel).

Spec: docs/planning/shared-lib-packaging/implementation-spec.md, section C.

Legacy mode is exercised in the source tree. Package mode is exercised in fresh
isolated interpreters against the UNPACKED WHEEL (a wheel's dist-info makes the
unzipped directory a real site directory). By default `bootstrap_lib` is ABSENT
(`_runtime.fleet()` is False): the one allowed touch is the fleet probe, which
sees ModuleNotFoundError, and any dotted import fails loudly. `fleet=True` puts
the marketplace's `bootstrap_lib` on the path instead. Variants of the wheel (a missing marker, a
duplicate distribution, an editable record) are built by editing a copy.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import zipfile
from pathlib import Path

import pytest

import dist_support as ds

BLOCK = ("bootstrap_lib",)
CONTRACTS = ds.CONTRACTS_SRC
FLEET_LIB = ds.REPO / "plugins" / "bootstrap"      # the directory that holds bootstrap_lib

#: Makes `bootstrap_lib` absent the way an environment without the fleet is:
#: the top-level import raises ModuleNotFoundError (so `fleet()` can answer
#: False), and any other bootstrap_lib name still fails loudly.
ABSENT_PRELUDE = (
    "class _Absent:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name == 'bootstrap_lib':\n"
    "            raise ModuleNotFoundError(\"No module named 'bootstrap_lib'\", name='bootstrap_lib')\n"
    "        return None\n"
    "sys.meta_path.insert(0, _Absent())\n"
)


def package(site, body, *, extra_paths=(), contracts=True, env=None, cwd=None, fleet=False):
    paths = [site, *extra_paths] + ([CONTRACTS] if contracts else [])
    if fleet:
        return ds.run(body, [*paths, FLEET_LIB], env=env, cwd=cwd)
    return ds.run(ABSENT_PRELUDE + body, paths, blocked=BLOCK, env=env, cwd=cwd)


@pytest.fixture()
def site(tmp_path):
    return ds.fresh_site(tmp_path)


def rewrite_record_hash(site: Path, member: str) -> None:
    """Make RECORD agree with an edited member, so only the edit under test differs."""
    import base64
    import hashlib
    record = ds.dist_info(site) / "RECORD"
    digest = base64.urlsafe_b64encode(hashlib.sha256((site / member).read_bytes()).digest()).rstrip(b"=").decode()
    rows = []
    for line in record.read_text(encoding="utf-8").splitlines():
        if line.startswith(member + ","):
            line = f"{member},sha256={digest},{(site / member).stat().st_size}"
        rows.append(line)
    record.write_text("\n".join(rows) + "\n", encoding="utf-8")


# --- the two modes ---------------------------------------------------------

def test_the_source_tree_is_legacy_mode():
    from llm_kit import _runtime
    assert _runtime._identify() == "legacy"
    assert _runtime.mode() == "legacy"


def test_unpacked_wheel_is_package_mode_with_the_build_identity(site):
    out = package(site, (
        "from llm_kit import _runtime, _deployment as d\n"
        "result = [_runtime.mode(), d.MODE, d.DIST, d.VERSION, d.TAG, d.COMMIT]"
    ))
    assert out["ok"] == ["package", "package", ds.DIST, ds.package_version(),
                         f"{ds.DIST}-v{ds.package_version()}", ds.COMMIT]


# --- C2: identity ----------------------------------------------------------

def test_marker_missing_raises(site):
    (site / "llm_kit" / "_deployment.py").unlink()
    out = package(site, "from llm_kit import _runtime\nresult = _runtime.mode()")
    assert out["error"] == "RuntimeIdentityError" and "_deployment.py is missing" in out["message"]


@pytest.mark.parametrize("text", ['MODE = "bogus"\n', "X = 1\n", 'MODE = None\n'])
def test_marker_invalid_raises(site, text):
    (site / "llm_kit" / "_deployment.py").write_text(text, encoding="utf-8")
    out = package(site, "from llm_kit import _runtime\nresult = _runtime.mode()")
    assert out["error"] == "RuntimeIdentityError" and "MODE" in out["message"]


def test_legacy_with_dist_metadata_requires_separation(site):
    (site / "llm_kit" / "_deployment.py").write_text('MODE = "legacy"\n', encoding="utf-8")
    out = package(site, "from llm_kit import _runtime\nresult = _runtime.mode()")
    assert out["error"] == "RuntimeIdentityError"
    assert "separate" in out["message"] and "environments" in out["message"]
    # remove nothing: the distribution is still there afterwards
    assert ds.dist_info(site).is_dir()


def test_legacy_without_dist_metadata_is_fine(tmp_path):
    lib = tmp_path / "lib"
    shutil.copytree(ds.SOURCE_DIR, lib / "llm_kit", ignore=shutil.ignore_patterns("__pycache__"))
    out = ds.run("from llm_kit import _runtime\nresult = _runtime.mode()", [lib], blocked=BLOCK)
    assert out["ok"] == "legacy"


def test_duplicate_dist_metadata_raises(site, tmp_path):
    second = tmp_path / "second"
    second.mkdir()
    shutil.copytree(ds.dist_info(site), second / ds.dist_info(site).name)
    out = package(site, "from llm_kit import _runtime\nresult = _runtime.mode()", extra_paths=[second])
    assert out["error"] == "RuntimeIdentityError" and "more than once" in out["message"]


def test_one_location_listed_twice_is_not_a_duplicate(site):
    out = package(site, "from llm_kit import _runtime\nresult = _runtime.mode()", extra_paths=[site])
    assert out["ok"] == "package"


def test_overlapping_provider_raises(site, tmp_path):
    other = tmp_path / "other"
    info = other / "llm_kit_plugin_copy-0.1.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: llm-kit\nVersion: 0.1\n", encoding="utf-8")
    (info / "RECORD").write_text("llm_kit/cli/__init__.py,,\n", encoding="utf-8")
    (other / "llm_kit" / "cli").mkdir(parents=True)          # importlib.metadata skips files not on disk
    (other / "llm_kit" / "cli" / "__init__.py").write_text("", encoding="utf-8")
    out = package(site, "from llm_kit import _runtime\nresult = _runtime.mode()", extra_paths=[other])
    assert out["error"] == "RuntimeIdentityError" and "also provides llm_kit" in out["message"]


def test_editable_raises(site):
    (ds.dist_info(site) / "direct_url.json").write_text(
        json.dumps({"url": "file:///x", "dir_info": {"editable": True}}), encoding="utf-8")
    out = package(site, "from llm_kit import _runtime\nresult = _runtime.mode()")
    assert out["error"] == "UnsupportedLayout" and "editable" in out["message"]


def test_a_non_editable_direct_url_is_accepted(site):
    (ds.dist_info(site) / "direct_url.json").write_text(
        json.dumps({"url": "file:///x", "dir_info": {}}), encoding="utf-8")
    out = package(site, "from llm_kit import _runtime\nresult = _runtime.mode()")
    assert out["ok"] == "package"


def test_zip_layout_raises(site, tmp_path):
    archive = tmp_path / "llm_kit.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name in ("__init__.py", "_runtime.py", "_deployment.py"):
            zf.write(site / "llm_kit" / name, f"llm_kit/{name}")
    out = ds.run("from llm_kit import _runtime\nresult = _runtime.mode()", [archive], blocked=BLOCK)
    assert out["error"] == "UnsupportedLayout" and "loaded from source files" in out["message"]


def test_frozen_interpreter_raises(site):
    out = package(site, "sys.frozen = True\nfrom llm_kit import _runtime\nresult = _runtime.mode()")
    assert out["error"] == "UnsupportedLayout" and "frozen" in out["message"]


def test_marker_edited_after_install_raises(site):
    marker = site / "llm_kit" / "_deployment.py"
    marker.write_text(marker.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    out = package(site, "from llm_kit import _runtime\nresult = _runtime.mode()")
    assert out["error"] == "RuntimeIdentityError" and "RECORD" in out["message"]


def test_marker_missing_from_record_raises(site):
    record = ds.dist_info(site) / "RECORD"
    record.write_text("\n".join(
        line for line in record.read_text(encoding="utf-8").splitlines()
        if not line.startswith("llm_kit/_deployment.py,")) + "\n", encoding="utf-8")
    out = package(site, "from llm_kit import _runtime\nresult = _runtime.mode()")
    assert out["error"] == "RuntimeIdentityError" and "does not list" in out["message"]


def test_marker_version_must_match_the_installed_distribution(site):
    marker = site / "llm_kit" / "_deployment.py"
    marker.write_text(marker.read_text(encoding="utf-8").replace(
        f"VERSION = '{ds.package_version()}'", "VERSION = '9.9.9'"), encoding="utf-8")
    rewrite_record_hash(site, "llm_kit/_deployment.py")
    out = package(site, "from llm_kit import _runtime\nresult = _runtime.mode()")
    assert out["error"] == "RuntimeIdentityError" and "9.9.9" in out["message"]


def test_loaded_copy_must_be_the_installed_copy(site, tmp_path):
    """A source checkout shadowing the installed package fails the identity check."""
    shadow = tmp_path / "shadow"
    shutil.copytree(site / "llm_kit", shadow / "llm_kit")
    out = ds.run("from llm_kit import _runtime\nresult = _runtime.mode()", [shadow, site, CONTRACTS], blocked=BLOCK)
    assert out["error"] == "RuntimeIdentityError" and "not the copy" in out["message"]


# --- C3: the three routed contracts ----------------------------------------

def test_contracts_too_old_is_worded_apart_from_absent(site, tmp_path):
    """katsoft_contracts imports but lacks the routed module: upgrade, not install."""
    stale = tmp_path / "stale"
    (stale / "katsoft_contracts").mkdir(parents=True)
    (stale / "katsoft_contracts" / "__init__.py").write_text("", encoding="utf-8")
    body = (
        "from llm_kit import _runtime\n"
        "try:\n"
        "    _runtime.load('model_declaration')\n"
        "except _runtime.ContractsMissing as exc:\n"
        "    result = str(exc)"
    )
    too_old = package(site, body, extra_paths=[stale], contracts=False)["ok"]
    absent = package(site, body, contracts=False)["ok"]
    assert "is installed but has no model_declaration" in too_old and "upgrade katsoft-contracts" in too_old
    assert ">=1.0,<2" in too_old and "install katsoft-contracts" not in too_old
    assert "install katsoft-contracts" in absent and "upgrade" not in absent
    assert too_old != absent


def test_contracts_missing_named(site):
    out = package(site, (
        "from llm_kit import _runtime\n"
        "try:\n"
        "    _runtime.load('model_declaration')\n"
        "except _runtime.ContractsMissing as exc:\n"
        "    result = [str(exc), isinstance(exc, ImportError), type(exc.__cause__).__name__]"
    ), contracts=False)
    message, is_import_error, cause = out["ok"]
    assert "install katsoft-contracts" in message and is_import_error and cause == "ModuleNotFoundError"


def test_contracts_missing_reaches_each_product_in_its_own_error_type(site):
    out = package(site, (
        "from llm_kit.registry.declaration_syntax import _model_declaration, DeclarationSupportError\n"
        "from llm_kit.dispatch.declaration import _execution_event\n"
        "from llm_kit.jobs.events import _execution_event as jobs_events, ExecutionEventSupportError\n"
        "from llm_kit.pipeline.execution.interrupts import _interrupt_contract, InterruptSupportError\n"
        "from llm_kit.jobs.model import _parse_declaration\n"
        "from llm_kit import _runtime\n"
        "seen = []\n"
        "for call, error in ((_model_declaration, DeclarationSupportError),\n"
        "                    (_execution_event, DeclarationSupportError),\n"
        "                    (jobs_events, ExecutionEventSupportError),\n"
        "                    (_interrupt_contract, InterruptSupportError),\n"
        "                    (lambda: _parse_declaration(['opus']), _runtime.ContractsMissing)):\n"
        "    try:\n"
        "        call()\n"
        "    except error as exc:\n"
        "        seen.append('install katsoft-contracts' in str(exc))\n"
        "result = seen"
    ), contracts=False)
    assert out["ok"] == [True] * 5, out


def test_internal_importerror_preserved(site, tmp_path):
    """A broken contracts install is not an absent one: its own ImportError propagates."""
    broken = tmp_path / "broken"
    (broken / "katsoft_contracts").mkdir(parents=True)
    (broken / "katsoft_contracts" / "__init__.py").write_text("", encoding="utf-8")
    (broken / "katsoft_contracts" / "model_declaration.py").write_text(
        "import a_dependency_that_does_not_exist\n", encoding="utf-8")
    (broken / "katsoft_contracts" / "execution_event.py").write_text(
        "from . import no_such_sibling\n", encoding="utf-8")
    out = package(site, (
        "from llm_kit import _runtime\n"
        "seen = {}\n"
        "for name in ('model_declaration', 'execution_event'):\n"
        "    try:\n"
        "        _runtime.load(name)\n"
        "    except BaseException as exc:\n"
        "        seen[name] = [type(exc).__name__, getattr(exc, 'name', None)]\n"
        "result = seen"
    ), extra_paths=[broken], contracts=False)
    assert out["ok"]["model_declaration"] == ["ModuleNotFoundError", "a_dependency_that_does_not_exist"]
    assert out["ok"]["execution_event"][0] == "ImportError"      # not ContractsMissing


def test_only_three_contracts_routed(site):
    from llm_kit import _runtime
    assert _runtime.ROUTED_CONTRACTS == ("execution_event", "interrupt_contract", "model_declaration")
    out = package(site, (
        "from llm_kit import _runtime\n"
        "seen = {}\n"
        "for name in ('codex', 'skill_material', 'config_resolve', 'claudx', 'katsoft_contracts', ''):\n"
        "    try:\n"
        "        _runtime.load(name)\n"
        "    except ValueError:\n"
        "        seen[name] = 'refused'\n"
        "result = [seen, [_runtime.load(n).__name__ for n in _runtime.ROUTED_CONTRACTS]]"
    ))
    refused, loaded = out["ok"]
    assert set(refused.values()) == {"refused"} and len(refused) == 6
    assert loaded == [f"katsoft_contracts.{n}" for n in _runtime.ROUTED_CONTRACTS]


def test_no_module_but_the_runtime_names_the_contracts_package():
    for path in sorted(ds.SOURCE_DIR.rglob("*.py")):
        if path.name == "_runtime.py":
            continue
        assert "katsoft_contracts" not in path.read_text(encoding="utf-8"), path


def test_load_is_a_package_mode_seam(tmp_path):
    from llm_kit import _runtime
    with pytest.raises(_runtime.RuntimeIdentityError, match="package-mode seam"):
        _runtime.load("model_declaration")


def test_package_mode_never_imports_bootstrap_lib(site, tmp_path):
    """Every routed site and a real CLI verb run with bootstrap_lib import-blocked."""
    home = tmp_path / "home"
    home.mkdir()
    out = package(site, (
        "import io, contextlib\n"
        "from llm_kit.registry.declaration_syntax import _model_declaration\n"
        "from llm_kit.dispatch import declaration\n"
        "from llm_kit.jobs import events as jobs_events, model as jobs_model\n"
        "from llm_kit.pipeline.execution import events as pe_events, interrupts\n"
        "from llm_kit.registry import models\n"
        "from llm_kit import cli\n"
        "names = [_model_declaration().__name__, declaration._execution_event().__name__,\n"
        "         jobs_events._execution_event().__name__, pe_events._execution_event_v2().__name__,\n"
        "         interrupts._interrupt_contract().__name__]\n"
        "parsed = list(jobs_model._parse_declaration(['opus', 'sonnet']))\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    code = cli.main(['completion', 'request-schema'])\n"
        "result = [names, parsed, code, buf.getvalue()[:1] == '{',\n"
        "          models.load_model_config() == models.DEFAULT_MODEL_CONFIG]"
    ), env={"HOME": str(home)})
    assert out["modules"] == [], out
    names, parsed, code, is_json, default_config = out["ok"]
    assert names == [f"katsoft_contracts.{n}" for n in (
        "model_declaration", "execution_event", "execution_event", "execution_event", "interrupt_contract")]
    assert parsed == ["opus", "sonnet"] and code == 0 and is_json and default_config


def test_package_mode_uses_the_contracts_the_wheel_depends_on(site):
    """The routed module is the katsoft_contracts file, not a bootstrap_lib stand-in."""
    out = package(site, (
        "from llm_kit.registry.declaration_syntax import _model_declaration\n"
        "result = _model_declaration().__file__"
    ))
    assert Path(out["ok"]) == CONTRACTS / "katsoft_contracts" / "model_declaration.py"


# --- C4: features that need bootstrap_lib ----------------------------------

UNSUPPORTED_CALLS = {
    "codex backend": (
        "from llm_kit.adapters.codex_backend import _codex_argv_builder\n"
        "_codex_argv_builder(need_ignore_user_config=False)"),
    "codex adapter": (
        "from llm_kit.adapters.harness_adapters import CodexAdapter\n"
        "CodexAdapter().is_available()"),
    "claudx (adapters)": (
        "from llm_kit.adapters.backends import _claudx_plugin_args\n_claudx_plugin_args()"),
    "claudx (jobs)": (
        "from llm_kit.jobs.run import _claudx_contract_command\n_claudx_contract_command(['claude'], None)"),
    "claudx (pipeline)": (
        "from llm_kit.pipeline.execution.drivers.claude_bg import _refuse_supervised_under_claudx\n"
        "_refuse_supervised_under_claudx('x', None)"),
    "skill context": (
        "from llm_kit.completion.skill_context import _skill_material\n_skill_material()"),
    "code-review lane": "import llm_kit.dispatch.review_lane",
}


@pytest.mark.parametrize("label", sorted(UNSUPPORTED_CALLS))
def test_package_mode_without_bootstrap_lib_refuses(site, label):
    out = package(site, UNSUPPORTED_CALLS[label], env={"CLAUDE_PLUGIN_TEST": "1"})
    assert out["error"] == "UnsupportedInPackageMode", out
    assert "bootstrap_lib" in out["message"] and ds.DIST in out["message"]
    assert out["modules"] == []              # refused BEFORE any bootstrap_lib import


def test_fleet_is_false_when_the_bootstrap_lib_import_is_blocked(site):
    out = package(site, (
        "from llm_kit import _runtime\n"
        "result = [_runtime.mode(), _runtime.fleet(), _runtime.fleet()]"
    ))
    assert out["ok"] == ["package", False, False] and out["modules"] == []


def test_fleet_is_true_when_bootstrap_lib_imports(site):
    out = package(site, (
        "from llm_kit import _runtime\n"
        "result = [_runtime.mode(), _runtime.fleet()]"
    ), fleet=True)
    assert out["ok"] == ["package", True]


def test_fleet_does_not_swallow_a_broken_bootstrap_lib(site, tmp_path):
    """Only an absent bootstrap_lib reads as no fleet; a broken one propagates."""
    broken = tmp_path / "broken"
    (broken / "bootstrap_lib").mkdir(parents=True)
    (broken / "bootstrap_lib" / "__init__.py").write_text("import not_a_real_module_xyz\n", encoding="utf-8")
    out = ds.run(
        "from llm_kit import _runtime\nresult = _runtime.fleet()",
        [site, CONTRACTS, broken])
    assert out["error"] == "ModuleNotFoundError" and "not_a_real_module_xyz" in out["message"]


def test_require_fleet_is_silent_in_legacy_mode():
    from llm_kit import _runtime
    assert _runtime.require_fleet("anything") is None


def test_package_mode_fleet_allows_review_lane(site):
    """With bootstrap_lib importable, the gate that used to refuse in package mode passes."""
    out = package(site, (
        "from llm_kit import _runtime\n"
        "import llm_kit.dispatch.review_lane as lane\n"
        "_runtime.require_fleet('anything')\n"
        "result = [_runtime.mode(), lane.__name__]"
    ), fleet=True, env={"CLAUDE_PLUGIN_TEST": "1"})
    assert out["ok"] == ["package", "llm_kit.dispatch.review_lane"], out


def test_package_mode_fleet_uses_codex_detection_for_reachability(site, tmp_path):
    """The fleet reaches bootstrap_lib.codex.detect_codex; the off-fleet verdict is the PATH check."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text('#!/bin/sh\necho "codex-cli 9.9.9"\n', encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    out = package(site, (
        "import sys\n"
        "from llm_kit.dispatch import reachability\n"
        "r = reachability._check_codex(timeout=10.0)\n"
        "result = [r.status, 'bootstrap_lib.codex' in sys.modules]"
    ), fleet=True, env={"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"})
    assert out["ok"] == ["reachable", True], out


def test_reachability_codex_path_check_in_package_mode(site, tmp_path):
    """A real verdict from the PATH + --version check, with bootstrap_lib blocked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text('#!/bin/sh\necho "codex-cli 9.9.9"\n', encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    out = package(site, (
        "from llm_kit.dispatch import reachability\n"
        "r = reachability._check_codex(timeout=10.0)\n"
        "result = [r.status, r.checked, r.detail]"
    ), env={"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"})
    assert out["modules"] == []
    assert out["ok"] == ["reachable", "cli-version", "codex-cli 9.9.9"]
    absent = package(site, (
        "from llm_kit.dispatch import reachability\n"
        "r = reachability._check_codex(timeout=10.0)\n"
        "result = [r.status, r.detail]"
    ), env={"PATH": str(tmp_path / "empty")})
    assert absent["ok"] == ["unreachable", "`codex` not found on PATH"]


# --- the model-list policy point (ruled 2026-10-22: shipped defaults) --

def test_package_mode_model_list_uses_shipped_defaults(site, tmp_path):
    """Shipped defaults, silent; MODEL_ENDPOINTS_REGISTRY overrides the registry.

    ONE seam: llm_kit.registry.models.package_mode_model_config.
    """
    registry = tmp_path / "registry.yaml"
    registry.write_text("default: only\nmodels:\n  only: {base_url: http://h.invalid/v1, model: m}\n",
                        encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    body = (
        "import io, contextlib\n"
        "from llm_kit.registry import models, model_endpoints\n"
        "err = io.StringIO()\n"
        "with contextlib.redirect_stderr(err):\n"
        "    config = models.load_model_config()\n"
        "ids = sorted(model_endpoints.load_endpoint_registry().entries)\n"
        "result = [config == models.DEFAULT_MODEL_CONFIG, err.getvalue(), ids]"
    )
    unset = package(site, body, env={"HOME": str(home)})
    assert unset["ok"][:2] == [True, ""]
    assert unset["ok"][2] == []                         # no registry file: the shipped baseline alone
    overridden = package(site, body, env={"HOME": str(home), "MODEL_ENDPOINTS_REGISTRY": str(registry)})
    assert overridden["ok"] == [True, "", ["only"]]


def test_package_mode_fleet_uses_layered_registry(site, tmp_path):
    """With bootstrap_lib importable, package mode reads the fleet config layer."""
    home = tmp_path / "home"
    (home / ".claude" / "config").mkdir(parents=True)
    (home / ".claude" / "config" / "llm-scripting-kit.yaml").write_text(
        "default_endpoint: fleet-chosen\nendpoints:\n  fleet-chosen:\n    model: m\n",
        encoding="utf-8")
    body = (
        "from llm_kit.registry import models\n"
        "config = models.load_model_config()\n"
        "result = [config['default_endpoint'], 'fleet-chosen' in config['endpoints'],\n"
        "          config == models.DEFAULT_MODEL_CONFIG]"
    )
    fleet = package(site, body, fleet=True, env={"HOME": str(home)})
    assert fleet["ok"] == ["fleet-chosen", True, False], fleet
    off = package(site, body, env={"HOME": str(home)})
    assert off["ok"] == ["openrouter", False, True], off     # shipped defaults, layers ignored
    assert off["modules"] == []


# --- C5: the re-exec ---------------------------------------------------------

def _stub_plugin_venv(home: Path) -> Path:
    python = home / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-kit" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    return python


REEXEC_BODY = (
    "import os\n"
    "calls = []\n"
    "def fake_execv(path, argv):\n"
    "    calls.append([path, argv])\n"
    "    raise RuntimeError('execv')\n"
    "os.execv = fake_execv\n"
    "sys.argv = ['llm-kit', '--help']\n"
    "from llm_kit import cli, _runtime\n"
    "try:\n"
    "    code = cli.main()\n"
    "except RuntimeError as exc:\n"
    "    code = str(exc)\n"
    "except SystemExit as exc:\n"
    "    code = exc.code\n"
    "result = [_runtime.mode(), code, calls]"
)


def test_cli_main_argv_none_package_no_exec(site, tmp_path):
    home = tmp_path / "home"
    _stub_plugin_venv(home)
    out = package(site, REEXEC_BODY, env={"HOME": str(home)})
    mode, code, calls = out["ok"]
    assert (mode, code, calls) == ("package", 0, [])     # --help parsed, no exec


def test_cli_main_argv_none_legacy_execs(tmp_path):
    home = tmp_path / "home"
    python = _stub_plugin_venv(home)
    out = ds.run(REEXEC_BODY, [ds.LIB], blocked=BLOCK, env={"HOME": str(home)})
    mode, code, calls = out["ok"]
    assert mode == "legacy" and code == "execv"
    assert [Path(call[0]).resolve() for call in calls] == [python.resolve()]
    assert calls[0][1][1:] == ["-m", "llm_kit.cli", "--help"]


# --- the goldens, forced through package mode ---------------------------------

def test_cli_goldens_pass_in_simulated_package_mode(tmp_path):
    """The U5 goldens must read the same in package mode.

    The marker is forced in-process (`_MODE`) and katsoft_contracts is put on
    sys.path; bootstrap_lib is made absent (the fleet probe sees it missing and
    any deeper import fails loudly), since the golden harness itself fakes backends. The real layout is covered by the
    unpacked-wheel tests above.
    """
    import subprocess
    import sys
    plugin = tmp_path / "force_package_mode.py"
    plugin.write_text(
        "import sys\n"
        f"sys.path[:0] = [{str(ds.LIB)!r}, {str(CONTRACTS)!r}]\n"
        "from llm_kit import _runtime\n"
        "_runtime._MODE = 'package'\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'bootstrap_lib':\n"
        "            raise ModuleNotFoundError(\"No module named 'bootstrap_lib'\", name=name)\n"
        "        if name.split('.')[0] == 'bootstrap_lib':\n"
        "            raise AssertionError('bootstrap_lib imported in package mode: ' + name)\n"
        "sys.meta_path.insert(0, _Block())\n", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__).with_name("test_cli_goldens.py")),
         "-q", "-p", "force_package_mode", "-p", "no:cacheprovider", "-x"],
        capture_output=True, text=True, env=env, cwd=ds.REPO)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert " passed" in proc.stdout
