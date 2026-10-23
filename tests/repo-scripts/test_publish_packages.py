"""scripts/publish.py with the package release machinery: preflight policy,
frozen identities (P, B, F), and the recovery gate.

Real git against a bare origin in a temporary directory, as in
test_publish.py. PyPI, the GitHub CLI and wheel builds are the in-memory
`World` from test_package_release_states.py; marketplace regeneration is a
stub that reads the temporary index, because the fixture has no generator.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "publish_under_package_test", _HERE.parents[1] / "scripts" / "publish.py")
publish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish)

_states_spec = importlib.util.spec_from_file_location(
    "package_release_helpers", _HERE / "test_package_release_states.py")
h = importlib.util.module_from_spec(_states_spec)
_states_spec.loader.exec_module(h)

import sys
sys.path.insert(0, str(_HERE.parents[1] / "scripts"))
import _packages  # noqa: E402
rb = _packages.rb
DIST, TAG = h.DIST, h.TAG


def _stub_regen(env: dict) -> dict:
    """regen_marketplace.regenerate(from_index=True), for the fixture: one
    entry per published plugin at the version in the temporary index."""
    listing = publish.git("ls-files", "-z", "--",
                          "plugins/*/.claude-plugin/plugin.json", env=env)
    plugins = []
    for path in filter(None, listing.split("\0")):
        data = json.loads(publish.git("show", f":{path}", env=env))
        if data.get("published", True):
            plugins.append({"name": data["name"], "version": data["version"]})
    plugins.sort(key=lambda p: p["name"])
    return {".claude-plugin/marketplace.json":
            json.dumps({"plugins": plugins}, indent=2) + "\n"}


class _NoNetwork:
    """Services that fail the test if anything leaves the machine."""

    def services(self) -> _packages.Services:
        def boom(*args, **kwargs):
            raise AssertionError(f"network-facing service called: {args}")
        return _packages.Services(pypi_json=boom, download=boom, gh=boom,
                                  build=boom, smoke=boom, sleep=boom, now=lambda: 0.0,
                                  simple_index=boom)


def _wire(monkeypatch, root: Path, world) -> None:
    monkeypatch.setattr(publish, "REPO_ROOT", root)
    monkeypatch.setattr(publish, "PLUGINS_DIR", root / "plugins")
    monkeypatch.setattr(publish, "MARKETPLACE_JSON",
                        root / ".claude-plugin" / "marketplace.json")
    monkeypatch.setattr(publish, "REAL_PLUGINS_DIR", root / "plugins")
    monkeypatch.setattr(publish, "_regenerate_derived_in", _stub_regen)
    monkeypatch.setattr(publish, "package_services", world.services)


def _bump_plugin(root: Path, version: str, *, regen: bool = True) -> str:
    h.write(root, "plugins/pub-kit/.claude-plugin/plugin.json",
            json.dumps({"name": "pub-kit", "version": version}, indent=2) + "\n")
    if regen:
        h.write(root, ".claude-plugin/marketplace.json", json.dumps(
            {"plugins": [{"name": "pub-kit", "version": version}]}, indent=2) + "\n")
    return h.commit_all(root, f"pub-kit {version}")


def _record_locally(root: Path, version: str = "1.0.0") -> str:
    """Commit a ledger entry for DIST naming the initial commit, as a past
    release would have, and push it."""
    initial = h.git(root, "rev-list", "--max-parents=0", "refs/heads/dev")
    entry = {"version": version, "tag": rb.tag_name(DIST, version),
             "tag_url": rb.tag_url(h.REPOSITORY, rb.tag_name(DIST, version)),
             "commit": initial, "filename": rb.wheel_filename(DIST, version),
             "wheel_sha256": "0" * 64, "verified_at": "2026-01-01T00:00:00Z"}
    h.write(root, rb.RELEASED_PATH, json.dumps({DIST: {version: entry}}, indent=2) + "\n")
    sha = h.commit_all(root, f"record {DIST} {version}")
    h.git(root, "push", "-q", "origin", "dev")
    return sha


def _released_on(root: Path, ref: str) -> dict:
    text = h.git(root, "show", f"{ref}:{rb.RELEASED_PATH}")
    return json.loads(text)


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = h.make_repo(tmp_path)
    _wire(monkeypatch, root, _NoNetwork())
    return root


# --- preflight policy --------------------------------------------------------------

class TestPreflightPolicy:
    def test_only_refused_when_release_set_nonempty(self, root):
        _bump_plugin(root, "1.1.0")
        with pytest.raises(publish.PublishError, match="--only is refused"):
            publish.preflight(only={"pub-kit"})

        _record_locally(root)
        bumps, _ = publish.preflight(only={"pub-kit"})
        assert bumps == ["pub-kit: 1.0.0 -> 1.1.0"]

    def test_workflow_absent_on_master_refuses_release(self, tmp_path, monkeypatch):
        root = h.make_repo(tmp_path, workflow_on_master=False)
        _wire(monkeypatch, root, _NoNetwork())
        _bump_plugin(root, "1.1.0")
        with pytest.raises(publish.PublishError, match="absent"):
            publish.preflight()

        # Recorded: no release pending, so the workflow's absence is no reason
        # to stop a plugin publish -- that publish is what carries it to master.
        _record_locally(root)
        assert publish.preflight()[0] == ["pub-kit: 1.0.0 -> 1.1.0"]

    def test_workflow_differing_on_master_refuses_release(self, root):
        h.write(root, _packages.WORKFLOW_PATH, "name: release-packages\n# edited\n")
        _bump_plugin(root, "1.1.0")
        with pytest.raises(publish.PublishError, match="not the version on dev"):
            publish.preflight()

    def test_unsatisfiable_and_cycle_refuse(self, tmp_path, monkeypatch):
        root = h.make_repo(tmp_path, ((DIST, "alpha"), ("beta-lib", "beta")))
        _wire(monkeypatch, root, _NoNetwork())
        h.write(root, "packages/beta-lib/pyproject.toml",
                h.pyproject("beta-lib", "1.0.0", (f"{DIST}>=2.0",)))
        h.commit_all(root, "beta needs alpha 2")
        with pytest.raises(publish.PublishError, match="unsatisfiable"):
            publish.preflight()

        h.write(root, "packages/beta-lib/pyproject.toml",
                h.pyproject("beta-lib", "1.0.0", (f"{DIST}>=1.0",)))
        h.write(root, f"packages/{DIST}/pyproject.toml",
                h.pyproject(DIST, "1.0.0", ("beta-lib>=1.0",)))
        h.commit_all(root, "a cycle")
        with pytest.raises(publish.PublishError, match="cycle"):
            publish.preflight()

    def test_shipping_plugin_requirement_must_be_satisfiable(self, root):
        _record_locally(root)
        h.write(root, "plugins/pub-kit/pyproject.toml",
                '[project]\nname = "pub-kit"\nversion = "1.1.0"\n'
                f'dependencies = ["{DIST}>=5.0,<6"]\n')
        _bump_plugin(root, "1.1.0")
        with pytest.raises(publish.PublishError, match="unsatisfiable"):
            publish.preflight()

    @pytest.mark.parametrize("requirement", [
        "{d}",                      # no range
        "{d}>=1.0",                 # no upper bound
        "{d}==1.0.0",               # exact pin
        "{d}~=1.0",                 # compatible release
        "{d}>=1.0,<2.0",            # upper bound is not a bare major
        "{d}<2,>=1.0",              # fixed order
        "{d}>=1.0,<2; python_version >= '3.12'",
    ])
    def test_shipping_plugin_requirement_must_use_the_restricted_form(
            self, root, requirement):
        """bootstrap resolves a venv's released-package target from one form only.
        Removing `restricted_form_problems` from `_require_satisfiable` turns this red."""
        _record_locally(root)
        h.write(root, "plugins/pub-kit/pyproject.toml",
                '[project]\nname = "pub-kit"\nversion = "1.1.0"\n'
                f'dependencies = ["{requirement.format(d=DIST)}"]\n')
        _bump_plugin(root, "1.1.0")
        with pytest.raises(publish.PublishError,
                           match=r"(?s)restricted form.*plugins/pub-kit/pyproject.toml"):
            publish.preflight()

    def test_restricted_form_requirement_passes_and_unregistered_is_free(self, root):
        _record_locally(root)
        h.write(root, "plugins/pub-kit/pyproject.toml",
                '[project]\nname = "pub-kit"\nversion = "1.1.0"\n'
                f'dependencies = ["{DIST}[extra]>=1.0,<2", "openai", "pyyaml~=6.0"]\n')
        _bump_plugin(root, "1.1.0")
        assert publish.preflight()[0] == ["pub-kit: 1.0.0 -> 1.1.0"]

    def test_removed_public_name_needs_a_major_bump(self, root):
        _record_locally(root)
        h.write(root, "libs/alpha/__init__.py", '__all__ = ["one"]\none = 1\n')
        h.write(root, f"packages/{DIST}/pyproject.toml", h.pyproject(DIST, "1.1.0"))
        h.commit_all(root, "alpha 1.1.0 drops two")
        with pytest.raises(publish.PublishError, match="without a major bump"):
            publish.preflight()

        h.write(root, f"packages/{DIST}/pyproject.toml", h.pyproject(DIST, "2.0.0"))
        h.commit_all(root, "alpha 2.0.0")
        _, _ = publish.preflight()

    def test_source_drift_is_reported_not_released(self, root):
        _record_locally(root)
        h.write(root, "libs/alpha/more.py", "x = 1\n")
        h.commit_all(root, "alpha: change without a bump")
        _bump_plugin(root, "1.1.0")

        flight = publish._preflight()

        assert flight.packages.release_set == []
        assert any("build inputs changed" in n for n in flight.packages.notes)

    def test_no_release_set_makes_no_network_call(self, root):
        _record_locally(root)
        _bump_plugin(root, "1.1.0")

        assert publish.main([]) == 0

        assert publish.version_at("origin/master", "pub-kit") == "1.1.0"
        assert DIST in _released_on(root, "origin/master")


# --- the whole flow -------------------------------------------------------------

class TestReleaseFlow:
    def _world(self, root, monkeypatch, ci="succeed"):
        world = h.World(root, ci=ci)
        monkeypatch.setattr(publish, "package_services", world.services)
        return world

    def test_package_then_plugin(self, root, monkeypatch):
        world = self._world(root, monkeypatch)
        _bump_plugin(root, "1.1.0")

        assert publish.main([]) == 0

        assert DIST in _released_on(root, "origin/master")
        assert publish.version_at("origin/master", "pub-kit") == "1.1.0"
        assert h.remote_tag(root, TAG)
        assert world.smoked

    def test_a_pending_package_stops_the_projection(self, root, monkeypatch):
        self._world(root, monkeypatch, ci="never")
        _bump_plugin(root, "1.1.0")
        master_before = h.git(root, "rev-parse", "origin/master")

        assert publish.main([]) == 1

        h.git(root, "fetch", "-q", "origin")
        assert h.git(root, "rev-parse", "origin/master") == master_before

    def test_rerun_after_ledger_before_projection_publishes(self, root, monkeypatch):
        """A run recorded the release and stopped before projecting. The next
        run has no plugin bump and an empty R; the unprojected ledger entry is
        what makes it publishable."""
        _record_locally(root)
        assert DIST not in json.loads(
            h.git(root, "show", f"origin/master:{rb.RELEASED_PATH}"))

        assert publish.main([]) == 0

        assert DIST in _released_on(root, "origin/master")

    def test_no_amend_of_derived_or_ledger(self, root, monkeypatch):
        """B is a child of P built in a private index, never an amend of P,
        and the ledger commit is a child of B -- even with P unpushed."""
        self._world(root, monkeypatch)
        p = _bump_plugin(root, "1.1.0", regen=False)   # stale listing: B != P

        assert publish.main([]) == 0

        final = h.git(root, "rev-parse", "origin/dev")
        b = h.git(root, "rev-parse", f"{final}^")
        assert h.git(root, "rev-parse", f"{b}^") == p
        assert json.loads(h.git(root, "show", f"{b}:.claude-plugin/marketplace.json")) == {
            "plugins": [{"name": "pub-kit", "version": "1.1.0"}]}
        assert DIST in _released_on(root, final)
        assert DIST not in _released_on(root, b)
        assert h.git(root, "rev-parse", "dev") == final
        assert h.git(root, "status", "--porcelain") == ""

    @pytest.mark.parametrize("dev_only", [False, True], ids=["fast-forward", "projection"])
    def test_projection_and_ff_use_frozen_F_when_dev_moves(self, root, monkeypatch, dev_only):
        if dev_only:
            h.write(root, "plugins/dev-kit/.claude-plugin/plugin.json",
                    json.dumps({"name": "dev-kit", "version": "0.1.0",
                                "published": False}) + "\n")
            h.commit_all(root, "dev-kit")
        _record_locally(root)
        _bump_plugin(root, "1.1.0")
        real_gate = publish._require_final_is_p_plus_owned
        seen = {}

        def another_session_pushes(p, final):
            real_gate(p, final)
            seen["F"] = final
            h.write(root, "late.txt", "pushed by another session after F\n")
            h.commit_all(root, "another session")
            h.git(root, "push", "-q", "origin", "dev")

        monkeypatch.setattr(publish, "_require_final_is_p_plus_owned",
                            another_session_pushes)

        assert publish.main([]) == 0

        final = seen["F"]
        assert publish.blob_at("origin/master", "late.txt") is None
        if dev_only:
            message = h.git(root, "log", "-1", "--format=%B", "origin/master")
            assert f"Published-From: {final}" in message
        else:
            assert h.git(root, "rev-parse", "origin/master") == final

    def test_verify_compares_master_to_F(self, root, monkeypatch):
        _record_locally(root)
        _bump_plugin(root, "1.1.0")
        assert publish.main([]) == 0
        final = h.git(root, "rev-parse", "origin/master")
        h.write(root, "late.txt", "another session\n")
        h.commit_all(root, "another session")
        h.git(root, "push", "-q", "origin", "dev")

        assert publish.verify(ref=final) == []
        assert any("differs from" in p for p in publish.verify())

    def test_final_outside_publish_owned_paths_refuses(self, root):
        p = h.git(root, "rev-parse", "dev")
        h.write(root, "late.txt", "x\n")
        moved = h.commit_all(root, "moved")
        with pytest.raises(publish.PublishError, match="late.txt"):
            publish._require_final_is_p_plus_owned(p, moved)


# --- the restricted requirement form --------------------------------------------

class TestRestrictedRequirementForm:
    """publish and the engine read one form, through one parser."""

    CASES = [
        (f"{DIST}>=1.0,<2", True),
        (f"{DIST}[sdk, jobs]>=1.2.3,<3", True),
        (f"{DIST}>=1.0", False),
        (f"{DIST}~=1.0", False),
        (f"{DIST}>=1.0,<2.0", False),
        (f"{DIST}>=1,<2", False),
        (DIST, False),
    ]

    def test_publish_and_engine_agree_on_the_form(self):
        sys.path.insert(0, str(_HERE.parents[1] / "plugins" / "bootstrap"))
        from bootstrap_lib.venv_check import parse_restricted_requirement
        registered = {rb.normalize_dist(DIST)}
        for text, ok in self.CASES:
            problems = _packages.restricted_form_problems([text], registered, "x")
            assert (not problems) is ok, text
            assert (parse_restricted_requirement(text) is not None) is ok, text

    def test_an_unregistered_requirement_is_never_a_problem(self):
        assert _packages.restricted_form_problems(
            ["openai", "pyyaml~=6.0", "x>=1"], {rb.normalize_dist(DIST)}, "x") == []

    def test_every_plugin_pyproject_in_this_checkout_uses_the_form(self):
        """Guards the checkout the suite runs in, not a fixture: a plugin that
        requires a package registered in packages/registry.toml in any other
        spelling gets no released-package target from bootstrap."""
        root = _HERE.parents[1]
        registry = rb.parse_registry((root / "packages" / "registry.toml").read_text())
        registered = {rb.normalize_dist(e.name) for e in registry.entries}
        problems = []
        for pyproject in sorted((root / "plugins").glob("*/pyproject.toml")):
            where = str(pyproject.relative_to(root)).replace("\\", "/")
            problems += _packages.restricted_form_problems(
                _packages._requirement_texts(pyproject.read_text(encoding="utf-8")),
                registered, where)
        assert problems == []
