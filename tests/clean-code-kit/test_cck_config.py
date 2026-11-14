"""Config loading: strict keys, missing file, unimplemented coverage."""

from __future__ import annotations

import pytest
import yaml
from cck_support import CONFIG_TEXT

from clean_code_kit import config
from clean_code_kit.errors import Refusal
from clean_code_kit.lanes import LANES


def _write(repo, text):
    path = repo / ".claude" / "clean-code-kit" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_valid_config_loads(tmp_path):
    _write(tmp_path, CONFIG_TEXT)
    cfg = config.load(tmp_path)
    assert cfg.snapshot == "head"
    assert cfg.languages == ["python"]
    assert cfg.enabled_lanes() == ["duplication", "complexity"]
    assert cfg.lanes["complexity"]["top"] == 50


def test_unknown_top_level_key_is_rejected(tmp_path):
    path = _write(tmp_path, CONFIG_TEXT + "colour: blue\n")
    with pytest.raises(Refusal, match=r"unknown key 'colour' in the top level") as info:
        config.load(tmp_path)
    assert str(path) in str(info.value)


def test_unknown_lane_setting_is_rejected(tmp_path):
    _write(tmp_path, CONFIG_TEXT.replace("top: 50", "top: 50, depth: 3"))
    with pytest.raises(Refusal, match=r"unknown key 'depth' in lanes.complexity"):
        config.load(tmp_path)


def test_missing_file_names_the_path_and_the_reference(tmp_path):
    with pytest.raises(Refusal) as info:
        config.load(tmp_path)
    message = str(info.value)
    assert str(config.config_path(tmp_path)) in message
    assert "references/configuring.md" in message


def test_coverage_true_is_refused(tmp_path):
    _write(tmp_path, CONFIG_TEXT.replace("coverage: false", "coverage: true"))
    with pytest.raises(Refusal, match="coverage-backed complexity is not implemented in this version"):
        config.load(tmp_path)


@pytest.mark.parametrize("old,new,needle", [
    ("schema: 1", "schema: 2", "schema must be 1"),
    ("snapshot: head", "snapshot: index", "snapshot must be one of"),
    ("languages: [python]", "languages: [cobol]", "unknown language 'cobol'"),
    ("source_roots: [src]", "source_roots: [/abs]", "relative posix path"),
    ("source_roots: [src]", "source_roots: []", "at least one entry"),
    ("threshold: 0.82", "threshold: 2", "lanes.duplication.threshold"),
    ("min_lines: 4", "min_lines: true", "lanes.duplication.min_lines"),
    ("enabled: true, coverage", "enabled: 1, coverage", "lanes.complexity.enabled"),
])
def test_bad_values_are_rejected(tmp_path, old, new, needle):
    assert old in CONFIG_TEXT
    _write(tmp_path, CONFIG_TEXT.replace(old, new, 1))
    with pytest.raises(Refusal, match=needle):
        config.load(tmp_path)


def test_missing_key_is_rejected(tmp_path):
    _write(tmp_path, CONFIG_TEXT.replace("test_roots: [tests]\n", ""))
    with pytest.raises(Refusal, match="missing key 'test_roots'"):
        config.load(tmp_path)


def test_lane_registry_names_match_config_lane_keys():
    assert list(LANES) == list(config._LANE_KEYS)  # noqa: SLF001
    assert list(yaml.safe_load(CONFIG_TEXT)["lanes"]) == list(LANES)
    for name, lane in LANES.items():
        assert lane.name == name


def test_min_complexity_is_optional_and_defaults_to_one(tmp_path):
    cfg = config.parse(tmp_path / "c.yaml", yaml.safe_load(CONFIG_TEXT))
    assert cfg.lanes["complexity"]["min_complexity"] == 1
    data = yaml.safe_load(CONFIG_TEXT.replace("top: 50}", "top: 50, min_complexity: 4}"))
    assert config.parse(tmp_path / "c.yaml", data).lanes["complexity"]["min_complexity"] == 4


@pytest.mark.parametrize("value", ["0", "true", "2.5"])
def test_min_complexity_bad_values_are_rejected(tmp_path, value):
    data = yaml.safe_load(CONFIG_TEXT.replace("top: 50}", f"top: 50, min_complexity: {value}}}"))
    with pytest.raises(Refusal, match="lanes.complexity.min_complexity"):
        config.parse(tmp_path / "c.yaml", data)
