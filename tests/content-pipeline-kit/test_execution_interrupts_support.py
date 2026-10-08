"""The two edges of content-pipeline-kit's interrupt verbs, and their absence.

``content_pipeline`` runs in project interpreters that may link neither
``bootstrap_lib`` nor llm-scripting-kit. So:

- no execution module imports either one statically;
- reading interrupts needs neither;
- the three writing verbs probe both before they open a transaction, and
  refuse with a diagnosis that tells absent from too old;
- the literals and limits the package holds equal the contract's.

Expected values are literals. The fake modules are handed to the probe
through its one import seam (``interrupts._import_module``), so the probe and
the verbs above it run unmodified.
"""

from __future__ import annotations

import ast
import contextlib
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from content_pipeline.execution import events as events_mod
from content_pipeline.execution import interrupts
from content_pipeline.execution import model as model_mod
from content_pipeline.execution import status as status_mod
from content_pipeline.execution import store as store_mod
from content_pipeline.execution.interrupts import InterruptSupportError
from content_pipeline.execution.model import InterruptRecord, InterruptRequest, UnitState
from content_pipeline.execution.store import ExecutionStore

from bootstrap_lib import interrupt_contract as contract

RUN = "r1"
REPO = Path(__file__).resolve().parents[2]
CPK = REPO / "plugins" / "content-pipeline-kit"
_SHARED_LIB = REPO / "plugins" / "llm-scripting-kit" / "lib"

_TABLES = ("runs", "units", "attempts", "dispatches", "interrupts", "interrupt_resolutions")

CONTRACT_MODULE = "bootstrap_lib.interrupt_contract"
VALIDATOR_MODULE = "llm_scripting_kit.completion.json_schema"
SUBSET = "llm-scripting-kit.json-schema-subset/v1"


def _lsk_names():
    return {n for n in sys.modules if n == "llm_scripting_kit" or n.startswith("llm_scripting_kit.")}


@pytest.fixture(autouse=True)
def lsk(monkeypatch):
    """Link llm-scripting-kit's validator for one test; unload it afterwards."""
    before = _lsk_names()
    monkeypatch.syspath_prepend(str(_SHARED_LIB))
    yield
    for name in _lsk_names() - before:
        del sys.modules[name]


def _rows(store) -> dict:
    with contextlib.closing(sqlite3.connect(str(store.db_path))) as conn:
        conn.row_factory = sqlite3.Row
        return {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in _TABLES
        }


def _request(**overrides) -> InterruptRequest:
    fields = {
        "kind": "approval",
        "request_schema": {"type": "object"},
        "payload": {"question": "ship it?"},
    }
    fields.update(overrides)
    return InterruptRequest(**fields)


def _lifecycle_store(tmp_path) -> ExecutionStore:
    """u0 answered, u1 waiting with an expiry, u2 claimed and never asked."""
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(
        RUN, driver="inline", backend="mock", model="m1", adapter_version="7", created_at=1000.0
    )
    store.register_units(RUN, ["u0", "u1", "u2"], at=1000.0)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    first = store.request_interrupt(RUN, "u0", token, _request(), at=1002.0)
    store.resolve_interrupt(RUN, first.id, decision="answer", input={"ok": True}, now=1003.0)
    token = store.claim_unit(RUN, "u1", "w", at=1004.0).fencing_token
    store.request_interrupt(RUN, "u1", token, _request(expires_in_s=60), at=1005.0)
    store.claim_unit(RUN, "u2", "w", at=1006.0)
    return store


# -- no static edge ------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [model_mod, store_mod, status_mod, interrupts, events_mod],
    ids=["model", "store", "status", "interrupts", "events"],
)
def test_interrupt_modules_import_no_shared_lib_statically(module):
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):  # module level and function level alike
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.append(node.module)
    assert imported, "the scan found no import at all"
    roots = {name.split(".")[0] for name in imported}
    assert not roots & {"bootstrap_lib", "llm_scripting_kit", "job_kit"}, sorted(roots)


# -- reads need no edge ------------------------------------------------------------


_EDGE_MODULES = (
    CONTRACT_MODULE,
    "llm_scripting_kit",
    "llm_scripting_kit.completion",
    VALIDATOR_MODULE,
)


@pytest.fixture
def block_edges(monkeypatch):
    """Return a function that makes both edges unimportable for the rest of
    the test: any import of either then raises, through the probe's seam and
    through a plain import statement alike. Restore the exact pre-test
    modules afterwards, including their package identity.
    """
    def block():
        for name in _EDGE_MODULES:
            monkeypatch.setitem(sys.modules, name, None)

    return block


@pytest.mark.parametrize(
    "read", ["list", "get", "open", "unit_resolutions", "waiting_units", "lapsed"]
)
def test_reads_work_without_either_shared_lib(tmp_path, block_edges, read):
    store = _lifecycle_store(tmp_path)
    block_edges()
    with pytest.raises(InterruptSupportError):
        interrupts.support()

    if read == "list":
        records = store.list_interrupts(RUN)
        assert [(r.id, r.unit_id, r.kind) for r in records] == [
            ("1", "u0", "approval"),
            ("2", "u1", "approval"),
        ]
        assert records[0].resolution.input == {"ok": True}
        assert records[1].resolution is None
        assert [r.id for r in store.list_interrupts(RUN, "u1")] == ["2"]
    elif read == "get":
        record = store.get_interrupt(RUN, "2")
        assert (record.unit_id, record.expires_at, record.on_expired) == ("u1", 1065.0, "stop")
        assert record.payload == {"question": "ship it?"}
    elif read == "open":
        assert store.open_interrupt(RUN, "u1").id == "2"
        assert store.open_interrupt(RUN, "u0") is None
    elif read == "unit_resolutions":
        assert interrupts.unit_resolutions(store, RUN, "u0") == [
            {
                "interrupt_id": "1",
                "kind": "approval",
                "outcome": "answered",
                "input": {"ok": True},
                "reason": None,
                "payload": {"question": "ship it?"},
            }
        ]
    elif read == "waiting_units":
        assert [u.unit_id for u in interrupts.waiting_units(store, RUN)] == ["u1"]
    else:
        record = store.open_interrupt(RUN, "u1")
        assert (record.lapsed(1064.999), record.lapsed(1065.0)) == (False, True)


# -- the verbs refuse before writing -------------------------------------------------


def _missing(name):
    return ModuleNotFoundError(f"No module named {name!r}", name=name)


def _old_validator():
    """The validator as it was before it had a subset marker or selector."""
    return types.SimpleNamespace(
        check_schema=lambda schema: None, validate=lambda schema, value: ()
    )


def _break_edge(monkeypatch, mode):
    """Make one edge unusable through the probe's import seam."""
    real = interrupts._import_module

    def fake(name):
        if mode == "contract_absent" and name == "bootstrap_lib":
            raise _missing("bootstrap_lib")
        if mode == "contract_too_old" and name == CONTRACT_MODULE:
            raise _missing(CONTRACT_MODULE)
        if mode == "validator_absent" and name == "llm_scripting_kit":
            raise _missing("llm_scripting_kit")
        if mode == "validator_too_old" and name == VALIDATOR_MODULE:
            raise _missing(VALIDATOR_MODULE)
        if mode == "validator_without_subset" and name == VALIDATOR_MODULE:
            return _old_validator()
        return real(name)

    monkeypatch.setattr(interrupts, "_import_module", fake)


_MODES = [
    "contract_absent",
    "contract_too_old",
    "validator_absent",
    "validator_too_old",
    "validator_without_subset",
]


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("verb", ["request", "resolve", "expire"])
def test_verbs_refuse_before_writing(tmp_path, monkeypatch, verb, mode):
    store = _lifecycle_store(tmp_path)
    token = store.get_unit(RUN, "u2").fencing_token
    before = _rows(store)
    _break_edge(monkeypatch, mode)
    opened = []

    def no_transaction(self):
        opened.append("writer")
        raise AssertionError("a transaction was opened before the edges were probed")

    monkeypatch.setattr(ExecutionStore, "_writer", no_transaction)
    with pytest.raises(InterruptSupportError) as info:
        if verb == "request":
            store.request_interrupt(RUN, "u2", token, _request(), at=1007.0)
        elif verb == "resolve":
            store.resolve_interrupt(RUN, "2", decision="answer", input={}, now=1007.0)
        else:
            store.expire_interrupts(RUN, now=5000.0)
    assert isinstance(info.value, ImportError)
    assert opened == []
    assert _rows(store) == before
    assert store.get_unit(RUN, "u1").state is UnitState.WAITING
    assert store.get_unit(RUN, "u2").state is UnitState.CLAIMED


_MESSAGES = {
    ("contract", "absent"): (
        "content-pipeline-kit interrupts need bootstrap_lib, which this interpreter "
        "cannot import. Run `claude plugin install bootstrap@plugins-kit`."
    ),
    ("contract", "too_old"): (
        "content-pipeline-kit interrupts need bootstrap 0.137.0 or newer: "
        "bootstrap_lib.interrupt_contract does not import. Run "
        "`claude plugin update bootstrap@plugins-kit` and restart the session."
    ),
    ("validator", "absent"): (
        "content-pipeline-kit interrupts validate requests and answers with "
        "llm_scripting_kit, which this interpreter cannot import. Run "
        "`claude plugin install llm-scripting-kit@plugins-kit`."
    ),
    ("validator", "too_old"): (
        "content-pipeline-kit interrupts need llm-scripting-kit 0.56.0 or newer: "
        "llm_scripting_kit.completion.json_schema does not import. Run "
        "`claude plugin update llm-scripting-kit@plugins-kit` and restart the session."
    ),
}


@pytest.mark.parametrize("state", ["absent", "too_old"])
@pytest.mark.parametrize("edge", ["contract", "validator"])
def test_support_messages(monkeypatch, edge, state):
    assert interrupts.INTERRUPT_CONTRACT_BOOTSTRAP == "0.137.0"
    assert interrupts.JSON_SCHEMA_LSK_VERSION == "0.56.0"
    _break_edge(monkeypatch, f"{edge}_{state}")
    with pytest.raises(InterruptSupportError) as info:
        interrupts.support()
    assert str(info.value) == _MESSAGES[(edge, state)]


def test_support_messages_name_the_consumers_constant_not_the_modules(monkeypatch):
    # A stale module cannot put its own version into the diagnosis.
    stale = types.SimpleNamespace(SUPPORTED_CONTRACTS=frozenset(), VERSION="9.9.9")
    real = interrupts._import_module
    monkeypatch.setattr(
        interrupts, "_import_module", lambda name: stale if name == CONTRACT_MODULE else real(name)
    )
    monkeypatch.setattr(interrupts, "INTERRUPT_CONTRACT_BOOTSTRAP", "7.7.7")
    with pytest.raises(InterruptSupportError) as info:
        interrupts.support()
    assert "bootstrap 7.7.7 or newer" in str(info.value)
    assert "plugins-kit.interrupt-contract/v1" in str(info.value)
    assert "9.9.9" not in str(info.value)


def test_support_messages_name_no_manifest_file(monkeypatch):
    messages = []
    for mode in _MODES:
        with monkeypatch.context() as patch:
            _break_edge(patch, mode)
            with pytest.raises(InterruptSupportError) as info:
                interrupts.support()
            messages.append(str(info.value))
    assert len(set(messages)) == 5
    for text in messages:
        for manifest in ("bootstrap.json", "plugin.json", "pyproject", "min_version", "requires_bootstrap"):
            assert manifest not in text, text
        assert "claude plugin " in text
    assert messages[4] == (
        "content-pipeline-kit interrupts need llm-scripting-kit 0.56.0 or newer: the "
        "installed validator does not advertise llm-scripting-kit.json-schema-subset/v1. "
        "Run `claude plugin update llm-scripting-kit@plugins-kit` and restart the session."
    )


# -- every call shape is bound -----------------------------------------------------

_CONTRACT_NAMES = [
    "canonical_json",
    "check_request",
    "check_request_mapping",
    "validate_input",
    "decision_outcome",
    "same_resolution",
    "expiry",
    "lapsed",
    "bound_reason",
    "resolution_document",
]
_VALIDATOR_NAMES = ["check_schema", "validate"]


def _copy_of(module):
    return types.SimpleNamespace(**{k: v for k, v in vars(module).items() if not k.startswith("__")})


@pytest.mark.parametrize("name", _CONTRACT_NAMES + _VALIDATOR_NAMES)
def test_probe_rejects_unbindable_call(monkeypatch, name):
    assert [call[0] for call in interrupts._CONTRACT_CALLS] == _CONTRACT_NAMES
    assert [call[0] for call in interrupts._VALIDATOR_CALLS] == _VALIDATOR_NAMES
    real = interrupts._import_module
    on_contract = name in _CONTRACT_NAMES
    target = CONTRACT_MODULE if on_contract else VALIDATOR_MODULE
    narrowed = _copy_of(real(target))
    # The same name, without the arguments this package passes.
    setattr(narrowed, name, lambda: None)
    monkeypatch.setattr(
        interrupts, "_import_module", lambda module: narrowed if module == target else real(module)
    )
    with pytest.raises(InterruptSupportError) as info:
        interrupts.support()
    text = str(info.value)
    assert f"the installed {name} does not accept this call shape" in text
    owner = "bootstrap@plugins-kit" if on_contract else "llm-scripting-kit@plugins-kit"
    assert f"`claude plugin update {owner}`" in text
    assert "plugin install" not in text


def test_probe_accepts_the_committed_modules():
    found_contract, found_validator = interrupts.support()
    assert found_contract is contract
    assert found_validator.__name__ == VALIDATOR_MODULE
    assert SUBSET in found_validator.SUPPORTED_SUBSETS


@pytest.mark.parametrize("name", ["RequestError", "InputError", "DecisionError", "ValidatorError"])
def test_probe_rejects_a_contract_without_an_error_class(monkeypatch, name):
    real = interrupts._import_module
    narrowed = _copy_of(contract)
    delattr(narrowed, name)
    monkeypatch.setattr(
        interrupts,
        "_import_module",
        lambda module: narrowed if module == CONTRACT_MODULE else real(module),
    )
    with pytest.raises(InterruptSupportError, match=f"the installed module has no {name}"):
        interrupts.support()


# -- parity with the contract -------------------------------------------------------


@pytest.mark.parametrize(
    "expires_at, now, expected",
    [(100.0, 99.999, False), (100.0, 100.0, True), (100.0, 100.001, True), (None, 1e12, False)],
    ids=["before", "at", "after", "none"],
)
def test_lapse_rule_matches_the_contract(expires_at, now, expected):
    record = InterruptRecord(
        id="1",
        run_id=RUN,
        unit_id="u0",
        fencing_token=1,
        envelope="plugins-kit.interrupt-request/v1",
        kind="approval",
        request_schema={},
        payload={},
        created_at=1.0,
        expires_at=expires_at,
    )
    assert record.lapsed(now) is expected
    assert contract.lapsed(expires_at, now) is expected
    assert interrupts.lapsed(expires_at, now) is expected


def test_cpk_literals_equal_the_contract():
    held = {
        "CONTRACT": interrupts.CONTRACT,
        "REQUEST_ENVELOPE": interrupts.REQUEST_ENVELOPE,
        "model.INTERRUPT_REQUEST_ENVELOPE": model_mod.INTERRUPT_REQUEST_ENVELOPE,
        "request default": InterruptRequest(kind="k", request_schema={}, payload={}).envelope,
        "RESOLUTION_ENVELOPE": interrupts.RESOLUTION_ENVELOPE,
        "VALIDATOR_SUBSET": interrupts.VALIDATOR_SUBSET,
        "INPUT_LIMIT": interrupts.INPUT_LIMIT,
        "KIND_LIMIT": interrupts.KIND_LIMIT,
        "EXPIRES_IN_S_MAX": interrupts.EXPIRES_IN_S_MAX,
        "REASON_LIMIT": interrupts.REASON_LIMIT,
    }
    assert held == {
        "CONTRACT": "plugins-kit.interrupt-contract/v1",
        "REQUEST_ENVELOPE": "plugins-kit.interrupt-request/v1",
        "model.INTERRUPT_REQUEST_ENVELOPE": "plugins-kit.interrupt-request/v1",
        "request default": "plugins-kit.interrupt-request/v1",
        "RESOLUTION_ENVELOPE": "plugins-kit.interrupt-resolution/v1",
        "VALIDATOR_SUBSET": "llm-scripting-kit.json-schema-subset/v1",
        "INPUT_LIMIT": 65536,
        "KIND_LIMIT": 64,
        "EXPIRES_IN_S_MAX": 2147483647,
        "REASON_LIMIT": 2000,
    }
    assert held == {
        "CONTRACT": contract.CONTRACT_V1,
        "REQUEST_ENVELOPE": contract.REQUEST_ENVELOPE_V1,
        "model.INTERRUPT_REQUEST_ENVELOPE": contract.REQUEST_ENVELOPE_V1,
        "request default": contract.REQUEST_ENVELOPE_V1,
        "RESOLUTION_ENVELOPE": contract.RESOLUTION_ENVELOPE_V1,
        "VALIDATOR_SUBSET": contract.VALIDATOR_SUBSET,
        "INPUT_LIMIT": contract.INPUT_LIMIT,
        "KIND_LIMIT": contract.KIND_LIMIT,
        "EXPIRES_IN_S_MAX": contract.EXPIRES_IN_S_MAX,
        "REASON_LIMIT": contract.REASON_LIMIT,
    }
    assert interrupts.REQUEST_ENVELOPES == frozenset({"plugins-kit.interrupt-request/v1"})
    assert model_mod.INTERRUPT_POLICIES == ("stop", "release")
    assert contract.CONTRACT_V1 in contract.SUPPORTED_CONTRACTS
    assert sorted(contract.OUTCOMES) == ["answered", "expired", "rejected"]


def test_request_from_mapping_takes_the_request_keys(tmp_path):
    request = interrupts.request_from_mapping(
        {
            "schema": "plugins-kit.interrupt-request/v1",
            "kind": "approval",
            "request_schema": {"type": "object"},
            "payload": {"b": 1, "a": 2},
            "expires_in_s": 30,
        }
    )
    assert request == InterruptRequest(
        kind="approval",
        request_schema={"type": "object"},
        payload={"a": 2, "b": 1},
        expires_in_s=30,
        envelope="plugins-kit.interrupt-request/v1",
    )
    with pytest.raises(model_mod.InterruptRequestError) as info:
        interrupts.request_from_mapping({"kind": "approval", "extra": 1})
    assert str(info.value) == (
        "interrupt request has unknown keys ['extra']; allowed: schema, kind, "
        "request_schema, payload, expires_in_s"
    )


# -- the owner's conditions: no manifest entry is added ----------------------------


def test_manifests_gain_no_entry_for_interrupts():
    plugin = json.loads((CPK / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    manifest = json.loads((CPK / "bootstrap.json").read_text(encoding="utf-8"))
    assert plugin["dependencies"] == ["bootstrap"]
    assert manifest["plugins"] == [
        {
            "ref": "plugins-kit:llm-scripting-kit",
            "enabled": True,
            "scope": "user",
            "install": "auto",
            # arm_call, HALT_BACKPRESSURE, HaltError.retry_after_s, classify_backpressure.
            "min_version": "0.61.0",
        }
    ]
    assert manifest["shared_lib_imports"] == ["llm_scripting_kit", "bootstrap_lib"]
    # Existing launcher call shapes need this floor; interrupts add none.
    assert manifest["requires_bootstrap"] == "0.120.0"
