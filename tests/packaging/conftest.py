"""Fixtures for the clean-venv wheel-closure tests.

The closure tests build wheels, create venvs and install third-party packages,
so they are opt-in (marker ``wheel_closure``): a plain run skips them and says
how to enable them. Opt in with ``--wheel-source local|pypi`` or with the
environment variable ``WHEEL_CLOSURE_SOURCE=local|pypi``:

    uv run --extra dev pytest tests/packaging --wheel-source local

``local`` builds both wheels from the working tree (the release builders'
recipe) and installs them. ``pypi`` installs the RELEASED versions named by
packages/*/pyproject.toml from PyPI and expects each marker's COMMIT to equal
the commit packages/released.json records; it fails loudly when the version
packages/released.json records is not on PyPI. pytest registers ``--wheel-source`` only for runs that collect this
directory's conftest at startup (name tests/packaging on the command line); the
environment variable works for a whole-suite run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import closure_support as support  # noqa: E402

SOURCES = ("local", "pypi")


def pytest_addoption(parser):
    parser.addoption("--wheel-source", action="store", choices=SOURCES, default=None,
                     help="enable the wheel-closure tests, installing from 'local' built "
                          "wheels or from 'pypi' releases")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "wheel_closure: builds wheels and venvs and installs third-party "
                   "packages; enabled by --wheel-source or WHEEL_CLOSURE_SOURCE")


def _selected(config) -> str | None:
    chosen = config.getoption("--wheel-source") or os.environ.get("WHEEL_CLOSURE_SOURCE") or None
    if chosen is not None and chosen not in SOURCES:
        raise pytest.UsageError(f"WHEEL_CLOSURE_SOURCE must be one of {SOURCES}, not {chosen!r}")
    return chosen


def pytest_collection_modifyitems(config, items):
    if _selected(config) is not None:
        return
    skip = pytest.mark.skip(reason="wheel-closure tests are opt-in: pass --wheel-source local|pypi "
                                   "(or set WHEEL_CLOSURE_SOURCE)")
    for item in items:
        if "wheel_closure" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def wheel_source(request) -> str:
    return _selected(request.config) or "local"


@pytest.fixture(scope="session")
def local_wheels(tmp_path_factory) -> dict:
    """Both wheels built from the working tree, one build per session."""
    return support.build_local_wheels(tmp_path_factory.mktemp("wheels"))


@pytest.fixture(scope="session")
def expected_commits(wheel_source, local_wheels) -> dict:
    """The build commit each distribution's release marker must carry."""
    if wheel_source == "local":
        return {d: local_wheels["commit"] for d in (support.KIT_DIST, support.CONTRACTS_DIST)}
    return {d: support.released_commit(d) for d in (support.KIT_DIST, support.CONTRACTS_DIST)}


def _requirements(wheel_source, local_wheels, extras):
    if wheel_source == "local":
        return support.local_requirements(local_wheels, extras=extras)
    return support.pypi_requirements(extras=extras)


@pytest.fixture(scope="session")
def closure_venv(wheel_source, local_wheels, tmp_path_factory) -> Path:
    """A fresh venv holding the two distributions and the databench extras, nothing else."""
    return support.make_venv(tmp_path_factory.mktemp("closure-full"),
                             _requirements(wheel_source, local_wheels, support.KIT_EXTRAS))


@pytest.fixture(scope="session")
def base_venv(wheel_source, local_wheels, tmp_path_factory) -> Path:
    """A fresh venv holding the two distributions with no extras (standard library only)."""
    return support.make_venv(tmp_path_factory.mktemp("closure-base"),
                             _requirements(wheel_source, local_wheels, ""))


@pytest.fixture(scope="session")
def stub_endpoint():
    with support.StubEndpoint() as stub:
        yield stub
