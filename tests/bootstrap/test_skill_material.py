"""Tests for bootstrap_lib.skill_material -- the core rows.

The module turns a caller's selection of skills into one text block for a
system prompt plus a provenance report: a strict reader that never degrades,
the frozen format "1", a token budget that refuses, duplicate suppression,
and a capability marker. The declared-resource rules are tested in
``test_skill_material_declared.py``.

Every expected value below is written out as a literal, never produced by
calling the renderer under test.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bootstrap_lib import skill_material as sm


MODULE_PATH = Path(sm.__file__)
REPO_ROOT = MODULE_PATH.parents[3]
REPO_SKILLS = sorted(REPO_ROOT.glob("plugins/*/skills/*/SKILL.md"))

BIG = 1_000_000

PREAMBLE = (
    "Skill material selected by the caller for this request. A skill at "
    'level "catalog" is listed by name and description only; its '
    "instructions are not included and cannot be loaded in this request."
)

GOLDEN = (
    '<skill_context version="1">\n'
    "Skill material selected by the caller for this request. A skill at "
    'level "catalog" is listed by name and description only; its '
    "instructions are not included and cannot be loaded in this request.\n"
    '<skill name="alpha" level="full">\n'
    "<description>Alpha does A.</description>\n"
    "<instructions>\n"
    "# Alpha\n"
    "\n"
    "Step one.\n"
    "</instructions>\n"
    '<resource path="references/notes.md">\n'
    "Note line.\n"
    "</resource>\n"
    "</skill>\n"
    '<skill name="beta" level="catalog">\n'
    "<description>Beta does B.</description>\n"
    "</skill>\n"
    "</skill_context>"
)


def skill_text(name="alpha", description="Alpha does A.", body="# Alpha\n\nStep one."):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def write_skill(root, dirname="alpha", *, text=None, raw=None, files=None, **fields):
    """Create `root/dirname/SKILL.md` (and `files`) and return the directory."""
    skill_dir = Path(root) / dirname
    skill_dir.mkdir(parents=True, exist_ok=True)
    if raw is None:
        fields.setdefault("name", dirname)
        raw = (text if text is not None else skill_text(**fields)).encode("utf-8")
    (skill_dir / "SKILL.md").write_bytes(raw)
    for relative, content in (files or {}).items():
        target = skill_dir.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        target.write_bytes(content)
    return skill_dir


def select(*refs, budget=BIG, **kwargs):
    return sm.SkillSelection(skills=tuple(refs), token_budget=budget, **kwargs)


def render(*refs, budget=BIG, **kwargs):
    return sm.materialize(select(*refs, budget=budget), **kwargs)


def dir_link(link: Path, target: Path) -> None:
    """A directory link, or skip where the host cannot make one."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        import _winapi

        try:
            _winapi.CreateJunction(str(target), str(link))
            return
        except OSError:
            pass
    pytest.skip("this host cannot create a directory link")


def golden_selection(root):
    alpha = write_skill(root, "alpha", files={"references/notes.md": "Note line.\n"})
    beta = write_skill(root, "beta", description="Beta does B.", body="Hidden body.")
    return select(
        sm.SkillRef(str(alpha), resources=("references/notes.md",)),
        sm.SkillRef(str(beta), level="catalog"),
    )


# --------------------------------------------------------------------------
# Format "1"
# --------------------------------------------------------------------------


def test_render_bytes_exact_full_and_catalog(tmp_path) -> None:
    result = sm.materialize(golden_selection(tmp_path))
    assert result.text == GOLDEN
    assert not result.text.endswith("\n")


def test_explicit_format_1_matches_golden(tmp_path) -> None:
    selection = golden_selection(tmp_path)
    explicit = sm.SkillSelection(
        skills=selection.skills, token_budget=BIG, format_version="1"
    )
    result = sm.materialize(explicit)
    assert result.text == GOLDEN
    assert result.report.format_version == "1"


def test_default_format_version_is_1(tmp_path) -> None:
    selection = select(sm.SkillRef(str(write_skill(tmp_path))))
    assert selection.format_version == "1"
    assert sm.materialize(selection).report.format_version == "1"
    assert selection.to_json()["format_version"] == "1"


def test_unknown_format_version_refused_names_registered(tmp_path) -> None:
    selection = sm.SkillSelection(
        skills=(sm.SkillRef(str(write_skill(tmp_path))),),
        token_budget=BIG,
        format_version="2",
    )
    with pytest.raises(sm.SkillMaterialError, match=r"'2' is not registered; registered: 1$"):
        sm.materialize(selection)


def test_supported_formats_are_the_registered_renderers() -> None:
    assert sm.SUPPORTED_FORMATS == frozenset({"1"})
    assert frozenset(sm._RENDERERS) == frozenset({"1"})


def test_render_keeps_caller_order(tmp_path) -> None:
    zeta = write_skill(tmp_path, "zeta")
    alpha = write_skill(tmp_path, "alpha")
    mid = write_skill(tmp_path, "mid")
    result = render(sm.SkillRef(str(zeta)), sm.SkillRef(str(alpha)), sm.SkillRef(str(mid)))
    assert [skill.name for skill in result.report.skills] == ["zeta", "alpha", "mid"]
    text = result.text
    assert text.index('<skill name="zeta"') < text.index('<skill name="alpha"')
    assert text.index('<skill name="alpha"') < text.index('<skill name="mid"')


def test_crlf_skill_renders_identically_to_lf(tmp_path) -> None:
    lf = skill_text(body="Line one.\n\nLine two.")
    resource = "First.\nSecond.\n"
    lf_dir = write_skill(tmp_path / "lf", "alpha", text=lf, files={"r.md": resource})
    crlf_dir = write_skill(
        tmp_path / "crlf", "alpha",
        raw=lf.replace("\n", "\r\n").encode("utf-8"),
        files={"r.md": resource.replace("\n", "\r\n")},
    )
    lf_result = render(sm.SkillRef(str(lf_dir), resources=("r.md",)))
    crlf_result = render(sm.SkillRef(str(crlf_dir), resources=("r.md",)))
    assert crlf_result.text == lf_result.text
    assert "\r" not in crlf_result.text
    assert "<instructions>\nLine one.\n\nLine two.\n</instructions>" in crlf_result.text
    assert crlf_result.report.digest == lf_result.report.digest


def test_lone_carriage_returns_become_newlines(tmp_path) -> None:
    skill = write_skill(tmp_path, files={"r.md": b"one\rtwo\r"})
    result = render(sm.SkillRef(str(skill), resources=("r.md",)))
    assert '<resource path="r.md">\none\ntwo\n</resource>' in result.text


def test_leading_bom_removed_before_frontmatter_match(tmp_path) -> None:
    plain = write_skill(tmp_path / "plain", "alpha", files={"r.md": "Text."})
    bom = write_skill(
        tmp_path / "bom", "alpha",
        raw=b"\xef\xbb\xbf" + skill_text().encode("utf-8"),
        files={"r.md": b"\xef\xbb\xbfText."},
    )
    plain_result = render(sm.SkillRef(str(plain), resources=("r.md",)))
    bom_result = render(sm.SkillRef(str(bom), resources=("r.md",)))
    assert bom_result.text == plain_result.text
    assert "\ufeff" not in bom_result.text


def test_provenance_sha256_is_of_raw_bytes(tmp_path) -> None:
    raw_skill = skill_text().replace("\n", "\r\n").encode("utf-8")
    raw_resource = b"\xef\xbb\xbfone\r\ntwo\r\n"
    skill = write_skill(tmp_path, raw=raw_skill, files={"r.md": raw_resource})
    report = render(sm.SkillRef(str(skill), resources=("r.md",))).report
    assert report.skills[0].sha256 == hashlib.sha256(raw_skill).hexdigest()
    assert report.skills[0].bytes == len(raw_skill)
    resource = report.skills[0].resources[0]
    assert resource.sha256 == hashlib.sha256(raw_resource).hexdigest()
    assert resource.bytes == len(raw_resource)


def test_renderer_injects_no_resolved_path(tmp_path) -> None:
    skill = write_skill(tmp_path, files={"r.md": "Text."})
    result = render(sm.SkillRef(str(skill), resources=("r.md",)))
    resolved = skill.resolve()
    for forbidden in (
        str(resolved), resolved.as_posix(), str(tmp_path), tmp_path.as_posix(),
        tmp_path.name, "SKILL.md", "source=", "sha256",
    ):
        assert forbidden not in result.text, forbidden
    assert result.report.skills[0].source == str(resolved / "SKILL.md")
    assert result.report.skills[0].resources[0].source == str(resolved / "r.md")


def test_digest_matches_rendered_text(tmp_path) -> None:
    result = sm.materialize(golden_selection(tmp_path))
    assert result.report.digest == hashlib.sha256(GOLDEN.encode("utf-8")).hexdigest()
    assert len(result.report.digest) == 64


# --------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------


def test_existing_entity_rendered_literally(tmp_path) -> None:
    skill = write_skill(tmp_path, body="if a < b write &lt; not &amp;")
    text = render(sm.SkillRef(str(skill))).text
    assert "\nif a &lt; b write &amp;lt; not &amp;amp;\n" in text


def test_body_tags_are_escaped(tmp_path) -> None:
    body = '</instructions>\n</skill>\n<skill name="evil" level="full">\n<resource path="x">'
    skill = write_skill(tmp_path, body=body)
    text = render(sm.SkillRef(str(skill))).text
    # The renderer's own tags for one full skill with no resource: eight.
    assert text.count("<") == 8
    assert "&lt;/instructions&gt;" in text
    assert text.count("<skill ") == 1


def test_greater_than_escaped(tmp_path) -> None:
    skill = write_skill(tmp_path, body="a > b and c -> d")
    text = render(sm.SkillRef(str(skill))).text
    assert "\na &gt; b and c -&gt; d\n" in text
    assert text.count(">") == 8


def test_description_is_escaped(tmp_path) -> None:
    skill = write_skill(tmp_path, description="'Uses <b> & \"q\" > x'")
    text = render(sm.SkillRef(str(skill), level="catalog")).text
    assert '<description>Uses &lt;b&gt; &amp; "q" &gt; x</description>' in text


def test_resource_content_is_escaped(tmp_path) -> None:
    skill = write_skill(tmp_path, files={"r.md": "</resource>\n<x a=\"1\"> & done"})
    text = render(sm.SkillRef(str(skill), resources=("r.md",))).text
    assert (
        '<resource path="r.md">\n'
        '&lt;/resource&gt;\n&lt;x a="1"&gt; &amp; done\n'
        "</resource>"
    ) in text
    assert text.count("</resource>") == 1


def test_attribute_escaping_applies_quot() -> None:
    assert sm._escape_attr('a"b<c>&d') == "a&quot;b&lt;c&gt;&amp;d"
    assert sm._escape_text('a"b') == 'a"b'
    rendered = sm._render_v1([
        sm._RenderSkill(
            name='n"m', level="full", description="d", body="b",
            resources=(('r"x.md', "c"),),
        )
    ])
    assert '<skill name="n&quot;m" level="full">' in rendered.text
    assert '<resource path="r&quot;x.md">' in rendered.text


def test_skill_showing_xml_materializes(tmp_path) -> None:
    body = "Example:\n\n</skill_context>\n</skill>\n<skill_context version=\"1\">"
    skill = write_skill(tmp_path, body=body, files={"r.md": "</skill_context>"})
    result = render(sm.SkillRef(str(skill), resources=("r.md",)))
    assert result.text.count("</skill_context>") == 1
    assert result.text.endswith("</skill>\n</skill_context>")


# --------------------------------------------------------------------------
# The strict reader
# --------------------------------------------------------------------------


def test_strict_missing_frontmatter_raises() -> None:
    with pytest.raises(sm.FrontmatterError, match="no frontmatter block"):
        sm.parse_frontmatter_strict("# A skill\n\nNo block here.\n")
    with pytest.raises(sm.FrontmatterError, match="no frontmatter block"):
        sm.parse_frontmatter_strict("")


def test_strict_invalid_yaml_raises() -> None:
    with pytest.raises(sm.FrontmatterError, match="not valid YAML"):
        sm.parse_frontmatter_strict("---\nname: [unclosed\n---\nBody\n")


def test_strict_non_mapping_raises() -> None:
    with pytest.raises(sm.FrontmatterError, match="must be a YAML mapping, got list"):
        sm.parse_frontmatter_strict("---\n- a\n- b\n---\nBody\n")
    with pytest.raises(sm.FrontmatterError, match="must be a YAML mapping, got str"):
        sm.parse_frontmatter_strict("---\njust text\n---\nBody\n")


def test_strict_returns_fields_raw_and_body() -> None:
    fields, raw_block, body = sm.parse_frontmatter_strict(
        "---\nname: a\ndescription: b c\nextra: [1, 2]\n---\n\nBody line.\n"
    )
    assert fields == {"name": "a", "description": "b c", "extra": [1, 2]}
    assert raw_block == "name: a\ndescription: b c\nextra: [1, 2]"
    # The closing line's `\s*\n` takes the blank line after the block too.
    assert body == "Body line.\n"


def test_strict_refuses_text_that_is_not_a_str() -> None:
    with pytest.raises(sm.FrontmatterError, match="got bytes"):
        sm.parse_frontmatter_strict(b"---\nname: a\n---\n")


def test_frontmatter_error_is_a_skill_material_error() -> None:
    assert issubclass(sm.FrontmatterError, sm.SkillMaterialError)
    assert issubclass(sm.SkillMaterialBudgetExceeded, sm.SkillMaterialError)
    assert issubclass(sm.SkillMaterialError, ValueError)


def _identity_skill(tmp_path, frontmatter: str) -> Path:
    return write_skill(tmp_path, raw=f"---\n{frontmatter}\n---\n\nBody.\n".encode("utf-8"))


def test_missing_name_refused(tmp_path) -> None:
    skill = _identity_skill(tmp_path, "description: d")
    with pytest.raises(sm.SkillMaterialError, match="frontmatter has no `name`"):
        sm.read_skill(skill)


def test_non_string_name_refused(tmp_path) -> None:
    skill = _identity_skill(tmp_path, "name: 12\ndescription: d")
    with pytest.raises(sm.SkillMaterialError, match="`name` must be a string, got int"):
        sm.read_skill(skill)


def test_empty_name_refused(tmp_path) -> None:
    skill = _identity_skill(tmp_path, 'name: ""\ndescription: d')
    with pytest.raises(sm.SkillMaterialError, match="`name` is empty"):
        sm.read_skill(skill)


@pytest.mark.parametrize("name", ["bad name", "-lead", "a/b", "x" * 65, "caf\u00e9"])
def test_invalid_skill_name_refused(tmp_path, name) -> None:
    skill = _identity_skill(tmp_path, f'name: "{name}"\ndescription: d')
    with pytest.raises(sm.SkillMaterialError, match="does not match"):
        sm.read_skill(skill)


def test_valid_skill_names_accepted(tmp_path) -> None:
    for index, name in enumerate(["a", "p4-code-review", "BP_MyActor", "v1.2", "x" * 64]):
        skill = write_skill(tmp_path / str(index), "s", name=name)
        assert sm.read_skill(skill).name == name


def test_missing_description_refused(tmp_path) -> None:
    skill = _identity_skill(tmp_path, "name: alpha")
    with pytest.raises(sm.SkillMaterialError, match="frontmatter has no `description`"):
        sm.read_skill(skill)


def test_non_string_description_refused(tmp_path) -> None:
    skill = _identity_skill(tmp_path, "name: alpha\ndescription: [a, b]")
    with pytest.raises(sm.SkillMaterialError, match="`description` must be a string, got list"):
        sm.read_skill(skill)


def test_empty_description_refused(tmp_path) -> None:
    skill = _identity_skill(tmp_path, 'name: alpha\ndescription: "  "')
    with pytest.raises(sm.SkillMaterialError, match="`description` is empty"):
        sm.read_skill(skill)


def test_identity_refusals_reach_materialize_with_the_ref_index(tmp_path) -> None:
    good = write_skill(tmp_path, "good")
    bad = write_skill(tmp_path, "bad", raw=b"---\ndescription: d\n---\n\nBody.\n")
    with pytest.raises(sm.SkillMaterialError, match=r"^skills\[1\]: frontmatter has no `name`"):
        render(sm.SkillRef(str(good)), sm.SkillRef(str(bad)))
    no_block = write_skill(tmp_path, "noblock", raw=b"# no block\n")
    with pytest.raises(sm.FrontmatterError, match=r"^skills\[1\]: .*no frontmatter block"):
        render(sm.SkillRef(str(good)), sm.SkillRef(str(no_block)))


def test_non_utf8_skill_refused(tmp_path) -> None:
    skill = write_skill(tmp_path, raw=skill_text().encode("utf-8") + b"caf\xe9\n")
    with pytest.raises(sm.SkillMaterialError, match="is not valid UTF-8"):
        sm.read_skill(skill)
    with pytest.raises(sm.SkillMaterialError, match="is not valid UTF-8"):
        render(sm.SkillRef(str(skill)))


def test_non_utf8_resource_refused(tmp_path) -> None:
    skill = write_skill(tmp_path, files={"r.md": b"caf\xe9\n"})
    with pytest.raises(sm.SkillMaterialError, match=r"resource 'r\.md' of skill 'alpha'.*is not valid UTF-8"):
        render(sm.SkillRef(str(skill), resources=("r.md",)))


# --------------------------------------------------------------------------
# Skill paths: the caller is the authority
# --------------------------------------------------------------------------


def test_directory_without_skill_md_refused(tmp_path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(sm.SkillMaterialError, match=r"^skills\[0\]: .* is a directory with no SKILL\.md$"):
        render(sm.SkillRef(str(tmp_path / "empty")))
    with pytest.raises(sm.SkillMaterialError, match="does not resolve to an existing path"):
        render(sm.SkillRef(str(tmp_path / "absent")))


def test_other_file_name_refused(tmp_path) -> None:
    skill = write_skill(tmp_path, files={"README.md": skill_text()})
    with pytest.raises(sm.SkillMaterialError, match=r"is a file that is not named SKILL\.md"):
        render(sm.SkillRef(str(skill / "README.md")))
    assert render(sm.SkillRef(str(skill / "SKILL.md"))).report.skills[0].name == "alpha"


def test_skill_path_absolute_and_parent_accepted(tmp_path) -> None:
    skill = write_skill(tmp_path / "skills", "alpha")
    (tmp_path / "skills" / "other").mkdir()
    absolute = render(sm.SkillRef(str(skill.resolve())))
    parent = render(sm.SkillRef("other/../alpha"), base_dir=tmp_path / "skills")
    outside = render(sm.SkillRef("../skills/alpha/SKILL.md"), base_dir=tmp_path / "skills")
    assert absolute.text == parent.text == outside.text
    assert parent.report.skills[0].source == str(skill.resolve() / "SKILL.md")


def test_symlinked_skill_dir_accepted(tmp_path) -> None:
    skill = write_skill(tmp_path / "real", "alpha", files={"r.md": "Text."})
    link = tmp_path / "link"
    dir_link(link, skill)
    result = render(sm.SkillRef(str(link), resources=("r.md",)))
    assert result.report.skills[0].name == "alpha"
    assert result.report.skills[0].source == str(skill.resolve() / "SKILL.md")
    assert result.report.skills[0].resources[0].source == str(skill.resolve() / "r.md")


def test_relative_path_resolves_against_base_dir(tmp_path, monkeypatch) -> None:
    write_skill(tmp_path / "base", "alpha")
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    assert render(sm.SkillRef("alpha"), base_dir=tmp_path / "base").report.skills[0].name == "alpha"
    assert sm.read_skill("alpha", base_dir=str(tmp_path / "base")).name == "alpha"
    with pytest.raises(sm.SkillMaterialError, match="does not resolve"):
        render(sm.SkillRef("alpha"))
    monkeypatch.chdir(tmp_path / "base")
    assert render(sm.SkillRef("alpha")).report.skills[0].name == "alpha"


def test_read_skill_returns_name_description_body_and_hash(tmp_path) -> None:
    raw = (
        b"---\r\nname: alpha\r\ndescription: Alpha does A.\r\n---\r\n\r\n"
        b"# Alpha\r\n\r\nStep one.\r\n"
    )
    skill = write_skill(tmp_path, raw=raw)
    document = sm.read_skill(skill / "SKILL.md")
    assert document == sm.SkillDocument(
        name="alpha",
        description="Alpha does A.",
        body="# Alpha\n\nStep one.",
        source=str(skill.resolve() / "SKILL.md"),
        sha256=hashlib.sha256(raw).hexdigest(),
        bytes=len(raw),
        declared=(),
        unparsed_yaml_blocks=0,
    )
    assert sm.read_skill(skill) == document


# --------------------------------------------------------------------------
# Resource paths
# --------------------------------------------------------------------------


def _named(path: str) -> sm.SkillRef:
    return sm.SkillRef("anywhere", resources=(path,))


def test_resource_empty_path_refused() -> None:
    with pytest.raises(sm.SkillMaterialError, match="a resource path is empty"):
        _named("")


def test_resource_backslash_refused() -> None:
    with pytest.raises(sm.SkillMaterialError, match="contains a backslash"):
        _named("references\\a.md")


def test_resource_absolute_refused() -> None:
    with pytest.raises(sm.SkillMaterialError, match="is absolute"):
        _named("/etc/passwd")


def test_resource_drive_refused() -> None:
    with pytest.raises(sm.SkillMaterialError, match="names a drive"):
        _named("C:/x.md")
    with pytest.raises(sm.SkillMaterialError, match="names a drive"):
        _named("c:x.md")


def test_resource_empty_segment_refused() -> None:
    with pytest.raises(sm.SkillMaterialError, match="has an empty segment"):
        _named("references//a.md")


def test_resource_dot_segment_refused() -> None:
    with pytest.raises(sm.SkillMaterialError, match=r"has a '\.' segment"):
        _named("./a.md")
    with pytest.raises(sm.SkillMaterialError, match=r"has a '\.' segment"):
        _named("references/./a.md")


def test_resource_parent_segment_refused() -> None:
    with pytest.raises(sm.SkillMaterialError, match=r"has a '\.\.' segment"):
        _named("../a.md")
    with pytest.raises(sm.SkillMaterialError, match=r"has a '\.\.' segment"):
        _named("references/../../a.md")


@pytest.mark.parametrize("path", ["my file.md", "a/b c/d.md", "caf\u00e9.md", "a*.md", "ab:cd"])
def test_resource_disallowed_character_refused(path) -> None:
    with pytest.raises(sm.SkillMaterialError, match=r"has a segment outside \[A-Za-z0-9\._-\]"):
        _named(path)


def test_well_formed_resource_paths_accepted() -> None:
    ref = sm.SkillRef("anywhere", resources=("a.md", "references/a-b_c.1.md", ".hidden/x"))
    assert ref.resources == ("a.md", "references/a-b_c.1.md", ".hidden/x")


def test_contained_helper_rejects_outside_root() -> None:
    root = Path("/srv/skills/alpha")
    assert sm._contained(root, Path("/srv/skills/alpha/references/a.md")) is True
    assert sm._contained(root, Path("/srv/skills/alpha")) is True
    assert sm._contained(root, Path("/srv/skills/beta/a.md")) is False
    assert sm._contained(root, Path("/srv/skills/alphabet/a.md")) is False
    assert sm._contained(root, Path("/srv/skills")) is False
    assert sm._contained(root, Path("/etc/passwd")) is False


def test_named_resource_outside_the_skill_refused(tmp_path, monkeypatch) -> None:
    skill = write_skill(tmp_path, files={"a.md": "A", "b.md": "B"})
    inside = skill.resolve()
    seen = []

    def contained(root, candidate):
        seen.append((root, candidate))
        return candidate.name != "b.md"

    monkeypatch.setattr(sm, "_contained", contained)
    with pytest.raises(
        sm.SkillMaterialError,
        match=r"^skills\[0\]: resource 'b\.md' of skill 'alpha' resolves outside the skill directory$",
    ):
        render(sm.SkillRef(str(skill), resources=("a.md", "b.md")))
    assert seen == [(inside, inside / "a.md"), (inside, inside / "b.md")]


def test_resource_symlink_escape_refused(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    skill = write_skill(tmp_path / "skills", "alpha")
    dir_link(skill / "linked", outside)
    with pytest.raises(sm.SkillMaterialError, match="resolves outside the skill directory"):
        render(sm.SkillRef(str(skill), resources=("linked/secret.md",)))


def test_named_resource_directory_refused(tmp_path) -> None:
    skill = write_skill(tmp_path, files={"references/a.md": "A"})
    with pytest.raises(sm.SkillMaterialError, match="is not a regular file; a named resource is one file"):
        render(sm.SkillRef(str(skill), resources=("references",)))


def test_named_resource_missing_refused(tmp_path) -> None:
    skill = write_skill(tmp_path)
    with pytest.raises(sm.SkillMaterialError, match=r"resource 'gone\.md' of skill 'alpha' does not exist"):
        render(sm.SkillRef(str(skill), resources=("gone.md",)))


def test_catalog_ref_with_resources_refused() -> None:
    with pytest.raises(sm.SkillMaterialError, match="names files on a catalog ref"):
        sm.SkillRef("anywhere", level="catalog", resources=("a.md",))


# --------------------------------------------------------------------------
# Estimate and budget
# --------------------------------------------------------------------------


def test_estimate_is_ceil_chars_over_4(tmp_path) -> None:
    assert sm.estimate_tokens("") == 0
    assert sm.estimate_tokens("a") == 1
    assert sm.estimate_tokens("abcd") == 1
    assert sm.estimate_tokens("abcde") == 2
    assert sm.estimate_tokens("a b c d") == 2
    assert sm.estimate_tokens("x" * 4000) == 1000
    assert sm.estimate_tokens("x" * 4001) == 1001
    # Code points, not bytes: four two-byte characters are one token.
    assert sm.estimate_tokens("\u00e9" * 4) == 1
    assert sm.TOKEN_ESTIMATE == "chars/4"
    result = sm.materialize(golden_selection(tmp_path))
    assert len(GOLDEN) == 519
    assert result.report.estimated_tokens == 130
    assert result.report.token_estimate == "chars/4"


def _two_bodies(tmp_path):
    one = write_skill(tmp_path, "one", body="one " * 50)
    two = write_skill(tmp_path, "two", body="two " * 50)
    return sm.SkillRef(str(one)), sm.SkillRef(str(two), level="catalog")


def test_over_budget_refused_itemizes_each_skill(tmp_path) -> None:
    refs = _two_bodies(tmp_path)
    with pytest.raises(sm.SkillMaterialBudgetExceeded) as caught:
        render(*refs, budget=70)
    message = str(caught.value)
    assert message.startswith(
        "skill material is over budget: estimated 160 tokens (chars/4), budget 70."
    )
    assert "\n  skill 'one' (full): 79\n" in message
    assert "\n  skill 'two' (catalog): 21\n" in message
    assert 'give a skill level "catalog"' in message
    assert "declared_resources=False" in message


def test_over_budget_returns_nothing_partial(tmp_path) -> None:
    refs = _two_bodies(tmp_path)
    fitted = render(*refs, budget=160)
    assert fitted.report.estimated_tokens == 160
    assert fitted.report.token_budget == 160
    assert fitted.text.endswith("</skill>\n</skill_context>")
    assert "one one one" in fitted.text
    with pytest.raises(sm.SkillMaterialBudgetExceeded, match="Nothing was truncated and nothing is returned"):
        render(*refs, budget=159)


def _spy_reads(monkeypatch):
    reads = []
    real = sm._read_bytes

    def spy(path):
        reads.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(sm, "_read_bytes", spy)
    return reads


def test_oversize_resource_refused_before_read(tmp_path, monkeypatch) -> None:
    # 100 tokens of budget admit a file of at most 1600 bytes.
    skill = write_skill(tmp_path, files={"big.md": "x" * 1601, "ok.md": "x" * 8})
    reads = _spy_reads(monkeypatch)
    with pytest.raises(sm.SkillMaterialBudgetExceeded) as caught:
        render(sm.SkillRef(str(skill), resources=("ok.md", "big.md")), budget=100)
    message = str(caught.value)
    assert "resource 'big.md' of skill 'alpha'" in message
    assert "is 1601 bytes, so it estimates to at least 101 tokens (chars/4), over the budget of 100" in message
    assert "It was not read" in message
    assert reads == ["SKILL.md", "ok.md"]


def test_oversize_skill_md_refused_before_read(tmp_path, monkeypatch) -> None:
    skill = write_skill(tmp_path, body="x" * 2000)
    reads = _spy_reads(monkeypatch)
    with pytest.raises(sm.SkillMaterialBudgetExceeded, match="It was not read"):
        render(sm.SkillRef(str(skill)), budget=100)
    assert reads == []


def test_catalog_ref_skill_md_is_guarded_before_read(tmp_path, monkeypatch) -> None:
    # Plan 2.4 step 2 guards every selected SKILL.md before it is read, at
    # either level: a catalog ref's SKILL.md is refused from its size alone.
    skill = write_skill(tmp_path, body="x" * 20000)
    reads = _spy_reads(monkeypatch)
    with pytest.raises(sm.SkillMaterialBudgetExceeded) as caught:
        render(sm.SkillRef(str(skill), level="catalog"), budget=100)
    message = str(caught.value)
    assert message.startswith("skills[0]: ")
    assert "estimates to at least" in message
    assert "over the budget of 100. It was not read" in message
    assert reads == []


def test_skill_md_refusal_remedy_depends_on_the_refs_level(tmp_path) -> None:
    # A catalog ref cannot be lowered, so its refusal names only a larger
    # budget; a full ref's refusal keeps the text consumers may match on.
    skill = write_skill(tmp_path, body="x" * 20000)
    skill_md = skill.resolve() / "SKILL.md"
    size = skill_md.stat().st_size
    lower = -(-size // 16)
    head = (
        f"skills[0]: {skill_md} is {size} bytes, so it estimates to at least "
        f"{lower} tokens (chars/4), over the budget of 100. It was not read "
        "and nothing is returned. "
    )
    with pytest.raises(sm.SkillMaterialBudgetExceeded) as catalog:
        render(sm.SkillRef(str(skill), level="catalog"), budget=100)
    assert str(catalog.value) == head + (
        'To fit: raise token_budget. The ref is already at level "catalog", '
        "and a SKILL.md is checked before it is read at either level."
    )
    with pytest.raises(sm.SkillMaterialBudgetExceeded) as full:
        render(sm.SkillRef(str(skill)), budget=100)
    assert str(full.value) == head + (
        'To fit: give a skill level "catalog", or set declared_resources=False '
        "on it and name a shorter list in resources."
    )


def test_preread_guard_admits_multibyte_text_within_budget(tmp_path) -> None:
    # 800 four-byte code points: 3200 bytes, 800 characters, 200 tokens.
    content = "\U0001F600" * 800
    skill = write_skill(tmp_path, files={"emoji.md": content})
    result = render(sm.SkillRef(str(skill), resources=("emoji.md",)), budget=400)
    resource = result.report.skills[0].resources[0]
    assert resource.bytes == 3200
    assert resource.estimated_tokens == 210
    assert result.report.estimated_tokens == 304
    assert sm._size_lower_bound(3200) == 200
    assert sm._size_lower_bound(3201) == 201
    assert sm._size_lower_bound(0) == 0


def test_each_file_is_read_once(tmp_path, monkeypatch) -> None:
    skill = write_skill(tmp_path, files={"r.md": "Text."})
    reads = _spy_reads(monkeypatch)
    render(
        sm.SkillRef(str(skill), level="catalog"),
        sm.SkillRef(str(skill), resources=("r.md",)),
        sm.SkillRef(str(skill / "SKILL.md"), resources=("r.md",)),
    )
    assert reads == ["SKILL.md", "r.md"]


# --------------------------------------------------------------------------
# Duplicate suppression
# --------------------------------------------------------------------------


def test_same_file_twice_merged_level_max_resources_union(tmp_path) -> None:
    skill = write_skill(tmp_path, files={"a.md": "A", "b.md": "B"})
    other = write_skill(tmp_path, "other")
    result = render(
        sm.SkillRef(str(skill), level="catalog"),
        sm.SkillRef(str(other), level="catalog"),
        sm.SkillRef(str(skill / "SKILL.md"), resources=("a.md",)),
        sm.SkillRef(str(skill), resources=("b.md", "a.md")),
    )
    report = result.report
    assert [(item.name, item.level) for item in report.skills] == [
        ("alpha", "full"), ("other", "catalog"),
    ]
    assert [item.path for item in report.skills[0].resources] == ["a.md", "b.md"]
    assert [item.ref_index for item in report.skills[0].resources] == [2, 3]
    assert report.suppressed == (
        sm.SuppressedRef(ref_index=2, name="alpha", reason="same-file", kept_index=0),
        sm.SuppressedRef(ref_index=3, name="alpha", reason="same-file", kept_index=0),
    )
    assert result.text.count('<skill name="alpha"') == 1
    assert result.text.index('<skill name="alpha"') < result.text.index('<skill name="other"')


def test_same_name_same_content_suppressed(tmp_path) -> None:
    one = write_skill(tmp_path / "one", "alpha")
    two = write_skill(tmp_path / "two", "alpha")
    result = render(sm.SkillRef(str(one), level="catalog"), sm.SkillRef(str(two)))
    assert [(item.name, item.level) for item in result.report.skills] == [("alpha", "full")]
    assert result.report.skills[0].source == str(one.resolve() / "SKILL.md")
    assert result.report.suppressed == (
        sm.SuppressedRef(ref_index=1, name="alpha", reason="same-content", kept_index=0),
    )
    assert result.text.count("<skill ") == 1


def test_mirrored_ref_resource_resolves_against_its_own_dir(tmp_path) -> None:
    one = write_skill(tmp_path / "one", "alpha", files={"only-one.md": "One."})
    two = write_skill(tmp_path / "two", "alpha", files={"only-two.md": "Two."})
    result = render(
        sm.SkillRef(str(one), resources=("only-one.md",)),
        sm.SkillRef(str(two), resources=("only-two.md",)),
    )
    resources = result.report.skills[0].resources
    assert [(item.path, item.ref_index, item.source) for item in resources] == [
        ("only-one.md", 0, str(one.resolve() / "only-one.md")),
        ("only-two.md", 1, str(two.resolve() / "only-two.md")),
    ]


def test_mirrored_identical_resource_rendered_once_and_reported(tmp_path) -> None:
    one = write_skill(tmp_path / "one", "alpha", files={"shared.md": "Same."})
    two = write_skill(tmp_path / "two", "alpha", files={"shared.md": "Same."})
    result = render(
        sm.SkillRef(str(one), resources=("shared.md",)),
        sm.SkillRef(str(two), resources=("shared.md",)),
    )
    assert result.text.count('<resource path="shared.md">') == 1
    assert [item.source for item in result.report.skills[0].resources] == [
        str(one.resolve() / "shared.md"),
    ]
    assert result.report.suppressed == (
        sm.SuppressedRef(ref_index=1, name="alpha", reason="same-content", kept_index=0),
        sm.SuppressedRef(
            ref_index=1, name="alpha", reason="duplicate-resource",
            kept_index=0, detail="shared.md",
        ),
    )


@pytest.mark.parametrize("other", ["Different.", "Originel."])
def test_mirrored_conflicting_resource_refused_names_both_files(tmp_path, other) -> None:
    one = write_skill(tmp_path / "one", "alpha", files={"shared.md": "Original."})
    two = write_skill(tmp_path / "two", "alpha", files={"shared.md": other})
    with pytest.raises(sm.SkillMaterialError) as caught:
        render(
            sm.SkillRef(str(one), resources=("shared.md",)),
            sm.SkillRef(str(two), resources=("shared.md",)),
        )
    message = str(caught.value)
    assert "resource 'shared.md' of skill 'alpha' is ambiguous" in message
    assert str(one.resolve() / "shared.md") in message
    assert str(two.resolve() / "shared.md") in message


def test_same_name_different_content_refused_names_both_paths(tmp_path) -> None:
    one = write_skill(tmp_path / "one", "alpha", body="First body.")
    two = write_skill(tmp_path / "two", "alpha", body="Second body.")
    with pytest.raises(sm.SkillMaterialError) as caught:
        render(sm.SkillRef(str(one)), sm.SkillRef(str(two)))
    message = str(caught.value)
    assert message.startswith("skills[1]: skill name 'alpha' is defined with different content")
    assert str(one.resolve() / "SKILL.md") in message
    assert str(two.resolve() / "SKILL.md") in message


def test_resources_are_never_merged_across_different_skills(tmp_path) -> None:
    one = write_skill(tmp_path, "one", files={"shared.md": "Same."})
    two = write_skill(tmp_path, "two", files={"shared.md": "Same."})
    result = render(
        sm.SkillRef(str(one), resources=("shared.md",)),
        sm.SkillRef(str(two), resources=("shared.md",)),
    )
    assert result.text.count('<resource path="shared.md">') == 2
    assert result.report.suppressed == ()


# --------------------------------------------------------------------------
# Construction: types only, no I/O
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["", None, 3, Path("x"), b"x"])
def test_skillref_path_must_be_non_empty_str(path) -> None:
    with pytest.raises(sm.SkillMaterialError, match="path must be a non-empty str"):
        sm.SkillRef(path)


@pytest.mark.parametrize("level", ["FULL", "summary", "", None, 1, ["full"]])
def test_unknown_level_refused(level) -> None:
    with pytest.raises(sm.SkillMaterialError, match="level must be 'catalog' or 'full'"):
        sm.SkillRef("x", level=level)


@pytest.mark.parametrize("value", [1, 0, None, "true"])
def test_declared_resources_must_be_bool(value) -> None:
    with pytest.raises(sm.SkillMaterialError, match="declared_resources must be a bool"):
        sm.SkillRef("x", declared_resources=value)


@pytest.mark.parametrize("value", [["a.md"], "a.md", None, (1,), ("a.md", None)])
def test_resources_must_be_tuple_of_str(value) -> None:
    with pytest.raises(sm.SkillMaterialError, match="resources must be a tuple of str"):
        sm.SkillRef("x", resources=value)


def test_bad_resource_refused_at_construction_without_io(tmp_path) -> None:
    missing = str(tmp_path / "no-such-skill")
    with pytest.raises(sm.SkillMaterialError, match=r"has a '\.\.' segment"):
        sm.SkillRef(missing, resources=("ok.md", "../escape.md"))
    # Construction touches no file: a ref to a path that does not exist is
    # built, and only `materialize` finds out.
    ref = sm.SkillRef(missing, resources=("ok.md",))
    selection = select(ref)
    with pytest.raises(sm.SkillMaterialError, match="does not resolve to an existing path"):
        sm.materialize(selection)


@pytest.mark.parametrize("skills", [(), [], None, "x"])
def test_empty_skills_refused(skills) -> None:
    with pytest.raises(sm.SkillMaterialError, match="skills must be a non-empty tuple of SkillRef"):
        sm.SkillSelection(skills=skills, token_budget=10)


def test_skills_elements_must_be_skillref() -> None:
    with pytest.raises(sm.SkillMaterialError, match="must hold SkillRef values only"):
        sm.SkillSelection(skills=(sm.SkillRef("x"), "y"), token_budget=10)
    with pytest.raises(sm.SkillMaterialError, match="must hold SkillRef values only"):
        sm.SkillSelection(skills=({"path": "x"},), token_budget=10)


def test_token_budget_bool_refused() -> None:
    with pytest.raises(sm.SkillMaterialError, match="token_budget must be an int"):
        sm.SkillSelection(skills=(sm.SkillRef("x"),), token_budget=True)


@pytest.mark.parametrize("budget", [0, -1])
def test_token_budget_must_be_positive(budget) -> None:
    with pytest.raises(sm.SkillMaterialError, match="token_budget must be greater than 0"):
        sm.SkillSelection(skills=(sm.SkillRef("x"),), token_budget=budget)


@pytest.mark.parametrize("budget", [1.5, "10", None])
def test_token_budget_must_be_an_int(budget) -> None:
    with pytest.raises(sm.SkillMaterialError, match="token_budget must be an int"):
        sm.SkillSelection(skills=(sm.SkillRef("x"),), token_budget=budget)


def test_token_budget_has_no_default() -> None:
    with pytest.raises(TypeError):
        sm.SkillSelection(skills=(sm.SkillRef("x"),))


@pytest.mark.parametrize("version", [1, None, 1.0])
def test_format_version_must_be_str(version) -> None:
    with pytest.raises(sm.SkillMaterialError, match="format_version must be a str"):
        sm.SkillSelection(skills=(sm.SkillRef("x"),), token_budget=10, format_version=version)


def test_materialize_takes_a_selection() -> None:
    with pytest.raises(sm.SkillMaterialError, match="takes a SkillSelection, got dict"):
        sm.materialize({"skills": [], "token_budget": 1})


# --------------------------------------------------------------------------
# JSON documents
# --------------------------------------------------------------------------


SELECTION_DOCUMENT = {
    "skills": [
        {
            "path": "skills/alpha",
            "level": "full",
            "declared_resources": False,
            "resources": ["references/a.md", "b.md"],
        },
        {
            "path": "skills/beta/SKILL.md",
            "level": "catalog",
            "declared_resources": True,
            "resources": [],
        },
    ],
    "token_budget": 4000,
    "format_version": "1",
}


def test_selection_json_round_trip() -> None:
    selection = sm.SkillSelection.from_json(SELECTION_DOCUMENT)
    assert selection == sm.SkillSelection(
        skills=(
            sm.SkillRef(
                "skills/alpha", level="full", declared_resources=False,
                resources=("references/a.md", "b.md"),
            ),
            sm.SkillRef("skills/beta/SKILL.md", level="catalog"),
        ),
        token_budget=4000,
    )
    assert selection.to_json() == SELECTION_DOCUMENT
    assert json.loads(json.dumps(selection.to_json())) == SELECTION_DOCUMENT
    assert sm.SkillSelection.from_json(selection.to_json()) == selection


def test_from_json_applies_the_defaults() -> None:
    selection = sm.SkillSelection.from_json({"skills": [{"path": "a"}], "token_budget": 5})
    assert selection == sm.SkillSelection(skills=(sm.SkillRef("a"),), token_budget=5)
    assert selection.skills[0] == sm.SkillRef(
        "a", level="full", declared_resources=True, resources=()
    )


def test_from_json_unknown_key_refused() -> None:
    with pytest.raises(sm.SkillMaterialError, match=r"the selection has unknown key\(s\) budget, extra; known: skills, token_budget, format_version"):
        sm.SkillSelection.from_json(
            {"skills": [{"path": "a"}], "token_budget": 5, "extra": 1, "budget": 2}
        )
    with pytest.raises(sm.SkillMaterialError, match=r"skills\[1\] has unknown key\(s\) name; known: path, level, declared_resources, resources"):
        sm.SkillSelection.from_json(
            {"skills": [{"path": "a"}, {"path": "b", "name": "b"}], "token_budget": 5}
        )


@pytest.mark.parametrize(
    "document, message",
    [
        ([], "a selection document must be a mapping"),
        ({"token_budget": 5}, "the selection has no `skills`"),
        ({"skills": [{"path": "a"}]}, "the selection has no `token_budget`"),
        ({"skills": {"path": "a"}, "token_budget": 5}, "`skills` must be a list"),
        ({"skills": ["a"], "token_budget": 5}, r"skills\[0\] must be a mapping"),
        ({"skills": [{}], "token_budget": 5}, r"skills\[0\] has no `path`"),
        (
            {"skills": [{"path": "a", "resources": "a.md"}], "token_budget": 5},
            r"skills\[0\]\.resources must be a list",
        ),
        ({"skills": [], "token_budget": 5}, "skills must be a non-empty tuple"),
        ({"skills": [{"path": "a"}], "token_budget": "5"}, "token_budget must be an int"),
        (
            {"skills": [{"path": "a", "level": "deep"}], "token_budget": 5},
            "level must be 'catalog' or 'full'",
        ),
    ],
)
def test_from_json_refuses_a_malformed_document(document, message) -> None:
    with pytest.raises(sm.SkillMaterialError, match=message):
        sm.SkillSelection.from_json(document)


def _json_native(value) -> bool:
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_native(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_json_native(item) for item in value)
    return value is None or isinstance(value, (str, int, float, bool))


def test_report_to_json_round_trips_through_json(tmp_path) -> None:
    one = write_skill(tmp_path / "one", "alpha", files={"shared.md": "Same."})
    two = write_skill(tmp_path / "two", "alpha", files={"shared.md": "Same."})
    beta = write_skill(tmp_path, "beta", description="Beta does B.")
    result = render(
        sm.SkillRef(str(one), resources=("shared.md",)),
        sm.SkillRef(str(two), resources=("shared.md",)),
        sm.SkillRef(str(beta), level="catalog"),
        budget=5000,
    )
    document = result.report.to_json()
    assert _json_native(document)
    assert json.loads(json.dumps(document)) == document
    raw_skill = (one / "SKILL.md").read_bytes()
    assert document == {
        "schema": "plugins-kit.skill-material-report/v1",
        "format_version": "1",
        "skills": [
            {
                "name": "alpha",
                "level": "full",
                "source": str(one.resolve() / "SKILL.md"),
                "sha256": hashlib.sha256(raw_skill).hexdigest(),
                "bytes": len(raw_skill),
                "estimated_tokens": 45,
                "declared": [],
                "unparsed_yaml_blocks": 0,
                "resources": [
                    {
                        "path": "shared.md",
                        "ref_index": 0,
                        "declared": False,
                        "source": str(one.resolve() / "shared.md"),
                        "sha256": hashlib.sha256(b"Same.").hexdigest(),
                        "bytes": 5,
                        "estimated_tokens": 12,
                    }
                ],
            },
            {
                "name": "beta",
                "level": "catalog",
                "source": str(beta.resolve() / "SKILL.md"),
                "sha256": hashlib.sha256((beta / "SKILL.md").read_bytes()).hexdigest(),
                "bytes": len((beta / "SKILL.md").read_bytes()),
                "estimated_tokens": 21,
                "declared": [],
                "unparsed_yaml_blocks": 0,
                "resources": [],
            },
        ],
        "suppressed": [
            {"ref_index": 1, "name": "alpha", "reason": "same-content", "kept_index": 0, "detail": ""},
            {"ref_index": 1, "name": "alpha", "reason": "duplicate-resource", "kept_index": 0, "detail": "shared.md"},
        ],
        "estimated_tokens": 126,
        "token_budget": 5000,
        "token_estimate": "chars/4",
        "digest": hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
    }


def test_report_schema_is_the_declared_constant(tmp_path) -> None:
    report = render(sm.SkillRef(str(write_skill(tmp_path)))).report
    assert sm.REPORT_SCHEMA_V1 == "plugins-kit.skill-material-report/v1"
    assert report.schema == "plugins-kit.skill-material-report/v1"
    assert report.to_json()["schema"] == "plugins-kit.skill-material-report/v1"


def test_supported_report_schemas_is_the_only_marker() -> None:
    assert isinstance(sm.SUPPORTED_REPORT_SCHEMAS, frozenset)
    assert "plugins-kit.skill-material-report/v1" in sm.SUPPORTED_REPORT_SCHEMAS
    assert sm.SUPPORTED_REPORT_SCHEMAS == frozenset({"plugins-kit.skill-material-report/v1"})
    # One marker: the documented surface, and no other version or capability name.
    assert sorted(sm.__all__) == sorted([
        "REPORT_SCHEMA_V1", "SUPPORTED_REPORT_SCHEMAS", "SUPPORTED_FORMATS",
        "LEVEL_CATALOG", "LEVEL_FULL", "TOKEN_ESTIMATE", "FRONTMATTER_RE",
        "SkillMaterialError", "FrontmatterError", "SkillMaterialBudgetExceeded",
        "SkillMaterialUnavailableError", "PyYamlUnavailableError",
        "parse_frontmatter_strict", "SkillDocument", "read_skill", "SkillRef",
        "SkillSelection", "ResourceProvenance", "SkillProvenance",
        "SuppressedRef", "SkillMaterialReport", "MaterializedSkills",
        "materialize", "estimate_tokens",
    ])
    assert len(sm.__all__) == len(set(sm.__all__))
    for name in sm.__all__:
        assert hasattr(sm, name), name
    markers = [
        name for name in vars(sm)
        if not name.startswith("_") and name.isupper() and isinstance(
            getattr(sm, name), (int, float, set, frozenset)
        )
    ]
    assert sorted(markers) == ["SUPPORTED_FORMATS", "SUPPORTED_REPORT_SCHEMAS"]


def test_frozen_literals() -> None:
    assert sm.LEVEL_CATALOG == "catalog"
    assert sm.LEVEL_FULL == "full"
    assert sm.FRONTMATTER_RE.pattern == r"\A---\s*\n(.*?)\n---\s*\n"
    assert sm.FRONTMATTER_RE.flags & __import__("re").DOTALL
    assert sm._V1_PREAMBLE == PREAMBLE


def test_call_shapes_a_consumer_probe_binds() -> None:
    inspect.signature(sm.SkillSelection.from_json).bind({})
    inspect.signature(sm.materialize).bind(object(), base_dir=None)
    inspect.signature(sm.SkillMaterialReport.to_json).bind(object())
    inspect.signature(sm.parse_frontmatter_strict).bind("")
    inspect.signature(sm.read_skill).bind("x", base_dir=None)
    with pytest.raises(TypeError):
        inspect.signature(sm.materialize).bind(object(), None)


# --------------------------------------------------------------------------
# Module boundary
# --------------------------------------------------------------------------


def _imports(nodes, *, descend_functions: bool):
    imported, relative = set(), []
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.append(node.lineno)
            elif node.module:
                imported.add(node.module.split(".")[0])
        if not descend_functions and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return imported, relative


def test_module_top_imports_stdlib_only() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported, relative = _imports(tree.body, descend_functions=False)
    assert not relative, f"relative imports at lines {relative}"
    assert imported, "the walk found no module-level import"
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported - set(
        sys.stdlib_module_names
    )
    assert "yaml" not in imported


def test_module_imports_no_first_party_package() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported, relative = _imports(tree.body, descend_functions=True)
    assert not relative, f"relative imports at lines {relative}"
    assert "yaml" in imported, "the walk did not reach the function-level imports"
    third_party = imported - set(sys.stdlib_module_names) - {"__future__"}
    # PyYAML is the one import outside the stdlib, and nothing is first-party.
    assert third_party == {"yaml"}
    for first_party in ("bootstrap_lib", "llm_scripting_kit", "skills_kit_lib", "content_pipeline"):
        assert first_party not in imported


def _child(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(MODULE_PATH.parents[1])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True,
    )


def test_module_imports_without_yaml() -> None:
    done = _child(
        "import sys\n"
        "sys.modules['yaml'] = None\n"
        "import bootstrap_lib.skill_material as module\n"
        "assert 'yaml' not in vars(module)\n"
        "print(module.REPORT_SCHEMA_V1)\n"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "plugins-kit.skill-material-report/v1"


def test_read_without_yaml_raises_pyyaml_unavailable(tmp_path, monkeypatch) -> None:
    skill = write_skill(tmp_path)
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(sm.PyYamlUnavailableError, match="PyYAML is not importable"):
        sm.read_skill(skill)
    with pytest.raises(sm.PyYamlUnavailableError, match="PyYAML is not importable"):
        sm.parse_frontmatter_strict(skill_text())


def test_pyyaml_unavailable_is_not_a_skill_material_error(tmp_path, monkeypatch) -> None:
    assert not issubclass(sm.PyYamlUnavailableError, sm.SkillMaterialError)
    assert not issubclass(sm.SkillMaterialError, sm.PyYamlUnavailableError)
    assert not issubclass(sm.SkillMaterialUnavailableError, sm.SkillMaterialError)
    assert not issubclass(sm.PyYamlUnavailableError, ValueError)
    assert issubclass(sm.PyYamlUnavailableError, sm.SkillMaterialUnavailableError)
    assert issubclass(sm.SkillMaterialUnavailableError, RuntimeError)
    skill = write_skill(tmp_path)
    monkeypatch.setitem(sys.modules, "yaml", None)
    caught_as_content = False
    with pytest.raises(sm.PyYamlUnavailableError):
        try:
            sm.read_skill(skill)
        except sm.SkillMaterialError:
            caught_as_content = True
    assert caught_as_content is False


def test_materialize_without_yaml_raises_pyyaml_unavailable(tmp_path, monkeypatch) -> None:
    skill = write_skill(tmp_path)
    selection = select(sm.SkillRef(str(skill)))
    monkeypatch.setitem(sys.modules, "yaml", None)
    caught_as_content = False
    with pytest.raises(sm.PyYamlUnavailableError, match="PyYAML is not importable"):
        try:
            sm.materialize(selection)
        except sm.SkillMaterialError:
            caught_as_content = True
    assert caught_as_content is False


def test_module_parses_as_python_3_10() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 10))


def test_module_source_is_ascii() -> None:
    MODULE_PATH.read_bytes().decode("ascii")


# --------------------------------------------------------------------------
# The specification names what the module freezes
# --------------------------------------------------------------------------


PLUGIN_DEV = MODULE_PATH.parent.parent / "skills" / "plugin-dev"


def test_reference_names_the_frozen_contract() -> None:
    reference = (PLUGIN_DEV / "references" / "skill-material.md").read_text(
        encoding="utf-8"
    )
    assert reference.isascii()
    for literal in (
        "plugins-kit.skill-material-report/v1",
        "bootstrap_lib.skill_material",
        "[A-Za-z0-9][A-Za-z0-9._-]{0,63}",
        "chars/4",
        "0.138.0",
        PREAMBLE,
        '<skill_context version="1">',
        "</skill_context>",
    ):
        assert literal in reference, literal
    for name in sm.__all__:
        assert f"`{name}" in reference, name
    skill = (PLUGIN_DEV / "SKILL.md").read_text(encoding="utf-8")
    assert "path: references/skill-material.md" in skill


# --------------------------------------------------------------------------
# The repo's own skills
# --------------------------------------------------------------------------


def test_corpus_has_at_least_20_skills() -> None:
    assert len(REPO_SKILLS) >= 20, len(REPO_SKILLS)


def test_every_repo_skill_passes_the_strict_reader() -> None:
    names = {}
    for path in REPO_SKILLS:
        document = sm.read_skill(path)
        names.setdefault(document.name, []).append(path)
    assert {name: paths for name, paths in names.items() if len(paths) > 1} == {}
    assert len(names) == len(REPO_SKILLS)
