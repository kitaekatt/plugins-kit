"""Tests for classified harness seat discovery and the ``seats`` command."""

import json

import pytest

from llm_scripting_kit import (
    EndpointEntry,
    EndpointMetadataError,
    EndpointRegistry,
    EndpointRegistryError,
    HARNESS_KIND,
    Reachability,
    Seat,
    SeatResolutionError,
    SeatSelf,
    SeatsResult,
    STATUS_REACHABLE,
    STATUS_UNKNOWN,
    UnclassifiedEntry,
    discover_seats,
    discover_model_entries,
)
from llm_scripting_kit import cli
from llm_scripting_kit import seats as seats_module
from llm_scripting_kit.model_endpoints import load_endpoint_registry


def _harness(
    endpoint: str,
    model: str,
    *,
    tier: int | None = None,
    family: str | None = None,
    harness: str = "claude",
) -> EndpointEntry:
    return EndpointEntry(
        id=endpoint,
        base_url=None,
        model=model,
        kind=HARNESS_KIND,
        harness=harness,
        tier=tier,
        family=family,
    )


def _registry(*entries: EndpointEntry) -> EndpointRegistry:
    return EndpointRegistry(entries={entry.id: entry for entry in entries})


def _config(monkeypatch):
    monkeypatch.setattr(seats_module, "load_model_config", lambda project_root=None: {"endpoints": {}})


def _reachable(status: str = STATUS_REACHABLE, detail: str = "ok") -> Reachability:
    return Reachability(status=status, checked="cli-version", detail=detail)


def test_discovery_selects_up_and_beside_and_orders_them(monkeypatch):
    _config(monkeypatch)
    registry = _registry(
        _harness("self", "self-model", tier=2, family="alpha"),
        _harness("same-family", "same-model", tier=2, family="alpha"),
        _harness("beside-z", "beside-z-model", tier=2, family="beta"),
        _harness("up-low", "up-low-model", tier=3, family="beta"),
        _harness("up-high", "up-high-model", tier=4, family="alpha"),
        _harness("lower", "lower-model", tier=1, family="beta"),
        EndpointEntry("transport", "http://example.invalid/v1", "transport-model", tier=4, family="beta"),
    )
    calls = []

    def fake_check_many(entries, *, timeout, project_root):
        calls.append((list(entries), timeout, project_root))
        return {name: _reachable() for name in entries}

    monkeypatch.setattr(seats_module, "check_many", fake_check_many)

    result = discover_seats("self", registry=registry, timeout=2.5)

    assert [seat.endpoint for seat in result.seats] == [
        "up-high", "up-low", "beside-z"
    ]
    assert [seat.relation for seat in result.seats] == ["UP", "UP", "BESIDE"]
    assert [seat.endpoint for seat in result.unclassified] == []
    assert calls == [
        (["up-high", "up-low", "beside-z"], 2.5, None)
    ]


def test_same_family_same_tier_is_not_beside_and_unclassified_is_listed(monkeypatch):
    _config(monkeypatch)
    registry = _registry(
        _harness("self", "self-model", tier=2, family="alpha"),
        _harness("same", "same-model", tier=2, family="alpha"),
        _harness("missing-tier", "missing-tier-model", family="beta"),
        _harness("missing-family", "missing-family-model", tier=3),
    )
    calls = []
    monkeypatch.setattr(
        seats_module,
        "check_many",
        lambda entries, **kwargs: (calls.append(list(entries)), {name: _reachable() for name in entries})[1],
    )

    result = discover_seats("self", registry=registry)

    assert result.seats == ()
    assert [entry.endpoint for entry in result.unclassified] == [
        "missing-family", "missing-tier"
    ]
    assert calls == [[]]


def test_unreachable_is_excluded_and_unknown_is_reported(monkeypatch):
    _config(monkeypatch)
    registry = _registry(
        _harness("self", "self-model", tier=2, family="alpha"),
        _harness("down", "down-model", tier=3, family="beta"),
        _harness("indeterminate", "unknown-model", tier=4, family="beta"),
        _harness("up", "up-model", tier=3, family="alpha"),
    )

    def fake_check_many(entries, **kwargs):
        return {
            "down": _reachable("unreachable", "down"),
            "indeterminate": _reachable(STATUS_UNKNOWN, "could not check"),
            "up": _reachable(),
        }

    monkeypatch.setattr(seats_module, "check_many", fake_check_many)
    result = discover_seats("self", registry=registry)

    assert [seat.endpoint for seat in result.seats] == ["up"]
    assert [seat.endpoint for seat in result.probe_unknown] == ["indeterminate"]
    assert result.probe_unknown[0].reachability.status == STATUS_UNKNOWN


def test_self_resolves_by_endpoint_then_unique_model(monkeypatch):
    _config(monkeypatch)
    registry = _registry(
        _harness("endpoint-id", "model-id", tier=2, family="alpha"),
        _harness("other", "other-model", tier=3, family="beta"),
    )
    monkeypatch.setattr(seats_module, "check_many", lambda entries, **kwargs: {})

    assert discover_seats("endpoint-id", registry=registry).self.endpoint == "endpoint-id"
    assert discover_seats("model-id", registry=registry).self.endpoint == "endpoint-id"


def test_ambiguous_unknown_and_unclassified_self_errors(monkeypatch):
    _config(monkeypatch)
    monkeypatch.setattr(seats_module, "check_many", lambda entries, **kwargs: {})
    ambiguous = _registry(
        _harness("one", "same-model", tier=2, family="alpha"),
        _harness("two", "same-model", tier=3, family="beta"),
    )
    with pytest.raises(SeatResolutionError, match="ambiguous"):
        discover_seats("same-model", registry=ambiguous)
    with pytest.raises(SeatResolutionError, match="unknown"):
        discover_seats("missing", registry=ambiguous)
    unclassified = _registry(_harness("unclassified", "u-model"))
    with pytest.raises(SeatResolutionError, match="unclassified"):
        discover_seats("unclassified", registry=unclassified)


@pytest.mark.parametrize(
    "field, value",
    [("tier", 0), ("tier", 5), ("tier", True), ("tier", "2"), ("family", "")],
)
def test_registry_classification_validation_is_a_named_config_error(
    tmp_path, monkeypatch, field, value
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    path = home / ".claude" / "config" / "model-endpoints.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "models:\n  broken:\n    harness: claude\n    model: test-model\n"
        f"    {field}: {json.dumps(value)}\n",
        encoding="utf-8",
    )

    with pytest.raises(EndpointMetadataError) as exc:
        load_endpoint_registry()
    assert "broken" in str(exc.value)
    assert field in str(exc.value)


def test_layered_config_classification_validation_is_a_named_config_error():
    config = {
        "endpoints": {
            "broken": {"harness": "claude", "model": "test-model", "tier": 9}
        }
    }
    with pytest.raises(EndpointMetadataError, match="broken"):
        discover_model_entries(config=config, registry=EndpointRegistry())


def _cli_result(*, unknown: bool = False, seats: tuple[Seat, ...] = ()) -> SeatsResult:
    self_record = SeatSelf("self", "self-model", 2, "workhorse", "alpha", "claude")
    unknown_records = ()
    if unknown:
        unknown_records = (
            Seat(
                "UP", "unknown", "unknown-model", 3, "strong", "beta", "claude",
                _reachable(STATUS_UNKNOWN, "probe unavailable"),
            ),
        )
    return SeatsResult(
        self=self_record,
        seats=seats,
        unclassified=(UnclassifiedEntry("unclassified", "u-model", None, None, None, "claude"),),
        probe_unknown=unknown_records,
    )


def test_cli_text_order_and_empty_output(monkeypatch, capsys):
    seats = (
        Seat("UP", "fable", "fable-model", 4, "frontier", "alpha", "claude", _reachable()),
        Seat("BESIDE", "sol", "sol-model", 2, "workhorse", "beta", "codex", _reachable()),
    )
    monkeypatch.setattr(cli, "discover_seats", lambda *args, **kwargs: _cli_result(seats=seats))

    assert cli.main(["seats", "--self", "self"]) == cli.EXIT_OK
    assert capsys.readouterr().out == (
        "UP fable (frontier, claude)\nBESIDE sol (workhorse, codex)\n"
    )

    monkeypatch.setattr(cli, "discover_seats", lambda *args, **kwargs: _cli_result())
    assert cli.main(["seats", "--self", "self"]) == cli.EXIT_OK
    assert capsys.readouterr().out == ""


def test_cli_json_shape_and_indeterminate_exit(monkeypatch, capsys):
    result = _cli_result(unknown=True)
    monkeypatch.setattr(cli, "discover_seats", lambda *args, **kwargs: result)

    assert cli.main(["seats", "--self", "self", "--json"]) == cli.EXIT_INDETERMINATE
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert set(payload) == {
        "self",
        "seats",
        "unclassified",
        "probe_unknown",
        "conserved",
    }
    assert payload["self"]["band"] == "workhorse"
    assert payload["probe_unknown"][0]["reachability"]["status"] == "unknown"
    assert "probe unavailable" in captured.err


def test_cli_self_resolution_error_is_exit_two_and_stderr(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "discover_seats",
        lambda *args, **kwargs: (_ for _ in ()).throw(SeatResolutionError("bad self")),
    )

    assert cli.main(["seats", "--self", "bad"]) == cli.EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["message"] == "bad self"


def test_frontier_callable_is_importable_from_package_top_level():
    from llm_scripting_kit import discover_seats as public_discover_seats

    assert public_discover_seats is discover_seats


# --- subscription-usage pacing (conserve_usage) ---------------------------


def _conserving(endpoint: str, tier: int, spec) -> EndpointEntry:
    return EndpointEntry(
        id=endpoint,
        base_url=None,
        model=f"{endpoint}-model",
        kind=HARNESS_KIND,
        harness="claude",
        tier=tier,
        family="alpha",
        conserve_usage=spec,
    )


def test_a_conserved_seat_is_withheld_but_reported(monkeypatch):
    from llm_scripting_kit import ConserveSpec, STATUS_CONSERVED
    from llm_scripting_kit.usage_budget import Budget

    _config(monkeypatch)
    spec = ConserveSpec(pool="model_scoped", display_name="Fable")
    registry = _registry(
        _harness("self", "self-model", tier=2, family="beta"),
        _conserving("frontier", 4, spec),
    )
    monkeypatch.setattr(
        seats_module, "check_many", lambda entries, **kw: {n: _reachable() for n in entries}
    )
    monkeypatch.setattr(
        seats_module,
        "pinned_evaluate",
        lambda entry_id, s, harness: Budget(
            status=STATUS_CONSERVED, pool=s.pool, detail="behind pace", resets_at=10
        ),
    )

    result = discover_seats("self", registry=registry)

    # Withheld from the usable seats...
    assert [seat.endpoint for seat in result.seats] == []
    # ...but reported, so a caller can tell "no frontier seat exists" from
    # "the frontier seat is being paced" -- only the second changes on reset.
    assert [seat.endpoint for seat in result.conserved] == ["frontier"]
    assert result.conserved[0].budget.conserved is True
    assert result.to_json()["conserved"][0]["budget"]["status"] == "conserved"


def test_an_available_verdict_leaves_the_seat_usable(monkeypatch):
    from llm_scripting_kit import ConserveSpec, STATUS_AVAILABLE
    from llm_scripting_kit.usage_budget import Budget

    _config(monkeypatch)
    registry = _registry(
        _harness("self", "self-model", tier=2, family="beta"),
        _conserving("frontier", 4, ConserveSpec(pool="seven_day")),
    )
    monkeypatch.setattr(
        seats_module, "check_many", lambda entries, **kw: {n: _reachable() for n in entries}
    )
    monkeypatch.setattr(
        seats_module,
        "pinned_evaluate",
        lambda entry_id, s, harness: Budget(
            status=STATUS_AVAILABLE, pool=s.pool, detail="ahead of pace"
        ),
    )

    result = discover_seats("self", registry=registry)
    assert [seat.endpoint for seat in result.seats] == ["frontier"]
    assert result.conserved == ()


def test_an_unreachable_candidate_is_never_budget_checked(monkeypatch):
    # A seat its probe already excluded gains nothing from a second reason,
    # and evaluating it would pin a verdict for a seat this session never had.
    from llm_scripting_kit import ConserveSpec, STATUS_UNREACHABLE

    _config(monkeypatch)
    registry = _registry(
        _harness("self", "self-model", tier=2, family="beta"),
        _conserving("frontier", 4, ConserveSpec(pool="seven_day")),
    )
    monkeypatch.setattr(
        seats_module,
        "check_many",
        lambda entries, **kw: {
            n: Reachability(status=STATUS_UNREACHABLE, checked="cli-version", detail="absent")
            for n in entries
        },
    )
    calls = []
    monkeypatch.setattr(
        seats_module,
        "pinned_evaluate",
        lambda *args, **kwargs: calls.append(args) or None,
    )

    result = discover_seats("self", registry=registry)
    assert calls == []
    assert result.seats == () and result.conserved == ()


def test_an_endpoint_without_conserve_usage_is_never_budget_checked(monkeypatch):
    _config(monkeypatch)
    registry = _registry(
        _harness("self", "self-model", tier=2, family="beta"),
        _harness("frontier", "frontier-model", tier=4, family="alpha"),
    )
    monkeypatch.setattr(
        seats_module, "check_many", lambda entries, **kw: {n: _reachable() for n in entries}
    )
    calls = []
    monkeypatch.setattr(
        seats_module, "pinned_evaluate", lambda *a, **k: calls.append(a) or None
    )
    result = discover_seats("self", registry=registry)
    assert calls == []
    assert [seat.endpoint for seat in result.seats] == ["frontier"]
    assert result.seats[0].budget is None
    assert "budget" not in result.to_json()["seats"][0]
