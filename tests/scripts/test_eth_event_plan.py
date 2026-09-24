"""RFC-104 阶段 0: ETH 事件计划生成器 hermetic 测试 (9/24 阶段 0 补齐批).

覆盖:
1. 计划生成 (完整事件人群, 不预筛)
2. 源冲突 → ambiguous (不投票不择优)
3. 状态机推进 (planned → armed)
4. 不预筛断言 (计划行不得含 price/volume/mcap 预筛字段)
5. 不是策略卡 (不含 direction/sl_tp/fee/expected_profit)
6. 幂等键与 plan_hash
7. 空源 → 空计划 (fail-closed)
8. 契约同一性 (plan_mod 函数 is EXEC 函数对象)
9. 刷新合并语义 (state 不回退 / ambiguous 自愈 / append 新票 / 幂等)
10. actual_arrived_at 只记首次
11. 对账 PASS/FAIL 两路 + missed 检出
12. query_actual_value (复用 EXEC._finnhub_today_calendar)
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import scripts.extended_hours_entry_executor as EXEC


def _load():
    """加载 eth_event_plan 模块 (避免与 conftest 冲突)."""
    spec = importlib.util.spec_from_file_location(
        "eth_event_plan", ROOT / "scripts" / "eth_event_plan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def plan_mod():
    return _load()


# ── Fixture 数据 ──

FINNHUB_ROWS_BMO = [
    {"symbol": "AAPL", "date": "2026-09-24", "timeOfTheDay": "BMO",
     "_src": "finnhub", "_asof": "2026-09-24T04:00:00"},
    {"symbol": "MSFT", "date": "2026-09-24", "timeOfTheDay": "BMO",
     "_src": "finnhub", "_asof": "2026-09-24T04:00:00"},
    {"symbol": "TSLA", "date": "2026-09-24", "timeOfTheDay": "AMC",
     "_src": "finnhub", "_asof": "2026-09-24T04:00:00"},
]

FUTU_ROWS_BMO = [
    {"symbol": "AAPL", "reportDate": "2026-09-24", "timeOfTheDay": "before",
     "_src": "futu", "_asof": "2026-09-24T04:00:00"},
    {"symbol": "GOOG", "reportDate": "2026-09-24", "timeOfTheDay": "before",
     "_src": "futu", "_asof": "2026-09-24T04:00:00"},
]

FINNHUB_ROWS_AMC = [
    {"symbol": "TSLA", "date": "2026-09-24", "timeOfTheDay": "AMC",
     "_src": "finnhub", "_asof": "2026-09-24T16:00:00"},
]

FUTU_ROWS_AMC = [
    {"symbol": "TSLA", "reportDate": "2026-09-24", "timeOfTheDay": "after",
     "_src": "futu", "_asof": "2026-09-24T16:00:00"},
    {"symbol": "NVDA", "reportDate": "2026-09-24", "timeOfTheDay": "after",
     "_src": "futu", "_asof": "2026-09-24T16:00:00"},
]


# ── 1. 计划生成 ──

class TestPlanGeneration:
    """计划生成: 完整事件人群, 按 session+相位过滤."""

    def test_premarket_bmo_tickets(self, plan_mod):
        """盘前计划: 只含 BMO 票."""
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO,
            futu_rows=FUTU_ROWS_BMO,
        )
        symbols = {r["symbol"] for r in rows}
        assert "AAPL" in symbols
        assert "MSFT" in symbols
        assert "GOOG" in symbols
        assert "TSLA" not in symbols

    def test_afterhours_amc_tickets(self, plan_mod):
        """盘后计划: 只含 AMC 票."""
        rows = plan_mod.generate_plan(
            "2026-09-24", "afterhours",
            finnhub_rows=FINNHUB_ROWS_AMC,
            futu_rows=FUTU_ROWS_AMC,
        )
        symbols = {r["symbol"] for r in rows}
        assert "TSLA" in symbols
        assert "NVDA" in symbols
        assert "AAPL" not in symbols

    def test_source_union(self, plan_mod):
        """多源并集: finnhub 独有 + futu 独有均入计划."""
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=[
                {"symbol": "ONLY_FH", "date": "2026-09-24", "timeOfTheDay": "BMO",
                 "_src": "finnhub", "_asof": "t"},
            ],
            futu_rows=[
                {"symbol": "ONLY_FT", "reportDate": "2026-09-24", "timeOfTheDay": "before",
                 "_src": "futu", "_asof": "t"},
            ],
        )
        symbols = {r["symbol"] for r in rows}
        assert "ONLY_FH" in symbols
        assert "ONLY_FT" in symbols

    def test_empty_sources_empty_plan(self, plan_mod):
        """两源皆空 → 空计划 (fail-closed)."""
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=[], futu_rows=[],
        )
        assert rows == []

    def test_plan_row_count_reasonable(self, plan_mod):
        """计划行数应在合理范围 (7-9, 非 44)."""
        fh = [
            {"symbol": f"SYM{i}", "date": "2026-09-24", "timeOfTheDay": "BMO",
             "_src": "finnhub", "_asof": "t"}
            for i in range(8)
        ]
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=fh, futu_rows=[],
        )
        assert len(rows) == 8
        assert 1 <= len(rows) <= 50


# ── 2. 源冲突 → ambiguous ──

class TestSourceConflict:
    """源冲突: 不投票不择优, state=ambiguous."""

    def test_phase_conflict_ambiguous(self, plan_mod):
        """两源相位不一致 → state=ambiguous."""
        fh = [
            {"symbol": "CONFLICT", "date": "2026-09-24", "timeOfTheDay": "BMO",
             "_src": "finnhub", "_asof": "t"},
        ]
        ft = [
            {"symbol": "CONFLICT", "reportDate": "2026-09-24", "timeOfTheDay": "after",
             "_src": "futu", "_asof": "t"},
        ]
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=fh, futu_rows=ft,
        )
        conflict = [r for r in rows if r["symbol"] == "CONFLICT"]
        assert len(conflict) == 1
        assert conflict[0]["state"] == "ambiguous"

    def test_same_phase_no_conflict(self, plan_mod):
        """两源相位一致 → state=planned."""
        fh = [
            {"symbol": "AGREE", "date": "2026-09-24", "timeOfTheDay": "BMO",
             "_src": "finnhub", "_asof": "t"},
        ]
        ft = [
            {"symbol": "AGREE", "reportDate": "2026-09-24", "timeOfTheDay": "before",
             "_src": "futu", "_asof": "t"},
        ]
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=fh, futu_rows=ft,
        )
        agree = [r for r in rows if r["symbol"] == "AGREE"]
        assert len(agree) == 1
        assert agree[0]["state"] == "planned"

    def test_unknown_phase_ambiguous(self, plan_mod):
        """相位字段为空/不可识别 → state=ambiguous."""
        fh = [
            {"symbol": "UNKNOWN", "date": "2026-09-24", "timeOfTheDay": "",
             "_src": "finnhub", "_asof": "t"},
        ]
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=fh, futu_rows=[],
        )
        unk = [r for r in rows if r["symbol"] == "UNKNOWN"]
        if unk:
            assert unk[0]["state"] == "ambiguous"


# ── 3. 状态机推进 ──

class TestStateMachine:
    """状态机推进: planned → armed (由时刻窗触发)."""

    def test_premarket_bmo_advance_after_0400(self, plan_mod):
        row = {
            "symbol": "AAPL", "et_date": "2026-09-24", "session": "premarket",
            "phase": "before", "state": "planned", "state_since": "t0",
        }
        advanced = plan_mod.advance_state(row, "2026-09-24T05:00:00")
        assert advanced["state"] == "armed"

    def test_premarket_bmo_no_advance_before_0400(self, plan_mod):
        row = {
            "symbol": "AAPL", "et_date": "2026-09-24", "session": "premarket",
            "phase": "before", "state": "planned", "state_since": "t0",
        }
        advanced = plan_mod.advance_state(row, "2026-09-24T03:00:00")
        assert advanced["state"] == "planned"

    def test_afterhours_amc_advance_after_1600(self, plan_mod):
        row = {
            "symbol": "TSLA", "et_date": "2026-09-24", "session": "afterhours",
            "phase": "after", "state": "planned", "state_since": "t0",
        }
        advanced = plan_mod.advance_state(row, "2026-09-24T17:00:00")
        assert advanced["state"] == "armed"

    def test_ambiguous_no_auto_advance(self, plan_mod):
        row = {
            "symbol": "CONFLICT", "et_date": "2026-09-24", "session": "premarket",
            "phase": "before", "state": "ambiguous", "state_since": "t0",
        }
        advanced = plan_mod.advance_state(row, "2026-09-24T10:00:00")
        assert advanced["state"] == "ambiguous"

    def test_terminal_state_no_advance(self, plan_mod):
        for terminal in ("ordered", "rejected", "expired"):
            row = {
                "symbol": "X", "et_date": "2026-09-24", "session": "premarket",
                "phase": "before", "state": terminal, "state_since": "t0",
            }
            advanced = plan_mod.advance_state(row, "2026-09-24T10:00:00")
            assert advanced["state"] == terminal


# ── 4. 不预筛断言 ──

class TestNoPrefilter:
    """计划行不得含 price/volume/mcap 预筛字段 (收紧 §1)."""

    def test_no_price_field(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert "price" not in r
            assert "min_price" not in r
            assert "current_price" not in r

    def test_no_volume_field(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert "volume" not in r
            assert "min_volume" not in r
            assert "min_volume_now" not in r

    def test_no_mcap_field(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert "mcap" not in r
            assert "market_cap" not in r

    def test_gate_snapshot_empty_audit_only(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert "gate_snapshot" in r
            assert r["gate_snapshot"] == {}


# ── 5. 不是策略卡 ──

class TestNotStrategyCard:
    """计划行不含方向/SL/TP/费用/预期收益 (收紧 §3)."""

    def test_no_direction(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert "direction" not in r

    def test_no_sl_tp(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert "sl_tp" not in r
            assert "stop_loss" not in r
            assert "take_profit" not in r

    def test_no_fee(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert "fee" not in r
            assert "fee_pct" not in r

    def test_no_expected_profit(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert "expected_profit" not in r
            assert "expected_return" not in r


# ── 6. 幂等键与 plan_hash ──

class TestIdempotency:
    """幂等键 (symbol, et_date, session, phase) + plan_hash."""

    def test_plan_hash_deterministic(self, plan_mod):
        rows1 = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        rows2 = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        hash1 = {r["symbol"]: r["plan_hash"] for r in rows1}
        hash2 = {r["symbol"]: r["plan_hash"] for r in rows2}
        assert hash1 == hash2

    def test_plan_hash_unique_per_symbol(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        hashes = [r["plan_hash"] for r in rows]
        assert len(hashes) == len(set(hashes))

    def test_dedup_by_symbol_date(self, plan_mod):
        fh = [
            {"symbol": "DUP", "date": "2026-09-24", "timeOfTheDay": "BMO",
             "_src": "finnhub", "_asof": "t1"},
        ]
        ft = [
            {"symbol": "DUP", "reportDate": "2026-09-24", "timeOfTheDay": "before",
             "_src": "futu", "_asof": "t2"},
        ]
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=fh, futu_rows=ft,
        )
        dup_rows = [r for r in rows if r["symbol"] == "DUP"]
        assert len(dup_rows) == 1
        assert len(dup_rows[0]["sources"]) == 2


# ── 7. Schema 完整性 ──

class TestSchema:
    """计划行 schema 完整性."""

    REQUIRED_FIELDS = {
        "symbol", "et_date", "session", "phase",
        "sources", "data_expectation", "gate_snapshot",
        "state", "state_since", "attempts", "reject_reason",
        "generated_at", "refreshed_at", "source_versions", "plan_hash",
        # 9/24 addendum 新增字段
        "actual_arrived_at", "actual_value",
    }

    def test_required_fields_present(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            for field in self.REQUIRED_FIELDS:
                assert field in r, f"缺失必需字段: {field}"

    def test_state_valid(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        valid = plan_mod.VALID_STATES
        for r in rows:
            assert r["state"] in valid

    def test_sources_structure(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert isinstance(r["sources"], list)
            for src in r["sources"]:
                assert "name" in src
                assert "reportDate" in src
                assert "timeOfTheDay" in src
                assert "asof" in src

    def test_data_expectation_structure(self, plan_mod):
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            de = r["data_expectation"]
            assert "basis" in de
            assert "expected_available_after_et" in de
            assert "fallback_days" in de

    def test_actual_fields_default_null(self, plan_mod):
        """actual_arrived_at / actual_value 初始为 null."""
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert r["actual_arrived_at"] is None
            assert r["actual_value"] is None


# ── 8. 文件 I/O ──

class TestFileIO:
    """计划文件读写 (原子写)."""

    def test_save_and_load(self, plan_mod, tmp_path, monkeypatch):
        monkeypatch.setattr(plan_mod, "PLAN_DIR", tmp_path)
        rows = [
            {"symbol": "AAPL", "et_date": "2026-09-24", "session": "premarket",
             "phase": "before", "state": "planned", "plan_hash": "abc123"},
        ]
        plan_mod.save_plan("2026-09-24", "premarket", rows)
        loaded = plan_mod.load_plan("2026-09-24", "premarket")
        assert loaded == rows

    def test_load_nonexistent(self, plan_mod, tmp_path, monkeypatch):
        monkeypatch.setattr(plan_mod, "PLAN_DIR", tmp_path)
        loaded = plan_mod.load_plan("2099-01-01", "premarket")
        assert loaded == []


# ── 9. 相位解析 (复用内核一致性) ──

class TestPhaseParsing:
    """相位解析与 executor._parse_earnings_phase 一致."""

    @pytest.mark.parametrize("input_val,expect_before,expect_after", [
        ("BMO", True, False),
        ("AMC", False, True),
        ("before", True, False),
        ("after", False, True),
        ("pre-market", True, False),
        ("post-market", False, True),
        ("", False, False),
        ("REGULAR", False, False),
    ])
    def test_parse_phase(self, plan_mod, input_val, expect_before, expect_after):
        before, after = plan_mod.parse_earnings_phase({"timeOfTheDay": input_val})
        assert before == expect_before
        assert after == expect_after


# ── 10. premarket AMC pairing ──

class TestPremarketAMCPairing:
    """盘前接受昨日 AMC (配置开关)."""

    def test_yesterday_amc_included_when_pairing_on(self, plan_mod):
        fh = [
            {"symbol": "YEST_AMC", "date": "2026-09-23", "timeOfTheDay": "AMC",
             "_src": "finnhub", "_asof": "t"},
        ]
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=fh, futu_rows=[],
            premarket_amc_pairing=True,
        )
        symbols = {r["symbol"] for r in rows}
        assert "YEST_AMC" in symbols

    def test_yesterday_amc_excluded_when_pairing_off(self, plan_mod):
        fh = [
            {"symbol": "YEST_AMC", "date": "2026-09-23", "timeOfTheDay": "AMC",
             "_src": "finnhub", "_asof": "t"},
        ]
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=fh, futu_rows=[],
            premarket_amc_pairing=False,
        )
        symbols = {r["symbol"] for r in rows}
        assert "YEST_AMC" not in symbols


# ── 11. 契约同一性 (RFC-104 §5: 消灭双实现漂移面) ──

class TestContractIdentity:
    """plan_mod 使用的函数对象 is EXEC 的同一对象 — 彻底消灭漂移面."""

    def test_parse_earnings_phase_is_exec(self, plan_mod):
        """parse_earnings_phase 与 EXEC._parse_earnings_phase 是同一函数对象."""
        assert plan_mod.parse_earnings_phase is EXEC._parse_earnings_phase

    def test_session_event_pairs_is_exec(self, plan_mod):
        """session_event_pairs 与 EXEC._session_event_pairs 是同一函数对象."""
        assert plan_mod.session_event_pairs is EXEC._session_event_pairs


# ── 12. 刷新合并语义 ──

class TestRefreshMerge:
    """refresh_plan 合并语义: state 只前进不回退 + ambiguous 自愈 + append 新票 + 幂等."""

    def _make_row(self, plan_mod, symbol="AAPL", state="planned", phase="before",
                  actual_arrived_at=None, actual_value=None):
        """构造测试用计划行."""
        return {
            "symbol": symbol,
            "et_date": "2026-09-24",
            "session": "premarket",
            "phase": phase,
            "sources": [{"name": "finnhub", "reportDate": "2026-09-24",
                         "timeOfTheDay": "BMO", "asof": "t1"}],
            "data_expectation": {"basis": "test", "expected_available_after_et": "08:05",
                                 "fallback_days": 1},
            "gate_snapshot": {},
            "state": state,
            "state_since": "2026-09-24T00:00:00",
            "attempts": 0,
            "reject_reason": None,
            "generated_at": "2026-09-24T00:00:00",
            "refreshed_at": "2026-09-24T00:00:00",
            "source_versions": {"finnhub": "t1"},
            "plan_hash": "test_hash",
            "actual_arrived_at": actual_arrived_at,
            "actual_value": actual_value,
        }

    def test_state_no_regression(self, plan_mod):
        """state 只前进不回退: armed 行不会被退回到 planned."""
        existing = [self._make_row(plan_mod, state="armed")]
        # 新数据给出 planned state (更低序数)
        fresh_fh = [
            {"symbol": "AAPL", "date": "2026-09-24", "timeOfTheDay": "BMO",
             "_src": "finnhub", "_asof": "t2"},
        ]
        result = plan_mod.refresh_plan(
            existing, "2026-09-24", "premarket",
            finnhub_rows=fresh_fh, futu_rows=[],
        )
        aapl = [r for r in result if r["symbol"] == "AAPL"][0]
        assert aapl["state"] == "armed"  # 保持 armed, 不回退

    def test_ambiguous_self_heal(self, plan_mod):
        """ambiguous 行拿到新相位证据 → 迁 planned."""
        existing = [self._make_row(plan_mod, state="ambiguous", phase="unknown")]
        # 新数据给出明确相位
        fresh_fh = [
            {"symbol": "AAPL", "date": "2026-09-24", "timeOfTheDay": "BMO",
             "_src": "finnhub", "_asof": "t2"},
        ]
        result = plan_mod.refresh_plan(
            existing, "2026-09-24", "premarket",
            finnhub_rows=fresh_fh, futu_rows=[],
        )
        aapl = [r for r in result if r["symbol"] == "AAPL"][0]
        assert aapl["state"] == "planned"  # ambiguous → planned (自愈)
        assert aapl["phase"] == "before"

    def test_append_new_ticket(self, plan_mod):
        """新发现的票 append 为 planned."""
        existing = [self._make_row(plan_mod, symbol="AAPL")]
        # 新数据包含 AAPL + 新票 MSFT
        fresh_fh = [
            {"symbol": "AAPL", "date": "2026-09-24", "timeOfTheDay": "BMO",
             "_src": "finnhub", "_asof": "t2"},
            {"symbol": "MSFT", "date": "2026-09-24", "timeOfTheDay": "BMO",
             "_src": "finnhub", "_asof": "t2"},
        ]
        result = plan_mod.refresh_plan(
            existing, "2026-09-24", "premarket",
            finnhub_rows=fresh_fh, futu_rows=[],
        )
        symbols = {r["symbol"] for r in result}
        assert "AAPL" in symbols
        assert "MSFT" in symbols
        assert len(result) == 2

    def test_idempotent_plan_hash(self, plan_mod):
        """同数据重跑 plan_hash 不变."""
        existing = [self._make_row(plan_mod)]
        fresh_fh = [
            {"symbol": "AAPL", "date": "2026-09-24", "timeOfTheDay": "BMO",
             "_src": "finnhub", "_asof": "t2"},
        ]
        r1 = plan_mod.refresh_plan(
            existing, "2026-09-24", "premarket",
            finnhub_rows=fresh_fh, futu_rows=[],
        )
        r2 = plan_mod.refresh_plan(
            existing, "2026-09-24", "premarket",
            finnhub_rows=fresh_fh, futu_rows=[],
        )
        assert r1[0]["plan_hash"] == r2[0]["plan_hash"]

    def test_actual_arrived_at_preserved(self, plan_mod):
        """actual_arrived_at 已有非 null 值时刷新不覆盖."""
        existing = [self._make_row(
            plan_mod,
            actual_arrived_at="2026-09-24T08:05:00",
            actual_value=1.23,
        )]
        fresh_fh = [
            {"symbol": "AAPL", "date": "2026-09-24", "timeOfTheDay": "BMO",
             "_src": "finnhub", "_asof": "t2"},
        ]
        result = plan_mod.refresh_plan(
            existing, "2026-09-24", "premarket",
            finnhub_rows=fresh_fh, futu_rows=[],
        )
        aapl = [r for r in result if r["symbol"] == "AAPL"][0]
        assert aapl["actual_arrived_at"] == "2026-09-24T08:05:00"
        assert aapl["actual_value"] == 1.23

    def test_refresh_updates_sources(self, plan_mod):
        """刷新更新 sources/source_versions."""
        existing = [self._make_row(plan_mod)]
        fresh_fh = [
            {"symbol": "AAPL", "date": "2026-09-24", "timeOfTheDay": "BMO",
             "_src": "finnhub", "_asof": "newer_asof"},
        ]
        fresh_ft = [
            {"symbol": "AAPL", "reportDate": "2026-09-24", "timeOfTheDay": "before",
             "_src": "futu", "_asof": "futu_asof"},
        ]
        result = plan_mod.refresh_plan(
            existing, "2026-09-24", "premarket",
            finnhub_rows=fresh_fh, futu_rows=fresh_ft,
        )
        aapl = [r for r in result if r["symbol"] == "AAPL"][0]
        src_names = {s["name"] for s in aapl["sources"]}
        assert "finnhub" in src_names
        assert "futu" in src_names


# ── 13. actual_arrived_at 只记首次 ──

class TestActualArrivedAt:
    """actual_arrived_at 只记第一次: 后续刷新不覆盖."""

    def test_first_actual_recorded(self, plan_mod):
        """generate_plan 产出 initial null actual 字段."""
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=[
                {"symbol": "AAPL", "date": "2026-09-24", "timeOfTheDay": "BMO",
                 "_src": "finnhub", "_asof": "t"},
            ],
            futu_rows=[],
        )
        assert rows[0]["actual_arrived_at"] is None
        assert rows[0]["actual_value"] is None

    def test_refresh_preserves_first_actual(self, plan_mod):
        """第一次记录 actual 后, 刷新不覆盖."""
        row = {
            "symbol": "AAPL", "et_date": "2026-09-24", "session": "premarket",
            "phase": "before", "state": "planned", "state_since": "t0",
            "sources": [], "data_expectation": {}, "gate_snapshot": {},
            "attempts": 0, "reject_reason": None,
            "generated_at": "t0", "refreshed_at": "t0",
            "source_versions": {}, "plan_hash": "h",
            "actual_arrived_at": "2026-09-24T08:05:00",
            "actual_value": 1.50,
        }
        fresh_fh = [
            {"symbol": "AAPL", "date": "2026-09-24", "timeOfTheDay": "BMO",
             "_src": "finnhub", "_asof": "t2"},
        ]
        result = plan_mod.refresh_plan(
            [row], "2026-09-24", "premarket",
            finnhub_rows=fresh_fh, futu_rows=[],
        )
        assert result[0]["actual_arrived_at"] == "2026-09-24T08:05:00"
        assert result[0]["actual_value"] == 1.50


# ── 14. query_actual_value ──

class TestQueryActualValue:
    """query_actual_value: 查 Finnhub actual (复用 EXEC._finnhub_today_calendar)."""

    def test_actual_present(self, plan_mod):
        """epsActual 存在 → 返回值."""
        mock_cal = {
            "AAPL": {"symbol": "AAPL", "date": "2026-09-24", "hour": "BMO",
                      "epsActual": 1.50, "epsEstimate": 1.20},
        }
        with patch.object(EXEC, "_finnhub_today_calendar", return_value=(mock_cal, None)):
            result = plan_mod.query_actual_value("AAPL", "2026-09-24")
        assert result is not None
        assert result["epsActual"] == 1.50

    def test_actual_missing(self, plan_mod):
        """epsActual 为 None → 返回 None."""
        mock_cal = {
            "AAPL": {"symbol": "AAPL", "date": "2026-09-24", "hour": "BMO",
                      "epsActual": None, "epsEstimate": 1.20},
        }
        with patch.object(EXEC, "_finnhub_today_calendar", return_value=(mock_cal, None)):
            result = plan_mod.query_actual_value("AAPL", "2026-09-24")
        assert result is None

    def test_ticket_not_found(self, plan_mod):
        """票不在日历中 → 返回 None."""
        mock_cal = {
            "MSFT": {"symbol": "MSFT", "date": "2026-09-24", "hour": "BMO",
                      "epsActual": 2.0, "epsEstimate": 1.8},
        }
        with patch.object(EXEC, "_finnhub_today_calendar", return_value=(mock_cal, None)):
            result = plan_mod.query_actual_value("AAPL", "2026-09-24")
        assert result is None

    def test_calendar_error(self, plan_mod):
        """日历查询失败 → 返回 None."""
        with patch.object(EXEC, "_finnhub_today_calendar",
                          return_value=(None, "finnhub_error")):
            result = plan_mod.query_actual_value("AAPL", "2026-09-24")
        assert result is None

    def test_us_prefix_stripped(self, plan_mod):
        """US. 前缀自动去除."""
        mock_cal = {
            "AAPL": {"symbol": "AAPL", "date": "2026-09-24", "hour": "BMO",
                      "epsActual": 1.50, "epsEstimate": 1.20},
        }
        with patch.object(EXEC, "_finnhub_today_calendar", return_value=(mock_cal, None)):
            result = plan_mod.query_actual_value("US.AAPL", "2026-09-24")
        assert result is not None
        assert result["epsActual"] == 1.50


# ── 15. 对账 PASS/FAIL ──

class TestReconcile:
    """对账: 读台账 vs 计划, 产出覆盖率 + 漏票检出."""

    def test_reconcile_pass_no_missed(self, plan_mod, tmp_path, monkeypatch):
        """计划完全覆盖事件票 → PASS."""
        monkeypatch.setattr(plan_mod, "PLAN_DIR", tmp_path)
        monkeypatch.setattr(plan_mod, "ROOT", tmp_path)

        # 写计划
        plan_rows = [
            {"symbol": "AAPL", "et_date": "2026-09-24", "session": "premarket",
             "phase": "before", "state": "planned", "sources": [],
             "data_expectation": {}, "gate_snapshot": {},
             "state_since": "t0", "attempts": 0, "reject_reason": None,
             "generated_at": "t0", "refreshed_at": "t0",
             "source_versions": {}, "plan_hash": "h1",
             "actual_arrived_at": "2026-09-24T08:05:00", "actual_value": 1.5},
            {"symbol": "MSFT", "et_date": "2026-09-24", "session": "premarket",
             "phase": "before", "state": "planned", "sources": [],
             "data_expectation": {}, "gate_snapshot": {},
             "state_since": "t0", "attempts": 0, "reject_reason": None,
             "generated_at": "t0", "refreshed_at": "t0",
             "source_versions": {}, "plan_hash": "h2",
             "actual_arrived_at": None, "actual_value": None},
        ]
        plan_mod.save_plan("2026-09-24", "premarket", plan_rows)

        # 写台账 ( earnings rejects )
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        rejects = [
            {"ts": "2026-09-24T08:05:00Z", "session": "premarket",
             "trade_date": "2026-09-24",
             "counts": {"calendar_missing": 1},
             "details": [{"symbol": "US.AAPL", "sub_reason": "actual_missing"}]},
        ]
        (data_dir / "extended_hours_earnings_gate_rejects.jsonl").write_text(
            json.dumps(rejects[0]) + "\n"
        )
        # 空 executions
        (data_dir / "extended_hours_entry_executions.jsonl").write_text("")
        (data_dir / "extended_hours_price_gate_rejects.jsonl").write_text("")

        result = plan_mod.reconcile("2026-09-24", "premarket")
        assert result["verdict"] == "PASS"
        assert result["missed"] == []
        assert result["coverage_pct"] == 100.0

    def test_reconcile_fail_with_missed(self, plan_mod, tmp_path, monkeypatch):
        """计划有漏票 → FAIL + missed 检出."""
        monkeypatch.setattr(plan_mod, "PLAN_DIR", tmp_path)
        monkeypatch.setattr(plan_mod, "ROOT", tmp_path)

        # 计划只含 AAPL (漏掉 MSFT)
        plan_rows = [
            {"symbol": "AAPL", "et_date": "2026-09-24", "session": "premarket",
             "phase": "before", "state": "planned", "sources": [],
             "data_expectation": {}, "gate_snapshot": {},
             "state_since": "t0", "attempts": 0, "reject_reason": None,
             "generated_at": "t0", "refreshed_at": "t0",
             "source_versions": {}, "plan_hash": "h1",
             "actual_arrived_at": None, "actual_value": None},
        ]
        plan_mod.save_plan("2026-09-24", "premarket", plan_rows)

        # 台账含 AAPL + MSFT (MSFT 不在计划中)
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        rejects_line = json.dumps({
            "ts": "2026-09-24T08:05:00Z", "session": "premarket",
            "trade_date": "2026-09-24",
            "counts": {"calendar_missing": 2},
            "details": [
                {"symbol": "US.AAPL", "sub_reason": "actual_missing"},
                {"symbol": "US.MSFT", "sub_reason": "calendar_missing"},
            ],
        })
        (data_dir / "extended_hours_earnings_gate_rejects.jsonl").write_text(
            rejects_line + "\n"
        )
        (data_dir / "extended_hours_entry_executions.jsonl").write_text("")
        (data_dir / "extended_hours_price_gate_rejects.jsonl").write_text("")

        result = plan_mod.reconcile("2026-09-24", "premarket")
        assert result["verdict"] == "FAIL"
        assert "MSFT" in result["missed"]

    def test_reconcile_arrival_stats(self, plan_mod, tmp_path, monkeypatch):
        """actual_arrived_at 统计 (p50/p90/max/n)."""
        monkeypatch.setattr(plan_mod, "PLAN_DIR", tmp_path)
        monkeypatch.setattr(plan_mod, "ROOT", tmp_path)

        plan_rows = [
            {"symbol": f"S{i}", "et_date": "2026-09-24", "session": "premarket",
             "phase": "before", "state": "planned", "sources": [],
             "data_expectation": {}, "gate_snapshot": {},
             "state_since": "t0", "attempts": 0, "reject_reason": None,
             "generated_at": "t0", "refreshed_at": "t0",
             "source_versions": {}, "plan_hash": f"h{i}",
             "actual_arrived_at": f"2026-09-24T08:{i:02d}:00" if i < 3 else None,
             "actual_value": 1.0 if i < 3 else None}
            for i in range(4)
        ]
        plan_mod.save_plan("2026-09-24", "premarket", plan_rows)

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "extended_hours_earnings_gate_rejects.jsonl").write_text("")
        (data_dir / "extended_hours_entry_executions.jsonl").write_text("")
        (data_dir / "extended_hours_price_gate_rejects.jsonl").write_text("")

        result = plan_mod.reconcile("2026-09-24", "premarket")
        assert result["actual_arrival_stats"]["n"] == 3
        assert result["actual_arrival_stats"]["max"] is not None

    def test_reconcile_empty_plan_empty_ledger(self, plan_mod, tmp_path, monkeypatch):
        """空计划 + 空台账 → PASS (无事件票 = 无漏票)."""
        monkeypatch.setattr(plan_mod, "PLAN_DIR", tmp_path)
        monkeypatch.setattr(plan_mod, "ROOT", tmp_path)

        plan_mod.save_plan("2026-09-24", "premarket", [])

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "extended_hours_earnings_gate_rejects.jsonl").write_text("")
        (data_dir / "extended_hours_entry_executions.jsonl").write_text("")
        (data_dir / "extended_hours_price_gate_rejects.jsonl").write_text("")

        result = plan_mod.reconcile("2026-09-24", "premarket")
        assert result["verdict"] == "PASS"
        assert result["coverage_pct"] == 100.0

    def test_reconcile_ledger_appended(self, plan_mod, tmp_path, monkeypatch):
        """对账后 reconcile_ledger.jsonl 追加一行."""
        monkeypatch.setattr(plan_mod, "PLAN_DIR", tmp_path)
        monkeypatch.setattr(plan_mod, "ROOT", tmp_path)

        plan_mod.save_plan("2026-09-24", "premarket", [])

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "extended_hours_earnings_gate_rejects.jsonl").write_text("")
        (data_dir / "extended_hours_entry_executions.jsonl").write_text("")
        (data_dir / "extended_hours_price_gate_rejects.jsonl").write_text("")

        plan_mod.reconcile("2026-09-24", "premarket")

        ledger = tmp_path / "reconcile_ledger.jsonl"
        assert ledger.exists()
        lines = [l for l in ledger.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["verdict"] == "PASS"
        assert entry["et_date"] == "2026-09-24"

    def test_reconcile_execution_ticket_counted(self, plan_mod, tmp_path, monkeypatch):
        """executions.jsonl 中的票也算事件票."""
        monkeypatch.setattr(plan_mod, "PLAN_DIR", tmp_path)
        monkeypatch.setattr(plan_mod, "ROOT", tmp_path)

        plan_rows = [
            {"symbol": "NOK", "et_date": "2026-09-24", "session": "premarket",
             "phase": "before", "state": "ordered", "sources": [],
             "data_expectation": {}, "gate_snapshot": {},
             "state_since": "t0", "attempts": 1, "reject_reason": None,
             "generated_at": "t0", "refreshed_at": "t0",
             "source_versions": {}, "plan_hash": "h1",
             "actual_arrived_at": "2026-09-24T08:05:00", "actual_value": 0.5},
        ]
        plan_mod.save_plan("2026-09-24", "premarket", plan_rows)

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "extended_hours_earnings_gate_rejects.jsonl").write_text("")
        (data_dir / "extended_hours_price_gate_rejects.jsonl").write_text("")
        exec_line = json.dumps({
            "symbol": "US.NOK", "qty": 1, "price": 9.0,
            "session": "premarket", "ok": True,
            "ts": "2026-09-24T08:06:00Z",
        })
        (data_dir / "extended_hours_entry_executions.jsonl").write_text(exec_line + "\n")

        result = plan_mod.reconcile("2026-09-24", "premarket")
        assert "NOK" in result["event_tickets_seen"]
        assert result["verdict"] == "PASS"
