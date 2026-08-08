"""Tests for the deterministic orchestration decision-half generator."""

import importlib.util
import io
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "generate_orchestration.py"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_orchestration", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generator = _load_generator()


_EMITS_DOCUMENT = """\
```yaml
emits:
  ladders.codex:
    order: 20
    id: codex
    label: Codex
  shape.tests[late]:
    order: 20
    text: late
  effort.raise_when:
    - order: 20
      value: later
```

```yaml
ignored:
  value: true
```

```yaml
emits:
  ladders.agent:
    order: 10
    id: agent
    label: Claude
  shape.tests[early]:
    order: 10
    id: early
    text: early
  effort.raise_when:
    - order: 10
      value: sooner
```

```yaml
generator:
  blocks:
    - order: 1
      path: shape
      label: Shape
  intra_block_order: principle-number
  intra_block_order_scope: slot
```
"""


def _has_order(value: Any) -> bool:
    if isinstance(value, dict):
        return "order" in value or any(_has_order(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_order(child) for child in value)
    return False


def test_emits_parser_keeps_list_append_and_addressed_targets() -> None:
    emits, structure = generator.parse_principles(_EMITS_DOCUMENT)

    assert len(emits) == 2
    assert emits[0]["effort.raise_when"] == [{"order": 20, "value": "later"}]
    assert emits[0]["shape.tests[late]"]["order"] == 20
    assert structure["intra_block_order_scope"] == "slot"


def test_order_sequences_every_generated_collection_and_never_leaks() -> None:
    emits, _structure = generator.parse_principles(_EMITS_DOCUMENT)
    merged = generator.merge_emits(emits)

    assert [ladder["id"] for ladder in merged["ladders"]] == ["agent", "codex"]
    assert [test["id"] for test in merged["shape"]["tests"]] == [
        "early",
        "late",
    ]
    assert merged["effort"]["raise_when"] == ["sooner", "later"]
    assert not _has_order(merged)
    assert "order:" not in yaml.safe_dump(merged, sort_keys=False)


def test_lexicon_parser_preserves_order_and_omits_concept_render_fields() -> None:
    text = """\
### `known` `[skill]` `render: glossed`
Description.
**Test:** can you write what *done*
looks like without doing the work?
**Gloss:** "you can describe done"

### `mechanical` `[concept]`
Description.
**Test:** can you verify it cheaply?
"""

    records = generator.parse_lexicon(text)

    assert [record["id"] for record in records] == ["known", "mechanical"]
    assert records[0] == {
        "id": "known",
        "kind": "skill",
        "render": "glossed",
        "test": "Can you write what `done` looks like without doing the work?",
        "gloss": "you can describe done",
    }
    assert records[1] == {
        "id": "mechanical",
        "kind": "concept",
        "test": "Can you verify it cheaply?",
    }


@pytest.fixture
def policy_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    policy = tmp_path / "orchestration.yaml"
    header = b"# header sentinel\r\n# preserve me exactly\r\n\r\n"
    stale_decision = (
        b"schema_version: 1\r\n"
        b"default_backend: stale\r\n"
        b"resolution: stale\r\n\r\n"
    )
    machine = (
        b"# " + (b"=" * 75) + b"\r\n"
        b"# MACHINE HALF -- not derived from the principles. "
        b"Machine sentinel.\r\n"
        b"# " + (b"=" * 75) + b"\r\n\r\n"
        b"backends: []\r\n"
        b"capacity: {}\r\n"
    )
    policy.write_bytes(header + stale_decision + machine)
    return policy, generator.PRINCIPLES_PATH, generator.LEXICON_PATH


def _point_cli_at(
    monkeypatch: pytest.MonkeyPatch, paths: tuple[Path, Path, Path]
) -> None:
    policy, principles, lexicon = paths
    monkeypatch.setattr(generator, "POLICY_PATH", policy)
    monkeypatch.setattr(generator, "PRINCIPLES_PATH", principles)
    monkeypatch.setattr(generator, "LEXICON_PATH", lexicon)


def test_write_preserves_header_and_machine_half_byte_for_byte(
    policy_paths: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, _principles, _lexicon = policy_paths
    before_header, _decision, before_machine, _newline = generator.split_policy_bytes(
        policy.read_bytes()
    )
    _point_cli_at(monkeypatch, policy_paths)

    assert generator.main(["--write"]) == 0

    after_header, _decision, after_machine, newline = generator.split_policy_bytes(
        policy.read_bytes()
    )
    assert after_header == before_header
    assert after_machine == before_machine
    assert newline == "\r\n"


def test_check_exits_zero_fresh_and_one_after_mutation(
    policy_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy, _principles, _lexicon = policy_paths
    _point_cli_at(monkeypatch, policy_paths)
    assert generator.main(["--write"]) == 0
    capsys.readouterr()

    assert generator.main([]) == 0
    assert capsys.readouterr().out == ""

    policy.write_bytes(
        policy.read_bytes().replace(b"schema_version: 2", b"schema_version: 9", 1)
    )
    assert generator.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert "--- plugins/awesome-kit" in output
    assert "schema_version: 9" in output


def test_write_is_idempotent(policy_paths: tuple[Path, Path, Path]) -> None:
    policy, principles, lexicon = policy_paths
    assert generator.write_policy(policy, principles, lexicon)
    first = policy.read_bytes()

    assert not generator.write_policy(policy, principles, lexicon)
    assert policy.read_bytes() == first


def test_checked_in_policy_matches_the_generator() -> None:
    output = io.StringIO()
    assert generator.check_policy(output=output) == 0, output.getvalue()
