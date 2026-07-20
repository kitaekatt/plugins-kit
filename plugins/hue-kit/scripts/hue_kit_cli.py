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
from bootstrap_guard import (require_bootstrap, reexec_under_plugin_venv,  # noqa: E402
                             data_dir)

reexec_under_plugin_venv("hue-kit")

PLUGIN_ROOT = _HERE.parent
EXAMPLES = PLUGIN_ROOT / "examples"
SCENE_LAYERS = _HERE / "scene-layers.py"
EXAMPLE_FILES = ("scene-groups.yaml", "scene-designs.yaml", "index.html")

# Philips' bridge discovery service: returns LAN bridges keyed to the caller's
# public IP. Fallback when HUE_BRIDGE_IP is unset. Needs internet.
DISCOVERY_URL = "https://discovery.meethue.com/"
# Where `hue-kit pair` stores the minted application key (user-scoped, 0600).
PAIRED_KEY_FILE = data_dir("hue-kit") / "app-key.txt"
# Cached discovered bridge IP, so we do not re-hit the rate-limited discovery
# service on every verb (env var still wins; delete the file to re-discover).
BRIDGE_IP_CACHE = data_dir("hue-kit") / "bridge-ip.txt"


def _discover_via_cloud(timeout: int = 10) -> list[dict]:
    """Query discovery.meethue.com (returns LAN bridges by public-IP match).
    Raises on HTTP/network error (incl. HTTP 429 rate-limit). stdlib-only."""
    import json
    import urllib.request
    req = urllib.request.Request(DISCOVERY_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (https)
        data = json.loads(r.read().decode())
    return [{"id": b.get("id"), "ip": b.get("internalipaddress"), "port": b.get("port")}
            for b in data if b.get("internalipaddress")]


def _discover_via_mdns(timeout: float = 4.0) -> list[dict]:
    """Local mDNS discovery of `_hue._tcp` bridges -- no cloud, no rate limit.
    Returns [] when zeroconf is unavailable (pre-venv) or nothing responds."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except Exception:
        return []
    import time
    found: dict[str, dict] = {}

    def _collect(zc, type_, name):
        try:
            info = zc.get_service_info(type_, name, timeout=int(timeout * 1000))
        except Exception:
            return
        if not info:
            return
        ipv4 = [a for a in info.parsed_addresses() if ":" not in a]
        if not ipv4:
            return
        props = info.properties or {}
        bid = props.get(b"bridgeid") or props.get(b"id")
        bid = bid.decode() if isinstance(bid, (bytes, bytearray)) else (bid or name.split(".")[0])
        found[ipv4[0]] = {"id": bid, "ip": ipv4[0], "port": info.port or 443}

    class _Listener:
        def add_service(self, zc, type_, name):
            _collect(zc, type_, name)

        def update_service(self, zc, type_, name):
            _collect(zc, type_, name)

        def remove_service(self, zc, type_, name):
            pass

    try:
        zc = Zeroconf()  # opens sockets / enumerates interfaces -- can raise
    except Exception:
        return []  # no usable network -- exactly the fallback's failure case
    try:
        ServiceBrowser(zc, "_hue._tcp.local.", _Listener())
        time.sleep(timeout)
    finally:
        zc.close()
    return list(found.values())


def _mdns_available() -> bool:
    """True if the zeroconf dependency (the mDNS fallback) is importable. It is a
    bootstrap-provisioned dep, so this is False until the plugin's venv exists --
    a state that must be reported distinctly from 'mDNS ran and found nothing',
    lest a fresh, un-provisioned install be told its live bridge does not exist."""
    try:
        import zeroconf  # noqa: F401
    except Exception:
        return False
    return True


def _discover_bridges(timeout: int = 10):
    """Find Hue bridges: try the cloud discovery service first (fast when it
    works), then fall back to local mDNS (rate-limit-free) on any failure.
    Returns (bridges, cloud_error): a list of {id, ip, port} deduped by IP, plus
    the cloud exception if the cloud path failed (None on cloud success). A
    non-None cloud_error WITH a non-empty list means mDNS rescued the lookup --
    callers surface that to the user (e.g. 'cloud was rate-limited')."""
    results: list[dict] = []
    cloud_err: Exception | None = None
    try:
        results = _discover_via_cloud(timeout)
    except Exception as e:  # HTTP 429, offline, DNS, TLS -- fall back to mDNS
        cloud_err = e
    if not results:
        results = _discover_via_mdns()
    seen: set[str] = set()
    deduped: list[dict] = []
    for b in results:
        if b.get("ip") and b["ip"] not in seen:
            seen.add(b["ip"])
            deduped.append(b)
    return deduped, cloud_err


def _cache_bridge_ip(ip: str) -> None:
    try:
        BRIDGE_IP_CACHE.parent.mkdir(parents=True, exist_ok=True)
        BRIDGE_IP_CACHE.write_text(ip + "\n")
    except OSError:
        pass  # a cache miss is non-fatal


def _resolve_bridge_ip() -> str:
    """HUE_BRIDGE_IP if set, else the cached IP, else auto-discover (and cache).
    Errors clearly -- there is no default."""
    ip = os.environ.get("HUE_BRIDGE_IP", "").strip()
    if ip:
        return ip
    if BRIDGE_IP_CACHE.is_file():
        try:
            cached = BRIDGE_IP_CACHE.read_text().strip()
        except OSError:
            cached = ""
        if cached:
            return cached
    bridges, cloud_err = _discover_bridges()
    if not bridges:
        if not _mdns_available():
            raise SystemExit(
                "hue-kit: HUE_BRIDGE_IP is not set and the local mDNS fallback is "
                "unavailable because the plugin's dependencies are not provisioned "
                "yet (zeroconf missing). Restart your Claude Code session so "
                "bootstrap builds the venv, then retry -- or set HUE_BRIDGE_IP="
                "<bridge ip> (from the Hue app or your router) to proceed now.")
        if cloud_err is not None:
            raise SystemExit(
                f"hue-kit: HUE_BRIDGE_IP is not set and auto-discovery found no "
                f"bridge (cloud: {cloud_err}; local mDNS: nothing). Make sure "
                "you're on the same LAN as the bridge, or set HUE_BRIDGE_IP="
                "<bridge ip> (from the Hue app or your router) and retry.")
        raise SystemExit(
            "hue-kit: no Hue bridge found (tried discovery.meethue.com + local "
            "mDNS). Set HUE_BRIDGE_IP=<bridge ip> manually and retry.")
    if len(bridges) > 1:
        listing = "\n".join(f"  {b['ip']}  (id {b['id']})" for b in bridges)
        raise SystemExit(
            "hue-kit: multiple bridges found -- set HUE_BRIDGE_IP to the one "
            f"you want:\n{listing}")
    b = bridges[0]
    via = " via local mDNS" if cloud_err is not None else ""
    if cloud_err is not None:
        print(f"hue-kit: discovery.meethue.com unavailable ({cloud_err}) -- "
              "used local mDNS instead.", file=sys.stderr)
    print(f"hue-kit: discovered bridge at {b['ip']} (id {b['id']}){via}",
          file=sys.stderr)
    _cache_bridge_ip(b["ip"])
    return b["ip"]


def _resolve_key_file() -> None:
    """If no key is configured but a paired key exists, point HUE_KEY_FILE at it.
    Leaves an explicit HUE_APP_KEY / HUE_KEY_FILE untouched (they win)."""
    if os.environ.get("HUE_APP_KEY") or os.environ.get("HUE_KEY_FILE"):
        return
    if PAIRED_KEY_FILE.is_file():
        os.environ["HUE_KEY_FILE"] = str(PAIRED_KEY_FILE)


def _run_scene_layers(flags: list[str], workdir: Path) -> int:
    """Resolve the bridge + key, point scene-layers.py at the working dir's YAML,
    then exec it under the current (already venv-resolved) interpreter."""
    os.environ["HUE_BRIDGE_IP"] = _resolve_bridge_ip()
    _resolve_key_file()
    env = os.environ
    env.setdefault("HUE_GROUPS_FILE", str(workdir / "scene-groups.yaml"))
    env.setdefault("HUE_DESIGNS_FILE", str(workdir / "scene-designs.yaml"))
    argv = [sys.executable, str(SCENE_LAYERS), *flags]
    os.execve(sys.executable, argv, env)  # replaces this process


def _cmd_discover(args) -> int:
    bridges, cloud_err = _discover_bridges()
    mdns_ok = _mdns_available()
    if cloud_err is not None:
        # Report the cloud failure even when mDNS rescued the lookup.
        rate = " (rate-limited)" if "429" in str(cloud_err) else ""
        if bridges:
            note = "falling back to local mDNS"
        elif not mdns_ok:
            note = "and the local mDNS fallback is unavailable (zeroconf not installed yet)"
        else:
            note = "and local mDNS found nothing"
        print(f"hue-kit: discovery.meethue.com unavailable{rate}: {cloud_err} -- "
              f"{note}.", file=sys.stderr)
    if not bridges:
        if not mdns_ok:
            print("hue-kit: the plugin's dependencies are not provisioned yet, so "
                  "the local mDNS fallback cannot run (zeroconf missing). Restart "
                  "your Claude Code session so bootstrap builds the venv, then "
                  "retry -- or set HUE_BRIDGE_IP=<bridge ip> manually to proceed "
                  "now.", file=sys.stderr)
            return 1
        print("no Hue bridges found (tried discovery.meethue.com + local mDNS). "
              "Set HUE_BRIDGE_IP=<bridge ip> manually.", file=sys.stderr)
        return 1
    for b in bridges:
        print(f"{b['ip']}\t{b['id']}\tport {b.get('port') or 443}")
    if len(bridges) == 1:
        _cache_bridge_ip(bridges[0]["ip"])
        print(f"\nCached {bridges[0]['ip']} -- verbs will use it automatically "
              "(env HUE_BRIDGE_IP still overrides; delete "
              f"{BRIDGE_IP_CACHE} to re-discover).", file=sys.stderr)
    return 0


def _cmd_pair(args) -> int:
    """Mint an application key: press the link button, POST generateclientkey,
    poll ~30s, store the key user-scoped. This is the app-authentication step."""
    import time
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    ip = _resolve_bridge_ip()
    if os.environ.get("HUE_APP_KEY") or os.environ.get("HUE_KEY_FILE") or \
            PAIRED_KEY_FILE.is_file():
        if not args.force:
            print(f"hue-kit: a key is already configured (paired key at "
                  f"{PAIRED_KEY_FILE} or via env). Pass --force to mint another.",
                  file=sys.stderr)
            return 0
    print(f"Pairing with the bridge at {ip}.", file=sys.stderr)
    if args.no_wait:
        # Non-interactive: the caller (an agent) has already confirmed the user
        # is ready, so poll immediately -- the press can land any time in the
        # window below. Keeps the flow runnable without a terminal on stdin.
        print("Press the round button on top of the bridge now "
              "(~30s window)...", file=sys.stderr)
    else:
        print("Press the round button on top of the bridge, then press Enter "
              "here (you have ~30s after pressing)...", file=sys.stderr)
        try:
            input()
        except EOFError:
            pass
    body = {"devicetype": "hue-kit#user", "generateclientkey": True}
    deadline = time.monotonic() + 30
    while True:
        try:
            resp = requests.post(f"https://{ip}/api", json=body, verify=False,
                                 timeout=10).json()
        except Exception as e:
            raise SystemExit(f"hue-kit: pairing request failed: {e}")
        entry = resp[0] if isinstance(resp, list) and resp else {}
        if "success" in entry:
            key = entry["success"]["username"]
            PAIRED_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            # create with 0600 from the start (no world-readable window)
            fd = os.open(PAIRED_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(key + "\n")
            try:
                PAIRED_KEY_FILE.chmod(0o600)  # ensure, in case it pre-existed
                perms = "(0600)"
            except OSError:
                perms = "(warning: could not set 0600 perms)"
            msg = f"hue-kit: paired. Application key saved to {PAIRED_KEY_FILE} {perms}."
            if os.environ.get("HUE_APP_KEY") or os.environ.get("HUE_KEY_FILE"):
                msg += (" NOTE: HUE_APP_KEY/HUE_KEY_FILE is set and OVERRIDES this "
                        "file -- unset it to use the paired key.")
            else:
                msg += " Verbs will use it automatically."
            print(msg, file=sys.stderr)
            return 0
        err = entry.get("error", {})
        if err.get("type") == 101:  # link button not pressed
            if time.monotonic() >= deadline:
                raise SystemExit("hue-kit: the link button was not pressed in "
                                 "time. Re-run `hue-kit pair` and press it first.")
            time.sleep(2)
            continue
        raise SystemExit(f"hue-kit: pairing error: {err or resp}")


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

    sub.add_parser("discover", help="Find Hue bridges on your network "
                                    "(discovery.meethue.com).")
    p_pair = sub.add_parser("pair", help="Mint an application key via the bridge "
                                         "link button (app authentication).")
    p_pair.add_argument("--force", action="store_true",
                        help="mint a new key even if one is already configured")
    p_pair.add_argument("--no-wait", action="store_true",
                        help="skip the Enter prompt and start polling at once "
                             "(for agents: confirm readiness first, then tell "
                             "the user to press the button)")
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

    # init + discover are self-contained (no venv, no scene-layers.py).
    if args.cmd == "init":
        return _cmd_init(args)
    if args.cmd == "discover":
        return _cmd_discover(args)

    require_bootstrap("hue-kit", feature="scene tooling")

    # pair needs requests (venv) but not scene-layers.py / a working dir.
    if args.cmd == "pair":
        return _cmd_pair(args)

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
