"""Tests for bootstrap_lib.model_declaration -- the shared declaration validator.

The validator owns SHAPE only: a declaration is a list of registry ids, a bare
scalar reads as a one-element list, and a literally-empty list or a repeated id
is an error. Whether an id resolves, or can be routed, is a runtime question
answered where the declaration is dispatched, so nothing here may consult a
registry, a file, or llm-scripting-kit.
"""

from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path
from typing import Any

import pytest

from bootstrap_lib import model_declaration as md


MODULE_PATH = Path(md.__file__)


class TestParseShape:
    def test_a_scalar_reads_as_a_one_element_list(self) -> None:
        assert md.parse("opus") == ["opus"]

    def test_a_list_keeps_its_order(self) -> None:
        assert md.parse(["qwen3.8-5090", "opus", "astra", "qwen3.8-m5pro"]) == [
            "qwen3.8-5090",
            "opus",
            "astra",
            "qwen3.8-m5pro",
        ]

    def test_a_tuple_carrier_is_written_back_as_a_list(self) -> None:
        parsed = md.parse(("astra", "fable"))
        assert parsed == ["astra", "fable"]
        assert type(parsed) is list

    def test_surrounding_whitespace_is_normalized_away(self) -> None:
        assert md.parse(["  sol ", "opus\n"]) == ["sol", "opus"]
        assert md.parse("  fable ") == ["fable"]

    def test_parse_returns_a_fresh_list(self) -> None:
        source = ["sol", "opus"]
        parsed = md.parse(source)
        parsed.append("haiku")
        assert source == ["sol", "opus"]

    def test_a_deprecated_prefix_is_still_a_structurally_valid_id(self) -> None:
        # `peer:` and `agent:` are rewritten by the sites that accept them;
        # the validator neither strips nor refuses them.
        assert md.parse(["peer:opus", "agent:opus"]) == ["peer:opus", "agent:opus"]


class TestParseRejects:
    def test_a_literally_empty_list_is_an_error(self) -> None:
        with pytest.raises(md.DeclarationError) as excinfo:
            md.parse([])
        assert "empty" in str(excinfo.value)

    @pytest.mark.parametrize("value", ["", "   ", "\n"])
    def test_a_blank_scalar_is_an_error(self, value: str) -> None:
        with pytest.raises(md.DeclarationError):
            md.parse(value)

    def test_a_blank_entry_is_an_error_naming_its_index(self) -> None:
        with pytest.raises(md.DeclarationError) as excinfo:
            md.parse(["opus", "  "])
        assert excinfo.value.index == 1

    @pytest.mark.parametrize("entry", [7, None, True, 1.5, {"peer": "opus"}, ["opus"]])
    def test_a_non_string_entry_is_an_error(self, entry: Any) -> None:
        with pytest.raises(md.DeclarationError) as excinfo:
            md.parse(["opus", entry])
        assert excinfo.value.index == 1

    @pytest.mark.parametrize("value", [None, 7, True, {"models": ["opus"]}])
    def test_a_value_that_is_neither_string_nor_list_is_an_error(
        self, value: Any
    ) -> None:
        with pytest.raises(md.DeclarationError) as excinfo:
            md.parse(value)
        assert excinfo.value.index is None

    def test_a_duplicate_id_is_an_error_naming_it(self) -> None:
        with pytest.raises(md.DeclarationError) as excinfo:
            md.parse(["sol", "opus", "sol"])
        assert "'sol'" in str(excinfo.value)
        assert "duplicate" in str(excinfo.value)
        assert excinfo.value.index == 2

    def test_duplicates_are_detected_after_normalization(self) -> None:
        with pytest.raises(md.DeclarationError):
            md.parse(["opus", " opus "])


class TestValidate:
    def test_validate_returns_a_declaration_holding_the_ids(self) -> None:
        declaration = md.validate(["astra", "fable"])
        assert isinstance(declaration, md.Declaration)
        assert declaration.ids == ("astra", "fable")
        assert list(declaration) == ["astra", "fable"]
        assert len(declaration) == 2

    def test_validate_accepts_the_scalar_carrier(self) -> None:
        assert md.validate("sonnet").ids == ("sonnet",)

    def test_validate_applies_the_same_structural_checks(self) -> None:
        with pytest.raises(md.DeclarationError):
            md.validate([])
        with pytest.raises(md.DeclarationError):
            md.validate(["opus", "opus"])

    def test_a_declaration_reports_its_core_ids_in_order(self) -> None:
        declaration = md.validate(["qwen3.8-5090", "opus", "astra", "haiku"])
        assert declaration.core_ids == ("opus", "haiku")


class TestNoKnownIdChecking:
    """R9: shape only -- no registry discovery, no known-id error."""

    @pytest.mark.parametrize(
        "value",
        [
            ["no-such-model-anywhere"],
            ["sonnett"],
            ["Opus"],
            ["qwen3.8-5090", "or-gpt-mini"],
        ],
    )
    def test_an_unknown_id_is_structurally_valid(self, value: list[str]) -> None:
        assert md.validate(value).ids == tuple(value)

    def test_validation_reads_no_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A registry lookup would have to open something; nothing may be opened."""

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("model_declaration opened a file")

        monkeypatch.setattr(builtins, "open", refuse)
        monkeypatch.setattr(Path, "read_text", refuse)
        monkeypatch.setattr(Path, "open", refuse)
        assert md.validate(["astra", "fable"]).ids == ("astra", "fable")
        assert md.parse("sol") == ["sol"]

    def test_the_module_imports_only_the_standard_library(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        assert imported <= set(sys.stdlib_module_names), imported - set(
            sys.stdlib_module_names
        )

    def test_the_module_carries_no_shipped_id_copies(self) -> None:
        # The only id set the validator owns is the harness-defined core set.
        assert not hasattr(md, "SHIPPED_EXTENSION_IDS")
        source = MODULE_PATH.read_text(encoding="utf-8")
        assert "llm_scripting_kit" not in source
        assert "model-endpoints" not in source


class TestCoreIds:
    def test_the_core_set_is_the_four_harness_ids(self) -> None:
        assert md.CORE_IDS == frozenset({"fable", "opus", "sonnet", "haiku"})

    @pytest.mark.parametrize("value", ["fable", "opus", "sonnet", "haiku", " opus "])
    def test_is_core_id_accepts_each_core_id(self, value: str) -> None:
        assert md.is_core_id(value)

    @pytest.mark.parametrize("value", ["sol", "Opus", "sonnett", "agent:opus", ""])
    def test_is_core_id_rejects_everything_else(self, value: str) -> None:
        assert not md.is_core_id(value)
