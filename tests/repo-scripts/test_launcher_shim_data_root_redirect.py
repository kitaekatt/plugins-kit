"""claudx containment in the launcher shims (CLAUDE_BOOTSTRAP_DATA_ROOT, CLAUDE_PLUGIN_TEST).

llm-kit and task-kit launch their CLI under the plugin venv at the
production path ``~/.claude/plugins/data/plugins-kit/<plugin>/.venv``. A
claude-plugin-test session sets ``CLAUDE_BOOTSTRAP_DATA_ROOT`` to a separate
tree that holds its own venvs, so a shim that named the production venv ran the
published release's interpreter inside a session meant to test the working
copy. The rules under test (root CLAUDE.md, "Scoped exception: Python CLI
launcher shims"):

* With the variable set, the redirected venv is the ONLY candidate. A missing
  one exits 2 with a message, never a fallback to the production venv. With the
  variable unset the existing chain is unchanged.
* All five shims (llm-kit, task-kit, hue-kit, secrets-kit,
  relaunch-kit's clauder) refuse to
  run as the CACHED copy, a path under ``plugins/cache/<marketplace>/``, when
  ``CLAUDE_PLUGIN_TEST`` is set. The marketplace is
  ``CLAUDE_PLUGIN_TEST_MARKETPLACE``; with it unset every cache path is refused,
  and a cache path under another marketplace is kept. Outside claudx nothing is
  refused.
* task-kit's shim is also the launcher copy bootstrap syncs into the plugin's
  production DATA directory (``plugins/data/<marketplace>/task-kit/bin``) and puts
  on PATH. That copy is a thin locator that runs the newest INSTALLED version, so
  under claudx it is refused like a cached copy. relaunch-kit's clauder is the
  same kind of PATH copy, but the whole launcher: bootstrap syncs ``bin`` and ``lib``
  to the data directory, and the shim finds the launcher as ``../lib/relaunch.py``.

The tests run each real shim, copied into a throwaway plugin tree whose
entrypoint prints ``sys.prefix``, against real venvs, so the printed prefix
names which interpreter the shim chose. The POSIX shim needs a bash; its
``.cmd`` twin needs Windows.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_IS_WINDOWS = os.name == "nt"


def _find_bash() -> str | None:
    """Git Bash on Windows (never the WSL launcher), else bash on PATH."""
    candidates = []
    if _IS_WINDOWS:
        candidates += [
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ]
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for candidate in candidates:
        if Path(candidate).exists() and "WindowsApps" not in candidate and "System32" not in candidate:
            return candidate
    return None


_BASH = _find_bash()


def _find_bash32() -> str | None:
    """``/bin/bash`` when it is GNU bash 3.x (a stock macOS), else None.

    The default ``bash`` on a developer Mac is usually a newer Homebrew bash, so
    the shim would never run under the bash that ``#!/usr/bin/env bash`` finds on
    an unmodified Mac. This lane runs it there.
    """
    if _IS_WINDOWS or not Path("/bin/bash").exists():
        return None
    probe = subprocess.run(
        ["/bin/bash", "-c", 'printf %s "${BASH_VERSINFO[0]}"'],
        capture_output=True, text=True, timeout=30,
    )
    return "/bin/bash" if probe.stdout.strip() == "3" else None


_BASH32 = _find_bash32()

# (plugin, shim file stem, entrypoint relative to the plugin root)
_VENV_SHIMS = [
    ("llm-kit", "llm-kit", "bin/llm_kit_entrypoint.py"),
    ("task-kit", "task-kit", "bin/task_kit_entrypoint.py"),
]
# hue-kit, secrets-kit and relaunch-kit's clauder choose no plugin venv (hue-kit's CLI
# re-execs itself; the other two are stdlib-only), so only the cached-copy refusal and
# the BOOTSTRAP_PYTHON validation apply to them.
_SHIMS = _VENV_SHIMS + [
    ("hue-kit", "hue-kit", "scripts/hue_kit_cli.py"),
    ("secrets-kit", "secrets-kit", "scripts/secrets_kit_cli.py"),
    ("relaunch-kit", "clauder", "lib/relaunch.py"),
]
# Variables a surrounding claudx session would leak into the shim under test.
_SCRUBBED_ENV = (
    "CLAUDE_PLUGIN_TEST",
    "CLAUDE_PLUGIN_TEST_MARKETPLACE",
    "CLAUDE_PLUGIN_TEST_DIRS",
    "CLAUDE_BOOTSTRAP_DATA_ROOT",
    "BOOTSTRAP_PYTHON",
    "BOOTSTRAP_PROJECT_PYTHON",
)
_KINDS = [
    pytest.param(
        "posix",
        marks=pytest.mark.skipif(_BASH is None, reason="bash not available"),
    ),
    # Bash 3.2 lane: every POSIX shim, under a stock macOS /bin/bash.
    pytest.param(
        "posix32",
        marks=pytest.mark.skipif(_BASH32 is None, reason="/bin/bash is not bash 3.x here"),
    ),
    pytest.param(
        "cmd",
        marks=pytest.mark.skipif(not _IS_WINDOWS, reason=".cmd shims run only on Windows"),
    ),
]
_POSIX_KINDS = ("posix", "posix32")


@pytest.fixture(scope="session")
def venv_template(tmp_path_factory) -> Path:
    """One real venv, copied per test (a venv is located by its pyvenv.cfg, not
    its path, so a copy runs as its own environment)."""
    root = tmp_path_factory.mktemp("venv-template") / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(root)],
        check=True, capture_output=True, timeout=120,
    )
    return root


def _make_venv(template: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, dest, symlinks=True)
    return dest


class _Launch:
    """A shim copied into a throwaway plugin tree, plus the two data trees.

    ``cache_marketplace`` places the plugin tree at
    ``<home>/.claude/plugins/cache/<marketplace>/<plugin>/1.0.0`` instead of an
    ordinary directory, so the shim runs as the cached copy.
    """

    def __init__(self, tmp_path: Path, template: Path, plugin: str, stem: str,
                 entrypoint: str, kind: str, cache_marketplace: str | None = None,
                 data_bundle_marketplace: str | None = None) -> None:
        self.template = template
        self.plugin = plugin
        self.kind = kind
        self.home = tmp_path / "home"
        self.home.mkdir()
        if data_bundle_marketplace is not None:
            # The launcher copy bootstrap syncs to <plugins>/data/<marketplace>/<plugin>/bin.
            plugin_root = (self.home / ".claude" / "plugins" / "data" / data_bundle_marketplace
                           / plugin)
        elif cache_marketplace is None:
            plugin_root = tmp_path / "plugin"
        else:
            plugin_root = (self.home / ".claude" / "plugins" / "cache" / cache_marketplace
                           / plugin / "1.0.0")
        suffix = ".cmd" if kind == "cmd" else ""
        shim_src = _REPO_ROOT / "plugins" / plugin / "bin" / (stem + suffix)
        self.shim = plugin_root / "bin" / shim_src.name
        self.shim.parent.mkdir(parents=True)
        shutil.copy2(shim_src, self.shim)
        entry = plugin_root / entrypoint
        # exist_ok: llm-kit's entrypoint sits in bin/, beside the shim just copied.
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("import sys\nprint(sys.prefix)\n", encoding="utf-8")
        self.redirect_root = tmp_path / "redirected-data"

    def production_venv(self) -> Path:
        return _make_venv(
            self.template,
            self.home / ".claude" / "plugins" / "data" / "plugins-kit" / self.plugin / ".venv",
        )

    def redirected_venv(self) -> Path:
        return _make_venv(
            self.template, self.redirect_root / "plugins-kit" / self.plugin / ".venv"
        )

    def run(self, *, redirect: bool, claudx: bool = False,
            marketplace: str | None = None, path: str | None = None,
            extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        for name in _SCRUBBED_ENV:
            env.pop(name, None)
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        if path is not None:
            env["PATH"] = path
        env.update(extra_env or {})
        if redirect:
            env["CLAUDE_BOOTSTRAP_DATA_ROOT"] = str(self.redirect_root)
        if claudx:
            env["CLAUDE_PLUGIN_TEST"] = "1"
        if marketplace is not None:
            env["CLAUDE_PLUGIN_TEST_MARKETPLACE"] = marketplace
        if self.kind == "posix":
            command = [_BASH, str(self.shim)]
        elif self.kind == "posix32":
            command = [_BASH32, str(self.shim)]
        else:
            command = ["cmd.exe", "/c", str(self.shim)]
        return subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=60,
        )

    def chosen_prefix(self, *, redirect: bool, claudx: bool = False) -> Path:
        result = self.run(redirect=redirect, claudx=claudx)
        assert result.returncode == 0, result.stderr
        return Path(result.stdout.strip().splitlines()[-1]).resolve()


@pytest.fixture(params=_VENV_SHIMS, ids=[s[0] for s in _VENV_SHIMS])
def shim_spec(request):
    return request.param


@pytest.fixture(params=_SHIMS, ids=[s[0] for s in _SHIMS])
def any_shim_spec(request):
    return request.param


@pytest.fixture(params=_KINDS)
def kind(request):
    return request.param


@pytest.fixture
def launch(tmp_path, venv_template, shim_spec, kind) -> _Launch:
    plugin, stem, entrypoint = shim_spec
    return _Launch(tmp_path, venv_template, plugin, stem, entrypoint, kind)


def _cached_launch(tmp_path, template, spec, kind, marketplace="plugins-kit") -> _Launch:
    plugin, stem, entrypoint = spec
    return _Launch(tmp_path, template, plugin, stem, entrypoint, kind,
                   cache_marketplace=marketplace)


def test_redirected_venv_outranks_production_when_root_is_set(launch):
    """Set + redirected venv present -> redirected venv.

    Shown to fail: restore the shim from git history before the redirect was
    added; it names only the production venv and this test sees the production
    prefix.
    """
    production = launch.production_venv()
    redirected = launch.redirected_venv()
    assert launch.chosen_prefix(redirect=True) == redirected.resolve()
    assert launch.chosen_prefix(redirect=True) != production.resolve()


def test_production_venv_is_chosen_when_root_is_unset(launch):
    """Normal behaviour is identical: with the variable unset, even a populated
    redirect tree on disk is ignored.

    Shown to fail: break the shim's production DATA_DIR (any other path) and
    the chosen prefix is no longer the production venv.
    """
    production = launch.production_venv()
    launch.redirected_venv()
    assert launch.chosen_prefix(redirect=False) == production.resolve()


def test_missing_redirected_venv_is_refused_not_replaced_by_production(launch):
    """Set but empty redirect tree: exit 2 with a message, even though a
    working production venv exists. The silent fallback is the defect.

    Shown to fail: restore the previous branch (use the redirected venv only
    when it exists, else keep the production one); the shim then exits 0 with
    the production prefix and this test sees returncode 0.
    """
    production = launch.production_venv()
    launch.redirect_root.mkdir()
    result = launch.run(redirect=True)
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "CLAUDE_BOOTSTRAP_DATA_ROOT" in result.stderr
    assert "refusing to fall back to the production venv" in result.stderr
    assert str(production.resolve()) not in result.stdout


def test_missing_redirected_venv_is_refused_under_claudx_too(launch):
    """The same refusal when the session is a claudx session."""
    launch.production_venv()
    launch.redirect_root.mkdir()
    result = launch.run(redirect=True, claudx=True, marketplace="plugins-kit")
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "refusing to fall back to the production venv" in result.stderr


@pytest.mark.parametrize("marketplace_env", ["plugins-kit", "Plugins-Kit", None],
                         ids=["owned", "owned-other-case", "marketplace-unset"])
def test_cached_copy_is_refused_under_claudx(tmp_path, venv_template, any_shim_spec, kind,
                                             marketplace_env):
    """A shim whose own path is under plugins/cache/<owned marketplace>/ exits 2.

    With the marketplace unset every cache path is refused, as in
    bootstrap_lib.env_var_check.cache_root_refusal.

    Shown to fail: delete the CLAUDE_PLUGIN_TEST block from the shim (between
    PLUGIN_ROOT and the interpreter chain); the shim then runs instead.
    """
    launch = _cached_launch(tmp_path, venv_template, any_shim_spec, kind)
    result = launch.run(redirect=False, claudx=True, marketplace=marketplace_env)
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "refusing to run the cached copy" in result.stderr
    assert "CLAUDE_PLUGIN_TEST" in result.stderr


def test_cached_copy_is_kept_outside_claudx(tmp_path, venv_template, any_shim_spec, kind):
    """Without CLAUDE_PLUGIN_TEST the cache IS the plugin in use: no refusal."""
    launch = _cached_launch(tmp_path, venv_template, any_shim_spec, kind)
    result = launch.run(redirect=False, claudx=False, marketplace="plugins-kit")
    assert "refusing to run the cached copy" not in result.stderr


def test_cached_copy_of_another_marketplace_is_kept(tmp_path, venv_template, any_shim_spec, kind):
    """A cache path under a marketplace claudx does not own is not refused.

    Shown to fail: make the shim refuse any plugins/cache path regardless of
    CLAUDE_PLUGIN_TEST_MARKETPLACE.
    """
    launch = _cached_launch(tmp_path, venv_template, any_shim_spec, kind,
                            marketplace="some-other-marketplace")
    result = launch.run(redirect=False, claudx=True, marketplace="plugins-kit")
    assert "refusing to run the cached copy" not in result.stderr


def test_working_copy_is_not_refused_under_claudx(launch):
    """A shim outside the plugin cache runs under claudx on its redirected venv."""
    launch.production_venv()
    redirected = launch.redirected_venv()
    assert launch.chosen_prefix(redirect=True, claudx=True) == redirected.resolve()


_BUNDLE_SHIMS = [spec for spec in _VENV_SHIMS if spec[0] == "task-kit"]


@pytest.fixture(params=_BUNDLE_SHIMS, ids=[s[0] for s in _BUNDLE_SHIMS])
def bundle_spec(request):
    return request.param


# Every shim whose data-directory copy (the one on PATH) must be refused under claudx.
_DATA_COPY_SHIMS = _BUNDLE_SHIMS + [spec for spec in _SHIMS if spec[0] == "relaunch-kit"]


@pytest.fixture(params=_DATA_COPY_SHIMS, ids=[s[0] for s in _DATA_COPY_SHIMS])
def data_copy_spec(request):
    return request.param


@pytest.mark.parametrize("marketplace_env", ["plugins-kit", None], ids=["owned", "marketplace-unset"])
def test_data_dir_launcher_copy_is_refused_under_claudx(tmp_path, venv_template, data_copy_spec, kind,
                                                        marketplace_env):
    """The launcher copy in the PRODUCTION data directory (the one on PATH) exits 2 under claudx.

    It is a thin locator that runs the installed release from the plugin cache,
    which a claudx session must never do in place of the working copy.

    clauder's PATH copy holds the whole launcher rather than a locator, but a claudx session
    still tests the working copy, so the installed release must not run there either.

    Shown to fail: drop the ``*"/plugins/data/"*`` alternative from the POSIX shim's case
    (and the ``_DATA_NEEDLE`` line from the .cmd twin); the shim then runs.
    """
    plugin, stem, entrypoint = data_copy_spec
    launch = _Launch(tmp_path, venv_template, plugin, stem, entrypoint, kind,
                     data_bundle_marketplace="plugins-kit")
    result = launch.run(redirect=False, claudx=True, marketplace=marketplace_env)
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "refusing to run the cached copy" in result.stderr


def test_data_dir_launcher_copy_runs_outside_claudx(tmp_path, venv_template, bundle_spec, kind):
    """Typed in an ordinary terminal, the PATH copy is the user's launcher: no refusal."""
    plugin, stem, entrypoint = bundle_spec
    launch = _Launch(tmp_path, venv_template, plugin, stem, entrypoint, kind,
                     data_bundle_marketplace="plugins-kit")
    production = launch.production_venv()
    assert launch.chosen_prefix(redirect=False) == production.resolve()


def test_data_dir_launcher_copy_never_reads_the_production_venv_in_a_redirected_session(
        tmp_path, venv_template, bundle_spec, kind):
    """Redirected data root, no redirected venv: exit 2 even from the data-dir copy."""
    plugin, stem, entrypoint = bundle_spec
    launch = _Launch(tmp_path, venv_template, plugin, stem, entrypoint, kind,
                     data_bundle_marketplace="plugins-kit")
    launch.production_venv()
    launch.redirect_root.mkdir()
    result = launch.run(redirect=True)
    assert result.returncode == 2
    assert "refusing to fall back to the production venv" in result.stderr


# --- BOOTSTRAP_PYTHON validation (every POSIX shim) ------------------------------
#
# Root CLAUDE.md: the variable counts only when its OWN realpath resolves inside
# the standalone directory; a stranger's BOOTSTRAP_PYTHON must never substitute.
# The cases run against each shim that accepts BOOTSTRAP_PYTHON (task-kit, llm-kit,
# hue-kit, secrets-kit, relaunch-kit).
# Every case runs with no plugin venv, a fakebin ``python3`` that prints PATHPY
# first on PATH, and a tools directory holding only the utilities the shim calls.

_STANDALONE_REL = Path(".local") / "share" / "python-standalone"
_TOOLS = ("dirname", "uname", "tr", "readlink")
_BP_SHIMS = [spec for spec in _SHIMS
             if spec[0] in ("task-kit", "llm-kit", "hue-kit", "secrets-kit", "relaunch-kit")]


def _script(path: Path, marker: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho {marker}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class _BootstrapPython:
    """A shim with no plugin venv, run against a controlled PATH.

    The standalone tree never holds ``python/bin/python3``: hue-kit and secrets-kit
    run that file directly when it exists, before BOOTSTRAP_PYTHON is consulted.
    """

    def __init__(self, tmp_path: Path, venv_template: Path, kind: str, spec) -> None:
        plugin, stem, entrypoint = spec
        self.launch = _Launch(tmp_path, venv_template, plugin, stem, entrypoint, kind)
        self.home = self.launch.home
        self.standalone = self.home / _STANDALONE_REL
        self.tools = tmp_path / "tools"
        self.tools.mkdir()
        for name in _TOOLS:
            found = shutil.which(name)
            assert found, f"{name} not found on PATH"
            (self.tools / name).symlink_to(found)
        self.fakebin = tmp_path / "fakebin"
        _script(self.fakebin / "python3", "PATHPY")
        self.stranger = _script(tmp_path / "stranger" / "python", "STRANGER")

    def inside(self, *parts: str, marker: str = "STANDALONE") -> Path:
        return _script(self.standalone.joinpath(*parts), marker)

    def run(self, bootstrap_python: Path | None, *, with_path_python: bool = True):
        path = (f"{self.fakebin}:{self.tools}" if with_path_python else str(self.tools))
        extra = {"BOOTSTRAP_PYTHON": str(bootstrap_python)} if bootstrap_python else {}
        return self.launch.run(redirect=False, path=path, extra_env=extra)


@pytest.fixture(params=[k for k in _KINDS if k.values[0] in _POSIX_KINDS],
                ids=lambda p: p)
def posix_kind(request):
    return request.param


@pytest.fixture(params=_BP_SHIMS, ids=[s[0] for s in _BP_SHIMS])
def bp_spec(request):
    return request.param


@pytest.fixture
def bp(tmp_path, venv_template, posix_kind, bp_spec) -> _BootstrapPython:
    return _BootstrapPython(tmp_path, venv_template, posix_kind, bp_spec)


def _marker(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return result.stdout.strip().splitlines()[-1]


def test_a_stranger_bootstrap_python_is_refused_when_the_standalone_dir_is_absent(bp):
    """(b) With no standalone directory the old pattern collapsed to ``/*`` and
    accepted ANY absolute interpreter.

    Shown to fail: restore the previous case pattern; the stranger runs.
    """
    assert not bp.standalone.exists()
    assert _marker(bp.run(bp.stranger)) == "PATHPY"


def test_a_real_file_inside_the_standalone_tree_is_accepted(bp):
    """(c) The variable names a real file in the standalone tree."""
    real = bp.inside("python", "bin", "python3.12")
    assert _marker(bp.run(real)) == "STANDALONE"


def test_a_symlink_outside_the_tree_pointing_into_it_is_accepted(bp):
    """(c2) This machine's real layout: ~/.local/bin/python3 -> standalone .../pylink
    -> python3.12. The old directory-only check rejected it.

    Shown to fail: resolve ``dirname "$BOOTSTRAP_PYTHON"`` instead of the followed
    link; the variable is rejected and PATHPY runs.
    """
    bp.inside("python", "bin", "python3.12")
    (bp.standalone / "python" / "bin" / "pylink").symlink_to("python3.12")
    outer = bp.home / ".local" / "bin" / "python3"
    outer.parent.mkdir(parents=True, exist_ok=True)
    outer.symlink_to(bp.standalone / "python" / "bin" / "pylink")
    assert _marker(bp.run(outer)) == "STANDALONE"


def test_a_relative_symlink_with_dotdot_that_stays_inside_is_accepted(bp):
    """(c3) A relative link target is resolved against the link's own directory."""
    bp.inside("python", "lib", "real-python")
    link = bp.standalone / "python" / "bin" / "py"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to("../lib/real-python")
    assert _marker(bp.run(link)) == "STANDALONE"


def test_a_bootstrap_python_outside_the_tree_is_refused(bp):
    """(d) The standalone directory exists; the variable is a stranger."""
    bp.inside("python", "bin", "python3.12")
    assert _marker(bp.run(bp.stranger)) == "PATHPY"


def test_a_symlink_inside_the_tree_pointing_out_is_refused(bp):
    """(d2) The old check resolved only the directory and accepted this.

    Shown to fail: resolve ``dirname "$BOOTSTRAP_PYTHON"`` instead of the followed
    link; the stranger runs.
    """
    link = bp.standalone / "python" / "bin" / "pylink"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(bp.stranger)
    assert _marker(bp.run(link)) == "PATHPY"


def test_a_relative_symlink_inside_the_tree_escaping_by_dotdot_is_refused(bp):
    """(d3) The relative branch of the link-following loop."""
    link = bp.standalone / "python" / "bin" / "escape"
    link.parent.mkdir(parents=True, exist_ok=True)
    relative = os.path.relpath(bp.stranger, link.parent)
    link.symlink_to(relative)
    assert _marker(bp.run(link)) == "PATHPY"


# --- relaunch-kit: the synced data-directory copy is the whole launcher ------------
#
# task-kit's PATH copy is a locator into the plugin cache. clauder's is the launcher
# itself, so bootstrap.json must sync ``lib`` beside ``bin`` and PATH must name the
# synced ``bin``: the shim finds the launcher as ``../lib/relaunch.py``.

_RELAUNCH_ROOT = _REPO_ROOT / "plugins" / "relaunch-kit"


def _sync_like_bootstrap(manifest: dict, data_dir: Path) -> None:
    """The copy bootstrap's sync_to_data phase makes (copytree, dirs_exist_ok)."""
    for entry in manifest["sync_to_data"]:
        shutil.copytree(_RELAUNCH_ROOT / entry["src"], data_dir / entry["dst"],
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))


def test_relaunch_kit_manifest_puts_the_synced_bin_on_path():
    """path_entries names the data directory's bin, and bin is what sync_to_data fills.

    Shown to fail: change the path_entries value, or the ``dst`` of the bin sync entry;
    the two stop agreeing.
    """
    manifest = json.loads((_RELAUNCH_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
    sync = {entry["src"]: entry["dst"] for entry in manifest["sync_to_data"]}
    assert sync.get("bin") == "bin"
    assert sync.get("lib") == "lib"
    assert manifest["path_entries"] == [
        "~/.claude/plugins/data/plugins-kit/relaunch-kit/" + sync["bin"]
    ]


def test_relaunch_kit_synced_copy_finds_its_launcher_and_the_real_claude(tmp_path, posix_kind):
    """clauder run from the bootstrap-synced data directory reaches claude.

    ``-v`` is a pass-through, so the launcher execs the target named by
    RELAUNCH_KIT_CLAUDE without a rendezvous file.

    Shown to fail: remove the ``lib`` entry from sync_to_data; the shim exits 2 with
    ``launcher not found``.
    """
    manifest = json.loads((_RELAUNCH_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
    home = tmp_path / "home"
    data = home / ".claude" / "plugins" / "data" / "plugins-kit" / "relaunch-kit"
    _sync_like_bootstrap(manifest, data)
    fake_claude = _script(tmp_path / "claude-target" / "claude", "REAL-CLAUDE")
    env = {name: value for name, value in os.environ.items() if name not in _SCRUBBED_ENV}
    env.update(HOME=str(home), USERPROFILE=str(home), RELAUNCH_KIT_CLAUDE=str(fake_claude))
    bash = _BASH if posix_kind == "posix" else _BASH32
    result = subprocess.run([bash, str(data / "bin" / "clauder"), "-v"],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert result.stdout.strip().splitlines()[-1] == "REAL-CLAUDE"
