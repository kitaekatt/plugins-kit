# Case Study: unreal-kit

Game development plugin with the most complex bootstrap — system tools, venv, config discovery, external app dependencies, and PyPI package extraction.

## Current Operations

### Automatable

| Category | Condition | Check Method | Remediation |
|----------|-----------|-------------|-------------|
| Configuration | `~/.local/bin` not in PATH | Read shell RC files / query OS env var | Modify persistent PATH (platform-specific) |
| Configuration | UE `bRemoteExecution` not enabled | Read `DefaultEngine.ini` and `UserEngine.ini` | Write `bRemoteExecution=True` to `UserEngine.ini` |
| Configuration | UE `bIsDeveloperMode` not enabled | Read `DefaultEngine.ini` and `UserEngine.ini` | Write `bIsDeveloperMode=True` to `UserEngine.ini` |
| Tool | `uv` not installed | `command -v uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Tool | `git` not installed | `command -v git` | Platform-specific install command |
| Tool | `curl` not installed (Windows/Ubuntu) | `command -v curl` | `winget install cURL.cURL` / `sudo apt install -y curl` |
| Library/Data | Python venv missing or broken | Check dir → binary → `import upyrc; import yaml` | `uv sync` from `pyproject.toml` |
| Library/Data | Stock UE stub missing | Check `${data_dir}/stubs/unreal.py` exists | Download `unreal-stub` from PyPI, extract from wheel into machine-local data |

### Manual

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| UE project path unknown (auto-detect failed) | Config check + auto-detect from CWD both fail | Ask user for `.uproject` path, write to config |
| UE Editor settings written but not active | Settings just written to `UserEngine.ini` | User restarts UE Editor, types `fixed` |
| Durable enriched stub absent or stale | Compare `<project>/.plugin-data/plugins-kit/unreal-kit/unreal.py` with the generated source, read-only | Record a deferred requirement; user explicitly runs the refresh action if enriched search is needed |

## Manifest (`bootstrap.json`)

Standard operations are declared in the manifest — the engine handles these without any script code:

```json
{
  "path_entries": ["~/.local/bin"],
  "tools": [
    {"name": "uv", "install": "curl -LsSf https://astral.sh/uv/install.sh | sh"},
    {"name": "git"},
    {"name": "curl", "platforms": ["windows", "linux"]}
  ],
  "venv": {
    "check_imports": ["upyrc", "yaml"]
  },
  "ini_settings": [
    {
      "file": "${uproject_dir}/Config/UserEngine.ini",
      "section": "/Script/PythonScriptPlugin.PythonScriptPluginSettings",
      "settings": {"bRemoteExecution": "True", "bIsDeveloperMode": "True"}
    }
  ],
  "pypi_packages": [
    {
      "package": "unreal-stub",
      "extract_to": "${data_dir}/stubs/unreal.py",
      "extract_pattern": "*.py"
    }
  ]
}
```

Note: `ini_settings` uses `${uproject_dir}` which the engine resolves from the plugin's config. If the config doesn't have this value yet (first run), the engine skips ini_settings and the script handles discovery.

## Bootstrap Script (Pseudocode)

The script handles only UE-specific custom logic — everything standard is in the manifest:

```python
def bootstrap(ctx):
    """unreal-kit bootstrap script — custom logic only.

    Standard operations (tools, PATH, venv, ini settings, stock PyPI stub)
    are handled by the manifest. This script handles:
    - UE project discovery (domain-specific heuristic)
    - Read-only durable enriched-stub presence/freshness check
    """

    # --- UE project discovery (custom) ---
    config = ctx.read_config()
    uproject = config.get("uproject")

    if not uproject:
        # Try auto-detection from CWD
        uproject = discover_uproject(Path.cwd())
        if uproject:
            config["uproject"] = str(uproject)
            config["engine_dir"] = str(discover_engine(uproject))
            ctx.write_config(config)
        else:
            ctx.add_fixall(
                agent_msg=(
                    f"Ask the user where the .uproject file is, "
                    f"then write that information to {ctx.data_dir / 'bootstrap-config.json'} "
                    f"as the value of the 'uproject' field. "
                    f"Also discover the engine directory and write it as 'engine_dir'."
                ),
                user_msg="No UE project detected. Type fix-all to configure."
            )
            return  # can't proceed without project path

    # --- Durable enriched stub (optional, read-only check) ---
    generated_stub = Path(uproject).parent / "Intermediate" / "PythonStub" / "unreal.py"
    durable_stub = resolve_plugin_data_dir(
        ctx.project_dir,
        marketplace="plugins-kit",
        plugin="unreal-kit",
        config=ctx.config,
    ) / "unreal.py"
    if not durable_stub.exists() or (
        generated_stub.exists()
        and not filecmp.cmp(generated_stub, durable_stub, shallow=False)
    ):
        defer = getattr(ctx, "add_deferred_requirement", None)
        if defer:
            defer(
                "unreal_enriched_stub",
                user_msg="The durable enriched stub is absent or stale.",
                agent_msg="Run unreal-kit's explicit refresh action if enriched search is needed.",
                satisfied_by="python ${CLAUDE_PLUGIN_ROOT}/scripts/refresh_unreal_stub.py --project-root <project-root>",
            )
        ctx.log_ok("Enriched UE stub refresh deferred to explicit action")
```

## Library Usage

| Source | Operation | Primitive |
|--------|-----------|-----------|
| Manifest | Add `~/.local/bin` to PATH | `ensure_path_entry()` |
| Manifest | Verify `uv`, `git`, `curl` installed | `check_tool()` |
| Manifest | Create/validate venv with `upyrc`, `yaml` | `ensure_venv()` |
| Manifest | Write UE editor settings to `UserEngine.ini` | `ensure_ini_setting()` |
| Manifest | Download and extract `unreal-stub` wheel | `ensure_pypi_package()` |
| Script | Discover `.uproject` from CWD | Custom (`discover_uproject()`) |
| Script | Discover engine directory | Custom (`discover_engine()`) |
| Script | Check durable enriched stub without writing | Custom (`filecmp.cmp`, `add_deferred_requirement`, `log_ok`) |
| Explicit action | Announce and refresh durable enriched stub | `scripts/refresh_unreal_stub.py` |

## Observations

- Most complex bootstrap of the three — but the manifest handles the bulk of operations, leaving the script focused on domain-specific discovery
- The hybrid split is clean: manifest for "ensure X exists," script for "figure out where X is"
- Custom bootstrap logic is limited to UE-specific discovery and a read-only enriched-stub check
- Three distinct fix-all/fixed scenarios:
  1. **fix-all**: Missing tools → install commands (manifest-driven)
  2. **fix-all**: Unknown project path → ask user (script-driven)
  3. **fixed**: Editor settings written → user restarts editor (manifest-driven, with fixed directive)
- The `ini_settings` manifest entry depends on `${uproject_dir}` — the engine gracefully skips entries with unresolved variables, so the manifest and script cooperate: first run discovers the project (script), subsequent runs apply ini settings (manifest)
- Stubs have two tiers: stock PyPI data is machine-local and automatic; the enriched stub is durable consuming-project data and only an explicit human-invoked refresh may write it. API search prefers enriched, then stock, and reports an actionable unavailable message when neither exists.
