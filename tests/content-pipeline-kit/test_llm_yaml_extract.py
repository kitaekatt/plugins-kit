"""Behavioral tests for content_pipeline.llm.yaml_extract.

Translates loc yaml_extract's fenced-block / preamble / malformed cases onto
the generic extractor.
"""

import pytest

from content_pipeline.llm.yaml_extract import (
    YamlExtractionError,
    extract_mapping,
    extract_yaml,
    strip_fence,
)


def test_extract_direct_yaml_no_fence():
    data = extract_yaml("items:\n  - a\n  - b\n")
    assert data == {"items": ["a", "b"]}


def test_extract_fenced_yaml_block():
    text = "Here you go:\n```yaml\nkey: value\n```\nThanks!"
    assert extract_yaml(text) == {"key": "value"}


def test_extract_fence_with_attrs():
    text = "```yaml linenos title=x\nkey: value\n```"
    assert extract_yaml(text) == {"key": "value"}


def test_extract_bare_fence_no_language_tag():
    text = "```\nkey: value\n```"
    assert extract_yaml(text) == {"key": "value"}


def test_extract_yml_alias():
    text = "```yml\nkey: 1\n```"
    assert extract_yaml(text) == {"key": 1}


def test_extract_cjk_roundtrip():
    # Non-ASCII content must round-trip through extraction unharmed. The
    # literal is written as an escape so this source file stays ASCII-only.
    cjk = chr(0x4F60) + chr(0x597D)  # CJK "ni hao"; source stays ASCII-only
    text = f"```yaml\ngreeting: {cjk}\n```"
    assert extract_yaml(text) == {"greeting": cjk}


def test_strip_fence_passthrough_when_unfenced():
    assert strip_fence("plain: text") == "plain: text"


def test_extract_none_raises():
    with pytest.raises(YamlExtractionError, match="None"):
        extract_yaml(None)  # type: ignore[arg-type]


def test_extract_malformed_yaml_raises():
    # Unbalanced flow mapping -> YAML parse error, no anchor recovery.
    with pytest.raises(YamlExtractionError, match="yaml parse failed"):
        extract_yaml("key: [unclosed")


def test_preamble_then_bare_yaml_recovered_via_anchor():
    text = "Sure, here is the data.\nSome prose that : breaks : yaml\nitems:\n  - a\n"
    data = extract_yaml(text, anchor="items:")
    assert data == {"items": ["a"]}


def test_anchor_recovery_still_fails_when_unrecoverable():
    text = "prose\nitems: [unclosed"
    with pytest.raises(YamlExtractionError):
        extract_yaml(text, anchor="items:")


def test_extract_mapping_requires_mapping():
    with pytest.raises(YamlExtractionError, match="not a mapping"):
        extract_mapping("- just\n- a\n- list\n")


def test_extract_mapping_required_keys_present():
    data = extract_mapping("items:\n  - a\nname: x\n", required_keys=["items", "name"])
    assert data["name"] == "x"


def test_extract_mapping_missing_required_key_raises():
    with pytest.raises(YamlExtractionError, match="missing required key"):
        extract_mapping("other: 1\n", required_keys=["items"])


def test_extract_mapping_default_anchor_from_first_required_key():
    text = "Preamble prose here.\nitems:\n  - a\n"
    data = extract_mapping(text, required_keys=["items"])
    assert data == {"items": ["a"]}
