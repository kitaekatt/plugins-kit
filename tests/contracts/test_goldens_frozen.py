"""The contract modules still behave as the bootstrap_lib modules did at a1257278.

The goldens are literals captured once from those modules
(tests/contracts/contract_goldens.py). The canonical package, and the
bootstrap_lib aliases of it, are run through the same fixed cases and must
reproduce them: messages for bad inputs, exception classes, constants,
canonical bytes.
"""
import ast
from pathlib import Path

import contract_cases
import contract_goldens
import contract_support as support


def test_canonical_package_matches_goldens(canonical, tmp_path):
    assert support.golden_mismatches(canonical, tmp_path) == []


def test_bootstrap_lib_alias_matches_goldens(bootstrap_alias, tmp_path):
    assert support.golden_mismatches(bootstrap_alias, tmp_path) == []


def test_canonical_modules_come_from_the_source_tree(canonical):
    for name in support.MODULES:
        assert Path(getattr(canonical, name).__file__).resolve() == \
            (support.CANONICAL_DIR / f"{name}.py").resolve()


def test_goldens_are_pure_literals():
    """A computed golden (an import, a call) could follow the code it checks."""
    tree = ast.parse(Path(contract_goldens.__file__).read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assigns = [n for n in tree.body if isinstance(n, ast.Assign)]
    assert [t.id for a in assigns for t in a.targets] == ["GOLDENS"]
    assert isinstance(ast.literal_eval(assigns[0].value), dict)


def test_goldens_cover_the_case_list_and_are_not_trivial():
    assert set(contract_goldens.GOLDENS) == set(contract_cases.CASES)
    outcomes = [value[0] for value in contract_goldens.GOLDENS.values()]
    assert len(outcomes) >= 200
    assert outcomes.count("raised") >= 100 and outcomes.count("ok") >= 50


def test_a_changed_refusal_message_is_a_mismatch(tmp_path):
    """The mutant: reword one refusal in a copy of the source; the check goes red."""
    mutant = support.load_modules(
        support.CANONICAL_DIR, "mutant_message",
        patch=("must not be an empty list", "must not be empty"))
    problems = support.golden_mismatches(mutant, tmp_path)
    assert "md.parse.refused.empty_list" in problems
    assert "md.validate.refused" in problems


def test_a_changed_exception_class_is_a_mismatch(tmp_path):
    mutant = support.load_modules(
        support.CANONICAL_DIR, "mutant_class",
        patch=("class RequestError(ContractError):", "class RequestError(ValueError):"))
    assert "ic.exception_hierarchy" in support.golden_mismatches(mutant, tmp_path)


def test_a_changed_canonical_byte_is_a_mismatch(tmp_path):
    mutant = support.load_modules(
        support.CANONICAL_DIR, "mutant_bytes",
        patch=('ensure_ascii=True, separators=(",", ":"))', 'ensure_ascii=True, separators=(", ", ": "))'))
    assert "ee.jsonl.canonical_bytes" in support.golden_mismatches(mutant, tmp_path)
