"""Phase 1 信号质量治理三项的回归测试 (2026-08-23, DEB-20260823-phase1).

覆盖:
  P1-1 VWAP 斜率证伪门: 逆向斜率拦截 / 顺向放行 / 数据缺失 fail-open
  P1-2 invalidation 核销: 析取-合取语义 + 时间限定词守卫 (真实 DA 文本样本)
  P1-3 量能门 p50 曲线: 覆盖外标的回落 / 曲线单调 / U 型偏差方向
  方向词表 SSOT: 指令词汇→成交词汇映射 (冷却门死门根因回归)
"""
import importlib.util
import json
from pathlib import Path

import pytest

from app.playbook.pipeline import (
    INVALIDATED_SKIP_REASONS,
    _calc_vwap_slope,
    _capital_flow_divergence,
    _intraday_vol_curve_p50,
)
from app.utils.direction_utils import is_short, to_trade_direction
from app.utils.timezone import now_et

ROOT = Path(__file__).resolve().parent.parent


def _load_tripwire_module():
    spec = importlib.util.spec_from_file_location(
        "tripwire_checker", ROOT / "scripts" / "tripwire_checker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bars(prices, vol=1000.0):
    """构造当日 1min bar (time/high/low/close/volume/turnover)."""
    today = now_et().strftime("%Y-%m-%d")
    return [{"time": f"{today} 10:{i:02d}:00", "high": p, "low": p, "close": p,
             "volume": vol, "turnover": p * vol} for i, p in enumerate(prices)]


class _Router:
    def __init__(self, bars):
        self.futu = type("F", (), {"get_kline": lambda _s, *a, **k: bars})()


class TestVwapSlopeGate:
    def test_rising_vwap_gives_positive_slope(self):
        slope = _calc_vwap_slope(_Router(_bars([100] * 5 + [101] * 5)), "US.TEST")
        assert slope is not None and slope > 0

    def test_falling_vwap_gives_negative_slope(self):
        slope = _calc_vwap_slope(_Router(_bars([101] * 5 + [100] * 5)), "US.TEST")
        assert slope is not None and slope < 0

    def test_insufficient_bars_returns_none_failopen(self):
        assert _calc_vwap_slope(_Router(_bars([100] * 6)), "US.TEST") is None

    def test_stale_bars_are_filtered_out(self):
        """非当日 bar 不得参与斜率 — 否则隔夜跳空被算成盘中动量."""
        stale = [{"time": "2026-01-01 10:00:00", "high": 1, "low": 1, "close": 1,
                  "volume": 1, "turnover": 1}] * 20
        assert _calc_vwap_slope(_Router(stale), "US.TEST") is None

    def test_missing_futu_returns_none_failopen(self):
        assert _calc_vwap_slope(object(), "US.TEST") is None


class TestTripwireInvalidation:
    """真实 DA invalidation 文本样本 — 合取腿不可验证时不得作废 (宁漏不误杀)."""

    @pytest.fixture(scope="class")
    def mod(self):
        return _load_tripwire_module()

    @pytest.fixture
    def at(self):
        base = now_et()
        return lambda h, m: base.replace(hour=h, minute=m)

    CASES = [
        # (文本, 方向, entry, current, (时,分), 期望作废)
        ("跌破$234.50或Google合约被UBS证实存在重大交付延迟/取消",
         "BUY", 240, 233, (11, 0), True),
        ("跌破$234.50或Google合约被UBS证实存在重大交付延迟/取消",
         "BUY", 240, 236, (11, 0), False),
        # 合取: "且量能>200万股" 不可解析 → 价格单腿不得作废
        ("若开盘后1小时跌破$407.88(昨日低点)且量能>200万股，或A16量产推迟公告，则多头逻辑失效",
         "BUY", 410, 400, (10, 0), False),
        # 收盘口径: 盘中不评估, 临收盘才评估
        ("正股收盘跌破$137.43(08-19低点)或天然气管道审批第三次被拒公告",
         "BUY", 140, 135, (11, 0), False),
        ("正股收盘跌破$137.43(08-19低点)或天然气管道审批第三次被拒公告",
         "BUY", 140, 135, (15, 55), True),
        # 窗口限定词: "开盘后30分钟内" 已过 → 条件不再可能成立
        ("开盘后30分钟内跌破$121.88且无反弹迹象，或买盘/卖盘比持续低于-30超过1小时",
         "BUY", 125, 120, (11, 30), False),
        ("若盘中跌破$144.9(8/19缺口完全回补)或收盘低于MA20($133.15)，则mRNA利好被证伪",
         "BUY", 150, 144, (11, 0), True),
        ("基本面恶化则离场", "BUY", 100, 50, (11, 0), False),
        ("", "BUY", 100, 50, (11, 0), False),
    ]

    @pytest.mark.parametrize("text,direction,entry,cur,hhmm,expected", CASES)
    def test_clause_semantics(self, mod, at, text, direction, entry, cur, hhmm, expected):
        got, detail = mod.evaluate_invalidation_text(
            invalidation=text, direction=direction, entry_price=entry,
            current_price=cur, now_et_dt=at(*hhmm))
        assert got is expected, detail

    def test_short_direction_uses_upside_threshold(self, mod, at):
        got, _ = mod.evaluate_invalidation_text(
            invalidation="跌破$50", direction="SHORT", entry_price=45,
            current_price=51, now_et_dt=at(11, 0))
        assert got is True

    def test_skip_reason_is_consumed_by_trigger_guards(self, mod):
        assert mod.SKIP_REASON in INVALIDATED_SKIP_REASONS


class TestIntradayVolCurve:
    def test_uncovered_symbol_falls_back_to_linear(self):
        assert _intraday_vol_curve_p50("US.NOSUCHSYMBOL") is None

    @pytest.mark.skipif(not (ROOT / "data" / "replay_bars_1m" / "US_AAPL.json").exists(),
                        reason="需 replay_bars_1m 数据基座")
    def test_curve_is_monotonic_and_front_loaded(self):
        """累积量曲线必须单调不减; 且开盘首 15min 占比显著高于线性折算的 15/390."""
        curve = _intraday_vol_curve_p50("US.AAPL")
        assert curve and len(curve) >= 300
        assert all(curve[i] <= curve[i + 1] for i in range(len(curve) - 1))
        assert curve[14] / curve[-1] > 15 / 390 * 1.5


class TestCapitalFlowShadow:
    def test_unknown_symbol_returns_none(self):
        assert _capital_flow_divergence("US.NOSUCHSYMBOL", False) is None

    @pytest.mark.skipif(not (ROOT / "data" / "capital_flow_daily.jsonl").exists(),
                        reason="需 capital_flow 数据基座")
    def test_divergence_is_direction_symmetric(self):
        """同一标的 BUY/SHORT 判定必须互补 (背离定义对称)."""
        with open(ROOT / "data" / "capital_flow_daily.jsonl", encoding="utf-8") as f:
            sym = json.loads(f.readline())["symbol"]
        buy = _capital_flow_divergence(sym, False)
        short = _capital_flow_divergence(sym, True)
        assert buy is None or buy is not short


class TestDirectionVocabularySSOT:
    """方向词表单一真源 (app/utils/direction_utils).

    根因回归: 2026-08-23 UE 冷却门用指令词汇 'BUY' 查只存 'LONG' 的 trade_records,
    全表 0 命中 → 门上线即死且静默。锁住"指令词汇必须先归一化到成交词汇"。
    """

    def test_instruction_vocab_maps_to_trade_vocab(self):
        assert to_trade_direction("BUY") == "LONG"
        assert to_trade_direction("SELL_SHORT") == "SHORT"

    def test_trade_vocab_is_idempotent(self):
        assert to_trade_direction("LONG") == "LONG"
        assert to_trade_direction("SHORT") == "SHORT"

    @pytest.mark.parametrize("bad", ["SKIP", "", None, "NEUTRAL", "xyz"])
    def test_unrecognized_returns_none_not_silent_default(self, bad):
        """不可识别必须 None — 返回默认值会把未知方向静默当多头 (死门失效模式)."""
        assert to_trade_direction(bad) is None
        assert is_short(bad) is None

    def test_trade_records_only_stores_trade_vocab(self):
        """DB 实弹: trade_records.direction 只有 LONG/SHORT, 指令词汇直查恒 0 命中."""
        import sqlite3
        from app.core.db_utils import get_main_db
        conn = sqlite3.connect(str(get_main_db()))
        try:
            stored = {r[0] for r in conn.execute(
                "SELECT DISTINCT direction FROM trade_records")}
            if not stored:
                pytest.skip("trade_records 为空")
            assert stored <= {"LONG", "SHORT"}, f"出现新词汇, 需同步 SSOT: {stored}"
            for instr in ("BUY", "SELL_SHORT"):
                assert instr not in stored
                assert to_trade_direction(instr) in stored
        finally:
            conn.close()
