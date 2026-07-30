#!/usr/bin/env python3
"""claude-explorer: browsable HTML view of ~/.claude/ + project.

Two phases:
  crawl  -- walk roots, compute deterministic summary projections, write index.json.
  serve  -- local HTTP server (127.0.0.1) serving the SPA + index + file endpoint.
  run    -- crawl, then serve, then open browser.

Read-only v1. LLM-generated summaries (Haiku via `claude -p`) are stubbed; the
hook exists but is disabled by default. See SKILL.md for the design rationale.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import http.server
import json
import os
import pathlib
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from typing import Any

try:
    import yaml  # PyYAML
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


HOME = pathlib.Path.home()
CLAUDE_DIR = HOME / ".claude"
DATA_DIR = CLAUDE_DIR / ".local-data" / "prototypes" / "claude-explorer"
INDEX_PATH = DATA_DIR / "index.json"
CACHE_DIR = DATA_DIR / "cache"
DEFAULT_PORT = 8923


# ----------------------------------------------------------------------------
# Hashing + cache
# ----------------------------------------------------------------------------

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def safe_read_bytes(p: pathlib.Path, limit: int = 2_000_000) -> bytes | None:
    try:
        return p.read_bytes()[:limit]
    except (OSError, PermissionError):
        return None


def safe_read_text(p: pathlib.Path, limit: int = 2_000_000) -> str | None:
    b = safe_read_bytes(p, limit)
    if b is None:
        return None
    try:
        return b.decode("utf-8", errors="replace")
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Frontmatter and structured extraction
# ----------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.+?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    raw = m.group(1)
    if HAVE_YAML:
        try:
            data = yaml.safe_load(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    # fallback: line-by-line key: value
    out = {}
    for line in raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def first_heading(text: str) -> str | None:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return None


def first_lines(text: str, n: int = 2) -> str:
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip() and not ln.startswith("---")]
    return "\n".join(lines[:n])


def count_tokens_approx(text: str) -> int:
    # cheap approximation: words * 1.3
    return int(len(text.split()) * 1.3)


# ----------------------------------------------------------------------------
# Per-primitive summary projections (deterministic)
# ----------------------------------------------------------------------------

def project_skill_md(p: pathlib.Path) -> dict:
    text = safe_read_text(p) or ""
    fm = parse_frontmatter(text)
    body_after = FRONTMATTER_RE.sub("", text, count=1)
    return {
        "kind": "skill_md",
        "name": fm.get("name"),
        "description": fm.get("description"),
        "skill_type": fm.get("skill-type"),
        "author": fm.get("author"),
        "body_lines": body_after.count("\n") + 1 if body_after else 0,
        "body_tokens": count_tokens_approx(body_after),
    }


def project_claude_md(p: pathlib.Path) -> dict:
    text = safe_read_text(p) or ""
    h1 = first_heading(text)
    # try to detect structured scope block
    scope_dir = None
    scope_covers = []
    m = re.search(r"^\s*scope:\s*\n((?:^[ \t]+.*\n?)+)", text, re.MULTILINE)
    if m and HAVE_YAML:
        try:
            scope = yaml.safe_load("scope:\n" + m.group(1))
            scope = scope.get("scope") if isinstance(scope, dict) else None
            if isinstance(scope, dict):
                scope_dir = scope.get("directory")
                covers = scope.get("covers", [])
                if isinstance(covers, list):
                    scope_covers = covers[:5]
        except Exception:
            pass
    return {
        "kind": "claude_md",
        "first_heading": h1,
        "scope_directory": scope_dir,
        "scope_covers": scope_covers,
        "lines": text.count("\n") + 1 if text else 0,
    }


def project_reference_doc(p: pathlib.Path) -> dict:
    text = safe_read_text(p) or ""
    return {
        "kind": "reference_doc",
        "filename": p.name,
        "first_heading": first_heading(text),
        "first_lines": first_lines(text, 2),
        "lines": text.count("\n") + 1 if text else 0,
    }


def project_plain_md(p: pathlib.Path) -> dict:
    text = safe_read_text(p) or ""
    return {
        "kind": "plain_md",
        "filename": p.name,
        "first_heading": first_heading(text),
        "lines": text.count("\n") + 1 if text else 0,
    }


def project_plugin_manifest(p: pathlib.Path) -> dict:
    try:
        data = json.loads(safe_read_text(p) or "{}")
    except Exception:
        data = {}
    return {
        "kind": "plugin_manifest",
        "name": data.get("name"),
        "version": data.get("version"),
        "description": data.get("description"),
        "razor": data.get("razor"),
    }


def project_marketplace_manifest(p: pathlib.Path) -> dict:
    try:
        data = json.loads(safe_read_text(p) or "{}")
    except Exception:
        data = {}
    plugins = data.get("plugins") or []
    return {
        "kind": "marketplace_manifest",
        "name": data.get("name"),
        "plugin_count": len(plugins) if isinstance(plugins, list) else 0,
    }


def project_bootstrap_manifest(p: pathlib.Path) -> dict:
    try:
        data = json.loads(safe_read_text(p) or "{}")
    except Exception:
        data = {}
    venv = data.get("venv") or {}
    imports = venv.get("check_imports") or []
    tools = data.get("tools") or []
    return {
        "kind": "bootstrap_manifest",
        "check_imports": imports if isinstance(imports, list) else [],
        "tool_count": len(tools) if isinstance(tools, list) else 0,
    }


def project_script(p: pathlib.Path) -> dict:
    text = safe_read_text(p, limit=20_000) or ""
    docstring = None
    if p.suffix == ".py":
        m = re.match(r'\A(?:#![^\n]*\n)?(?:from __future__[^\n]*\n)?\s*"""(.*?)"""', text, re.DOTALL)
        if m:
            docstring = m.group(1).strip().splitlines()[0].strip()
    elif p.suffix in (".sh", ".bash"):
        for line in text.splitlines()[:10]:
            s = line.strip()
            if s.startswith("#") and not s.startswith("#!"):
                docstring = s.lstrip("#").strip()
                break
    return {
        "kind": "script",
        "filename": p.name,
        "language": p.suffix.lstrip("."),
        "leading_doc": docstring,
        "lines": text.count("\n") + 1 if text else 0,
    }


# ----------------------------------------------------------------------------
# Composition detection
# ----------------------------------------------------------------------------

def detect_composition(d: pathlib.Path) -> str:
    """Return composition id for a directory."""
    if (d / ".claude-plugin" / "marketplace.json").exists():
        return "marketplace"
    if (d / ".claude-plugin" / "plugin.json").exists():
        return "plugin"
    if (d / "SKILL.md").exists():
        return "skill"
    return "directory"


# ----------------------------------------------------------------------------
# Crawl
# ----------------------------------------------------------------------------

IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", "dist", "build"}
MAX_DEPTH = 12


def walk(root: pathlib.Path, max_depth: int = MAX_DEPTH, context: dict | None = None) -> dict:
    """Walk a root composition tree. Returns a tree node dict."""
    if not root.exists():
        return {"kind": "missing", "path": str(root)}
    return _walk_dir(root, depth=0, max_depth=max_depth, context=context or {})


def _walk_dir(d: pathlib.Path, depth: int, max_depth: int, context: dict) -> dict:
    composition = detect_composition(d)
    node: dict[str, Any] = {
        "kind": composition,
        "path": str(d),
        "name": d.name,
        "children": [],
        "files": [],
    }
    # composition-level projections + context tagging
    new_context = dict(context)
    if composition == "marketplace":
        proj = project_marketplace_manifest(d / ".claude-plugin" / "marketplace.json")
        mkt_name = proj.get("name") or d.name
        new_context["marketplace"] = mkt_name
        node["projection"] = proj
        node["marketplace_name"] = mkt_name
    elif composition == "plugin":
        proj = project_plugin_manifest(d / ".claude-plugin" / "plugin.json")
        plugin_name = proj.get("name") or d.name
        new_context["plugin"] = plugin_name
        node["projection"] = proj
        node["plugin_name"] = plugin_name
        node["marketplace_name"] = context.get("marketplace")
        bs = d / "bootstrap.json"
        if bs.exists():
            node["bootstrap"] = project_bootstrap_manifest(bs)
    elif composition == "skill":
        proj = project_skill_md(d / "SKILL.md")
        node["projection"] = proj
        skill_name = proj.get("name") or d.name
        node["plugin_name"] = context.get("plugin")
        node["marketplace_name"] = context.get("marketplace")
        if context.get("plugin"):
            node["slash_command"] = f"/{context['plugin']}:{skill_name}"
        else:
            node["slash_command"] = f"/{skill_name}"
    if depth >= max_depth:
        return node
    try:
        entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except (PermissionError, OSError):
        return node
    for child in entries:
        if child.name.startswith(".") and child.name not in {".claude", ".claude-plugin"}:
            continue
        if child.name in IGNORE_DIRS:
            continue
        if child.is_dir():
            child_node = _walk_dir(child, depth + 1, max_depth, new_context)
            # Skip plain directories with no compositional value at deeper levels
            if child_node["kind"] == "directory" and not child_node["children"] and not child_node["files"]:
                continue
            node["children"].append(child_node)
        elif child.is_file():
            file_node = _project_file(child)
            if file_node:
                node["files"].append(file_node)
    return node


def _project_file(p: pathlib.Path) -> dict | None:
    name = p.name
    suffix = p.suffix.lower()
    rel_kind = None
    proj: dict = {}
    if name == "SKILL.md":
        # SKILL.md is part of its parent skill composition's projection, not a separate file node
        return None
    if name == "CLAUDE.md":
        proj = project_claude_md(p)
        rel_kind = "claude_md"
    elif name == "plugin.json" and p.parent.name == ".claude-plugin":
        return None  # part of plugin composition
    elif name == "marketplace.json" and p.parent.name == ".claude-plugin":
        return None  # part of marketplace composition
    elif name == "bootstrap.json":
        return None  # part of plugin's bootstrap field
    elif suffix == ".md":
        # if we're inside a skill's references/, classify as reference_doc; otherwise plain
        parents = [par.name for par in p.parents]
        if "references" in parents:
            proj = project_reference_doc(p)
            rel_kind = "reference_doc"
        else:
            proj = project_plain_md(p)
            rel_kind = "plain_md"
    elif suffix in (".py", ".sh", ".bash", ".ps1", ".bat"):
        proj = project_script(p)
        rel_kind = "script"
    elif suffix == ".json":
        proj = {"kind": "json", "filename": name}
        rel_kind = "json"
    elif suffix in (".yaml", ".yml"):
        proj = {"kind": "yaml", "filename": name}
        rel_kind = "yaml"
    else:
        return None  # skip unknown file types in v1
    return {
        "kind": rel_kind,
        "path": str(p),
        "name": name,
        "projection": proj,
    }


def crawl(project_root: pathlib.Path | None) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    roots = []
    # Claude user dir root
    if CLAUDE_DIR.exists():
        user_root = walk(CLAUDE_DIR / "plugins" / "marketplaces") if (CLAUDE_DIR / "plugins" / "marketplaces").exists() else {"kind": "missing", "path": str(CLAUDE_DIR / "plugins" / "marketplaces")}
        user_skills = walk(CLAUDE_DIR / "skills") if (CLAUDE_DIR / "skills").exists() else None
        roots.append({
            "kind": "claude_user_dir",
            "name": "Claude user directory",
            "path": str(CLAUDE_DIR),
            "marketplaces": user_root,
            "user_skills": user_skills,
        })
    # Project root
    if project_root and project_root.exists():
        roots.append({
            "kind": "project",
            "name": project_root.name,
            "path": str(project_root),
            "tree": walk(project_root),
        })
    index = {
        "version": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "roots": roots,
    }
    INDEX_PATH.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


# ----------------------------------------------------------------------------
# Serve
# ----------------------------------------------------------------------------

class Handler(http.server.SimpleHTTPRequestHandler):
    """Request handler. make_server() binds the per-server state below onto a
    subclass, so allowed roots/hosts are instance state of one server -- they
    do not accumulate across serve() calls (they used to live in a module
    global that only ever grew)."""

    project_root: pathlib.Path | None = None
    allowed_roots: tuple[pathlib.Path, ...] = ()
    allowed_hosts: frozenset[str] = frozenset()

    def log_message(self, format, *args):
        pass  # quiet

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # DNS-rebinding guard: a hostile page can point its own hostname at
        # 127.0.0.1 and read responses same-origin. Only honor requests whose
        # Host header names this server directly.
        host = (self.headers.get("Host") or "").strip()
        if host not in self.allowed_hosts:
            self._send(403, b'{"error":"forbidden host"}', "application/json")
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send(200, HTML.encode("utf-8"))
        elif parsed.path == "/index.json":
            if INDEX_PATH.exists():
                self._send(200, INDEX_PATH.read_bytes(), "application/json")
            else:
                self._send(404, b'{"error":"no index; run crawl"}', "application/json")
        elif parsed.path == "/file":
            self._serve_file(urllib.parse.parse_qs(parsed.query))
        elif parsed.path == "/refresh":
            try:
                crawl(self.project_root)
                self._send(200, b'{"ok":true}', "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def _serve_file(self, qs: dict):
        paths = qs.get("path", [])
        if not paths:
            self._send(400, b'{"error":"missing path"}', "application/json")
            return
        requested = pathlib.Path(paths[0]).resolve()
        # path-traversal guard: requested must be within allowed_roots
        ok = False
        for root in self.allowed_roots:
            try:
                requested.relative_to(root.resolve())
                ok = True
                break
            except ValueError:
                continue
        if not ok or not requested.exists() or not requested.is_file():
            self._send(403, b'{"error":"forbidden or not found"}', "application/json")
            return
        if requested.stat().st_size > 5_000_000:
            self._send(413, b'{"error":"file too large"}', "application/json")
            return
        try:
            body = requested.read_bytes()
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return
        # content-type
        ct = "text/plain; charset=utf-8"
        if requested.suffix.lower() in (".json",):
            ct = "application/json"
        elif requested.suffix.lower() in (".md",):
            ct = "text/markdown; charset=utf-8"
        self._send(200, body, ct)


def make_server(project_root: pathlib.Path | None, port: int = DEFAULT_PORT) -> socketserver.TCPServer:
    """Build a localhost-bound server with per-server allowed roots/hosts."""
    roots = [CLAUDE_DIR]
    if project_root:
        roots.append(project_root)
    handler = type("BoundHandler", (Handler,), {
        "project_root": project_root,
        "allowed_roots": tuple(r.resolve() for r in roots),
    })
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    actual_port = httpd.server_address[1]  # resolves port=0 (tests) to the real one
    handler.allowed_hosts = frozenset({f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"})
    return httpd


def serve(project_root: pathlib.Path, port: int = DEFAULT_PORT, open_browser: bool = True):
    with make_server(project_root, port) as httpd:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        print(f"claude-explorer serving at {url}")
        if open_browser:
            threading.Thread(target=lambda: (time.sleep(0.4), webbrowser.open(url)), daemon=True).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")


# ----------------------------------------------------------------------------
# Embedded SPA (HTML + CSS + JS)
# ----------------------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>claude-explorer</title>
<style>
:root {
  --base: #1e1e2e;
  --mantle: #181825;
  --crust: #11111b;
  --surface0: #313244;
  --surface1: #45475a;
  --surface2: #585b70;
  --text: #cdd6f4;
  --subtext1: #bac2de;
  --subtext0: #a6adc8;
  --overlay0: #6c7086;
  --blue: #89b4fa;
  --lavender: #b4befe;
  --green: #a6e3a1;
  --yellow: #f9e2af;
  --peach: #fab387;
  --red: #f38ba8;
  --mauve: #cba6f7;
  --pink: #f5c2e7;
  --teal: #94e2d5;
  --sky: #89dceb;
  --rosewater: #f5e0dc;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.45;
  background: var(--base);
  color: var(--text);
  min-height: 100vh;
}
header {
  background: var(--mantle);
  border-bottom: 1px solid var(--surface0);
  padding: 6px 12px;
  display: flex;
  align-items: center;
  gap: 12px;
  position: sticky; top: 0; z-index: 10;
  font-size: 12px;
}
header .title { font-weight: 600; color: var(--lavender); }
header .hints { color: var(--overlay0); font-size: 10.5px; }
header .meta { color: var(--overlay0); margin-left: auto; font-size: 10.5px; }
main {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: var(--surface0);
  min-height: calc(100vh - 42px);
}
.root-pane { background: var(--base); padding: 12px 16px; overflow: auto; }
.root-pane h2 {
  margin: 0 0 8px 0;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--overlay0);
  font-weight: 500;
}
.node {
  border-left: 1px solid var(--surface0);
  padding-left: 6px;
  margin: 1px 0 1px 3px;
}
.node-row {
  display: flex;
  align-items: baseline;
  cursor: pointer;
  gap: 5px;
  padding: 1px 3px;
  border-radius: 0;
}
.node-row:hover { background: var(--surface0); }
/* .focused is the keyboard cursor -- only paint it when keyboard owns input. */
body.mouse-off .node-row.focused { background: var(--surface1); outline: 1px solid var(--lavender); outline-offset: -1px; }
.node-marker {
  display: inline-block;
  width: 0.9em;
  color: var(--overlay0);
  text-align: center;
  font-size: 10px;
}
.node.open > .node-row > .node-marker.has-children::before { content: "▾"; color: var(--blue); }
.node.open > .node-row > .node-marker.has-action::before { content: "▾"; color: var(--peach); }
.node-marker.has-children::before { content: "▸"; }
.node-marker.has-action::before { content: "▸"; color: var(--mauve); }
.node-marker.is-file::before { content: "·"; }
.node-kind {
  display: inline-block;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  padding: 1px 5px;
  border: 1px solid var(--surface1);
  color: var(--subtext0);
  margin-right: 4px;
}
.kind-marketplace .node-kind { color: var(--peach); border-color: var(--peach); }
.kind-plugin .node-kind { color: var(--blue); border-color: var(--blue); }
.kind-skill .node-kind { color: var(--green); border-color: var(--green); }
.kind-claude_md .node-kind { color: var(--yellow); border-color: var(--yellow); }
.kind-reference_doc .node-kind { color: var(--teal); border-color: var(--teal); }
.kind-plain_md .node-kind { color: var(--subtext0); border-color: var(--surface1); }
.kind-script .node-kind { color: var(--mauve); border-color: var(--mauve); }
.kind-json .node-kind { color: var(--pink); border-color: var(--pink); }
.kind-yaml .node-kind { color: var(--rosewater); border-color: var(--rosewater); }
.kind-directory .node-kind { color: var(--overlay0); border-color: var(--surface0); }
.kind-claude_user_dir .node-kind, .kind-project .node-kind { color: var(--lavender); border-color: var(--lavender); }
.node-name { color: var(--text); }
.node-name.is-file { color: var(--subtext1); }
.node-meta { color: var(--overlay0); font-size: 11px; margin-left: 4px; }
.node-meta strong { color: var(--subtext0); font-weight: 400; }
.node-summary { color: var(--subtext0); padding-left: 22px; padding-bottom: 2px; font-size: 11.5px; }
.node-summary .label { color: var(--overlay0); }
.node-children { margin-left: 6px; display: none; }
.node.open > .node-children { display: block; }
.deep { display: none; padding: 10px; background: var(--mantle); border-left: 1px solid var(--surface1); margin: 2px 0 4px 22px; max-height: 60vh; overflow: auto; }
.deep.open { display: block; }
/* Walker -- summoned launcher overlay (jump + actions modes) */
#walker { display: none; position: fixed; inset: 0; background: rgba(17,17,27,0.78); z-index: 200; align-items: flex-start; justify-content: center; padding-top: 10vh; }
#walker.open { display: flex; }
.walker-box { background: var(--mantle); border: 1px solid var(--lavender); width: 600px; max-width: 90vw; max-height: 70vh; display: flex; flex-direction: column; }
.walker-title { color: var(--lavender); padding: 6px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; border-bottom: 1px solid var(--surface0); background: var(--crust); }
.walker-subtitle { color: var(--subtext1); padding: 6px 12px; font-size: 11.5px; border-bottom: 1px solid var(--surface0); background: var(--mantle); white-space: pre-wrap; }
.walker-input { background: var(--base); color: var(--text); border: 0; padding: 8px 12px; font-family: inherit; font-size: 13px; outline: none; border-bottom: 1px solid var(--surface0); }
.walker-list { list-style: none; margin: 0; padding: 0; overflow: auto; flex: 1; }
.walker-list li { padding: 4px 12px; display: flex; gap: 12px; align-items: baseline; cursor: pointer; }
/* Walker .focused is the keyboard cursor -- only paint when keyboard owns input. */
body.mouse-off .walker-list li.focused { background: var(--surface1); border-left: 2px solid var(--peach); padding-left: 10px; }
.walker-list li:hover { background: var(--surface0); }
.walker-label { color: var(--text); }
.walker-meta { color: var(--overlay0); font-size: 11px; margin-left: auto; }
.walker-desc { color: var(--subtext0); font-size: 11.5px; }
.walker-foot { color: var(--overlay0); font-size: 10.5px; padding: 4px 12px; border-top: 1px solid var(--surface0); background: var(--crust); }

/* Mako-style toast */
#toast { position: fixed; bottom: 16px; right: 16px; background: var(--mantle); border-left: 2px solid var(--green); color: var(--text); padding: 8px 14px; font-size: 11.5px; z-index: 300; display: none; max-width: 360px; }
#toast.show { display: block; animation: toast-in 120ms ease-out; }
@keyframes toast-in { from { transform: translateY(8px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
.deep pre { margin: 0; white-space: pre-wrap; word-wrap: break-word; color: var(--subtext1); }
.deep .md h1, .deep .md h2, .deep .md h3 { color: var(--lavender); margin: 0.8em 0 0.4em 0; }
.deep .md h1 { font-size: 18px; }
.deep .md h2 { font-size: 15px; border-bottom: 1px solid var(--surface0); padding-bottom: 4px; }
.deep .md h3 { font-size: 13px; color: var(--blue); }
.deep .md code { color: var(--peach); background: var(--surface0); padding: 1px 4px; }
.deep .md pre { background: var(--crust); padding: 8px; border-left: 2px solid var(--surface1); }
.deep .md pre code { background: transparent; color: var(--subtext1); padding: 0; }
.deep .md a { color: var(--sky); }
.deep .md table { border-collapse: collapse; margin: 8px 0; }
.deep .md th, .deep .md td { border: 1px solid var(--surface1); padding: 4px 8px; text-align: left; }
.deep .md th { background: var(--surface0); color: var(--text); }
.deep .md blockquote { border-left: 2px solid var(--mauve); padding-left: 12px; color: var(--subtext0); margin: 8px 0; }
.deep table.kv { border-collapse: collapse; }
.deep table.kv td { padding: 2px 12px 2px 0; vertical-align: top; }
.deep table.kv td:first-child { color: var(--overlay0); }
.search {
  background: var(--surface0);
  color: var(--text);
  border: 1px solid var(--surface1);
  padding: 4px 8px;
  font-family: inherit;
  font-size: 12px;
  width: 240px;
}
.empty { color: var(--overlay0); padding: 16px; font-style: italic; }
footer {
  background: var(--mantle);
  border-top: 1px solid var(--surface0);
  padding: 6px 16px;
  color: var(--overlay0);
  font-size: 11px;
  text-align: right;
}
.kbd {
  font-size: 10px;
  color: var(--subtext0);
  border: 1px solid var(--surface1);
  padding: 1px 4px;
  background: var(--surface0);
  margin: 0 2px;
}
/* Vim-style: keyboard input parks the mouse until it physically moves.
 * Hide the cursor (incl. all descendants -- Chromium honors per-element
 * cursor over the parent's), disable hit-testing on every descendant, and
 * explicitly neutralize the sticky :hover state that Chromium/Firefox keep
 * painting until the next real mousemove. Use !important to win against any
 * existing specific :hover rule. Only keyboard-driven .focused state remains. */
body.mouse-off, body.mouse-off * { cursor: none !important; }
body.mouse-off * { pointer-events: none !important; }
body.mouse-off .node-row:hover,
body.mouse-off .walker-list li:hover { background: transparent !important; }
body.mouse-off .walker-list li.focused:hover { background: var(--surface1) !important; }
body.mouse-off .action-cmd:hover { background: var(--crust) !important; border-color: var(--surface1) !important; }
/* Visible state badge: lights up when keyboard owns input. */
.mouse-badge { display: inline-block; width: 7px; height: 7px; background: var(--overlay0); border: 1px solid var(--surface1); margin-right: 6px; vertical-align: middle; }
body.mouse-off .mouse-badge { background: var(--green); border-color: var(--green); }
#help-overlay {
  position: fixed; inset: 0; background: rgba(17,17,27,0.85); z-index: 100;
  display: none; align-items: center; justify-content: center;
}
#help-overlay.open { display: flex; }
.help-box {
  background: var(--mantle); border: 1px solid var(--lavender); padding: 16px 20px;
  max-width: 560px; color: var(--text);
}
.help-box h3 { margin: 6px 0 6px 0; color: var(--lavender); font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 500; }
.help-box table { width: 100%; border-collapse: collapse; }
.help-box td { padding: 2px 8px; vertical-align: top; }
.help-box td:first-child { color: var(--subtext0); white-space: nowrap; }
.help-foot { margin-top: 10px; color: var(--overlay0); font-size: 10.5px; text-align: right; }
.help-box .badge { display: inline-block; min-width: 1.4em; text-align: center; color: var(--peach); }
</style>
</head>
<body>
<header>
  <span class="mouse-badge" title="green = keyboard in control (mouse parked); dim = mouse in control"></span>
  <span class="title">claude-explorer</span>
  <span class="hints"><span class="kbd">/</span> jump <span class="kbd">a</span> actions <span class="kbd">r</span> refresh <span class="kbd">?</span> help</span>
  <span class="meta" id="meta">loading...</span>
</header>
<main>
  <section class="root-pane" id="left"><h2>Claude user directory</h2><div id="left-body" class="empty">loading...</div></section>
  <section class="root-pane" id="right"><h2>Project</h2><div id="right-body" class="empty">loading...</div></section>
</main>
<div id="walker" style="display:none">
  <div class="walker-box">
    <div class="walker-title" id="walker-title">jump</div>
    <div class="walker-subtitle" id="walker-subtitle" style="display:none"></div>
    <input class="walker-input" id="walker-input" placeholder="type to filter" autocomplete="off">
    <ul class="walker-list" id="walker-list"></ul>
    <div class="walker-foot"><span class="kbd">↑↓</span> navigate &nbsp; <span class="kbd">Enter</span> select &nbsp; <span class="kbd">Esc</span> close</div>
  </div>
</div>
<div id="toast"></div>
<div id="help-overlay" style="display:none">
  <div class="help-box">
    <h3>navigation</h3>
    <table>
      <tr><td><span class="kbd">j</span> <span class="kbd">↓</span></td><td>focus next</td></tr>
      <tr><td><span class="kbd">k</span> <span class="kbd">↑</span></td><td>focus previous</td></tr>
      <tr><td><span class="kbd">Enter</span> <span class="kbd">Space</span></td><td>open / close the focused row</td></tr>
      <tr><td><span class="kbd">Esc</span></td><td>collapse every open node and deep-render</td></tr>
    </table>
    <h3>summoned overlays</h3>
    <table>
      <tr><td><span class="kbd">/</span></td><td>jump -- fuzzy-find any node, Enter to scroll-to-and-focus</td></tr>
      <tr><td><span class="kbd">a</span></td><td>actions -- command palette for the focused node (run / update / path / ...)</td></tr>
      <tr><td><span class="kbd">r</span></td><td>refresh (re-crawl)</td></tr>
      <tr><td><span class="kbd">?</span></td><td>toggle this help</td></tr>
    </table>
    <h3>markers</h3>
    <table>
      <tr><td>▸ blue</td><td>container with children -- click or Enter to expand</td></tr>
      <tr><td>▸ mauve</td><td>composition with actions -- press a to summon the action palette</td></tr>
      <tr><td>· dim</td><td>file -- click to deep-render its contents inline</td></tr>
    </table>
    <div class="help-foot"><span class="kbd">Esc</span> or <span class="kbd">?</span> closes this overlay</div>
  </div>
</div>
<script>
// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let INDEX = null;
let NODES = []; // flat list of visible node DOM elements for j/k nav
let focused = -1;
const ACTION_PAYLOADS = {}; // id -> { subtitle, actions }

// Walker (summoned overlay) state -- modes: "jump" | "actions"
let walkerMode = null;
let walkerItems = [];      // full unfiltered item list
let walkerFiltered = [];   // current filtered list
let walkerCursor = 0;

// Mouse parking -- keyboard input hides the cursor + disables hover/click until
// the mouse physically moves. Standard Vim / terminal-UI convention.
let mouseOff = false;
let lastMx = -1, lastMy = -1;
// What the mouse was last hovering, so the keyboard can pick up where the mouse left off.
let hoveredRow = null;        // .node-row element
let hoveredWalkerIdx = null;  // integer index in walkerFiltered

function setMouseOff(off) {
  if (off === mouseOff) return;
  mouseOff = off;
  document.body.classList.toggle("mouse-off", off);
  if (!off) return; // mouse re-engaging: just hide the focused outline; keep the index for later
  // mouse -> keyboard transition: promote whatever the cursor was over to the keyboard cursor.
  if (walkerMode && hoveredWalkerIdx != null && hoveredWalkerIdx < walkerFiltered.length) {
    walkerCursor = hoveredWalkerIdx;
    walkerRender(document.getElementById("walker-input").value);
  } else if (!walkerMode) {
    if (hoveredRow) {
      rebuildNodeList();
      const i = NODES.indexOf(hoveredRow);
      if (i >= 0) {
        if (focused >= 0 && focused < NODES.length && focused !== i) NODES[focused].classList.remove("focused");
        focused = i;
        NODES[i].classList.add("focused");
      }
    } else if (focused < 0) {
      // No prior hover and no prior keyboard focus -- start at the top.
      rebuildNodeList();
      if (NODES.length) { focused = 0; NODES[0].classList.add("focused"); NODES[0].scrollIntoView({block: "nearest"}); }
    }
  }
}
window.addEventListener("keydown", () => setMouseOff(true), true);
window.addEventListener("mousemove", ev => {
  if (ev.clientX !== lastMx || ev.clientY !== lastMy) {
    lastMx = ev.clientX; lastMy = ev.clientY;
    setMouseOff(false);
  }
}, true);
// Track what the cursor is over, for the mouse->keyboard promotion above.
document.addEventListener("mouseover", ev => {
  let el = ev.target;
  // walker item?
  const wli = el.closest && el.closest("#walker-list li");
  if (wli) { hoveredWalkerIdx = parseInt(wli.getAttribute("data-i"), 10); return; }
  // .node-row?
  const row = el.closest && el.closest(".node-row");
  if (row) { hoveredRow = row; return; }
  hoveredRow = null;
}, true);

// ---------------------------------------------------------------------------
// Minimal markdown renderer (CommonMark subset)
// ---------------------------------------------------------------------------
function esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function renderMarkdown(src) {
  const lines = src.split(/\r?\n/);
  let out = [];
  let inCode = false, codeLang = "", codeBuf = [];
  let inTable = false, tableRows = [];
  let inList = false, listType = null, listBuf = [];
  let inQuote = false, quoteBuf = [];
  let inPara = [], finalize = () => {
    if (inPara.length) { out.push("<p>" + inlineFmt(inPara.join(" ")) + "</p>"); inPara = []; }
    if (inList) { out.push("<" + listType + ">" + listBuf.join("") + "</" + listType + ">"); inList = false; listBuf = []; }
    if (inQuote) { out.push("<blockquote>" + inlineFmt(quoteBuf.join(" ")) + "</blockquote>"); inQuote = false; quoteBuf = []; }
    if (inTable) { out.push(renderTable(tableRows)); inTable = false; tableRows = []; }
  };
  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];
    if (line.startsWith("```")) {
      if (inCode) { out.push('<pre><code class="lang-' + esc(codeLang) + '">' + esc(codeBuf.join("\n")) + "</code></pre>"); inCode = false; codeBuf = []; codeLang = ""; }
      else { finalize(); inCode = true; codeLang = line.slice(3).trim(); }
      continue;
    }
    if (inCode) { codeBuf.push(line); continue; }
    if (/^#{1,6}\s/.test(line)) {
      finalize();
      const m = line.match(/^(#{1,6})\s+(.*)$/);
      out.push("<h" + m[1].length + ">" + inlineFmt(m[2]) + "</h" + m[1].length + ">");
      continue;
    }
    if (/^\s*[-*+]\s/.test(line)) {
      if (inPara.length) { out.push("<p>" + inlineFmt(inPara.join(" ")) + "</p>"); inPara = []; }
      if (!inList || listType !== "ul") { if (inList) out.push("<" + listType + ">" + listBuf.join("") + "</" + listType + ">"); inList = true; listType = "ul"; listBuf = []; }
      listBuf.push("<li>" + inlineFmt(line.replace(/^\s*[-*+]\s+/, "")) + "</li>");
      continue;
    }
    if (/^\s*\d+\.\s/.test(line)) {
      if (inPara.length) { out.push("<p>" + inlineFmt(inPara.join(" ")) + "</p>"); inPara = []; }
      if (!inList || listType !== "ol") { if (inList) out.push("<" + listType + ">" + listBuf.join("") + "</" + listType + ">"); inList = true; listType = "ol"; listBuf = []; }
      listBuf.push("<li>" + inlineFmt(line.replace(/^\s*\d+\.\s+/, "")) + "</li>");
      continue;
    }
    if (/^>\s?/.test(line)) {
      if (!inQuote) { finalize(); inQuote = true; }
      quoteBuf.push(line.replace(/^>\s?/, ""));
      continue;
    }
    if (/^\|.*\|\s*$/.test(line)) {
      if (!inTable) { finalize(); inTable = true; }
      tableRows.push(line);
      continue;
    }
    if (line.trim() === "") { finalize(); continue; }
    if (inList || inQuote || inTable) finalize();
    inPara.push(line);
  }
  finalize();
  return out.join("\n");
}
function renderTable(rows) {
  if (rows.length < 2) return "<pre>" + esc(rows.join("\n")) + "</pre>";
  const split = r => r.replace(/^\||\|$/g, "").split("|").map(c => c.trim());
  const header = split(rows[0]);
  const body = rows.slice(2).map(split);
  let html = "<table><thead><tr>" + header.map(c => "<th>" + inlineFmt(c) + "</th>").join("") + "</tr></thead><tbody>";
  for (const r of body) html += "<tr>" + r.map(c => "<td>" + inlineFmt(c) + "</td>").join("") + "</tr>";
  return html + "</tbody></table>";
}
function inlineFmt(s) {
  let out = esc(s);
  out = out.replace(/`([^`]+)`/g, (_, c) => "<code>" + c + "</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
  return out;
}

// ---------------------------------------------------------------------------
// Render tree
// ---------------------------------------------------------------------------
function nodeInner(label, kind, meta, summaryLines, markerClass, filePath, isFile) {
  // Returns { id, inner } -- the contents that go INSIDE the <div class="node">.
  // The caller wraps with the .node open/close and appends .node-children if any.
  // markerClass is "has-children" / "has-action" / "is-file" / "" -- CSS picks the glyph.
  const id = "node-" + (window._nodeIdSeq = (window._nodeIdSeq || 0) + 1);
  let inner = '<div class="node-row" data-id="' + id + '"';
  if (filePath) inner += ' data-file="' + esc(filePath) + '" data-kind="' + kind + '"';
  inner += '>';
  inner += '<span class="node-marker ' + (markerClass || "") + '"></span>';
  inner += '<span class="node-kind">' + kind + '</span>';
  inner += '<span class="node-name' + (isFile ? " is-file" : "") + '">' + esc(label) + '</span>';
  if (meta) inner += '<span class="node-meta">' + meta + '</span>';
  inner += '</div>';
  if (summaryLines && summaryLines.length) {
    inner += '<div class="node-summary">' + summaryLines.map(s => '<div>' + s + '</div>').join("") + '</div>';
  }
  if (filePath) inner += '<div class="deep" id="' + id + '-deep"></div>';
  return { id, inner };
}

function buildActionPayload(n) {
  // Returns { subtitle, actions: [{label, meta, cmd}] } or null if no actions for this kind.
  // cmd is the string copied to the clipboard when the action is selected.
  const kind = n.kind;
  if (kind === "skill") {
    const p = n.projection || {};
    const cmd = n.slash_command || "/" + (p.name || "");
    const subtitle = (cmd + (p.description ? "\n" + p.description : ""));
    const actions = [
      {label: "run",   meta: "copy slash-command", cmd: cmd},
      {label: "audit", meta: "copy /md-domain audit skill invocation", cmd: "/md-domain audit skill " + (n.path || "")},
      {label: "path",  meta: "copy file path",     cmd: n.path || ""}
    ];
    return { subtitle, actions };
  }
  if (kind === "plugin") {
    const p = n.projection || {};
    const name = p.name || n.name;
    const mkt = n.marketplace_name || "";
    const ref = name + (mkt ? "@" + mkt : "");
    const subtitle = (ref + (p.razor ? "\n" + p.razor : "") + (p.description ? "\n" + p.description : ""));
    return { subtitle, actions: [
      {label: "update",  meta: "copy /plugin update " + ref,  cmd: "/plugin update " + ref},
      {label: "disable", meta: "copy /plugin disable " + ref, cmd: "/plugin disable " + ref},
      {label: "path",    meta: "copy plugin path",            cmd: n.path || ""}
    ]};
  }
  if (kind === "marketplace") {
    const name = n.marketplace_name || n.name;
    return { subtitle: name, actions: [
      {label: "update", meta: "copy /plugin marketplace update " + name, cmd: "/plugin marketplace update " + name},
      {label: "remove", meta: "copy /plugin marketplace remove " + name, cmd: "/plugin marketplace remove " + name},
      {label: "path",   meta: "copy marketplace path",                    cmd: n.path || ""}
    ]};
  }
  if (kind === "reference_doc" || kind === "claude_md" || kind === "plain_md" || kind === "script" || kind === "json" || kind === "yaml") {
    return { subtitle: n.path || "", actions: [
      {label: "path", meta: "copy file path", cmd: n.path || ""}
    ]};
  }
  return null;
}
function metaPairs(pairs) {
  return pairs.filter(p => p[1] != null && p[1] !== "").map(p => '<strong>' + esc(p[0]) + '</strong>:' + esc(String(p[1]))).join(" &nbsp; ");
}
function fmtTokens(n) { return n >= 1000 ? (n/1000).toFixed(1) + "k" : String(n); }

function renderNode(n) {
  if (!n || n.kind === "missing") return '<div class="empty">missing</div>';
  const kind = n.kind;
  let label = n.name || (n.projection && n.projection.name) || "(unnamed)";
  let meta = "";
  let summary = [];
  if (kind === "marketplace") {
    const p = n.projection || {};
    meta = metaPairs([["plugins", p.plugin_count]]);
  } else if (kind === "plugin") {
    const p = n.projection || {};
    label = p.name || label;
    meta = metaPairs([["v", p.version]]);
    if (p.description) summary.push('<span class="label">desc</span>: ' + esc(p.description));
    if (p.razor) summary.push('<span class="label">razor</span>: ' + esc(p.razor));
  } else if (kind === "skill") {
    const p = n.projection || {};
    label = p.name || label;
    meta = metaPairs([["type", p.skill_type], ["author", p.author], ["lines", p.body_lines], ["tok", fmtTokens(p.body_tokens || 0)]]);
    if (p.description) summary.push('<span class="label">desc</span>: ' + esc(p.description));
  } else if (kind === "claude_user_dir") {
    label = "~/.claude/";
  } else if (kind === "project") {
    label = n.name + " (project)";
  }
  const childrenHtml = [];
  if (n.marketplaces) childrenHtml.push(renderNode({ ...n.marketplaces, name: "marketplaces" }));
  if (n.user_skills) childrenHtml.push(renderNode({ ...n.user_skills, name: "user skills" }));
  if (n.tree) childrenHtml.push(renderNode(n.tree));
  if (n.children) for (const c of n.children) childrenHtml.push(renderNode(c));
  if (n.files) for (const f of n.files) childrenHtml.push(renderFile(f));
  const hasChildren = childrenHtml.length > 0;
  const payload = buildActionPayload(n);
  const hasAction = !!payload;
  let markerClass = "";
  if (hasChildren) markerClass = "has-children";
  else if (hasAction) markerClass = "has-action";
  const { id, inner } = nodeInner(label, kind, meta, summary, markerClass, null, false);
  // Stash the action payload on the .node so Walker (`a`) can retrieve it for the focused row.
  ACTION_PAYLOADS[id] = payload;
  let html = '<div class="node kind-' + kind + '" id="' + id + '">' + inner;
  if (hasChildren) html += '<div class="node-children">' + childrenHtml.join("") + '</div>';
  html += '</div>';
  return html;
}

function renderFile(f) {
  const k = f.kind;
  const p = f.projection || {};
  let label = p.filename || f.name;
  let meta = "";
  let summary = [];
  if (k === "claude_md") {
    if (p.scope_directory) summary.push('<span class="label">scope</span>: ' + esc(p.scope_directory));
    if (p.scope_covers && p.scope_covers.length) summary.push('<span class="label">covers</span>: ' + p.scope_covers.slice(0, 3).map(esc).join(" | "));
    meta = metaPairs([["lines", p.lines]]);
  } else if (k === "reference_doc" || k === "plain_md") {
    if (p.first_heading) summary.push('<span class="label">#</span> ' + esc(p.first_heading));
    if (p.first_lines) summary.push(esc(p.first_lines));
    meta = metaPairs([["lines", p.lines]]);
  } else if (k === "script") {
    if (p.leading_doc) summary.push(esc(p.leading_doc));
    meta = metaPairs([["lang", p.language], ["lines", p.lines]]);
  }
  const { id, inner } = nodeInner(label, k, meta, summary, "is-file", f.path, true);
  ACTION_PAYLOADS[id] = buildActionPayload(f);
  return '<div class="node kind-' + k + '" id="' + id + '">' + inner + '</div>';
}

function attachHandlers() {
  document.querySelectorAll(".node-row").forEach(row => {
    row.addEventListener("click", ev => {
      ev.stopPropagation();
      const id = row.getAttribute("data-id");
      const node = document.getElementById(id);
      const file = row.getAttribute("data-file");
      if (file) {
        toggleDeep(id, file, row.getAttribute("data-kind"));
      } else if (node) {
        node.classList.toggle("open");
        rebuildNodeList();
      }
    });
  });
}

async function toggleDeep(id, path, kind) {
  const deep = document.getElementById(id + "-deep");
  if (!deep) return;
  if (deep.classList.contains("open")) { deep.classList.remove("open"); deep.innerHTML = ""; return; }
  deep.classList.add("open");
  deep.innerHTML = '<div class="empty">loading...</div>';
  try {
    const resp = await fetch("/file?path=" + encodeURIComponent(path));
    if (!resp.ok) { deep.innerHTML = '<div class="empty">error: ' + resp.status + '</div>'; return; }
    const txt = await resp.text();
    if (kind === "json") {
      try { const data = JSON.parse(txt); deep.innerHTML = renderJsonKV(data); }
      catch { deep.innerHTML = "<pre>" + esc(txt) + "</pre>"; }
    } else if (kind === "yaml") {
      deep.innerHTML = "<pre>" + esc(txt) + "</pre>";
    } else if (kind === "script") {
      deep.innerHTML = "<pre>" + esc(txt) + "</pre>";
    } else { // md kinds
      deep.innerHTML = '<div class="md">' + renderMarkdown(txt) + "</div>";
    }
  } catch (e) {
    deep.innerHTML = '<div class="empty">error: ' + esc(String(e)) + '</div>';
  }
}

function renderJsonKV(data, depth=0) {
  if (data === null || data === undefined) return '<em>null</em>';
  if (typeof data !== "object") return esc(String(data));
  if (Array.isArray(data)) {
    if (data.length === 0) return '<em>[]</em>';
    return '<table class="kv">' + data.map((v, i) => '<tr><td>[' + i + ']</td><td>' + renderJsonKV(v, depth+1) + '</td></tr>').join("") + '</table>';
  }
  const keys = Object.keys(data);
  if (keys.length === 0) return '<em>{}</em>';
  return '<table class="kv">' + keys.map(k => '<tr><td>' + esc(k) + '</td><td>' + renderJsonKV(data[k], depth+1) + '</td></tr>').join("") + '</table>';
}

function rebuildNodeList() {
  NODES = Array.from(document.querySelectorAll(".node-row"));
  if (focused >= NODES.length) focused = NODES.length - 1;
}

// ---------------------------------------------------------------------------
// Keyboard nav
// ---------------------------------------------------------------------------
document.addEventListener("keydown", ev => {
  // Self-heal: if walkerMode somehow drifted from the actual overlay state,
  // reset it before deciding which keymap owns this keypress.
  if (walkerMode && !document.getElementById("walker").classList.contains("open")) {
    walkerMode = null; walkerItems = []; walkerFiltered = []; walkerCursor = 0;
  }
  // Walker has its own keymap when open
  if (walkerMode) {
    if (ev.key === "Escape") { ev.preventDefault(); closeWalker(); return; }
    if (ev.key === "ArrowDown" || (ev.ctrlKey && ev.key === "n")) { ev.preventDefault(); walkerMove(1); return; }
    if (ev.key === "ArrowUp"   || (ev.ctrlKey && ev.key === "p")) { ev.preventDefault(); walkerMove(-1); return; }
    if (ev.key === "Enter") { ev.preventDefault(); walkerSelect(); return; }
    return; // let the input field handle the typing
  }
  if (ev.target.tagName === "INPUT") return;
  if (ev.key === "/") { ev.preventDefault(); openJumpWalker(); return; }
  if (ev.key === "a") { ev.preventDefault(); openActionsWalker(); return; }
  if (ev.key === "?") { ev.preventDefault(); toggleHelp(); return; }
  if (ev.key === "r" || ev.key === "R") { ev.preventDefault(); refresh(); return; }
  if (ev.key === "j" || ev.key === "ArrowDown") { ev.preventDefault(); moveFocus(1); }
  else if (ev.key === "k" || ev.key === "ArrowUp") { ev.preventDefault(); moveFocus(-1); }
  else if (ev.key === "Enter" || ev.key === " " || ev.key === "ArrowRight" || ev.key === "ArrowLeft" || ev.key === "h" || ev.key === "l") {
    ev.preventDefault();
    if (focused >= 0 && NODES[focused]) NODES[focused].click();
  } else if (ev.key === "Escape") {
    if (document.getElementById("help-overlay").classList.contains("open")) { toggleHelp(); return; }
    document.querySelectorAll(".deep.open").forEach(d => { d.classList.remove("open"); d.innerHTML = ""; });
    document.querySelectorAll(".node.open").forEach(n => n.classList.remove("open"));
  }
});

function toggleHelp() {
  document.getElementById("help-overlay").classList.toggle("open");
}

// ---------------------------------------------------------------------------
// Toast (Mako-style ephemeral popup)
// ---------------------------------------------------------------------------
function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => el.classList.remove("show"), 1400);
}

function copyToClipboard(txt) {
  if (!txt) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(() => toast("copied: " + (txt.length > 60 ? txt.slice(0, 57) + "..." : txt)));
  } else {
    const ta = document.createElement("textarea");
    ta.value = txt;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); toast("copied"); } catch {}
    document.body.removeChild(ta);
  }
}

// ---------------------------------------------------------------------------
// Walker -- summoned launcher (jump + actions)
// ---------------------------------------------------------------------------
function openWalker(mode, title, subtitle, items) {
  walkerMode = mode;
  walkerItems = items;
  walkerCursor = 0;
  hoveredWalkerIdx = null;
  document.getElementById("walker-title").textContent = title;
  const sub = document.getElementById("walker-subtitle");
  if (subtitle) { sub.textContent = subtitle; sub.style.display = "block"; }
  else { sub.style.display = "none"; }
  const input = document.getElementById("walker-input");
  input.value = "";
  document.getElementById("walker").classList.add("open");
  input.focus();
  walkerRender("");
}
function closeWalker() {
  document.getElementById("walker").classList.remove("open");
  walkerMode = null;
  walkerItems = [];
  walkerFiltered = [];
  walkerCursor = 0;
  hoveredWalkerIdx = null;
}
// Click-outside-to-close on the walker
document.getElementById("walker").addEventListener("click", ev => {
  if (ev.target.id === "walker") closeWalker();
});
function walkerRender(filter) {
  filter = (filter || "").toLowerCase();
  walkerFiltered = !filter ? walkerItems.slice() : walkerItems.filter(it => {
    return (it.label || "").toLowerCase().includes(filter) || (it.meta || "").toLowerCase().includes(filter);
  });
  if (walkerCursor >= walkerFiltered.length) walkerCursor = Math.max(0, walkerFiltered.length - 1);
  const list = document.getElementById("walker-list");
  list.innerHTML = walkerFiltered.slice(0, 40).map((it, i) =>
    '<li class="' + (i === walkerCursor ? "focused" : "") + '" data-i="' + i + '">' +
    '<span class="walker-label">' + esc(it.label || "") + '</span>' +
    (it.meta ? '<span class="walker-meta">' + esc(it.meta) + '</span>' : '') + '</li>'
  ).join("");
  Array.from(list.children).forEach(li => li.addEventListener("click", ev => {
    walkerCursor = parseInt(li.getAttribute("data-i"), 10);
    walkerSelect();
  }));
}
function walkerMove(d) {
  walkerCursor = Math.max(0, Math.min(walkerFiltered.length - 1, walkerCursor + d));
  walkerRender(document.getElementById("walker-input").value);
  const focused = document.querySelector("#walker-list li.focused");
  if (focused) focused.scrollIntoView({block: "nearest"});
}
function walkerSelect() {
  const it = walkerFiltered[walkerCursor];
  if (!it) return;
  if (walkerMode === "jump") {
    closeWalker();
    jumpToNode(it.id);
  } else if (walkerMode === "actions") {
    closeWalker();
    copyToClipboard(it.cmd);
  }
}
document.getElementById("walker-input").addEventListener("input", ev => { walkerCursor = 0; walkerRender(ev.target.value); });

function openJumpWalker() {
  const items = [];
  document.querySelectorAll(".node-row").forEach(row => {
    const id = row.getAttribute("data-id");
    const kindEl = row.querySelector(".node-kind");
    const nameEl = row.querySelector(".node-name");
    items.push({ id, label: nameEl ? nameEl.textContent : "(unnamed)", meta: kindEl ? kindEl.textContent : "" });
  });
  openWalker("jump", "jump", null, items);
}
function openActionsWalker() {
  if (focused < 0 || !NODES[focused]) { toast("focus a node first (j/k)"); return; }
  const row = NODES[focused];
  const id = row.getAttribute("data-id");
  const payload = ACTION_PAYLOADS[id];
  if (!payload) { toast("no actions for this node"); return; }
  const nameEl = row.querySelector(".node-name");
  const kindEl = row.querySelector(".node-kind");
  const title = (kindEl ? kindEl.textContent + ": " : "") + (nameEl ? nameEl.textContent : "");
  openWalker("actions", title, payload.subtitle || null, payload.actions);
}
function jumpToNode(id) {
  const node = document.getElementById(id);
  if (!node) return;
  // open ancestors
  let p = node.parentElement;
  while (p && p.id !== "left-body" && p.id !== "right-body") {
    if (p.classList && p.classList.contains("node")) p.classList.add("open");
    p = p.parentElement;
  }
  // focus the row
  const row = node.querySelector(":scope > .node-row");
  rebuildNodeList();
  if (row) {
    const i = NODES.indexOf(row);
    if (i >= 0) {
      if (focused >= 0 && NODES[focused]) NODES[focused].classList.remove("focused");
      focused = i;
      NODES[i].classList.add("focused");
      NODES[i].scrollIntoView({block: "center"});
    }
  }
}
function moveFocus(delta) {
  rebuildNodeList();
  if (!NODES.length) return;
  if (focused >= 0 && NODES[focused]) NODES[focused].classList.remove("focused");
  focused = Math.max(0, Math.min(NODES.length-1, focused + delta));
  NODES[focused].classList.add("focused");
  NODES[focused].scrollIntoView({block: "nearest"});
}

// ---------------------------------------------------------------------------
// Refresh
// ---------------------------------------------------------------------------
async function refresh() {
  toast("refreshing...");
  try {
    await fetch("/refresh");
    await load();
    toast("refreshed");
  } catch (e) { toast("refresh failed: " + e); }
}

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------
async function load() {
  try {
    const resp = await fetch("/index.json");
    if (!resp.ok) { document.getElementById("left-body").innerHTML = '<div class="empty">no index</div>'; return; }
    INDEX = await resp.json();
    document.getElementById("meta").textContent = "generated " + (INDEX.generated_at || "?").replace("T", " ").replace(/\.\d+Z?$/, "");
    const userRoot = INDEX.roots.find(r => r.kind === "claude_user_dir");
    const project = INDEX.roots.find(r => r.kind === "project");
    document.getElementById("left-body").innerHTML = userRoot ? renderNode(userRoot) : '<div class="empty">no user dir</div>';
    document.getElementById("right-body").innerHTML = project ? renderNode(project) : '<div class="empty">no project</div>';
    attachHandlers();
    rebuildNodeList();
  } catch (e) {
    document.getElementById("left-body").innerHTML = '<div class="empty">error: ' + e + '</div>';
  }
}

load();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-explorer")
    parser.add_argument("command", nargs="?", default="run", choices=["crawl", "serve", "run"])
    parser.add_argument("--project", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)

    if args.command in ("crawl", "run"):
        print(f"crawl: scanning {args.project} ...")
        idx = crawl(args.project)
        roots = idx.get("roots", [])
        print(f"crawl: wrote index with {len(roots)} roots to {INDEX_PATH}")
    if args.command in ("serve", "run"):
        serve(args.project, port=args.port, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
