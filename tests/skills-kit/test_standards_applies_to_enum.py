"""I9: standards_resolve._parse_standards_file validates `applies_to` only
as a non-empty string; the four primitives (skill_md, claude_md,
reference_doc, plain_md -- schemas/standards.py, configuring-standards.md)
are not enforced, so a typo (e.g. `skill-md`) silently unions under a
phantom key no lane ever queries, and resolve_standards.py exits 0 --
violating the documented "configuration errors are loud, never silent"
contract.
"""

import textwrap

import pytest

from skills_kit_lib.standards_resolve import StandardsConfigError, resolve


def _user_layer(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    layer = config_dir / "skills-kit"
    layer.mkdir(parents=True)
    return layer


_BAD_APPLIES_TO_MD = textwrap.dedent(
    """\
    # SKILL standards

    ```yaml
    standards_set:
      identity: Optional description-hygiene standards for SKILL.md files.
      applies_to: skill-md
      criteria:
        - id: desc-160-char
          statement: The description frontmatter field is at most 160 characters.
          severity: fail
          keywords: [description length, 160 char, hygiene]
    ```
    """
)


def test_off_enum_applies_to_raises(tmp_path, monkeypatch):
    layer = _user_layer(tmp_path, monkeypatch)
    (layer / "SKILL-standards.md").write_text(_BAD_APPLIES_TO_MD, encoding="utf-8")
    with pytest.raises(StandardsConfigError) as exc:
        resolve(None)
    msg = str(exc.value)
    assert "skill-md" in msg
    for primitive in ("skill_md", "claude_md", "reference_doc", "plain_md"):
        assert primitive in msg, msg
