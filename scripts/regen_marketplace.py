#!/usr/bin/env python3
"""Regenerate .claude-plugin/marketplace.json from per-plugin plugin.json files.

Marketplace.json is treated as derived data:
- Top-level fields ($schema, name, description, owner) are preserved verbatim.
- The plugins[] array is rebuilt from plugins/<name>/.claude-plugin/plugin.json,
  filtered by the "published" field (missing = true; false = excluded).

Existing plugin ordering in marketplace.json is preserved; new plugins (newly
"published": true) are appended alphabetically.

Usage:
  python scripts/regen_marketplace.py                     # rewrite marketplace.json
  python scripts/regen_marketplace.py --check             # working tree; exit non-zero on drift
  python scripts/regen_marketplace.py --check --staged    # index-aware, for the pre-commit hook
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGINS_DIR = REPO_ROOT / "plugins"

TOP_LEVEL_KEYS = ("$schema", "name", "description", "owner")
DEFAULT_CATEGORY = "development"


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _git(args: list[str]) -> str | None:
    """Run git and return stdout, or None when git cannot answer."""
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def staged_paths() -> list[str] | None:
    """Repo-relative staged paths, or None when Git does not answer."""
    out = _git(["diff", "--cached", "--name-only"])
    if out is None:
        return None
    return [line for line in out.splitlines() if line]


def _read(path: Path, *, from_index: bool) -> str | None:
    """File text, read from the index when asked (falling back to the worktree).

    The index is what a plain `git commit` turns into history, so it -- not the
    worktree -- is the thing a pre-commit check must judge. Falling back rather
    than failing keeps an unreadable-Git clone committable; the publish preflight
    is the gate that cannot be missed.
    """
    if from_index:
        out = _git(["show", f":{_rel(path)}"])
        if out is not None:
            return out
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _load_plugin_manifests(*, from_index: bool = False) -> dict[str, dict]:
    """Return {plugin_name: manifest_dict} for every plugin on disk."""
    manifests = {}
    for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
        pj_path = plugin_dir / ".claude-plugin" / "plugin.json"
        if not pj_path.is_file():
            continue
        text = _read(pj_path, from_index=from_index)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"error: {pj_path}: {e}", file=sys.stderr)
            sys.exit(1)
        name = data.get("name") or plugin_dir.name
        data["__dir"] = plugin_dir.name
        manifests[name] = data
    return manifests


def _build_entry(manifest: dict) -> dict:
    """Project a plugin.json into a marketplace.json plugins[] entry."""
    entry = {
        "name": manifest["name"],
        "description": manifest.get("description", ""),
        "version": manifest.get("version", ""),
        "author": manifest.get("author", {"name": ""}),
        "source": f"./plugins/{manifest['__dir']}",
        "category": manifest.get("category", DEFAULT_CATEGORY),
    }
    # Propagate inter-plugin dependencies so they are declared in both plugin.json
    # and the marketplace entry (the spec accepts either location).
    if manifest.get("dependencies"):
        entry["dependencies"] = manifest["dependencies"]
    return entry


def _is_published(manifest: dict) -> bool:
    return manifest.get("published", True) is not False


def regenerate(*, from_index: bool = False) -> dict:
    """Return the regenerated marketplace.json contents."""
    if not MARKETPLACE_JSON.is_file():
        print(f"error: {MARKETPLACE_JSON} not found", file=sys.stderr)
        sys.exit(1)
    current_text = _read(MARKETPLACE_JSON, from_index=from_index)
    if current_text is None:
        print(f"error: {MARKETPLACE_JSON} not readable", file=sys.stderr)
        sys.exit(1)
    current = json.loads(current_text)
    manifests = _load_plugin_manifests(from_index=from_index)

    published = {name: m for name, m in manifests.items() if _is_published(m)}

    # Preserve existing order from marketplace.json; append new published plugins
    # alphabetically. Plugins flipped to published: false drop out silently.
    existing_order = [p.get("name") for p in current.get("plugins", [])]
    seen: set[str] = set()
    ordered_names: list[str] = []
    for name in existing_order:
        if name in published and name not in seen:
            ordered_names.append(name)
            seen.add(name)
    for name in sorted(published):
        if name not in seen:
            ordered_names.append(name)
            seen.add(name)

    out = {}
    for k in TOP_LEVEL_KEYS:
        if k in current:
            out[k] = current[k]
    out["plugins"] = [_build_entry(published[name]) for name in ordered_names]
    return out


def _serialize(data: dict) -> str:
    """Stable JSON serialization matching repo conventions (2-space indent, trailing newline)."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _is_derivation_input(path: str) -> bool:
    """Does this repo-relative path participate in the marketplace derivation?"""
    return path == _rel(MARKETPLACE_JSON) or (
        path.startswith("plugins/") and path.endswith("/.claude-plugin/plugin.json")
    )


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    # --staged judges the INDEX (what a plain `git commit` will record) instead
    # of the worktree, and only when the commit actually touches the derivation.
    # Both halves matter, and the second is the one that unblocks the repo's
    # stated workflow -- commit and push freely, gate publishes:
    #
    #   * A worktree check answers the wrong question. It fails on edits you are
    #     NOT committing -- in this shared tree, another session's in-flight
    #     version bump blocked every unrelated commit -- and it passes on a
    #     genuinely inconsistent pair that IS staged, because history is built
    #     from the index, not the worktree. It is both too strict and too loose.
    #   * A commit staging neither marketplace.json nor any plugin.json cannot
    #     change their relationship, so it has nothing to answer for. Pre-existing
    #     drift is the fault of the commit that introduced it.
    #
    # Nothing is given up by relaxing this: publish.py REGENERATES marketplace.json
    # and then verifies plugin.json agreement before pushing, so drift cannot
    # reach master (and master is what consumers fetch). Mirrors the index-aware
    # convention already used by check-staged-version-bump.sh and
    # generate_orchestration.py --check; this script was the last worktree holdout.
    staged_mode = "--staged" in argv

    from_index = False
    if staged_mode:
        staged = staged_paths()
        if staged is None:
            # Git could not answer -- fall back to the worktree check rather than
            # skipping. A check that silently passes when its input is missing is
            # not a check.
            print(
                "regen_marketplace: could not read the index; "
                "checking the working tree instead.",
                file=sys.stderr,
            )
        elif not any(_is_derivation_input(p) for p in staged):
            return 0
        else:
            from_index = True

    regenerated = regenerate(from_index=from_index)
    new_text = _serialize(regenerated)
    current_text = _read(MARKETPLACE_JSON, from_index=from_index) or ""

    if check_only:
        if new_text != current_text:
            where = "staged" if from_index else "working-tree"
            print(
                f"marketplace.json is out of sync with its {where} plugin.json sources.\n"
                "Run: python scripts/regen_marketplace.py",
                file=sys.stderr,
            )
            return 1
        return 0

    current_text = MARKETPLACE_JSON.read_text(encoding="utf-8")

    if new_text == current_text:
        print("marketplace.json already up to date.")
        return 0
    MARKETPLACE_JSON.write_text(new_text, encoding="utf-8")
    print(f"wrote {MARKETPLACE_JSON.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
