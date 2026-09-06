"""Tests for llm_scripting_kit.request_protocol._coerce's type dispatch (I11).

The coercer used to classify a field by substring-matching str(annotation)
and silently RETURN THE RAW VALUE for any type it did not recognize -- so a
future BackendOptions field of an unhandled type (List/Tuple/Union of
something other than Optional, ...) would be accepted unvalidated instead of
raising. Dispatch is now on typing.get_origin/get_args + isinstance, and an
unhandled annotation raises ProtocolError rather than falling through.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

from llm_scripting_kit.completion import BackendOptions
from llm_scripting_kit.request_protocol import ProtocolError, _coerce, _ensure_coercible


class TestEveryBackendOptionsFieldIsHandled:
    def test_ensure_coercible_does_not_raise_for_the_real_dataclass(self):
        """The self-check that runs at import/first-use time must accept
        every field BackendOptions actually declares today."""
        _ensure_coercible()

    def test_an_unhandled_annotation_raises_protocolerror(self):
        with pytest.raises(ProtocolError):
            _coerce("weird", ["a", "b"], List[str])

    def test_an_unhandled_annotation_is_caught_by_the_self_check(self):
        @dataclasses.dataclass
        class _Bogus:
            thing: List[str] = dataclasses.field(default_factory=list)

        with pytest.raises(ProtocolError):
            _ensure_coercible(_Bogus)


class TestCoerceKnownTypes:
    def test_int_field(self):
        assert _coerce("max_tokens", 5, int) == 5

    def test_int_field_rejects_bool(self):
        with pytest.raises(ProtocolError, match="must be an integer"):
            _coerce("max_tokens", True, int)

    def test_int_field_rejects_non_int(self):
        with pytest.raises(ProtocolError, match="must be an integer"):
            _coerce("max_tokens", "many", int)

    def test_optional_float_accepts_int_and_float(self):
        assert _coerce("temperature", 1, Optional[float]) == 1.0
        assert _coerce("temperature", 1.5, Optional[float]) == 1.5

    def test_optional_float_accepts_none(self):
        assert _coerce("temperature", None, Optional[float]) is None

    def test_non_optional_rejects_none(self):
        with pytest.raises(ProtocolError, match="may not be null"):
            _coerce("max_tokens", None, int)

    def test_str_field(self):
        assert _coerce("log_prefix", "[x]", str) == "[x]"

    def test_str_field_rejects_non_str(self):
        with pytest.raises(ProtocolError, match="must be a string"):
            _coerce("effort", 5, Optional[str])

    def test_optional_path_field(self):
        assert _coerce("cwd", "/tmp/x", Optional[Path]) == Path("/tmp/x")

    def test_path_field_rejects_non_str(self):
        with pytest.raises(ProtocolError, match="must be a string path"):
            _coerce("cwd", 5, Optional[Path])

    def test_mapping_field(self):
        assert _coerce("extras", {"a": 1}, Mapping[str, Any]) == {"a": 1}

    def test_mapping_field_rejects_non_mapping(self):
        with pytest.raises(ProtocolError, match="must be a JSON object"):
            _coerce("extras", 5, Mapping[str, Any])

    def test_bool_field(self):
        assert _coerce("flag", True, bool) is True

    def test_bool_field_rejects_non_bool(self):
        with pytest.raises(ProtocolError, match="must be a boolean"):
            _coerce("flag", 1, bool)
