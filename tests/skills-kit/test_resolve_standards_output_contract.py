"""Pinning: audit-lane.md's documented resolve_standards.py output key set
matches what the script actually emits.

resolve_standards.py's `out` dict carries five keys (disabled, thresholds,
standards, audit, notes) and exits 1 with a stderr-only message on
StandardsConfigError. audit-lane.md must document all five, the non-zero-exit
contract, and that a non-empty `notes` must be surfaced verbatim.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_LANE_MD = (
    REPO_ROOT
    / "plugins"
    / "skills-kit"
    / "skills"
    / "md-domain"
    / "references"
    / "lanes"
    / "audit-lane.md"
)
SCRIPT = REPO_ROOT / "plugins" / "skills-kit" / "scripts" / "resolve_standards.py"


def _documented_keys() -> list[str]:
    text = AUDIT_LANE_MD.read_text(encoding="utf-8")
    m = re.search(r"parse its JSON `\{([^}]*)\}`", text, re.I)
    assert m, "audit-lane.md has no `parse its JSON { ... }` sentence"
    return [tok.strip() for tok in m.group(1).split(",") if tok.strip()]


def _script_out_keys() -> list[str]:
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"out = \{\n(.*?)\n    \}\n", src, re.S)
    assert m, "resolve_standards.py has no `out = { ... }` dict literal"
    return re.findall(r'"([a-z_]+)":', m.group(1))


def test_documented_keys_match_script_output_keys():
    documented = set(_documented_keys())
    actual = set(_script_out_keys())
    assert documented == actual, (documented, actual)


def test_script_exits_nonzero_on_config_error(tmp_path):
    # A malformed config.yaml root triggers StandardsConfigError.
    config_dir = tmp_path / "config" / "skills-kit"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("- not-a-mapping\n", encoding="utf-8")
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "config"), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--project-root", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.strip()


def test_output_carries_the_audit_block_with_its_default(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    env = {"CLAUDE_CONFIG_DIR": str(config_dir), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--project-root", str(tmp_path)],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["audit"] == {"fix_mode": "apply"}


def test_output_reflects_a_configured_fix_mode_and_rejects_a_bad_one(tmp_path):
    config_dir = tmp_path / "config" / "skills-kit"
    config_dir.mkdir(parents=True)
    cfg = config_dir / "config.yaml"
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "config"), "PATH": "/usr/bin:/bin"}
    cmd = [sys.executable, str(SCRIPT), "--project-root", str(tmp_path)]
    cfg.write_text("audit:\n  fix_mode: propose\n", encoding="utf-8")
    ok = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert json.loads(ok.stdout)["audit"] == {"fix_mode": "propose"}
    cfg.write_text("audit:\n  fix_mode: maybe\n", encoding="utf-8")
    bad = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert bad.returncode != 0 and bad.stdout == "" and "fix_mode" in bad.stderr
