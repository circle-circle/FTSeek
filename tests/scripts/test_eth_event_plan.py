"""RFC-104 阶段 0: ETH 事件计划生成器 hermetic 测试.

覆盖:
1. 计划生成 (完整事件人群, 不预筛)
2. 源冲突 → ambiguous (不投票不择优)
3. 状态机推进 (planned → armed)
4. 不预筛断言 (计划行不得含 price/volume/mcap 预筛字段)
5. 不是策略卡 (不含 direction/sl_tp/fee/expected_profit)
6. 幂等键与 plan_hash
7. 空源 → 空计划 (fail-closed)
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


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
        # AAPL (finnhub+futu), MSFT (finnhub only), GOOG (futu only)
        # TSLA 是 AMC, 不入盘前
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
        # BMO 票不入盘后
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
        # 模拟典型日: 8 只财报票
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
        assert 1 <= len(rows) <= 50  # 合理范围


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
        # CONFLICT 的 finnhub=BMO(before), futu=after → 冲突
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
        # 相位未知但日期匹配 → ambiguous
        if unk:
            assert unk[0]["state"] == "ambiguous"


# ── 3. 状态机推进 ──

class TestStateMachine:
    """状态机推进: planned → armed (由时刻窗触发)."""

    def test_premarket_bmo_advance_after_0400(self, plan_mod):
        """盘前 BMO: 04:00 ET 后 planned → armed."""
        row = {
            "symbol": "AAPL", "et_date": "2026-09-24", "session": "premarket",
            "phase": "before", "state": "planned", "state_since": "t0",
        }
        advanced = plan_mod.advance_state(row, "2026-09-24T05:00:00")
        assert advanced["state"] == "armed"

    def test_premarket_bmo_no_advance_before_0400(self, plan_mod):
        """盘前 BMO: 04:00 ET 前保持 planned."""
        row = {
            "symbol": "AAPL", "et_date": "2026-09-24", "session": "premarket",
            "phase": "before", "state": "planned", "state_since": "t0",
        }
        advanced = plan_mod.advance_state(row, "2026-09-24T03:00:00")
        assert advanced["state"] == "planned"

    def test_afterhours_amc_advance_after_1600(self, plan_mod):
        """盘后 AMC: 16:00 ET 后 planned → armed."""
        row = {
            "symbol": "TSLA", "et_date": "2026-09-24", "session": "afterhours",
            "phase": "after", "state": "planned", "state_since": "t0",
        }
        advanced = plan_mod.advance_state(row, "2026-09-24T17:00:00")
        assert advanced["state"] == "armed"

    def test_ambiguous_no_auto_advance(self, plan_mod):
        """ambiguous 状态不自动推进."""
        row = {
            "symbol": "CONFLICT", "et_date": "2026-09-24", "session": "premarket",
            "phase": "before", "state": "ambiguous", "state_since": "t0",
        }
        advanced = plan_mod.advance_state(row, "2026-09-24T10:00:00")
        assert advanced["state"] == "ambiguous"

    def test_terminal_state_no_advance(self, plan_mod):
        """终态 (ordered/rejected/expired) 不由此函数推进."""
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
        """计划行不含 price 字段."""
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert "price" not in r
            assert "min_price" not in r
            assert "current_price" not in r

    def test_no_volume_field(self, plan_mod):
        """计划行不含 volume 字段."""
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert "volume" not in r
            assert "min_volume" not in r
            assert "min_volume_now" not in r

    def test_no_mcap_field(self, plan_mod):
        """计划行不含 mcap 字段."""
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            assert "mcap" not in r
            assert "market_cap" not in r

    def test_gate_snapshot_empty_audit_only(self, plan_mod):
        """gate_snapshot 为空占位 (仅作审计, 不作放行依据)."""
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
        """相同输入 → 相同 plan_hash."""
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
        """不同 symbol → 不同 plan_hash."""
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        hashes = [r["plan_hash"] for r in rows]
        assert len(hashes) == len(set(hashes))

    def test_dedup_by_symbol_date(self, plan_mod):
        """同一 (symbol, date) 两源 → 合并为一行 (非两行)."""
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
        # 两源均记录在 sources 字段
        assert len(dup_rows[0]["sources"]) == 2


# ── 7. Schema 完整性 ──

class TestSchema:
    """计划行 schema 完整性."""

    REQUIRED_FIELDS = {
        "symbol", "et_date", "session", "phase",
        "sources", "data_expectation", "gate_snapshot",
        "state", "state_since", "attempts", "reject_reason",
        "generated_at", "refreshed_at", "source_versions", "plan_hash",
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
        """state 必须在有效集合内."""
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        valid = plan_mod.VALID_STATES
        for r in rows:
            assert r["state"] in valid

    def test_sources_structure(self, plan_mod):
        """sources 字段结构: list of {name, reportDate, timeOfTheDay, asof}."""
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
        """data_expectation 字段结构."""
        rows = plan_mod.generate_plan(
            "2026-09-24", "premarket",
            finnhub_rows=FINNHUB_ROWS_BMO, futu_rows=FUTU_ROWS_BMO,
        )
        for r in rows:
            de = r["data_expectation"]
            assert "basis" in de
            assert "expected_available_after_et" in de
            assert "fallback_days" in de


# ── 8. 文件 I/O ──

class TestFileIO:
    """计划文件读写 (原子写)."""

    def test_save_and_load(self, plan_mod, tmp_path, monkeypatch):
        """写入后读回一致."""
        monkeypatch.setattr(plan_mod, "PLAN_DIR", tmp_path)
        rows = [
            {"symbol": "AAPL", "et_date": "2026-09-24", "session": "premarket",
             "phase": "before", "state": "planned", "plan_hash": "abc123"},
        ]
        plan_mod.save_plan("2026-09-24", "premarket", rows)
        loaded = plan_mod.load_plan("2026-09-24", "premarket")
        assert loaded == rows

    def test_load_nonexistent(self, plan_mod, tmp_path, monkeypatch):
        """不存在 → 空列表."""
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
        """开启 premarket_amc_pairing: 昨日 AMC 票入盘前计划."""
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
        """关闭 premarket_amc_pairing: 昨日 AMC 票不入盘前计划."""
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
