"""The maintainer lock pins the released library that the consumer targets name.

``data/package-targets.json`` holds the library versions that bootstrap installs
into plugin venvs (the latest release of each major). The root ``uv.lock`` is
the environment that this repository's plugin and integration tests run in. A
lock that lags the target runs those tests against a library that no plugin
user has: a change in the new release (a refused call, a new CLI group) shows
up as a failure only after the lock is refreshed, long after the release.

Rule: for every package that both files name, the version in ``uv.lock`` is one
of the versions in ``package-targets.json``.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK = REPO_ROOT / "uv.lock"
TARGETS = REPO_ROOT / "data" / "package-targets.json"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def lock_versions(lock_text: str) -> dict[str, str]:
    """Package name -> locked version, from the text of a ``uv.lock``."""
    packages = tomllib.loads(lock_text).get("package", [])
    return {_norm(p["name"]): p["version"] for p in packages if "version" in p}


def target_versions(targets: dict) -> dict[str, set[str]]:
    """Package name -> the versions the consumer targets name."""
    return {_norm(name): set(versions) for name, versions in targets["packages"].items()}


def mismatches(locked: dict[str, str], targets: dict[str, set[str]]) -> list[str]:
    """One message for each package that both name where the lock is not a target."""
    problems = []
    for name in sorted(set(locked) & set(targets)):
        if locked[name] not in targets[name]:
            problems.append(
                f"uv.lock pins {name} {locked[name]}, but data/package-targets.json names "
                f"{', '.join(sorted(targets[name]))}. Refresh the lock to the target: "
                f"`uv lock --upgrade-package {name} --refresh-package {name}`, then commit uv.lock "
                f"(the root lock only; never a plugin lock)."
            )
    return problems


def test_the_root_lock_pins_the_imported_targets() -> None:
    locked = lock_versions(LOCK.read_text(encoding="utf-8"))
    targets = target_versions(json.loads(TARGETS.read_text(encoding="utf-8")))
    assert set(locked) & set(targets), "the lock and the targets share no package, so this guard checks nothing"
    assert not mismatches(locked, targets), "\n".join(mismatches(locked, targets))


def test_the_guard_names_katsoft_llm_kit_in_both_files() -> None:
    locked = lock_versions(LOCK.read_text(encoding="utf-8"))
    targets = target_versions(json.loads(TARGETS.read_text(encoding="utf-8")))
    assert "katsoft-llm-kit" in locked and "katsoft-llm-kit" in targets


class TestTheGuardCanFail:
    LOCK_TEXT = (
        '[[package]]\nname = "katsoft-llm-kit"\nversion = "1.24.0"\n\n'
        '[[package]]\nname = "katsoft_contracts"\nversion = "1.0.1"\n\n'
        '[[package]]\nname = "unrelated"\nversion = "9.9.9"\n'
    )

    def test_a_lagging_lock_is_reported_with_the_refresh_command(self) -> None:
        targets = {"katsoft-llm-kit": {"1.37.0"}, "katsoft-contracts": {"1.0.1"}}
        problems = mismatches(lock_versions(self.LOCK_TEXT), targets)
        assert len(problems) == 1
        assert "katsoft-llm-kit 1.24.0" in problems[0] and "1.37.0" in problems[0]
        assert "uv lock --upgrade-package katsoft-llm-kit" in problems[0]

    def test_a_lock_on_a_target_passes_and_names_are_normalised(self) -> None:
        targets = {"katsoft-llm-kit": {"1.24.0", "2.0.0"}, "katsoft-contracts": {"1.0.1"}}
        assert not mismatches(lock_versions(self.LOCK_TEXT), targets)

    def test_a_package_only_one_file_names_is_not_compared(self) -> None:
        assert not mismatches(lock_versions(self.LOCK_TEXT), {"other": {"1.0.0"}})
