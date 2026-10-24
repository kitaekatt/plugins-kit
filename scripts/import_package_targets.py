#!/usr/bin/env python3
"""Import verified library releases into the marketplace's offline target artifact.

Run after katsoft-libs records a successful release. This command reads a fixed
Git commit and verifies selected wheel hashes on PyPI; bootstrap never does so.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

REPOSITORY = "https://github.com/kitaekatt/katsoft-libs"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "package-targets.json"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def project_targets(ledger: dict[str, Any], commit: str) -> dict[str, Any]:
    """Keep each distribution's highest verified final release in every major."""
    packages: dict[str, dict[str, dict[str, str]]] = {}
    if not isinstance(ledger, dict) or not ledger:
        raise ValueError("the release ledger must be a nonempty mapping")
    for dist, versions in sorted(ledger.items()):
        if not re.fullmatch(r"katsoft-[a-z0-9]+(?:-[a-z0-9]+)*", dist) or not isinstance(versions, dict) or not versions:
            raise ValueError(f"invalid ledger distribution {dist!r}")
        selected: dict[int, tuple[tuple[int, ...], str]] = {}
        for version, entry in versions.items():
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ValueError(f"unsupported ledger release {dist} {version}")
            if (not isinstance(entry, dict) or entry.get("version") != version
                    or entry.get("tag") != f"{dist}-v{version}"
                    or not re.fullmatch(r"[0-9a-f]{40}", str(entry.get("commit", "")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("wheel_sha256", "")))
                    or not entry.get("verified_at") or not entry.get("filename")):
                raise ValueError(f"unverified or malformed ledger entry {dist} {version}")
            parts = tuple(map(int, version.split(".")))
            if parts > selected.get(parts[0], ((-1,), ""))[0]:
                selected[parts[0]] = (parts, version)
        packages[dist] = {version: {"version": version}
                          for _parts, version in sorted(selected.values())}
    return {"schema": 1, "source": {"repository": REPOSITORY, "commit": commit},
            "packages": packages}


def verify_pypi(dist: str, version: str, entry: dict[str, Any]) -> None:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{dist}/{version}/json", timeout=30) as response:
        data = json.load(response)
    matching = [row for row in data.get("urls", [])
                if row.get("filename") == entry["filename"] and not row.get("yanked")
                and row.get("digests", {}).get("sha256") == entry["wheel_sha256"]]
    if len(matching) != 1:
        raise ValueError(f"PyPI does not expose the verified non-yanked wheel for {dist} {version}")


def import_targets(library_repo: Path, ref: str, output: Path) -> dict[str, Any]:
    commit = git(library_repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    registry = tomllib.loads(git(library_repo, "show", f"{commit}:packages/registry.toml"))
    if registry.get("repository") != REPOSITORY:
        raise ValueError(f"the library registry must identify {REPOSITORY}")
    ledger = json.loads(git(library_repo, "show", f"{commit}:packages/released.json"))
    artifact = project_targets(ledger, commit)
    for dist, versions in artifact["packages"].items():
        for version in versions:
            verify_pypi(dist, version, ledger[dist][version])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="ascii")
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-repo", type=Path, required=True)
    parser.add_argument("--ref", required=True, help="Library commit or ref to resolve once")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    artifact = import_targets(args.library_repo, args.ref, args.output)
    print(f"imported {len(artifact['packages'])} distributions from {artifact['source']['commit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
