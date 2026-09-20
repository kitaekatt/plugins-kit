"""hue-kit CLI -- a small verb front-end over the layered Hue scene tool.

Subcommands (the common operations):

    start       THE DEFAULT. First run: build the registry + design, render the
                report, open it. Afterwards: check whether the bridge still
                matches the local YAML, and stop for a decision if it does not.
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

The YAML/HTML working files (scene-groups.yaml, scene-designs.yaml,
index.html) live in the plugin data directory
(~/.claude/plugins/data/plugins-kit/hue-kit) -- a single source of truth
regardless of where you run from. Point elsewhere with --dir, or per file
with the HUE_GROUPS_FILE / HUE_DESIGNS_FILE env vars.

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
try:
    from bootstrap_guard import plugin_venv_python  # noqa: E402
except ImportError:  # pragma: no cover - only an out-of-date vendored copy
    plugin_venv_python = None

reexec_under_plugin_venv("hue-kit")

def _is_windows() -> bool:
    """Platform test, behind a seam so tests can exercise both branches.

    Deliberately NOT an inline `os.name == "nt"`: pathlib reads `os.name` at
    call time to decide between PosixPath and WindowsPath, so monkeypatching it
    to cover the Windows branch on a POSIX runner breaks every Path() in this
    module. A function is the only substitutable surface. Local by design --
    bootstrap_guard has an identical seam, but that module is vendored
    byte-for-byte into eight locations under a drift test, so importing from it
    is not how it is consumed.
    """
    return os.name == "nt"


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
# Default home of the working files (scene-groups.yaml / scene-designs.yaml /
# index.html), so every verb sees the same files no matter the invocation cwd.
DEFAULT_WORKDIR = data_dir("hue-kit")

# scene-layers.py --validate-design's distinct exit code for "ran cleanly and
# found a real discrepancy" -- must mirror scene-layers.py's EXIT_DISCREPANCY
# (the two scripts do not import each other). Never 2 (argparse) or 3 (this
# script's own bootstrap-guard exit, require_bootstrap in main() below). The
# exit-code table in skills/hue-domain/references/scene-layers.md is the
# single documented copy of this value.
DISCREPANCY_EXIT_CODE = 4


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


def _write_text_atomic(path: Path, text: str, mode: int | None = None) -> None:
    """Write `text` to `path` without ever exposing a truncated or partial
    file: create a fresh `<path>.tmp` in the same directory, write, flush
    and fsync it, then `os.replace` it over `path` in one filesystem
    operation. On any failure the temp file is removed and `path`'s prior
    bytes are left untouched -- a reader never observes a half-written
    file. `mode` sets the temp file's permissions at the moment it is
    created (any pre-existing same-named temp file is removed first, so
    `mode` always governs a freshly created file rather than a chmod after
    the fact); `mode=None` uses the platform's normal create permissions.

    This function is duplicated byte-for-byte in scene-layers.py, since the
    two scripts do not import each other -- keep the two copies identical.
    """
    path = Path(path)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.unlink()
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(tmp_path, flags, 0o666 if mode is None else mode)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _cache_bridge_ip(ip: str) -> None:
    try:
        BRIDGE_IP_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(BRIDGE_IP_CACHE, ip + "\n")
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


def _workfile_path(workdir: Path, name: str, env_var: str) -> Path:
    """Resolve ONE working file's path once: env_var if set (absolutized),
    else <workdir>/<name> (absolutized). The single resolution rule for
    scene-groups.yaml / scene-designs.yaml -- used for existence checks,
    write targets, and (via _scene_layers_env) the child's env, so every
    caller agrees on where the file lives. A user-set relative override must
    keep meaning "relative to where I ran from", so it is resolved against
    the invocation cwd before anything below may chdir."""
    override = os.environ.get(env_var, "").strip()
    if override:
        return Path(override).resolve()
    return (workdir / name).resolve()


def _scene_layers_env(workdir: Path) -> dict:
    """Resolve the bridge + key and point scene-layers.py at the working dir's
    YAML. Shared by the exec and subprocess runners below."""
    os.environ["HUE_BRIDGE_IP"] = _resolve_bridge_ip()
    _resolve_key_file()
    env = os.environ
    env["HUE_GROUPS_FILE"] = str(_workfile_path(workdir, "scene-groups.yaml",
                                                "HUE_GROUPS_FILE"))
    env["HUE_DESIGNS_FILE"] = str(_workfile_path(workdir, "scene-designs.yaml",
                                                 "HUE_DESIGNS_FILE"))
    if env.get("HUE_KEY_FILE"):
        env["HUE_KEY_FILE"] = str(Path(env["HUE_KEY_FILE"]).resolve())
    workdir.mkdir(parents=True, exist_ok=True)
    return env


def _run_scene_layers(flags: list[str], workdir: Path) -> int:
    """Single-shot runner: exec scene-layers.py in place of this process. Used by
    the one-verb commands, where handing over the tty (and never returning) is
    exactly right.

    On Windows it delegates to _call_scene_layers and exits with its status."""
    if _is_windows():
        # Windows has no exec. CPython routes os.execv/os.execve through the CRT
        # _execv, which SPAWNS the replacement and terminates the caller
        # immediately: the parent returns exit 0 before the child has done any
        # work, and the child is orphaned rather than waited on. A caller reading
        # this process's stdout therefore sees an empty stream and a false
        # success. (Same defect, same cause, as the one fixed in
        # bootstrap_lib/bootstrap_guard.py::reexec_under_plugin_venv.)
        #
        # _call_scene_layers is already the spawn-and-wait path and is
        # semantically identical here: same env, `cwd=workdir` in place of the
        # chdir below, and capture=False so the child INHERITS this process's
        # stdio -- the tty stays attached, which is what the one-verb commands
        # need. Exit with the waited-for child's status so this single-shot
        # runner keeps its original "never returning" contract.
        rc, _ = _call_scene_layers(flags, workdir)
        sys.exit(rc)
    env = _scene_layers_env(workdir)
    # scene-layers.py writes its relative paths (the tmp/ apply backups) to the
    # cwd, so anchor the process in the working dir before handing over.
    os.chdir(workdir)
    argv = [sys.executable, str(SCENE_LAYERS), *flags]
    os.execve(sys.executable, argv, env)  # replaces this process


def _call_scene_layers(flags: list[str], workdir: Path, *,
                       capture: bool = False):
    """Chainable runner: run scene-layers.py as a SUBPROCESS and come back.

    `start` composes several scene-layers runs in one invocation, which the exec
    runner above cannot do -- it never returns. Returns (returncode, stdout);
    stdout is None unless capture.

    Only stdout is ever captured -- stderr always INHERITS this process's
    stderr, so a captured run's diagnostics (an unmatched --scene, a
    malformed registry, ...) reach the terminal/log immediately instead of
    being captured into `proc.stderr` and then discarded."""
    import subprocess
    env = _scene_layers_env(workdir)
    argv = [sys.executable, str(SCENE_LAYERS), *flags]
    proc = subprocess.run(argv, env=env, cwd=str(workdir), text=True,
                          stdout=subprocess.PIPE if capture else None)
    return proc.returncode, (proc.stdout if capture else None)


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
    try:
        import requests
        import urllib3
    except ImportError:
        require_bootstrap("hue-kit", force=True, missing="requests")
        raise  # pragma: no cover - require_bootstrap always exits the process
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
    # Non-interactive whenever there is no terminal on stdin (an agent running
    # this in the background), so the flow never depends on a FLAG the caller's
    # CLI might predate: a session's PATH keeps pointing at the version dir it
    # started with, so a mid-session update can leave `hue-kit` older than the
    # instructions telling an agent how to call it. --no-wait stays as an
    # explicit override for a TTY.
    if args.no_wait or not sys.stdin.isatty():
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
            # atomic write, 0600 from the moment the temp file is created --
            # no world-readable window, and a failed write leaves any prior
            # key file's bytes intact rather than truncated.
            try:
                _write_text_atomic(PAIRED_KEY_FILE, key + "\n", mode=0o600)
            except OSError as exc:
                # The bridge has ALREADY minted this key, and it is only in
                # memory here. Reporting success would lose it silently and
                # cost another link-button press, so a failed write is fatal
                # and names the path -- never a warning glued to "paired.".
                raise SystemExit(
                    f"hue-kit: paired, but could not save the application key "
                    f"to {PAIRED_KEY_FILE}: {exc}. The key is lost -- make that "
                    f"path writable and re-run `hue-kit pair`, pressing the "
                    f"link button again.")
            # Separate concern, separate message: the key IS saved. Whether
            # its mode took (an odd umask masks the create mode) is cosmetic.
            try:
                PAIRED_KEY_FILE.chmod(0o600)
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


def _open_report(path: Path) -> bool:
    """Open the rendered report in the user's default browser. Returns False if
    no browser could be launched (headless box, sandbox) -- a non-fatal outcome
    the caller reports, since the file is written either way."""
    import webbrowser
    try:
        return webbrowser.open(path.as_uri())
    except Exception:
        return False


def _cmd_start(args) -> int:
    """The default entry point: get the user to a current report in one command.

    Nine verdicts, distinguished by what already exists, whether the bridge
    still matches it, and whether each step actually succeeded:

      first-run        nothing here yet -> build the registry, materialise the
                        design, render, open. Nothing exists to overwrite, so
                        this is the one branch that writes without asking.
      accepted         --accept re-baselined the bridge's current shape as the
                        reference without touching any YAML.
      clean            bridge matches -> ensure a report exists; the caller
                        offers to view it or to make changes.
      changed          --validate-design ran cleanly and found a real
                        discrepancy (colour/brightness), or the fingerprint
                        shows the SHAPE moved -> report WHAT differs and stop.
                        Deliberately does not auto-export: a diff is ambiguous
                        between "the bridge changed" and "the user edited the
                        YAML and has not applied it yet", and guessing wrong
                        destroys whichever side was the real work. The caller
                        asks which way to sync.
      validate-failed  --validate-design did NOT finish comparing (a malformed
                        registry, an unmatched --scene, ...) -- distinct from
                        `changed`, which means it compared and found a diff.
      bridge-unreachable  the bridge could not be read at all (a fingerprint
                        read failed, or bridge/key resolution raised).
      setup-failed     a first-run step (registry/design/report) failed.
      render-failed    the design already matched (clean-equivalent) but
                        re-rendering the missing report failed.

    The final `hue-kit-verdict: <state>` line is the machine-readable handoff."""
    # Our prints interleave with those of the scene-layers.py subprocesses, which
    # write straight to the shared fd. Without this our output is block-buffered
    # when piped and lands after theirs, so the progress lines describe steps
    # that already scrolled past.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    workdir = Path(args.dir).resolve()
    groups_f = _workfile_path(workdir, "scene-groups.yaml", "HUE_GROUPS_FILE")
    designs_f = _workfile_path(workdir, "scene-designs.yaml", "HUE_DESIGNS_FILE")
    report_f = workdir / "index.html"
    fp_f = workdir / "bridge-fingerprint.txt"

    def verdict(state: str, rc: int = 0) -> int:
        print(f"\nhue-kit-verdict: {state}")
        return rc

    try:
        rc, fp_now = _call_scene_layers(["--fingerprint"], workdir, capture=True)
        if rc != 0:
            print("hue-kit: could not read the bridge -- run `hue-kit discover` / "
                  "`hue-kit pair` and check the connection.", file=sys.stderr)
            return verdict("bridge-unreachable", 1)
        fp_now = (fp_now or "").strip()

        if args.accept:
            # Re-baseline without touching the YAML. The escape hatch for a
            # shape change the user has reviewed and does not want reflected
            # locally -- otherwise `start` would keep reporting it, since a
            # shape change cannot be resolved by `apply` (that writes colours,
            # it cannot create a light).
            _write_text_atomic(fp_f, fp_now + "\n")
            print(f"Accepted the bridge's current shape as the reference "
                  f"({workdir / 'bridge-fingerprint.txt'}).")
            return verdict("accepted")

        # ---- exactly one working file: refuse, write nothing ------------
        # `start` writes unasked ONLY when there is nothing to lose. With one
        # file already here the missing one cannot be rebuilt safely in
        # either direction: regenerating the registry yields placeholder
        # group names the existing design does not reference, and
        # regenerating the design discards colour edits the user has not
        # applied (--export-designs has no exists guard, unlike
        # --export-groups). Name the missing file and the way forward.
        if groups_f.is_file() != designs_f.is_file():
            missing, present = ((designs_f, groups_f) if groups_f.is_file()
                                else (groups_f, designs_f))
            print(f"hue-kit: {present.name} is here but {missing.name} is "
                  f"missing, so this is not a first run and nothing was "
                  f"written.", file=sys.stderr)
            if missing is designs_f:
                print("  Rebuild the design from the registry: `hue-kit "
                      "export`.", file=sys.stderr)
            else:
                print(f"  Restore {missing.name} from wherever it went. To "
                      f"start over from the bridge instead, delete "
                      f"{present.name} first -- a rebuilt registry has "
                      f"placeholder group names.", file=sys.stderr)
            return verdict("incomplete", 1)

        # ---- first run: nothing local to lose, so build the whole chain ----
        if not groups_f.is_file() or not designs_f.is_file():
            print("No working files yet -- setting up from your bridge.\n")
            for label, flags in (
                    ("registry (scene-groups.yaml)", ["--export-groups", str(groups_f)]),
                    ("design (scene-designs.yaml)", ["--export-designs", str(designs_f)]),
                    ("report (index.html)", ["--html", str(report_f)])):
                print(f"  building the {label} ...")
                rc, _ = _call_scene_layers(flags, workdir)
                if rc != 0:
                    print(f"hue-kit: failed while building the {label}.",
                          file=sys.stderr)
                    return verdict("setup-failed", rc)
            _write_text_atomic(fp_f, fp_now + "\n")
            opened = _open_report(report_f) if args.open else False
            print(f"\nSet up in {workdir}")
            print(f"Report: {report_f}"
                  + ("  (opened in your browser)" if opened else ""))
            if args.open and not opened:
                print("  (could not launch a browser -- open the path above "
                      "manually)")
            print("\nThe group names are placeholders (G1, G2, ...). They work "
                  "as-is; rename them in\nscene-groups.yaml whenever a better "
                  "name suggests itself.")
            return verdict("first-run")

        # ---- established: has anything moved since we last looked? --------
        fp_old = fp_f.read_text().strip() if fp_f.is_file() else ""
        shape_changed = bool(fp_old) and fp_old != fp_now
        if not fp_old:
            # Working files predate fingerprinting (or it was deleted).
            # Establish the baseline rather than crying "changed" on no
            # evidence; the colour diff below still covers this run.
            _write_text_atomic(fp_f, fp_now + "\n")

        print("Checking your bridge against the local design ...\n")
        drift_rc, drift_out = _call_scene_layers(["--validate-design"], workdir,
                                                 capture=True)
        print((drift_out or "").rstrip())
        if drift_rc == 0:
            colours_changed = False
        elif drift_rc == DISCREPANCY_EXIT_CODE:
            colours_changed = True
        else:
            # Ran, but did not finish comparing (an unmatched --scene, a
            # malformed registry, ...) -- report the failure, distinct from a
            # completed comparison that found real drift.
            print(f"hue-kit: could not validate the design against the bridge "
                  f"(scene-layers exited {drift_rc}); see the diagnostic above.",
                  file=sys.stderr)
            return verdict("validate-failed", drift_rc)

        if shape_changed or colours_changed:
            print("\nYour bridge no longer matches the local design:")
            if shape_changed:
                print("  - the SHAPE changed (a light, zone, or scene was "
                      "added, removed, or renamed)")
            if colours_changed:
                print("  - scene colours/brightness differ (see the per-light "
                      "diff above)")
            print("\nNot changing anything yet: a difference can mean the "
                  "bridge moved, OR that\nthe local YAML holds edits that were "
                  "never applied. Those need opposite fixes.")
            return verdict("changed")

        if not report_f.is_file():
            print("Report missing -- re-rendering it.")
            rc, _ = _call_scene_layers(["--html", str(report_f)], workdir)
            if rc != 0:
                return verdict("render-failed", rc)

        print(f"\nBridge matches the local design. Report: {report_f}")
        return verdict("clean")
    except SystemExit as e:
        # Bridge/key resolution (inside _scene_layers_env, reached from every
        # _call_scene_layers above) raises SystemExit("hue-kit: ...") rather
        # than returning a code -- without this it would escape _cmd_start (and
        # main()) with no `hue-kit-verdict:` line at all. Any raise reachable
        # from this function is a bridge-resolution failure; there is no other
        # SystemExit source in this body.
        msg = str(e)
        if msg:
            print(msg, file=sys.stderr)
        return verdict("bridge-unreachable", 1)


def _cmd_init(args) -> int:
    # The init positional wins if given; otherwise fall back to the shared --dir.
    dest = Path(args.init_dir or args.dir).resolve()
    # REFUSED into the live working directory. These are the AUTHOR's registry
    # and design -- their home's zones and scenes -- useful as a worked example
    # and never as live data. Landing them in the working directory makes both
    # YAML files exist, so `start` reads an established workdir, skips first
    # run, and every verb then fails on zones no bridge has. Give it somewhere
    # to read from instead.
    if dest == Path(DEFAULT_WORKDIR).resolve():
        print(f"hue-kit: refusing to write the bundled example into the live "
              f"working directory ({dest}).\n"
              f"  These examples are one author's home, not a starting point "
              f"for yours: with both YAML files present `hue-kit start` treats "
              f"the directory as already set up and every verb then fails on "
              f"zones your bridge does not have.\n"
              f"  Read them somewhere else -- `hue-kit init ~/hue-example` -- "
              f"or build from your own bridge with `hue-kit start`.",
              file=sys.stderr)
        return 2
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
    parser.add_argument("--dir", default=str(DEFAULT_WORKDIR), metavar="PATH",
                        help="working directory for the YAML/HTML files "
                             f"(default: the plugin data dir, {DEFAULT_WORKDIR})")
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
    p_start = sub.add_parser("start", help="Default entry point: set up on "
                                           "first run (renders + opens the "
                                           "report); otherwise check the "
                                           "bridge against the local design "
                                           "and print a hue-kit-verdict: "
                                           "state -- nothing else is written.")
    p_start.add_argument("--no-open", dest="open", action="store_false",
                         help="render the report but do not launch a browser")
    p_start.add_argument("--accept", action="store_true",
                         help="record the bridge's current shape as the "
                              "reference without changing any YAML (clears a "
                              "reviewed 'shape changed' report)")
    sub.add_parser("report", help="Read the bridge; print the minimal group "
                                  "family + each scene as a layer stack.")
    p_groups = sub.add_parser("groups", help="Write a starter scene-groups.yaml "
                                             "(placeholder names to rename).")
    p_groups.add_argument("path", nargs="?",
                          help="output path (default: <dir>/scene-groups.yaml)")
    p_groups.add_argument("--force", action="store_true",
                          help="overwrite an existing registry (this "
                               "regenerates placeholder names and discards "
                               "any you set)")
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
                        help="destination directory (default: --dir, else the "
                             "plugin data dir)")
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

    # Every remaining verb hands off to scene-layers.py, in place (exec) or as
    # a subprocess. The bootstrap-log check above says the plugin was
    # provisioned at least once; it says nothing about whether the venv built
    # then is still there to run the child under. Confirm the interpreter
    # exists first, so a missing venv is reported with the same message
    # instead of the child dying partway through with a bare
    # ModuleNotFoundError.
    if plugin_venv_python is None or plugin_venv_python("hue-kit") is None:
        require_bootstrap("hue-kit", feature="scene tooling", force=True)

    if args.cmd == "start":
        return _cmd_start(args)
    if args.cmd == "report":
        return _run_scene_layers([], workdir)
    if args.cmd == "groups":
        # User paths resolve against the invocation cwd before _run_scene_layers
        # changes the POSIX cwd or launches the child with this cwd. With no
        # explicit path, fall back to the one resolution rule (HUE_GROUPS_FILE
        # override, else <dir>/scene-groups.yaml) so this write target agrees
        # with everything else that reads/writes the registry.
        out_path = Path(args.path).resolve() if args.path else \
            _workfile_path(workdir, "scene-groups.yaml", "HUE_GROUPS_FILE")
        # Refuse before _run_scene_layers -- it execve()s and never returns on
        # POSIX, so this is the only point that can still stop the write.
        # scene-layers.py carries the same guard for a standalone invocation.
        if out_path.exists() and not args.force:
            print(f"hue-kit: {out_path} already exists -- pass --force to "
                  "overwrite (this regenerates placeholder group names and "
                  "discards any you set)", file=sys.stderr)
            return 1
        flags = ["--export-groups", str(out_path)]
        if args.force:
            flags.append("--force")
        return _run_scene_layers(flags, workdir)
    if args.cmd == "export":
        # Re-baseline the shape fingerprint alongside the design: export IS the
        # pull, so afterwards the local files reflect the bridge as it is now.
        # Without this a shape change stays reported forever -- the user fixes
        # it the only way they can, and `start` keeps insisting it is broken.
        designs_f = _workfile_path(workdir, "scene-designs.yaml", "HUE_DESIGNS_FILE")
        rc, _ = _call_scene_layers(["--export-designs", str(designs_f)], workdir)
        if rc == 0:
            frc, fp = _call_scene_layers(["--fingerprint"], workdir, capture=True)
            if frc == 0 and (fp or "").strip():
                _write_text_atomic(workdir / "bridge-fingerprint.txt", fp.strip() + "\n")
            else:
                print("hue-kit: exported the design, but could not re-baseline "
                      f"bridge-fingerprint.txt (scene-layers --fingerprint "
                      f"exited {frc}) -- `start` may report a stale shape "
                      "change until this is retried.", file=sys.stderr)
        return rc
    if args.cmd == "render":
        out = str(Path(args.path).resolve()) if args.path else str(workdir / "index.html")
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
