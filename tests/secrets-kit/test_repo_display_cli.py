"""I18 / SK-SOL-15: the CLI's own 'cloning <url> ...' print must be sanitized.

Loaded via importlib rather than a package import, matching the pattern the
rest of this suite uses for ``secrets_kit_cli.py`` (a script, not a package
module). The clone directory is never created -- ``_ensure_clone`` is
exercised directly with ``repo_mod.clone`` faked, so no real git call and no
real credential are ever involved.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cli():
    source = ROOT / "plugins/secrets-kit/scripts/secrets_kit_cli.py"
    spec = importlib.util.spec_from_file_location("dummy_display_cli", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ensure_clone_prints_a_sanitized_url_but_clones_the_original(cli, tmp_path, monkeypatch, capsys):
    url = "https://fixture-user:fixture-token@example.invalid/acct/fleet-secrets.git"
    config = cli.Config(tmp_path / "secrets.json", {"repo": url})

    captured = {}

    def fake_clone(repo_url, dest):
        captured["repo_url"] = repo_url
        dest.mkdir(parents=True)

    monkeypatch.setattr(cli.repo_mod, "clone", fake_clone)

    cli._ensure_clone(config, data_dir=tmp_path / "data")

    out = capsys.readouterr().out
    assert "fixture-user" not in out and "fixture-token" not in out
    assert "example.invalid/acct/fleet-secrets.git" in out
    # The subprocess-facing call still gets the original, credential-bearing URL.
    assert captured["repo_url"] == url
