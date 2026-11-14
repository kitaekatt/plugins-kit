"""Golden characterization of engine._main_pass across its phases.

Each scenario drives engine._main in-process over a temp HOME and data dir,
with every collaborator _main_pass calls replaced by a recording fake that
contributes scenario-chosen entries, failures and return values. The recording
captures everything observable from the pass: the ordered stream of
collaborator calls (with their arguments AT CALL TIME, so the content of the
entry lists each phase is handed is pinned too), every pass-record event,
every log block written, stdout, the exit status, and the files left under the
temp root. It is compared to tests/bootstrap/golden/main_pass.json, which was
captured from _main_pass before it was split into phase functions.

Regenerate ONLY for an intended behaviour change:
    BOOTSTRAP_UPDATE_GOLDEN=1 uv run --extra dev pytest tests/bootstrap/test_main_pass_golden.py
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bootstrap_lib import (
    apt,
    config as config_mod,
    engine,
    interpreter_env,
    log as log_mod,
    path_repair,
    platform_detect,
    plugin_resolve,
    plugins_snapshot,
    registry_repair,
    self_register,
)
from bootstrap_lib.plugin_resolve import PluginInfo
from bootstrap_lib.records import Entry

GOLDEN = Path(__file__).parent / "golden" / "main_pass.json"

CONFIG = {
    "log_success_checks": False,
    "self_setup": {"tools": ["uv"]},
    "notify_reload_needed": True,
}

RICH_MANIFEST = {
    "plugins": [{"ref": "mk:declared"}, {"ref": "barename"}],
    "project_git_pull": {"enabled": True},
    "project_venv": {"path": ".venv"},
    "project_npm": {"path": "."},
    "agent_skills_link": True,
}

BASE: dict[str, Any] = {
    "mode": ["--background"],
    "project": True,
    "project_key": None,
    "os": "macos",
    "always": None,
    "config": CONFIG,
    "version": "0.168.0",
    "stamps": {"last_version": "0.167.0", "engine_ran_version": "0.167.0"},
    "path_repair": {"changed": True, "deduped": 3, "restored": 1},
    "project_default": ("/proj/.venv/bin/python", "venv"),
    "layout": "prod",
    "env": {},
    "legacy_user_bootstrap": True,
    "repair": "repaired",
    "unrepairable": ["mk:chimera"],
    "layered": [
        (dict(RICH_MANIFEST), [], "selected"),
        (dict(RICH_MANIFEST, moved=True), [{"path": "/x/bootstrap.json", "error": "bad comma"}],
         "selected"),
    ],
    "pull": {"failures": [{"type": "pull", "message": "pull blocked"}],
             "notice": "project pulled: 2 commits", "moved": True},
    "fail": {"self_setup", "manifest", "env"},
    "export_source": "venv",
    "venv_failures": [],
    "venv_python": "/proj/.venv/bin/python",
    "plugins": [
        [("bootstrap", "mk"), ("zeta", "mk"), ("alpha", "other")],
        [("bootstrap", "mk"), ("zeta", "mk"), ("alpha", "other")],
        [("bootstrap", "mk"), ("zeta", "mk"), ("alpha", "other"), ("late", "mk")],
    ],
    "cache_changed": [True, False, True],
    "plugin_failures": {"zeta"},
    "plugin_manifests": {"zeta": {"plugins": [{"ref": "mk:dep"}]}, "late": "not json"},
    "layers": [("/h/.claude/bootstrap.json", "user", {"plugins": [{"ref": "mk:u"}]}),
               ("/h/.claude/bootstrap.local.json", "user_local", {"plugins": [{"ref": "mk:l"}]}),
               ("/p/.claude/bootstrap.json", "project", "junk")],
    "self_register": (["self-register: added mk:zeta"], ["self-register: 2 already declared"]),
    "sweep": (["sweep action"], ["sweep quiet"], ["sweep ok"], [{"type": "sweep", "message": "x"}]),
    "revoke": (["revoke action"], ["revoke ok"], [{"type": "revoke", "message": "y"}]),
    "project_links": (["plink action"], ["plink quiet"], ["plink ok"], [{"type": "plink"}]),
    "link_summary": "shared-libs: linked bootstrap_lib -> 3 venvs",
    "installed_before": {"bootstrap@mk": "/c/b"},
    "installed_after": {"bootstrap@mk": "/c/b", "late@mk": "/c/late"},
    "stale_advice": "bootstrap 0.169.0 is cached; restart to load it",
    "codex_hook": (["codex-hook: armed"], ["codex-hook: ok"], []),
    "shell": "--- shell: 1 action ---",
    "elevation": False,
    "persist_failure": False,
    "snapshot_raises": False,
    "summary_empty": False,
}


def _sc(**overrides: Any) -> dict[str, Any]:
    return {**BASE, **overrides}


QUIET = dict(
    path_repair={"changed": False, "deduped": 0, "restored": 0},
    legacy_user_bootstrap=False,
    repair="clean",
    unrepairable=[],
    layered=[({}, [], "selected")],
    pull=None,
    fail=set(),
    plugins=[[("bootstrap", "mk")], [("bootstrap", "mk")], [("bootstrap", "mk")]],
    cache_changed=[False, False, False],
    plugin_failures=set(),
    plugin_manifests={},
    layers=[],
    self_register=([], []),
    sweep=([], [], [], []),
    revoke=([], [], []),
    project_links=([], [], [], []),
    link_summary="",
    installed_before={"bootstrap@mk": "/c/b"},
    installed_after={"bootstrap@mk": "/c/b"},
    stale_advice="",
    codex_hook=([], [], []),
    shell="",
)

SCENARIOS: dict[str, dict[str, Any]] = {
    "hook_rich": _sc(),
    "hook_quiet": _sc(**QUIET),
    "hook_quiet_verbose": _sc(**QUIET, mode=["--background", "--verbose"]),
    "hook_log_success_config": _sc(config=dict(CONFIG, log_success_checks=True)),
    "console_rich": _sc(mode=["--console"]),
    "console_exit_status_failures": _sc(mode=["--console", "--exit-status"],
                                        layered=[(dict(RICH_MANIFEST), [], "unselected")]),
    "console_exit_status_clean": _sc(**dict(QUIET, layered=[({}, [], "unknown")]),
                                     mode=["--console", "--exit-status"]),
    "console_fix_all_elevation_ran": _sc(mode=["--console", "--fix-all"], elevation=True),
    "console_fix_all_no_launch": _sc(mode=["--console", "--fix-all"]),
    "console_recheck": _sc(mode=["--console", "--recheck"]),
    "hook_elevation_ran": _sc(elevation=True),
    "unsupported_platform": _sc(os=None),
    "unsupported_platform_exit_status": _sc(os=None, mode=["--console", "--exit-status"]),
    "always_hook": _sc(always={"failures": [{"name": "e1", "message": "boom"}, {}],
                               "actions": ["always: cloned x"]},
                       mode=["--background", "--run-kind", "always", "--verbose"]),
    "always_console_crash": _sc(always="raise", mode=["--console", "--run-kind", "always"]),
    "always_hook_silent": _sc(always={"failures": [], "actions": []},
                              mode=["--background", "--run-kind", "always"]),
    "no_project_dir": _sc(project=False),
    "explicit_project_key": _sc(project_key="_global_"),
    "first_install": _sc(stamps={}),
    "same_version": _sc(stamps={"last_version": "0.168.0", "engine_ran_version": "0.168.0"}),
    "downgrade_keeps_newer_ran_stamp": _sc(
        stamps={"last_version": "0.169.0", "engine_ran_version": "0.170.0"}),
    "console_downgrade_writes_no_stamp": _sc(
        mode=["--console"], stamps={"last_version": "0.169.0"}),
    "no_plugin_json": _sc(version=None),
    "registry_repair_error": _sc(repair="error"),
    "dev_layout": _sc(layout="dev"),
    "claudx_layout": _sc(layout="dev", env={"CLAUDE_PLUGIN_TEST": "1"}),
    "redirected_self_register": _sc(env={"CLAUDE_BOOTSTRAP_DATA_ROOT": "/redirect"}),
    "console_self_register": _sc(mode=["--console"], config=dict(CONFIG, log_success_checks=True)),
    "reload_notice_off": _sc(config=dict(CONFIG, notify_reload_needed=False)),
    "export_outranked_opt_out": _sc(export_source="opt_out"),
    "export_outranked_virtual_env": _sc(export_source="virtual_env",
                                        env={"BOOTSTRAP_PROJECT_PYTHON": "/ve/bin/python"}),
    "venv_blocking_failure": _sc(venv_failures=[{"type": "venv", "message": "broken"}]),
    "venv_nonblocking_failure": _sc(
        venv_failures=[{"type": "lib", "message": "bad lib", "blocks_venv": False}]),
    "venv_export_none": _sc(venv_python=None),
    "no_export_source": _sc(export_source=None),
    "no_export_source_no_venv_export": _sc(export_source=None, venv_python=None),
    "pull_not_moved": _sc(pull={"failures": [], "notice": "", "moved": False}),
    "persistent_failure_keeps_alert": _sc(persist_failure=True),
    "snapshot_raises": _sc(snapshot_raises=True),
    "summary_only_sweep": _sc(**dict(QUIET, link_summary="shared-libs: linked 1")),
}


# -- normalisation ---------------------------------------------------------- #

class _Norm:
    def __init__(self, tmp: Path) -> None:
        self.tmp = str(tmp)

    def __call__(self, obj: Any) -> Any:
        if isinstance(obj, Entry):
            text = self(str(obj))
            if obj.short or obj.detail is not None:
                return {"entry": text, "short": self(obj.short), "detail": self(obj.detail)}
            return text
        if isinstance(obj, str):
            return obj.replace(self.tmp, "<TMP>")
        if isinstance(obj, float):
            return "<FLOAT>"  # only ever a wall-clock timestamp here
        if obj is None or isinstance(obj, (bool, int)):
            return obj
        if isinstance(obj, datetime):
            return "<DATETIME>"
        if isinstance(obj, dict):
            return {str(k): self(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self(v) for v in obj]
        if isinstance(obj, (set, frozenset)):
            return sorted(self(v) for v in obj)
        if dataclasses.is_dataclass(obj):
            return {"<" + type(obj).__name__ + ">": self(dataclasses.asdict(obj))}
        if isinstance(obj, PluginInfo):
            return {"<PluginInfo>": self(vars(obj))}
        if isinstance(obj, (SimpleNamespace,)) or type(obj).__name__ == "Namespace":
            return {"<" + type(obj).__name__ + ">": self(vars(obj))}
        return "<" + type(obj).__name__ + ">"


# Fakes whose arguments ARE the pass's output: recorded verbatim. Every other
# fake's long list arguments are recorded as a length plus a content digest --
# still exact (any change to the entries changes the digest), but the golden
# stays readable instead of repeating the growing entry lists at every call.
_FULL_ARGS = {"write_log_block", "_emit_pass_results", "_emit_unsupported_platform",
              "ensure_self_registration", "save_config"}


def _compact(obj: Any, depth: int = 0) -> Any:
    if isinstance(obj, list):
        if depth >= 2 and len(obj) > 3:
            blob = json.dumps(obj, sort_keys=True).encode("utf-8")
            return {"len": len(obj), "sha1": hashlib.sha1(blob).hexdigest()[:12]}
        return [_compact(v, depth + 1) for v in obj]
    if isinstance(obj, dict):
        return {k: _compact(v, depth + 1) for k, v in obj.items()}
    return obj


# -- the harness ------------------------------------------------------------ #

class _Harness:
    def __init__(self, sc: dict[str, Any], tmp: Path, monkeypatch) -> None:
        self.sc = sc
        self.tmp = tmp
        self.mp = monkeypatch
        self.norm = _Norm(tmp)
        self.events: list[Any] = []
        self.list_calls = 0
        self.layered_calls = 0
        self.installed_calls = 0
        self.home = tmp / "home"
        self.data_dir = tmp / "data"
        self.project = tmp / "project"
        self.root = tmp / "plugin_root"

    def ev(self, *items: Any) -> None:
        self.events.append(self.norm(list(items)))

    def fake(self, owner: Any, name: str, behave=None) -> None:
        full = name in _FULL_ARGS

        def stub(*args, **kwargs):
            call = self.norm([name, list(args), kwargs])
            self.events.append(call if full else _compact(call))
            return behave(*args, **kwargs) if behave else None
        self.mp.setattr(owner, name, stub)

    # -- fixture layout --------------------------------------------------- #

    def build(self) -> list[str]:
        sc = self.sc
        (self.home / ".claude").mkdir(parents=True)
        self.data_dir.mkdir()
        self.project.mkdir()
        (self.root / ".claude-plugin").mkdir(parents=True)
        if sc["version"] is not None:
            (self.root / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": "bootstrap", "version": sc["version"]}))
        for name, value in sc["stamps"].items():
            (self.data_dir / name).write_text(value)
        if sc["legacy_user_bootstrap"]:
            (self.data_dir / "user-bootstrap.json").write_text("{}")
        (self.data_dir / "bootstrap_alert.json").write_text("{}")
        (self.data_dir / "import_retry_pending").write_text("1")
        (self.data_dir / "import_retry_launched").write_text("1")
        for name, manifest in sc["plugin_manifests"].items():
            d = self.tmp / "installs" / name
            d.mkdir(parents=True)
            (d / "bootstrap.json").write_text(
                manifest if isinstance(manifest, str) else json.dumps(manifest))
        for key in ("CLAUDE_BOOTSTRAP_DATA_ROOT", "CLAUDE_PLUGIN_TEST",
                    interpreter_env.PROJECT_VAR):
            self.mp.delenv(key, raising=False)
        self.mp.setenv("HOME", str(self.home))
        self.mp.setenv("USERPROFILE", str(self.home))
        for key, value in sc["env"].items():
            self.mp.setenv(key, value)
        argv = ["bootstrap_engine.py", "--plugin-root", str(self.root),
                "--data-dir", str(self.data_dir), *sc["mode"]]
        if sc["project"]:
            argv += ["--project-dir", str(self.project)]
        if sc["project_key"]:
            argv += ["--project-key", sc["project_key"]]
        return argv

    def plugin(self, name: str, mkt: str) -> PluginInfo:
        install = self.tmp / "installs" / name
        return PluginInfo(name=name, install_path=str(install), version="1.0", marketplace=mkt)

    # -- collaborator behaviour ------------------------------------------- #

    def install(self) -> None:
        sc, h = self.sc, self

        class _Recorder:
            enabled = True

            def record(self, kind, text="", sev=None, **kw):
                h.ev("record", kind, str(text), sev,
                     {k: v for k, v in kw.items() if v is not None})

            def record_entry(self, sev, text, **kw):
                h.ev("record_entry", sev, text, {k: v for k, v in kw.items() if v is not None})

            def record_emit(self, channel, response):
                h.ev("record_emit", channel, response)

            def flush(self):
                pass

        self.mp.setattr(engine, "_new_recorder", lambda *a, **k: _Recorder())

        pr = sc["path_repair"]
        self.fake(path_repair, "repair_path", lambda: SimpleNamespace(
            changed=pr["changed"], deduped=pr["deduped"], restored=pr["restored"],
            before_entries=40, after_entries=36))
        self.fake(interpreter_env, "begin_pass", lambda: "/py/engine")
        self.fake(interpreter_env, "project_key", lambda d: "key:" + os.path.basename(d))
        self.fake(interpreter_env, "export_project_default",
                  lambda d, record_dir, key: tuple(sc["project_default"]))
        self.fake(config_mod, "load_config", lambda d, defaults: copy.deepcopy(sc["config"]))
        self.fake(config_mod, "save_config")

        def detect():
            if sc["os"] is None:
                raise platform_detect.UnsupportedPlatformError("fedora is not supported")
            return sc["os"]
        self.fake(platform_detect, "detect_os", detect)
        self.fake(engine, "_emit_unsupported_platform")

        def always(*_a):
            if sc["always"] == "raise":
                raise RuntimeError("always entry exploded")
            return sc["always"]["failures"], list(sc["always"]["actions"])
        self.fake(engine, "_run_always_pass", always)
        self.fake(log_mod, "write_log_block")
        self.fake(apt, "reset_apt_pass_state")

        plugins_dir = (self.home / ".claude" / "plugins" if sc["layout"] == "prod"
                       else self.tmp / "dev" / "plugins")
        self.fake(engine, "_find_plugins_dir", lambda root: str(plugins_dir))
        self.fake(engine, "marketplace_name_for", lambda root: "mk")

        def self_setup(_s, _os, _d, _r, action, ok, plugin_name, quiet_entries):
            action.append("self-setup: installed uv")
            ok.append("self-setup: jq ok")
            quiet_entries.append("self-setup: quiet detail")
            return [{"type": "tool", "message": "uv"}] if "self_setup" in sc["fail"] else []
        self.fake(engine, "_process_self_setup", self_setup)
        self.fake(engine, "_activate_bootstrap_venv")

        def apply(_p):
            if sc["repair"] == "error":
                return SimpleNamespace(error="registry locked")
            return SimpleNamespace(error="", n=2) if sc["repair"] == "repaired" else None
        self.fake(registry_repair, "apply_repair", apply)
        self.fake(registry_repair, "describe_repair", lambda r: "registry: repaired 2 records")
        self.fake(registry_repair, "load_registry", lambda p: {"plugins": {}})
        self.fake(registry_repair, "find_unrepairable", lambda reg: list(sc["unrepairable"]))
        self.fake(registry_repair, "describe_unrepairable",
                  lambda s: f"registry: left {len(s)} unrepaired")

        self.fake(engine, "_load_enabled_refs", lambda d: {"mk:bootstrap", "mk:zeta"})

        def installed(_p, enabled_refs=None):
            self.installed_calls += 1
            return dict(sc["installed_before"] if self.installed_calls == 1
                        else sc["installed_after"])
        self.fake(engine, "_read_installed_plugins", installed)

        def layered(_d, _dd):
            item = sc["layered"][min(self.layered_calls, len(sc["layered"]) - 1)]
            self.layered_calls += 1
            manifest, errors, status = item
            return copy.deepcopy(manifest), list(errors), SimpleNamespace(status=status)
        self.fake(engine, "_load_layered_manifests_ex", layered)

        def pull(_def, _d, quiet_entries):
            quiet_entries.append("git_pull: fetched")
            p = sc["pull"]
            return (["git_pull: fast-forwarded"], ["git_pull: ok"], list(p["failures"]),
                    p["notice"], p["moved"])
        if sc["pull"] is not None:
            self.fake(engine, "_process_project_git_pull", pull)

        def tiers(manifest, _ps, _d, a, o, q):
            a.append("tiers: installed mk:t")
            o.append("tiers: ok")
            q.append("tiers: quiet")
            if manifest:
                manifest = dict(manifest, _tiers=True)
            return manifest, [{"type": "tier", "message": "t"}]
        self.fake(engine, "_process_plugin_tiers", tiers)

        def uninstall(manifest, _ps, _d, a, o, q):
            a.append("uninstall: removed mk:u")
            o.append("uninstall: ok")
            q.append("uninstall: quiet")
            if manifest:
                manifest = dict(manifest, _uninstall=True)
            return manifest, []
        self.fake(engine, "_process_plugin_uninstall_entries", uninstall)

        def list_enabled(*_a, **_k):
            idx = min(self.list_calls, 2)
            self.list_calls += 1
            return ([self.plugin(n, m) for n, m in sc["plugins"][idx]],
                    sc["cache_changed"][idx])
        self.fake(plugin_resolve, "list_enabled_plugins", list_enabled)

        def barrier(*args):
            args[7].append("marketplace: refreshed mk")
            args[8].append("marketplace: ok")
            args[9].append("marketplace: quiet")
            return [{"type": "marketplace", "message": "m"}]
        self.fake(engine, "_marketplace_barrier", barrier)
        self.fake(engine, "_interpreter_env_layers", lambda d, ps: (["user-layer"], ["proj-layer"]))
        self.fake(interpreter_env, "interpreter_env_settings",
                  lambda u, parse_errors, project_layers: (True, False, ["ie: note"]))
        self.fake(engine, "_process_interpreter_env",
                  lambda *a, **k: (["ie: persisted"], ["ie: ok"], [{"type": "ie"}]))

        def export_default(_d, _m, _k, ok, quiet, user_layers, project_layers):
            ok.append("project default: ok")
            quiet.append("project default: quiet")
            return "/proj/py", sc["export_source"]
        self.fake(engine, "_export_project_default_for_pass", export_default)

        def manifest(_m, _os, _d, _r, a, o, plugin_name, project_dir, quiet_entries,
                     shared_lib_links):
            a.append(Entry("tools: installed rg into /a/very/long/path", short="tools: rg"))
            o.append("tools: fd ok")
            quiet_entries.append("tools: quiet")
            return [{"type": "manifest", "message": "x"}] if "manifest" in sc["fail"] else []
        self.fake(engine, "_process_manifest", manifest)

        def profile(_ps, a, o, q):
            a.append("profile: applied engineer")
            o.append("profile: ok")
            q.append("profile: quiet")
            return [{"type": "profile", "message": "p"}]
        self.fake(engine, "_report_profile_state", profile)

        def venv(_def, _d, quiet_entries):
            quiet_entries.append("project_venv: quiet")
            return ["project_venv: synced"], ["project_venv: ok"], copy.deepcopy(sc["venv_failures"])
        self.fake(engine, "_process_project_venv", venv)

        def export_python(_def, _d):
            if sc["venv_python"]:
                self.mp.setenv(interpreter_env.PROJECT_VAR, sc["venv_python"])
            return sc["venv_python"]
        self.fake(engine, "_export_project_python", export_python)

        def record(_dd, _k, _v, _s, _pe, ok, quiet):
            ok.append("record: ok")
            quiet.append("record: quiet")
        self.fake(engine, "_maintain_project_python_record", record)

        def npm(_def, _d, quiet_entries):
            quiet_entries.append("project_npm: quiet")
            return ["project_npm: installed"], ["project_npm: ok"], [{"type": "npm"}]
        self.fake(engine, "_process_project_npm", npm)
        self.fake(engine, "_run_agent_skills_link_check",
                  lambda d, v: (["agent_skills: linked"], ["agent_skills: ok"], [{"type": "asl"}]))

        def env_pass(_d, _os, _dd, _r, a, o, engine_version, quiet_entries, project_key,
                     write_stamp):
            a.append(Entry("git_config: set user.email to a long value", short="git_config"))
            o.append("git_config: ok")
            quiet_entries.append("git_config: quiet")
            return [{"type": "env", "message": "e"}] if "env" in sc["fail"] else []
        self.fake(engine, "_process_env_pass", env_pass)
        self.fake(engine, "_order_plugins_by_shared_lib_dependencies",
                  lambda plugins, default_marketplace, bootstrap_ref: list(reversed(plugins)))

        def single(pi, _os, _dd, all_failures, log_success, display_sections, deferred, _args,
                   engine_version, shared_lib_links, recorder):
            label = f"{pi.marketplace}:{pi.name}@{pi.version}"
            display_sections.append((label, [f"{pi.name}: action"], [f"{pi.name}: ok"]))
            deferred.append((os.path.join(_dd, pi.name), label, [f"{pi.name}: action"]))
            if pi.name in sc["plugin_failures"]:
                all_failures.append({"type": "plugin", "plugin": pi.name, "message": "bad",
                                     "persist_across_sessions": sc["persist_failure"]})
        self.fake(engine, "_bootstrap_single_plugin_isolated", single)
        self.fake(engine, "_collect_manifest_layers",
                  lambda d, dd: ([tuple(x) for x in sc["layers"]], []))
        self.fake(self_register, "ensure_self_registration",
                  lambda *a, **k: (list(sc["self_register"][0]), list(sc["self_register"][1])))

        def env_records(_dd, _plugins, ok, quiet):
            ok.append("env records: ok")
            quiet.append("env records: rewrote")
        self.fake(engine, "_maintain_env_records", env_records)
        self.fake(engine, "_shared_lib_convergence_sweep",
                  lambda *a, **k: copy.deepcopy(tuple(sc["sweep"])))
        self.fake(engine, "_revoke_standalone_links",
                  lambda *a, **k: copy.deepcopy(tuple(sc["revoke"])))
        self.fake(engine, "_link_project_shared_libs",
                  lambda *a, **k: copy.deepcopy(tuple(sc["project_links"])))

        class _Links:
            def summary(self_inner):
                h.ev("links.summary")
                return sc["link_summary"]
        self.mp.setattr(engine, "_SharedLibLinkLog", _Links)
        self.fake(engine, "_reload_advice",
                  lambda newly: ("reload: " + ",".join(p.name for p in newly)) if newly else "")
        self.fake(engine, "_bootstrap_stale_advice", lambda *a, **k: sc["stale_advice"])
        self.fake(engine, "_run_codex_hook_setup",
                  lambda *a, **k: copy.deepcopy(tuple(sc["codex_hook"])))
        self.fake(engine, "_read_new_log_entries", lambda d, start_time=None: sc["shell"])
        self.fake(engine, "_elevation_step", lambda *a, **k: sc["elevation"])
        self.fake(engine, "_update_display_marker")
        self.fake(engine, "_emit_pass_results")
        self.fake(engine, "_clear_transient_import_state")

        def snapshot(_dd):
            if sc["snapshot_raises"]:
                raise OSError("snapshot failed")
        self.fake(plugins_snapshot, "stamp_plugins_state", snapshot)

    def files(self) -> dict[str, Any]:
        out = {}
        for p in sorted(self.tmp.rglob("*")):
            if p.is_file():
                rel = p.relative_to(self.tmp).as_posix()
                if "__pycache__" in rel:
                    continue
                out[rel] = self.norm(p.read_text(errors="replace"))
        return out


def _record(name: str, tmp: Path, monkeypatch, capsys) -> dict[str, Any]:
    h = _Harness(SCENARIOS[name], tmp, monkeypatch)
    argv = h.build()
    h.install()
    monkeypatch.setattr(sys, "argv", argv)
    capsys.readouterr()
    exit_code = None
    try:
        engine._main()
    except SystemExit as exc:
        exit_code = exc.code
    out = capsys.readouterr().out
    return {
        "events": h.events,
        "stdout": h.norm(out),
        "exit": exit_code,
        "files": h.files(),
        "env_project_python": h.norm(os.environ.get(interpreter_env.PROJECT_VAR)),
    }


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    if os.environ.get("BOOTSTRAP_UPDATE_GOLDEN"):
        return {}
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


_UPDATED: dict[str, Any] = {}


def _dump(data: dict[str, Any]) -> str:
    """One event per line: diffable, without indenting every nested value."""
    def line(v: Any) -> str:
        return json.dumps(v, sort_keys=True)
    out = ["{"]
    names = sorted(data)
    for i, name in enumerate(names):
        rec = data[name]
        out.append(f"{line(name)}: {{")
        keys = sorted(rec)
        for j, key in enumerate(keys):
            tail = "," if j < len(keys) - 1 else ""
            if key == "events":
                out.append(f"{line(key)}: [")
                ev = rec[key]
                out.extend(line(e) + ("," if k < len(ev) - 1 else "") for k, e in enumerate(ev))
                out.append("]" + tail)
            else:
                out.append(f"{line(key)}: {line(rec[key])}{tail}")
        out.append("}" + ("," if i < len(names) - 1 else ""))
    out.append("}")
    return "\n".join(out) + "\n"


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_main_pass_matches_golden(name, tmp_path, monkeypatch, capsys, golden):
    got = _record(name, tmp_path, monkeypatch, capsys)
    if os.environ.get("BOOTSTRAP_UPDATE_GOLDEN"):
        _UPDATED[name] = got
        if len(_UPDATED) == len(SCENARIOS):
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(_dump(_UPDATED), encoding="utf-8")
        return
    assert name in golden, f"no golden for {name}; regenerate deliberately"
    expected = golden[name]
    for key in ("exit", "stdout", "files", "env_project_python"):
        assert got[key] == expected[key], key
    # Events last, with a pointer to the first divergence: the stream is long.
    for i, (a, b) in enumerate(zip(got["events"], expected["events"])):
        assert a == b, f"event {i} differs"
    assert len(got["events"]) == len(expected["events"])


def test_golden_is_not_vacuous(golden):
    """A golden that a do-nothing _main_pass would satisfy pins nothing. Every
    scenario must record collaborator calls and a nontrivial outcome, and the
    full-pass scenarios must reach the emit (or console) stage."""
    if not golden:
        pytest.skip("regenerating")
    for name, rec in golden.items():
        assert len(rec["events"]) > 5, name
    full = [n for n, s in SCENARIOS.items()
            if s["os"] and s["always"] is None and not s["elevation"]]
    for name in full:
        names = [e[0] for e in golden[name]["events"]]
        console = "--console" in SCENARIOS[name]["mode"]
        assert ("_emit_pass_results" in names) != console, name
        assert "_bootstrap_single_plugin_isolated" in names, name
