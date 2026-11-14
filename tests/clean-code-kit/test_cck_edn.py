"""The strict EDN reader against samples captured from the pinned tools."""

from __future__ import annotations

import pytest
from cck_support import FIXTURES

from clean_code_kit.edn import EdnError, loads, read_crap, read_dry


def _sample(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_reads_captured_grouped_dry_edn():
    candidates = read_dry(_sample("dry_mixed.edn"))
    assert len(candidates) == 4
    first = candidates[0]
    assert first["score"] == 1.0
    assert first["language"] == "python"
    assert [s["path"] for s in first["spans"]] == ["src/pkg/a.py", "src/pkg/b.py", "src/pkg/b.py"]
    assert first["spans"][2] == {"path": "src/pkg/b.py", "start_line": 10, "end_line": 16}
    assert [len(c["spans"]) for c in candidates[1:]] == [2, 2, 2]


def test_reads_captured_pairs_and_empty_dry_edn():
    assert len(read_dry(_sample("dry_pairs.edn"))) == 6
    assert read_dry(_sample("dry_empty.edn")) == []


def test_reads_captured_crap_edn():
    entries = read_crap(_sample("crap_no_coverage.edn"))
    assert entries[0] == {"name": "branchy", "namespace": "pkg.b", "complexity": 6,
                          "coverage": None, "crap": None}
    assert len(entries) == 5


def test_crap_accepts_numeric_coverage():
    text = '{:entries [{:name "f", :namespace "m", :complexity 2, :coverage 50.0, :crap 2.5}]}'
    assert read_crap(text)[0]["coverage"] == 50.0


def test_string_escapes():
    assert loads(r'{:a "q\"b\\s"}') == {"a": 'q"b\\s'}


@pytest.mark.parametrize("text", [
    '{:candidates [] :extra 1}',                  # unknown top-level key
    '{:candidates [{:score 1.0}]}',               # missing candidate keys
    '{:candidates [{:score 1 :language "py" :left {:file "a" :start-line 1 :end-line 2}'
    ' :right {:file "b" :start-line 1 :end-line 2} :left-nodes 1 :right-nodes 1}]}',  # int score
    '{:entries []}',                              # the other tool's document
])
def test_dry_rejects_unknown_shapes(text):
    with pytest.raises(EdnError):
        read_dry(text)


@pytest.mark.parametrize("text", [
    '#{1 2}',                                     # a set
    '{:a (1 2)}',                                 # a list
    '{:a []}  extra',                             # trailing symbol
    '{:a true}',                                  # booleans are not emitted
    r'{:a "\n"}',                                # unsupported escape
    '{"a" 1}',                                    # string key
    '{:a :b}',                                    # keyword value
    '{:a 1 :a 2}',                                # duplicate key
    '{:a [}',                                     # mismatched close
    '',                                           # empty
])
def test_reader_rejects_unsupported_syntax(text):
    with pytest.raises(EdnError):
        loads(text)


def test_crap_rejects_an_entry_with_a_path():
    text = ('{:entries [{:name "f", :namespace "m", :complexity 2, :coverage nil, :crap nil,'
            ' :file "x.py"}]}')
    with pytest.raises(EdnError, match="unexpected keys"):
        read_crap(text)
