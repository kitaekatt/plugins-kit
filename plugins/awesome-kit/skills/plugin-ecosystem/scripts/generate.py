#!/usr/bin/env python3
"""Generate a 16:9 HTML poster of the installed Claude Code plugin ecosystem.

Reads:
  ~/.claude/plugins/installed_plugins.json
  <each plugin>/.claude-plugin/plugin.json
  <each plugin>/skills/*/SKILL.md                                      (YAML frontmatter)
  ~/.claude/plugins/marketplaces/<m>/.claude-plugin/poster.yaml         (per-marketplace opt-in)
  ~/.claude/settings.json + <project>/.claude/settings.json             (live enabledPlugins)
  <project>/.claude/bootstrap.json                                      (declared on/opt-in fallback)
  ~/.claude/.local-data/awesome-kit/plugin-ecosystem-poster.yaml        (title / tagline / state overrides)

Emits a single self-contained HTML file (default ~/.claude/plugin-ecosystem.html,
regardless of where the skill is run from) and opens it in the browser unless
--no-open is passed.

A marketplace appears in the poster only if it ships a poster.yaml (opt-in gate).
Plugin on/off badge precedence: YAML override > settings.json enabledPlugins > bootstrap.json declaration.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import webbrowser
from pathlib import Path


# ---------- minimal YAML reader ----------
# Handles the subset we need: top-level scalar keys, optionally one-level-nested
# mappings (plain `key: value` lines, # comments, optional surrounding quotes).
# Values "true"/"false"/"on"/"off" are returned as strings; callers normalize.

def _strip_quotes(v: str) -> str:
    v = v.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    return v


def parse_yaml(text: str) -> dict:
    out: dict = {}
    cur_map: dict | None = None
    cur_indent = -1
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_.:-]*):\s*(.*?)\s*(?:#.*)?$", line.lstrip())
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if indent == 0:
            if val == "":
                cur_map = {}
                out[key] = cur_map
                cur_indent = 0
            else:
                out[key] = _strip_quotes(val)
                cur_map = None
                cur_indent = -1
        elif cur_map is not None and indent > cur_indent:
            cur_map[key] = _strip_quotes(val)
    return out


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return parse_yaml(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


# ---------- data gathering ----------

def home_claude() -> Path:
    return Path.home() / ".claude"


def load_installed() -> dict:
    p = home_claude() / "plugins" / "installed_plugins.json"
    if not p.exists():
        sys.exit(f"installed_plugins.json not found at {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _version_key(version: str):
    """Tolerant ordering key for cache version-dir names: numeric dot-parts
    compare numerically ("0.10.0" > "0.9.0"); non-numeric names (git-SHA cache
    keys) sort below any numeric version and tie-break lexically."""
    parts = re.findall(r"\d+", version)
    return (1 if parts else 0, tuple(int(p) for p in parts), version)


def merge_cache_fallback(installed: dict) -> dict:
    """Registry-v2 fallback: newer Claude Code keeps installed_plugins.json at
    {"version": 2, "plugins": {}} for marketplace installs -- the code lives in
    ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/. Without this the
    poster renders EMPTY on such machines. Synthesize an entry for every cached
    plugin the registry does not record, picking the highest version dir (the
    code Claude Code actually loads). Registry entries keep precedence, and
    collect_plugins' marketplace.json filter still drops phantoms."""
    plugins = dict(installed.get("plugins", {}))
    cache_root = home_claude() / "plugins" / "cache"
    if cache_root.is_dir():
        for mkt_dir in sorted(cache_root.iterdir()):
            if not mkt_dir.is_dir():
                continue
            for plugin_dir in sorted(mkt_dir.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                key = f"{plugin_dir.name}@{mkt_dir.name}"
                if plugins.get(key):
                    continue
                versions = [d for d in plugin_dir.iterdir() if d.is_dir()]
                if not versions:
                    continue
                best = max(versions, key=lambda d: _version_key(d.name))
                plugins[key] = [{"installPath": str(best), "version": best.name}]
    merged = dict(installed)
    merged["plugins"] = plugins
    return merged


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def parse_frontmatter(text: str) -> dict:
    """Extract the YAML frontmatter block from SKILL.md text and parse it with
    the same minimal YAML reader used for poster.yaml files (single parser)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    return parse_yaml(text[3:end])


def parse_skill_frontmatter(skill_md: Path) -> dict:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_frontmatter(text)


def collect_skills(plugin_root: Path, poster_overrides: dict) -> list[dict]:
    skills_dir = plugin_root / "skills"
    if not skills_dir.is_dir():
        return []
    skill_overrides = poster_overrides.get("skills") or {}
    skills = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        md = child / "SKILL.md"
        if not md.exists():
            continue
        fm = parse_skill_frontmatter(md)
        name = fm.get("name", child.name)
        skills.append({
            "name": name,
            "description": skill_overrides.get(name) or fm.get("description", ""),
            "type": fm.get("skill-type", ""),
            "author": fm.get("author", ""),
        })
    return skills


def collect_marketplace_metadata() -> dict:
    """Return {marketplace_name: {poster, plugin_names}} for marketplaces that opted in.

    plugin_names is the set of plugin names currently listed in the marketplace's
    marketplace.json -- used to filter out phantom installs (plugins still cached
    locally but already removed from the marketplace source)."""
    root = home_claude() / "plugins" / "marketplaces"
    out = {}
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("temp_"):
            continue
        poster = child / ".claude-plugin" / "poster.yaml"
        if not poster.exists():
            continue
        marketplace_json = load_json(child / ".claude-plugin" / "marketplace.json")
        plugin_names = {p.get("name") for p in marketplace_json.get("plugins", []) if p.get("name")}
        out[child.name] = {
            "poster": load_yaml(poster),
            "plugin_names": plugin_names,
        }
    return out


def merged_enabled_plugins(project_root: Path) -> dict:
    """Merge enabledPlugins from project + project.local + user settings.json files.
    Returns {ref: bool} (project local wins over project wins over user)."""
    candidates = [
        home_claude() / "settings.json",
        project_root / ".claude" / "settings.json",
        project_root / ".claude" / "settings.local.json",
    ]
    merged: dict = {}
    for s in candidates:
        data = load_json(s)
        ep = data.get("enabledPlugins") or {}
        for k, v in ep.items():
            merged[k] = bool(v)
    return merged


def index_bootstrap_plugins(bootstrap: dict) -> dict:
    out = {}
    for entry in bootstrap.get("plugins", []) or []:
        ref = entry.get("ref")
        if ref:
            out[ref] = entry
    return out


def normalize_state(value: str) -> str:
    v = value.strip().lower()
    if v in ("on", "true", "enabled", "yes"):
        return "on"
    if v in ("off", "false", "disabled", "no"):
        return "off"
    if v in ("opt-in", "optin", "manual"):
        return "opt-in"
    if v in ("required", "mandatory"):
        return "required"
    return v


def compute_state(ref: str, settings_enabled: dict, bs_index: dict, overrides: dict,
                  marketplace_states: dict, defaults_mode: bool = False) -> str:
    """Resolve a plugin's display state.

    Precedence (first match wins):
      1. user config `states:` map (poster author's depiction override)
      2. marketplace `poster.yaml` `states:` (marketplace owner's declaration -- this is
         where `required` lives; the marketplace is asserting structural facts about its
         own plugins, e.g. 'bootstrap cannot be turned off')
      3. settings.json `enabledPlugins` (live operator state)
      4. project `bootstrap.json` plugin declaration
      5. defaults_mode -> 'opt-in', otherwise 'unmanaged'

    `marketplace_states` is keyed by marketplace name; each value is a dict of
    short-plugin-name -> raw state string (pre-normalize).
    """
    if ref in overrides:
        return normalize_state(str(overrides[ref]))
    short = ref.split(":", 1)[1] if ":" in ref else ref
    if short in overrides:
        return normalize_state(str(overrides[short]))

    if ":" in ref:
        marketplace, plugin = ref.split(":", 1)
        m_states = marketplace_states.get(marketplace) or {}
        if plugin in m_states:
            return normalize_state(str(m_states[plugin]))
        settings_key = f"{plugin}@{marketplace}"
        if settings_key in settings_enabled:
            return "on" if settings_enabled[settings_key] else "off"

    bs = bs_index.get(ref, {})
    if bs.get("install") == "manual":
        return "opt-in"
    if bs.get("enabled") is True:
        return "on"
    if bs:
        return "off"
    return "opt-in" if defaults_mode else "unmanaged"


def collect_plugins(installed: dict, marketplaces: dict, settings_enabled: dict,
                    bs_index: dict, overrides: dict, marketplace_states: dict,
                    defaults_mode: bool = False) -> list[dict]:
    """Yield one dict per installed plugin from a participating marketplace.
    Filters out phantom installs (no longer present in the marketplace's marketplace.json)."""
    out = []
    for key, entries in installed.get("plugins", {}).items():
        if "@" not in key:
            continue
        plugin_name, marketplace = key.split("@", 1)
        if marketplace not in marketplaces:
            continue  # marketplace did not opt in
        if plugin_name not in marketplaces[marketplace]["plugin_names"]:
            continue  # phantom install: removed from marketplace.json upstream
        if not entries:
            continue
        entry = entries[0]
        install_path = Path(entry["installPath"])
        meta = load_json(install_path / ".claude-plugin" / "plugin.json")

        ref = f"{marketplace}:{plugin_name}"
        state = compute_state(ref, settings_enabled, bs_index, overrides,
                              marketplace_states, defaults_mode)

        poster_overrides = load_yaml(install_path / ".claude-plugin" / "poster.yaml")

        out.append({
            "marketplace": marketplace,
            "name": meta.get("name", plugin_name),
            "version": entry.get("version", ""),
            "description": poster_overrides.get("description") or meta.get("description", ""),
            "razor": poster_overrides.get("razor") or meta.get("razor", ""),
            "state": state,
            "skills": collect_skills(install_path, poster_overrides),
        })
    return out


# ---------- HTML rendering ----------

CSS = r"""
:root {
  --bg: #0f1419;
  --bg-2: #161c24;
  --bg-3: #1f2730;
  --line: #2a3340;
  --fg: #e6edf3;
  --fg-dim: #8b949e;
  --accent: #58a6ff;
  --accent-2: #79c0ff;
  --on: #3fb950;
  --on-bg: #14351e;
  --opt: #d29922;
  --opt-bg: #3a2c0a;
  --off: #6e7681;
  --off-bg: #21262d;
  --req: #d4a3ff;
  --req-bg: #3a1f4d;
}
* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  font-size: 14px;
  line-height: 1.4;
  overflow: hidden;
}
.poster {
  width: 100vw;
  height: 100vh;
  display: grid;
  grid-template-rows: auto 1fr;
  padding: 32px 48px 40px;
  gap: 24px;
  aspect-ratio: 16 / 9;
  max-width: calc(100vh * 16 / 9);
  max-height: calc(100vw * 9 / 16);
  margin: 0 auto;
}
header { text-align: center; }
header h1 {
  font-size: clamp(28px, 4vw, 56px);
  font-weight: 800;
  margin: 0 0 6px;
  letter-spacing: -0.02em;
  background: linear-gradient(90deg, var(--accent-2), #f0b4ff);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
header .sub {
  color: var(--fg-dim);
  font-size: clamp(11px, 1.2vw, 14px);
}
.columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  min-height: 0;
}
.col {
  background: var(--bg-2);
  border: 1px solid var(--line);
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.col-header {
  padding: 14px 18px;
  border-bottom: 1px solid var(--line);
}
.col-header .name-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}
.col-header .name {
  font-weight: 700;
  font-size: clamp(14px, 1.4vw, 18px);
  letter-spacing: 0.02em;
  color: inherit;
  text-decoration: none;
}
.col-header a.name:hover {
  color: var(--accent);
  text-decoration: underline;
}
.col-header .count {
  color: var(--fg-dim);
  font-size: 12px;
}
.col-header .sub {
  color: var(--opt);
  font-size: 11px;
  margin-top: 4px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.col-body {
  padding: 14px;
  overflow-y: auto;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  align-content: start;
}
.card {
  background: var(--bg-3);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 12px;
  cursor: pointer;
  transition: border-color 120ms, transform 120ms;
  display: flex;
  flex-direction: column;
  gap: 4px;
  text-align: left;
  color: inherit;
  font: inherit;
}
.card:hover {
  border-color: var(--accent);
  transform: translateY(-1px);
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.card-name {
  font-weight: 600;
  font-size: clamp(12px, 1.1vw, 15px);
}
.badge {
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.08em;
  padding: 2px 6px;
  border-radius: 999px;
  text-transform: uppercase;
  white-space: nowrap;
}
.badge.on       { background: var(--on-bg);  color: var(--on); }
.badge.opt-in   { background: var(--opt-bg); color: var(--opt); }
.badge.off      { background: var(--off-bg); color: var(--off); }
.badge.unmanaged{ background: var(--off-bg); color: var(--fg-dim); }
.badge.required { background: var(--req-bg); color: var(--req); }
.card-desc {
  color: var(--fg-dim);
  font-size: clamp(10px, 0.95vw, 12px);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* side panel */
.scrim {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.5);
  opacity: 0;
  pointer-events: none;
  transition: opacity 200ms;
  z-index: 50;
}
.scrim.open { opacity: 1; pointer-events: auto; }
.panel {
  position: fixed;
  top: 0;
  right: 0;
  height: 100vh;
  width: min(560px, 42vw);
  background: var(--bg-2);
  border-left: 1px solid var(--line);
  transform: translateX(100%);
  transition: transform 220ms ease;
  z-index: 60;
  display: flex;
  flex-direction: column;
  box-shadow: -8px 0 24px rgba(0,0,0,0.4);
}
.panel.open { transform: translateX(0); }
.panel-head {
  padding: 20px 24px 12px;
  border-bottom: 1px solid var(--line);
}
.panel-head .crumb {
  color: var(--fg-dim);
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.panel-head h2 {
  margin: 4px 0 6px;
  font-size: 24px;
  font-weight: 700;
  display: flex;
  align-items: center;
  gap: 10px;
}
.panel-head .ver { color: var(--fg-dim); font-size: 12px; font-weight: 400; }
.panel-head .razor {
  color: var(--fg);
  font-size: 13px;
  margin-top: 8px;
  line-height: 1.5;
}
.panel-head .close {
  position: absolute;
  top: 12px;
  right: 12px;
  background: transparent;
  color: var(--fg-dim);
  border: 1px solid var(--line);
  border-radius: 6px;
  width: 28px;
  height: 28px;
  font: inherit;
  cursor: pointer;
}
.panel-head .close:hover { color: var(--fg); border-color: var(--accent); }
.panel-body {
  padding: 16px 24px 24px;
  overflow-y: auto;
}
.panel-section-title {
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--fg-dim);
  margin: 14px 0 8px;
}
.skill {
  padding: 10px 0;
  border-top: 1px solid var(--line);
}
.skill:first-child { border-top: none; }
.skill-name {
  font-size: 16px;
  font-weight: 600;
  color: var(--accent-2);
}
.skill-author {
  color: var(--fg-dim);
  font-size: 11px;
  font-weight: 400;
  margin-left: 6px;
}
.skill-desc {
  color: var(--fg);
  font-size: 12.5px;
  margin-top: 3px;
  line-height: 1.45;
}
.skill-empty {
  color: var(--fg-dim);
  font-style: italic;
  font-size: 12px;
}
footer {
  position: fixed;
  bottom: 8px;
  left: 0;
  right: 0;
  text-align: center;
  color: var(--fg-dim);
  font-size: 10px;
  pointer-events: none;
}
"""

JS = r"""
const data = __DATA__;

function el(tag, attrs = {}, children = []) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children) if (c) n.appendChild(c);
  return n;
}

function badge(state) {
  const label = { on: "On", "opt-in": "Opt-In", off: "Off", unmanaged: "Installed", required: "Required" }[state] || state;
  return el("span", { class: `badge ${state}`, text: label });
}

function renderColumn(marketplace, plugins) {
  const displayName = `${marketplace} marketplace`;
  const url = (data.marketplace_urls || {})[marketplace];
  const nameEl = url
    ? el("a", { class: "name", href: url, target: "_blank", rel: "noopener", text: displayName })
    : el("span", { class: "name", text: displayName });
  const nameRow = el("div", { class: "name-row" }, [
    nameEl,
    el("span", { class: "count", text: `${plugins.length} plugin${plugins.length === 1 ? "" : "s"}` }),
  ]);
  const headChildren = [nameRow];
  const sub = (data.marketplace_subtitles || {})[marketplace];
  if (sub) headChildren.push(el("div", { class: "sub", text: sub }));
  const head = el("div", { class: "col-header" }, headChildren);
  const body = el("div", { class: "col-body" });
  const rank = s => ({ required: 0, on: 1, "opt-in": 2, unmanaged: 3, off: 4 }[s] ?? 5);
  plugins
    .slice()
    .sort((a, b) => rank(a.state) - rank(b.state) || a.name.localeCompare(b.name))
    .forEach(p => body.appendChild(renderCard(p)));
  return el("div", { class: "col" }, [head, body]);
}

function renderCard(p) {
  return el("button", {
    class: "card",
    onclick: () => openPanel(p),
  }, [
    el("div", { class: "card-head" }, [
      el("span", { class: "card-name", text: p.name }),
      badge(p.state),
    ]),
    el("div", { class: "card-desc", text: p.description || "(no description)" }),
  ]);
}

function openPanel(p) {
  const panel = document.getElementById("panel");
  const body = document.getElementById("panelBody");
  const head = document.getElementById("panelHead");
  head.innerHTML = "";
  body.innerHTML = "";

  head.appendChild(el("div", { class: "crumb", text: p.marketplace }));
  head.appendChild(el("h2", {}, [
    document.createTextNode(p.name),
    el("span", { class: "ver", text: p.version ? `v${p.version}` : "" }),
    badge(p.state),
  ]));
  if (p.razor || p.description) {
    head.appendChild(el("div", { class: "razor", text: p.razor || p.description }));
  }
  const close = el("button", { class: "close", text: "x", onclick: closePanel, "aria-label": "Close" });
  head.appendChild(close);

  body.appendChild(el("div", { class: "panel-section-title", text: `Skills (${p.skills.length})` }));
  if (p.skills.length === 0) {
    body.appendChild(el("div", { class: "skill-empty", text: "This plugin ships no user-facing skills (commands or hooks only)." }));
  } else {
    p.skills.forEach(s => {
      const nameEl = el("div", { class: "skill-name" }, [document.createTextNode(s.name)]);
      if (s.author) nameEl.appendChild(el("span", { class: "skill-author", text: `by ${s.author}` }));
      const row = el("div", { class: "skill" }, [
        nameEl,
        el("div", { class: "skill-desc", text: s.description || "(no description)" }),
      ]);
      body.appendChild(row);
    });
  }

  panel.classList.add("open");
  document.getElementById("scrim").classList.add("open");
}

function closePanel() {
  document.getElementById("panel").classList.remove("open");
  document.getElementById("scrim").classList.remove("open");
}

function init() {
  const root = document.getElementById("cols");
  const groups = {};
  data.plugins.forEach(p => {
    (groups[p.marketplace] = groups[p.marketplace] || []).push(p);
  });
  const order = data.marketplace_order.filter(m => groups[m]);
  for (const m of Object.keys(groups)) if (!order.includes(m)) order.push(m);
  order.forEach(m => root.appendChild(renderColumn(m, groups[m])));

  document.getElementById("scrim").addEventListener("click", closePanel);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closePanel(); });
}

window.addEventListener("DOMContentLoaded", init);
"""


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>__CSS__</style>
</head>
<body>
<div class="poster">
  <header>
    <h1>__TITLE__</h1>
    <div class="sub">__SUB__</div>
  </header>
  <div class="columns" id="cols" style="__COLS_STYLE__"></div>
</div>
<div class="scrim" id="scrim"></div>
<aside class="panel" id="panel">
  <div class="panel-head" id="panelHead"></div>
  <div class="panel-body" id="panelBody"></div>
</aside>
<footer>generated by /plugin-ecosystem -- click a card for details.</footer>
<script>__JS__</script>
</body>
</html>
"""


def render_html(title: str, tagline: str, plugins: list[dict],
                marketplace_order: list[str], marketplace_subtitles: dict,
                marketplace_urls: dict) -> str:
    data = {
        "plugins": plugins,
        "marketplace_order": marketplace_order,
        "marketplace_subtitles": marketplace_subtitles,
        "marketplace_urls": marketplace_urls,
    }
    js = JS.replace("__DATA__", json.dumps(data))
    n = len(marketplace_order)
    if n <= 1:
        col_template = "minmax(0, 720px)"
        cols_style = f"grid-template-columns: {col_template}; justify-content: center;"
    else:
        cols_style = f"grid-template-columns: repeat({min(n, 2)}, 1fr);"
    return (HTML_TEMPLATE
            .replace("__TITLE__", html.escape(title))
            .replace("__SUB__", html.escape(tagline))
            .replace("__COLS_STYLE__", cols_style)
            .replace("__CSS__", CSS)
            .replace("__JS__", js))


# ---------- main ----------

DEFAULT_USER_CONFIG = Path.home() / ".claude" / ".local-data" / "awesome-kit" / "plugin-ecosystem-poster.yaml"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", type=Path, default=None,
                    help="Project root (defaults to cwd). Determines where settings.json, "
                         "bootstrap.json, and the output HTML live.")
    ap.add_argument("--config", type=Path, default=DEFAULT_USER_CONFIG,
                    help=f"User config YAML (default: {DEFAULT_USER_CONFIG})")
    ap.add_argument("--output", type=Path, default=None,
                    help="Output HTML path (default: ~/.claude/plugin-ecosystem.html)")
    ap.add_argument("--title", default=None, help="Override page title from config")
    ap.add_argument("--no-open", action="store_true", help="Do not open the file in the browser")
    ap.add_argument("--defaults", action="store_true",
                    help="Source on/off state from project bootstrap.json declarations only, "
                         "ignoring the user's live settings.json enabledPlugins. Use to depict "
                         "'how this project ships' regardless of the operator's local toggles.")
    ap.add_argument("--marketplace", action="append", default=None,
                    help="Restrict the poster to the named marketplace. Repeatable, or pass a "
                         "comma-separated list. Unknown names are silently ignored. When exactly "
                         "one marketplace remains, the column grid collapses to a single column.")
    args = ap.parse_args(argv)

    project_root = (args.project or Path.cwd()).resolve()
    output = args.output or (home_claude() / "plugin-ecosystem.html")

    user_config = load_yaml(args.config)
    title = args.title or user_config.get("title") or "Claude Plugin Ecosystem"
    tagline = user_config.get("tagline", "")
    overrides = user_config.get("states") or {}

    marketplaces = collect_marketplace_metadata()
    if not marketplaces:
        sys.exit("No marketplaces have opted in (no .claude-plugin/poster.yaml files found "
                 "under ~/.claude/plugins/marketplaces/).")

    if args.marketplace:
        wanted: set[str] = set()
        for raw in args.marketplace:
            for part in raw.split(","):
                part = part.strip()
                if part:
                    wanted.add(part)
        filtered = {m: meta for m, meta in marketplaces.items() if m in wanted}
        if not filtered:
            sys.exit(f"--marketplace {sorted(wanted)} matched none of the opted-in marketplaces: "
                     f"{sorted(marketplaces.keys())}.")
        marketplaces = filtered

    bootstrap = load_json(project_root / ".claude" / "bootstrap.json")
    bs_index = index_bootstrap_plugins(bootstrap)
    settings_enabled = {} if args.defaults else merged_enabled_plugins(project_root)

    marketplace_states = {
        m: (meta["poster"].get("states") or {}) for m, meta in marketplaces.items()
    }

    installed = merge_cache_fallback(load_installed())
    plugins = collect_plugins(installed, marketplaces, settings_enabled, bs_index, overrides,
                              marketplace_states, defaults_mode=args.defaults)

    # column order: declared in bootstrap.json first, then alphabetical
    declared = [m["name"] for m in bootstrap.get("marketplaces", []) or [] if m.get("name") in marketplaces]
    seen = set()
    order = [m for m in declared if not (m in seen or seen.add(m))]
    for m in sorted(marketplaces.keys()):
        if m not in order:
            order.append(m)

    subtitles = {m: meta["poster"].get("subtitle", "") for m, meta in marketplaces.items()}
    urls = {m: meta["poster"].get("url", "") for m, meta in marketplaces.items()}

    out_html = render_html(title, tagline, plugins, order, subtitles, urls)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(out_html, encoding="utf-8")
    print(f"Wrote {output} ({len(out_html):,} bytes, {len(plugins)} plugins, "
          f"{len(marketplaces)} marketplaces)")

    if not args.no_open:
        webbrowser.open(output.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
