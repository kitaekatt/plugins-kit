"""bootstrap_lib keeps full byte copies of the canonical contract modules.

An edit is made in packages/katsoft-contracts and carried over with
scripts/sync_contracts_copy.py. These tests compare the bytes directly (the
module names are spelled out here, not read from the sync script, so emptying
the script's list cannot make this vacuous) and exercise the script on a
scratch tree.
"""
import contract_support as support

NAMES = ("execution_event", "interrupt_contract", "model_declaration")


def test_bootstrap_lib_copy_identical():
    assert NAMES == support.MODULES
    for name in NAMES:
        canonical = support.CANONICAL_DIR / f"{name}.py"
        copy = support.COPY_DIR / f"{name}.py"
        assert canonical.is_file() and copy.is_file()
        assert copy.read_bytes() == canonical.read_bytes(), \
            f"{copy.name} drifted; run scripts/sync_contracts_copy.py"


def _script():
    return support.load_file(support.SYNC_SCRIPT, "_contract_tests_sync_script")


def _scratch_tree(tmp_path, monkeypatch):
    script = _script()
    canonical, copy = tmp_path / "canonical", tmp_path / "copy"
    canonical.mkdir()
    copy.mkdir()
    for name in NAMES:
        data = (support.CANONICAL_DIR / f"{name}.py").read_bytes()
        (canonical / f"{name}.py").write_bytes(data)
        (copy / f"{name}.py").write_bytes(data)
    monkeypatch.setattr(script, "CANONICAL_DIR", canonical)
    monkeypatch.setattr(script, "COPY_DIR", copy)
    return script, canonical, copy


def test_sync_script_reports_the_real_tree_in_sync():
    assert _script().drifted() == []
    assert _script().main(["--check"]) == 0


def test_sync_check_detects_one_changed_byte(tmp_path, monkeypatch, capsys):
    script, canonical, copy = _scratch_tree(tmp_path, monkeypatch)
    victim = copy / "model_declaration.py"
    data = bytearray(victim.read_bytes())
    data[100] ^= 0x01
    victim.write_bytes(bytes(data))
    assert script.drifted() == [victim]
    assert script.main(["--check"]) == 1
    assert "drift: " in capsys.readouterr().out
    assert victim.read_bytes() != (canonical / "model_declaration.py").read_bytes()


def test_sync_check_detects_a_missing_copy(tmp_path, monkeypatch):
    script, _canonical, copy = _scratch_tree(tmp_path, monkeypatch)
    (copy / "interrupt_contract.py").unlink()
    assert script.drifted() == [copy / "interrupt_contract.py"]


def test_sync_copies_canonical_over_the_copy(tmp_path, monkeypatch):
    script, canonical, copy = _scratch_tree(tmp_path, monkeypatch)
    edited = (canonical / "execution_event.py").read_bytes() + b"# edited\n"
    (canonical / "execution_event.py").write_bytes(edited)
    assert script.main([]) == 0
    assert (copy / "execution_event.py").read_bytes() == edited
    assert script.drifted() == []
    assert script.main(["--check"]) == 0
