"""Configuration layers of the task answers chain (shipped defaults, then the fleet file)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from task_system import answers_config
from task_system.answers_config import AnswersConfigError, load

SHIPPED = answers_config.default_shipped_path()


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestShippedDefaults:
    def test_the_shipped_file_loads_to_the_documented_defaults(self, tmp_path):
        config = load(fleet_path=tmp_path / "absent.yaml")
        assert config.models == ("luna", "haiku") and config.effort == "medium"
        assert config.sources == (SHIPPED,)

    def test_no_switch_exists_in_the_shipped_file(self):
        assert set(yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))["answers"]) == {"models", "effort"}

    def test_a_broken_shipped_file_is_an_error_naming_it(self, tmp_path):
        bad = write(tmp_path / "answers.yaml", '_schema_version: "1"\nanswers:\n  effort: medium\n')
        with pytest.raises(AnswersConfigError, match=r"answers\.yaml.*must set \['models'\]"):
            load(shipped_path=bad, fleet_path=tmp_path / "none.yaml")
        bad = write(tmp_path / "answers.yaml", 'answers: {models: [a], effort: low}\n')
        with pytest.raises(AnswersConfigError, match="_schema_version"):
            load(shipped_path=bad, fleet_path=tmp_path / "none.yaml")


class TestFleetLayer:
    def test_models_replace_the_shipped_list_whole_and_effort_is_kept(self, tmp_path):
        fleet = write(tmp_path / "task-kit.yaml", "answers:\n  models: [qwen38-5090, qwen38-m5, luna, haiku]\n")
        config = load(fleet_path=fleet)
        assert config.models == ("qwen38-5090", "qwen38-m5", "luna", "haiku")
        assert config.effort == "medium" and config.sources == (SHIPPED, fleet)

    def test_effort_alone(self, tmp_path):
        fleet = write(tmp_path / "task-kit.yaml", "answers:\n  effort: high\n")
        config = load(fleet_path=fleet)
        assert config.effort == "high" and config.models == ("luna", "haiku")

    def test_other_sections_of_the_fleet_file_are_not_read_here(self, tmp_path):
        fleet = write(tmp_path / "task-kit.yaml", "dashboard:\n  port: 1\nanswers:\n  effort: low\n")
        assert load(fleet_path=fleet).effort == "low"

    @pytest.mark.parametrize("text", ["", "answers:\n", "other: 1\n"])
    def test_an_empty_or_unrelated_fleet_file_changes_nothing(self, tmp_path, text):
        config = load(fleet_path=write(tmp_path / "task-kit.yaml", text))
        assert config.models == ("luna", "haiku") and config.sources == (SHIPPED,)

    @pytest.mark.parametrize("key", ["service", "spend", "batch_size"])
    def test_a_retired_or_unknown_key_is_an_error_naming_the_file(self, tmp_path, key):
        fleet = write(tmp_path / "task-kit.yaml", f"answers:\n  {key}: true\n")
        with pytest.raises(AnswersConfigError, match=rf"task-kit\.yaml: unknown answers keys \['{key}'\]"):
            load(fleet_path=fleet)

    @pytest.mark.parametrize("body", [
        "models: luna", "models: []", "models: [luna, luna]", "models: [luna, 3]", "models: [' luna']",
        "models: ['a,b']", "effort: ''", "effort: 3",
    ])
    def test_wrong_types_are_errors_naming_the_file(self, tmp_path, body):
        fleet = write(tmp_path / "task-kit.yaml", f"answers:\n  {body}\n")
        with pytest.raises(AnswersConfigError, match=r"task-kit\.yaml"):
            load(fleet_path=fleet)

    def test_a_section_that_is_not_a_mapping_and_a_file_that_is_not_yaml(self, tmp_path):
        with pytest.raises(AnswersConfigError, match="answers must be a mapping"):
            load(fleet_path=write(tmp_path / "a.yaml", "answers: [1]\n"))
        with pytest.raises(AnswersConfigError, match="must be a mapping"):
            load(fleet_path=write(tmp_path / "b.yaml", "- 1\n"))
        with pytest.raises(AnswersConfigError, match="cannot read"):
            load(fleet_path=write(tmp_path / "c.yaml", "answers: {models: [a\n"))

    def test_the_default_fleet_path_follows_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert answers_config.default_fleet_path() == tmp_path / ".claude" / "config" / "task-kit.yaml"
        (tmp_path / ".claude" / "config").mkdir(parents=True)
        write(tmp_path / ".claude" / "config" / "task-kit.yaml", "answers:\n  effort: low\n")
        assert load().effort == "low"
