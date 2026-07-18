"""hue-kit CLI -- a small verb front-end over the layered Hue scene tool.

Subcommands (the common operations):

    report      Read the live bridge, solve the SMALLEST meta-group vocabulary,
                and print each scene as a layer stack. Read-only. Start here.
    groups      Write a starter group registry (scene-groups.yaml) with
                placeholder names for you to rename. Read-only against the bridge.
    export      Materialise scene-designs.yaml from your live scenes + the
                registry (your current configuration, written to YAML).
                Read-only against the bridge.
    render      Render the browsable HTML report (config + source embedded).
    validate    Diff your YAML (scene-groups.yaml + scene-designs.yaml) against
                the bridge, per light. Read-only.
    apply       Write the YAML layer stacks back to the bridge. DRY-RUN by
                default; pass --yes to actually write. Backs each scene up first.
    init        Copy the shipped example scene-groups.yaml, scene-designs.yaml,
                and index.html into a directory so you can overwrite them with
                your own.

By default the YAML/HTML working files live in the current directory:
scene-groups.yaml, scene-designs.yaml, index.html. Point elsewhere with
--dir, or per file with the HUE_GROUPS_FILE / HUE_DESIGNS_FILE env vars.

Bridge connection (see the plugin README): set HUE_BRIDGE_IP and either
HUE_APP_KEY or HUE_KEY_FILE.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# Re-exec under the plugin's bootstrap-provisioned venv so requests/pyyaml/
# urllib3 (declared in pyproject.toml) are importable regardless of how this
# script was launched. No-op once already under that interpreter.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from bootstrap_guard import require_bootstrap, reexec_under_plugin_venv  # noqa: E402

reexec_under_plugin_venv("hue-kit")

PLUGIN_ROOT = _HERE.parent
EXAMPLES = PLUGIN_ROOT / "examples"
SCENE_LAYERS = _HERE / "scene-layers.py"
EXAMPLE_FILES = ("scene-groups.yaml", "scene-designs.yaml", "index.html")


def _run_scene_layers(flags: list[str], workdir: Path) -> int:
    """Point scene-layers.py at the working directory's YAML, then exec it under
    the current (already venv-resolved) interpreter."""
    env = os.environ
    env.setdefault("HUE_GROUPS_FILE", str(workdir / "scene-groups.yaml"))
    env.setdefault("HUE_DESIGNS_FILE", str(workdir / "scene-designs.yaml"))
    argv = [sys.executable, str(SCENE_LAYERS), *flags]
    os.execve(sys.executable, argv, env)  # replaces this process


def _cmd_init(args) -> int:
    # The init positional wins if given; otherwise fall back to the shared --dir.
    dest = Path(args.init_dir or args.dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for name in EXAMPLE_FILES:
        src = EXAMPLES / name
        target = dest / name
        if target.exists() and not args.force:
            print(f"skip (exists): {target}  -- pass --force to overwrite",
                  file=sys.stderr)
            continue
        shutil.copy2(src, target)
        print(f"wrote {target}")
    print("\nEdit scene-groups.yaml / scene-designs.yaml, then "
          "`hue-kit validate` and `hue-kit apply`. Or regenerate from your own "
          "bridge with `hue-kit report` -> `hue-kit groups` -> `hue-kit export`.",
          file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hue-kit",
        description="Layered Hue scene framework -- read, analyse, and sync "
                    "scenes with your bridge.")
    parser.add_argument("--dir", default=".", metavar="PATH",
                        help="working directory for the YAML/HTML files "
                             "(default: current directory)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("report", help="Read the bridge; print the minimal group "
                                  "family + each scene as a layer stack.")
    p_groups = sub.add_parser("groups", help="Write a starter scene-groups.yaml "
                                             "(placeholder names to rename).")
    p_groups.add_argument("path", nargs="?",
                          help="output path (default: <dir>/scene-groups.yaml)")
    sub.add_parser("export", help="Write scene-designs.yaml from live scenes + "
                                  "the registry.")
    p_render = sub.add_parser("render", help="Render the HTML report.")
    p_render.add_argument("path", nargs="?",
                          help="output path (default: <dir>/index.html)")
    sub.add_parser("validate", help="Diff your YAML against the bridge (per light).")
    p_apply = sub.add_parser("apply", help="Write the YAML to the bridge "
                                           "(dry-run unless --yes).")
    p_apply.add_argument("--yes", action="store_true",
                         help="actually write to the bridge (else dry-run)")
    p_apply.add_argument("--scene", action="append", dest="scenes", metavar="NAME",
                         help="limit to this scene (repeatable)")
    p_init = sub.add_parser("init", help="Copy the example YAML + HTML into a "
                                         "directory to overwrite with your own.")
    p_init.add_argument("init_dir", nargs="?", default=None, metavar="DIR",
                        help="destination directory (default: --dir, else current)")
    p_init.add_argument("--force", action="store_true",
                        help="overwrite existing files")

    args = parser.parse_args(argv)

    # init is local file work -- no bridge, no scene-layers.py.
    if args.cmd == "init":
        return _cmd_init(args)

    require_bootstrap("hue-kit", feature="scene tooling")
    workdir = Path(args.dir).resolve()

    if args.cmd == "report":
        return _run_scene_layers([], workdir)
    if args.cmd == "groups":
        out = args.path or str(workdir / "scene-groups.yaml")
        return _run_scene_layers(["--export-groups", out], workdir)
    if args.cmd == "export":
        return _run_scene_layers(["--export-designs", str(workdir / "scene-designs.yaml")], workdir)
    if args.cmd == "render":
        out = args.path or str(workdir / "index.html")
        return _run_scene_layers(["--html", out], workdir)
    if args.cmd == "validate":
        return _run_scene_layers(["--validate-design"], workdir)
    if args.cmd == "apply":
        flags = ["--apply"]
        if args.yes:
            flags.append("--yes")
        for s in args.scenes or []:
            flags += ["--scene", s]
        return _run_scene_layers(flags, workdir)

    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
