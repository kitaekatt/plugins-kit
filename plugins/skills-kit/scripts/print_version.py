#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

# CLAUDE_PLUGIN_ROOT is expanded by Claude Code only in a hook's command field;
# it is unset in a Bash-tool launch. This script lives at <plugin root>/scripts/,
# so its own location answers "which version of this plugin am I". Deriving from
# __file__ is legitimate here because the question is provenance of the running
# code, not where durable data goes (the durability-root rule does not apply).
plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(
    Path(__file__).resolve().parent.parent
)

manifest_path = os.path.join(plugin_root, ".claude-plugin", "plugin.json")
try:
    with open(manifest_path, "r", encoding="utf-8") as f:
        d = json.load(f)
except (OSError, ValueError) as exc:
    # Fail loudly: a provenance line that cannot be established must not exit 0.
    print(f"print_version: cannot read {manifest_path}: {exc}", file=sys.stderr)
    sys.exit(1)

print(f"Running {d.get('name', '<unnamed>')}@{d.get('version', '<unversioned>')}")

# Soft bootstrap-provisioning check. Never exit -- the banner above must always
# print. Kept stdlib-only since this script runs under a bare/uv interpreter.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from bootstrap_guard import is_provisioned

    if not is_provisioned("skills-kit"):
        print(
            "[skills-kit] bootstrap has not provisioned skills-kit -- schema "
            "validation and helper scripts are unavailable. Install/enable the "
            "'plugins-kit:bootstrap' plugin and start a new session.",
            file=sys.stderr,
        )
except Exception:
    pass
