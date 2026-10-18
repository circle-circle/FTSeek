"""UE SHORT 借券 UNKNOWN 逐票补查契约测试 (2026-10-18)."""
from app.data.borrow_verifier import enrich_surfaced_short_borrow


def _short(symbol="EBS", basis="UNKNOWN", feasible=False):
    return {
        "symbol": symbol,
        "direction": "SHORT",
        "short_check": {
            "borrow_feasible": feasible,
            "borrow_data_basis": basis,
            "downside_catalyst_expiry_hours": 24,
            "squeeze_risk": 3,
            "provided": True,
        },
    }


def test_shortable_is_resolved_material_true():
    item = _short()
    counts = enrich_surfaced_short_borrow(
        [item],
        fetch=lambda syms: {"US.EBS": {"shortable": True, "pool": 251400.0, "fee_rate": 3.0}},
        checked_at="2026-10-18T09:10:00+00:00",
        cache={},
    )
    assert counts["resolved_true"] == 1
    sc = item["short_check"]
    assert sc["borrow_feasible"] is True
    assert sc["borrow_data_basis"] == "MATERIAL"
    assert sc["borrow_pool"] == 251400.0
    assert sc["borrow_fee_rate"] == 3.0
    assert sc["borrow_source"] == "futu_get_margin_ratio_on_demand"
    assert sc["downside_catalyst_expiry_hours"] == 24
    assert sc["squeeze_risk"] == 3


def test_not_shortable_is_resolved_material_false():
    item = _short("ERAS")
    counts = enrich_surfaced_short_borrow(
        [item],
        fetch=lambda syms: {"US.ERAS": {"shortable": False, "pool": 0.0, "fee_rate": 0.0}},
        checked_at="2026-10-18T09:10:00+00:00",
        cache={},
    )
    assert counts["resolved_false"] == 1
    sc = item["short_check"]
    assert sc["borrow_feasible"] is False
    assert sc["borrow_data_basis"] == "MATERIAL"
    assert sc["borrow_reject_reason"] == "not_shortable_or_zero_pool"


def test_no_data_keeps_unknown_and_records_audit():
    item = _short("QNCX")
    counts = enrich_surfaced_short_borrow(
        [item],
        fetch=lambda syms: {},
        checked_at="2026-10-18T09:10:00+00:00",
        cache={},
    )
    assert counts["no_data"] == 1
    sc = item["short_check"]
    assert sc["borrow_data_basis"] == "UNKNOWN"
    assert sc["borrow_status"] == "no_data"
    assert sc["borrow_checked_at"] == "2026-10-18T09:10:00+00:00"
    assert sc["borrow_source"] == "futu_get_margin_ratio_on_demand"


def test_fetch_error_keeps_unknown_and_does_not_raise():
    item = _short()

    def _boom(syms):
        raise RuntimeError("futu timeout")

    counts = enrich_surfaced_short_borrow(
        [item], fetch=_boom, checked_at="2026-10-18T09:10:00+00:00", cache={})
    assert counts["error"] == 1
    sc = item["short_check"]
    assert sc["borrow_data_basis"] == "UNKNOWN"
    assert sc["borrow_status"] == "fetch_error"
    assert "futu timeout" in sc["borrow_error"]


def test_already_material_is_not_queried():
    item = _short("TSM", basis="MATERIAL", feasible=True)

    def _never(syms):
        raise AssertionError("already MATERIAL should not be queried")

    counts = enrich_surfaced_short_borrow([item], fetch=_never, cache={})
    assert counts["already_material"] == 1
    assert item["short_check"]["borrow_data_basis"] == "MATERIAL"


def test_non_short_and_shortable_without_pool_are_safe():
    long_item = {"symbol": "AMD", "direction": "LONG", "short_check": None}
    short_item = _short("ASTS")
    counts = enrich_surfaced_short_borrow(
        [long_item, short_item],
        fetch=lambda syms: {"US.ASTS": {"shortable": True, "pool": None, "fee_rate": 3.0}},
        checked_at="2026-10-18T09:10:00+00:00",
        cache={},
    )
    assert long_item["short_check"] is None
    assert counts["no_data"] == 1
    assert short_item["short_check"]["borrow_data_basis"] == "UNKNOWN"


def test_per_symbol_isolation_and_daily_cache():
    calls = []

    def _fetch(syms):
        calls.append(syms[0])
        if syms[0] == "US.BAD":
            raise RuntimeError("bad symbol")
        return {"US.GOOD": {"shortable": True, "pool": 1000.0, "fee_rate": 3.0}}

    cache = {}
    items = [_short("BAD"), _short("GOOD"), _short("GOOD")]
    counts = enrich_surfaced_short_borrow(
        items, fetch=_fetch, checked_at="2026-10-18T09:10:00+00:00", cache=cache,
        batch_size=1)
    assert calls == ["US.BAD", "US.GOOD"]
    assert counts["error"] == 1
    # 两个 GOOD 都从同一缓存结果补全, 但只调用一次 Futu.
    assert counts["resolved_true"] == 2
    assert counts["checked"] == 2


def test_max_symbols_limit():
    items = [_short(f"S{i}") for i in range(3)]
    counts = enrich_surfaced_short_borrow(
        items,
        fetch=lambda syms: {syms[0]: {"shortable": True, "pool": 1.0, "fee_rate": 3.0}},
        checked_at="2026-10-18T09:10:00+00:00",
        cache={},
        max_symbols=1,
        batch_size=1,
    )
    assert counts["checked"] == 1
    assert counts["skipped_limit"] == 2


def test_batch_chunk_poisoning_falls_back_per_symbol():
    items = [_short("A"), _short("B"), _short("C")]
    calls = []

    def _fetch(syms):
        calls.append(tuple(syms))
        if "US.B" in syms:
            return {}
        return {s: {"shortable": True, "pool": 10.0, "fee_rate": 3.0} for s in syms}

    counts = enrich_surfaced_short_borrow(
        items,
        fetch=_fetch,
        checked_at="2026-10-18T09:10:00+00:00",
        cache={},
        batch_size=3,
    )
    assert calls[0] == ("US.A", "US.B", "US.C")
    assert counts["resolved_true"] == 2
    assert counts["no_data"] == 1
    assert items[1]["short_check"]["borrow_status"] == "no_data"
