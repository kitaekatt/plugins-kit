"""I2: classify's suggested_type / tie-break reason must be deterministic
across interpreter starts (PYTHONHASHSEED), not depend on set iteration
order. markdown_heuristics.CANONICAL_TYPES was a set, so type_signals'
scores dict inherited set iteration order and classify's stable sort
resolved ties by it.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_KIT_ROOT = REPO_ROOT / "plugins" / "skills-kit"

# A body scoring two types to an exact tie so the tie-break path is exercised:
# has_recognition_marker + has_counter_example -> pattern-skill = 1 + 2 = 3;
# has_excuse_reality_table + has_red_green_refactor -> discipline-skill =
# 2 + 2 = ... use a body that ties two types at score 1 via single markers.
TIE_BODY = textwrap.dedent(
    """\
    ---
    name: s
    ---
    # S

    This skill will recognize the situation.

    | a | b | c |
    |---|---|---|
    | 1 | 2 | 3 |
    """
)


def _run(tmp_path: Path, hash_seed: str) -> dict:
    skill_dir = tmp_path / f"seed-{hash_seed}"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(TIE_BODY, encoding="utf-8")

    script = textwrap.dedent(
        f"""
        import sys, json
        sys.path.insert(0, {str(SKILLS_KIT_ROOT)!r})
        from pathlib import Path
        from skills_kit_lib.classify import classify
        print(json.dumps(classify(Path({str(skill_md)!r}))))
        """
    )
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hash_seed
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return json.loads(result.stdout)


class TestClassifyDeterministicAcrossHashSeeds:
    def test_identical_json_under_two_hash_seeds(self, tmp_path):
        report_0 = _run(tmp_path, "0")
        report_1 = _run(tmp_path, "1")
        report_0.pop("path")
        report_1.pop("path")
        assert report_0 == report_1, (report_0, report_1)
