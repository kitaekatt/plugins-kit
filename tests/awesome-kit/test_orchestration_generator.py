"""Tests for the deterministic orchestration decision-half generator."""

import importlib.util
import io
import subprocess
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


def test_principles_parser_rejects_duplicate_yaml_keys_at_source_line() -> None:
    text = """\
# heading

```yaml
emits:
  resolution: first
  resolution: last
```

```yaml
generator: {}
```
"""

    with pytest.raises(generator.GenerationError) as exc_info:
        generator.parse_principles(text)

    message = str(exc_info.value)
    assert "duplicate mapping key 'resolution'" in message
    assert "tier-principles.md:6" in message


def test_principles_parser_rejects_recognized_block_with_sibling_key() -> None:
    text = """\
# heading

```yaml
emits:
  resolution: ignored
stray: true
```

```yaml
emits:
  resolution: retained
```

```yaml
generator: {}
```
"""

    with pytest.raises(generator.GenerationError) as exc_info:
        generator.parse_principles(text)

    message = str(exc_info.value)
    assert "stray" in message
    assert "tier-principles.md:6" in message


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


def test_nonterminal_addressed_selector_assigns_and_validates_member_id() -> None:
    merged = generator.merge_emits(
        [
            {
                "ladders.codex.rungs[codex-top].order": 10,
                "ladders.codex.rungs[codex-top].effort": "high",
            }
        ]
    )

    assert merged["ladders"]["codex"]["rungs"] == [
        {"id": "codex-top", "effort": "high"}
    ]

    with pytest.raises(generator.GenerationError, match="addresses id 'codex-top'"):
        generator.merge_emits(
            [
                {
                    "ladders.codex.rungs[codex-top].order": 10,
                    "ladders.codex.rungs[codex-top].id": "different",
                }
            ]
        )


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


def test_split_policy_ignores_machine_marker_text_inside_a_value() -> None:
    marker_in_value = (
        b'note: "prefix # MACHINE HALF -- not derived from the principles. suffix"\n'
    )
    policy = (
        b"# header\n"
        b"schema_version: 2\n"
        + marker_in_value
        + b"# "
        + (b"=" * 75)
        + b"\n"
        + b"# MACHINE HALF -- not derived from the principles. Machine data.\n"
        + b"# "
        + (b"=" * 75)
        + b"\n"
        + b"backends: []\n"
    )

    _header, decision, machine, _newline = generator.split_policy_bytes(policy)

    assert marker_in_value in decision
    assert machine.startswith(b"# " + (b"=" * 75))


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


def test_check_reads_staged_inputs_and_keeps_consistent_tree_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    policy = repo / generator.POLICY_REL
    principles = repo / generator.PRINCIPLES_REL
    lexicon = repo / generator.LEXICON_REL
    for path in (policy, principles, lexicon):
        path.parent.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir()

    old_principles = _minimal_principles("old-backend")
    new_principles = _minimal_principles("new-backend")
    lexicon_text = """\
### `known` `[concept]`
**Test:** can the work be specified?
"""
    template = (
        b"# header\n\n"
        b"schema_version: 1\n"
        b"default_backend: stale\n\n"
        b"# "
        + (b"=" * 75)
        + b"\n"
        + b"# MACHINE HALF -- not derived from the principles. Machine data.\n"
        + b"# "
        + (b"=" * 75)
        + b"\n\n"
        + b"backends: []\n"
    )
    old_policy = generator.generate_policy_bytes(
        template, old_principles, lexicon_text
    )
    new_policy = generator.generate_policy_bytes(
        old_policy, new_principles, lexicon_text
    )
    policy.write_bytes(new_policy)
    principles.write_text(new_principles, encoding="utf-8")
    lexicon.write_text(lexicon_text, encoding="utf-8")

    monkeypatch.setattr(generator, "REPO_ROOT", repo)
    monkeypatch.setattr(
        generator, "staged_paths", lambda _repo: None, raising=False
    )
    monkeypatch.setattr(
        generator, "is_git_repo", lambda _repo: True, raising=False
    )

    control_output = io.StringIO()
    assert (
        generator.check_policy(
            policy, principles, lexicon, output=control_output
        )
        == 0
    )
    assert control_output.getvalue() == ""

    index_text = {
        generator.POLICY_REL: old_policy.decode("utf-8"),
        generator.PRINCIPLES_REL: new_principles,
        generator.LEXICON_REL: lexicon_text,
    }
    monkeypatch.setattr(
        generator,
        "staged_paths",
        lambda _repo: [generator.PRINCIPLES_REL],
        raising=False,
    )
    monkeypatch.setattr(
        generator,
        "index_blob",
        lambda _repo, rel_path: index_text.get(rel_path),
        raising=False,
    )

    bypass_output = io.StringIO()
    assert (
        generator.check_policy(
            policy,
            principles,
            lexicon,
            output=bypass_output,
        )
        == 1
    )
    assert "old-backend" in bypass_output.getvalue()
    assert "new-backend" in bypass_output.getvalue()

    seam_output = io.StringIO()
    assert (
        generator.check_policy(
            policy,
            principles,
            lexicon,
            output=seam_output,
            staged=[generator.PRINCIPLES_REL],
        )
        == 1
    )


def _minimal_principles(default_backend: str) -> str:
    return f"""\
```yaml
emits:
  default_backend: {default_backend}
  resolution: first-match
  shape:
    note: stable
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


class TestStagedScoping:
    """``--staged`` judges the COMMIT: index-aware AND scoped to the three
    generator inputs, mirroring TestStagedScoping in
    tests/repo-scripts/test_bootstrap_dependency.py.

    Without ``--staged``, ``check_policy`` still reads from the index
    whenever ANYTHING is staged (preserved -- that is today's behavior for
    every caller that does not pass the flag). That gate is "is ANYTHING
    staged", not "are MY three inputs staged" -- so a pre-existing,
    already-committed inconsistency between the three inputs blocks a commit
    that touches neither of them, merely because something else is staged.
    ``--staged`` fixes that by scoping to the three canonical paths via
    ``_gitindex.classify_scope``.
    """

    def _repo(self, tmp_path: Path) -> Path:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(
                ["git", "-C", str(tmp_path), "config", k, v], check=True
            )
        return tmp_path

    def _commit_all(self, root: Path) -> None:
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "init"], check=True
        )

    def _add(self, root: Path, *paths: str) -> None:
        subprocess.run(["git", "-C", str(root), "add", *paths], check=True)

    def _write_inputs(
        self,
        root: Path,
        policy_principles_text: str,
        principles_text: str,
        lexicon_text: str,
    ) -> tuple[Path, Path, Path]:
        """Write the three canonical files. The policy is generated from
        ``policy_principles_text``, which may differ from ``principles_text``
        left on disk -- that mismatch IS the inconsistency under test.
        """
        policy = root / generator.POLICY_REL
        principles = root / generator.PRINCIPLES_REL
        lexicon = root / generator.LEXICON_REL
        for path in (policy, principles, lexicon):
            path.parent.mkdir(parents=True, exist_ok=True)
        template = (
            b"# header\n\n"
            b"schema_version: 1\n"
            b"default_backend: stale\n\n"
            b"# " + (b"=" * 75) + b"\n"
            b"# MACHINE HALF -- not derived from the principles. Machine data.\n"
            b"# " + (b"=" * 75) + b"\n\n"
            b"backends: []\n"
        )
        policy.write_bytes(
            generator.generate_policy_bytes(
                template, policy_principles_text, lexicon_text
            )
        )
        principles.write_text(principles_text, encoding="utf-8")
        lexicon.write_text(lexicon_text, encoding="utf-8")
        return policy, principles, lexicon

    def _point_at(
        self,
        monkeypatch: pytest.MonkeyPatch,
        root: Path,
        policy: Path,
        principles: Path,
        lexicon: Path,
    ) -> None:
        monkeypatch.setattr(generator, "REPO_ROOT", root)
        monkeypatch.setattr(generator, "POLICY_PATH", policy)
        monkeypatch.setattr(generator, "PRINCIPLES_PATH", principles)
        monkeypatch.setattr(generator, "LEXICON_PATH", lexicon)

    def test_unrelated_staged_commit_is_not_blocked_by_a_preexisting_drift(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo = self._repo(tmp_path)
        lexicon_text = "### `known` `[concept]`\n**Test:** can it be specified?\n"
        # A genuinely inconsistent triple, already committed: the policy was
        # generated from old-backend, but principles.md now says new-backend.
        policy, principles, lexicon = self._write_inputs(
            repo,
            _minimal_principles("old-backend"),
            _minimal_principles("new-backend"),
            lexicon_text,
        )
        (repo / "README.md").write_text("hi\n")
        self._commit_all(repo)
        self._point_at(monkeypatch, repo, policy, principles, lexicon)

        # My commit stages only an unrelated file.
        (repo / "README.md").write_text("changed\n")
        self._add(repo, "README.md")

        # Preserved default behavior (no --staged): "is ANYTHING staged"
        # still reads the index and is still blocked by the pre-existing
        # drift -- proving the scenario is real, not a no-op.
        assert generator.main(["--check"]) == 1
        capsys.readouterr()

        # --staged scopes to the three inputs, none of which this commit
        # stages -> SCOPE_SKIP -> pass.
        assert generator.main(["--check", "--staged"]) == 0
        assert capsys.readouterr().out == ""

    def test_worktree_fallback_is_loud_and_still_catches_drift(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Deliberately not a Git repo: classify_scope cannot ask the index,
        # so it must fall back to the working tree LOUDLY, never silently.
        repo = tmp_path
        lexicon_text = "### `known` `[concept]`\n**Test:** can it be specified?\n"
        policy, principles, lexicon = self._write_inputs(
            repo,
            _minimal_principles("old-backend"),
            _minimal_principles("new-backend"),
            lexicon_text,
        )
        self._point_at(monkeypatch, repo, policy, principles, lexicon)

        assert generator.main(["--check", "--staged"]) == 1
        captured = capsys.readouterr()
        assert "could not read the index" in captured.err
        assert "old-backend" in captured.out
        assert "new-backend" in captured.out


def test_pre_commit_hook_chains_generator_check_and_remediation() -> None:
    hook = (_REPO_ROOT / "scripts" / "pre-commit-version-check.sh").read_text(
        encoding="utf-8"
    )

    assert (
        '"${UV_PY[@]}" "$REPO_ROOT/scripts/generate_orchestration.py" '
        "--check --staged"
        in hook
    )
    assert "uv run python scripts/generate_orchestration.py --write" in hook
    assert "stage the result" in hook


def test_checked_in_policy_matches_the_generator() -> None:
    output = io.StringIO()
    assert generator.check_policy(output=output) == 0, output.getvalue()
