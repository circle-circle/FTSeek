"""复盘链净口径契约钉 (2026-10-09, hotfix 净口径补漏).

背景: 全仓在 7d9a800fa (用户裁"切") 已把日报/周报/OKR 改净口径
COALESCE(pnl_net, realized_pnl), 但复盘链漏改, 辩论触发/lesson 提取仍用毛口径。
实锤案例: trade_records id=1631 US.PEP — 毛 +$1.13 / 费 $4.33 / 净 -$3.20,
盘前入场, DEAD_SIGNAL_EXIT, 旧代码按毛口径误触发"盘前盈利待辩论"推送。
污染面: 全仓 553 笔毛正中 78 笔 (14.1%) 实为净亏。

本文件钉住:
  1. 毛正净负 + 盘前入场 ⇒ 不触发辩论 (核心守卫)
  2. 毛正净正 + 盘前入场 ⇒ 仍触发辩论 (放行安全方向, 双向审计)
  3. pnl_net=NULL 旧行 ⇒ 回退毛额 (COALESCE 语义, 历史行行为不变)
  4. net_pnl=0 是合法值 ⇒ 不得被 `or` 当缺失回退成毛额
  5. lesson 提取同口径: 毛正净负不产 lesson / 金额用净额
"""
import pytest

from app.review.trade_analyzer import TradeAnalyzer, TradeVerdict
from app.review.experience_accumulator import ExperienceAccumulator

# 生产实锤形态 (id=1631 US.PEP 2026-10-08): 盘前 ET 07:30 入场, 毛正净负
PEP_GROSS_POS_NET_NEG = {
    "id": 1631, "symbol": "US.PEP", "direction": "LONG",
    "entry_price": 126.511, "exit_price": 126.54, "quantity": 39,
    "entry_time": "2026-10-08T07:30:36.282939-04:00",
    "exit_reason": "DEAD_SIGNAL_EXIT", "holding_hours": 3.0,
    "realized_pnl": 1.13, "fees_est": 4.33, "pnl_net": -3.20,
    "pnl_pct": 0.0002, "signal_strength": 0, "signal_confidence": 0,
}


def _premarket(gross: float, net, **over):
    t = dict(PEP_GROSS_POS_NET_NEG)
    t.update({"realized_pnl": gross, "pnl_net": net})
    t.update(over)
    return t


@pytest.fixture
def analyzer():
    # _analyze_single_trade 不触 DB, db_path 用占位即可
    return TradeAnalyzer(db_path=":memory:")


class TestDebateTriggerNetBasis:
    def test_gross_pos_net_neg_no_debate(self, analyzer):
        """核心守卫: PEP 形态 (毛+$1.13/净-$3.20) 不得触发辩论"""
        a = analyzer._analyze_single_trade(_premarket(1.13, -3.20), {})
        assert a.net_pnl == -3.20
        assert "盘前入场" in " ".join(a.issues), "前置: 盘前入场 issue 必须存在"
        assert a.needs_debate is False

    def test_gross_pos_net_pos_still_debates(self, analyzer):
        """放行方向: 真净盈利的盘前单仍触发辩论 (双向审计)"""
        a = analyzer._analyze_single_trade(_premarket(50.0, 45.0), {})
        assert a.net_pnl == 45.0
        assert a.needs_debate is True
        assert "净盈利" in a.debate_reason

    def test_null_pnl_net_falls_back_to_gross(self, analyzer):
        """旧行 pnl_net=NULL ⇒ COALESCE 语义回退毛额, 历史行为不变"""
        a = analyzer._analyze_single_trade(_premarket(30.0, None), {})
        assert a.net_pnl == 30.0
        assert a.needs_debate is True

    def test_zero_net_is_legal_not_fallback(self, analyzer):
        """净 0 是合法值: 不得被 `or` 当缺失回退成毛额正数"""
        a = analyzer._analyze_single_trade(_premarket(4.0, 0.0), {})
        assert a.net_pnl == 0.0
        assert a.needs_debate is False

    def test_observe_branch_uses_net(self, analyzer):
        """待定观察大亏分支同用净额 (毛-480/净-520 ⇒ 触发)"""
        t = _premarket(-480.0, -520.0,
                       entry_time="2026-10-08T10:30:00-04:00",  # 盘中入场, 避开盘前 issue
                       pnl_pct=-0.03,  # 小亏 → verdict=OBSERVE (不触发 unreasonable)
                       signal_strength=55, signal_confidence=0.6)
        a = analyzer._analyze_single_trade(t, {})
        assert a.verdict == TradeVerdict.OBSERVE
        assert a.needs_debate is True
        assert "净-$520.00" in a.debate_reason

    def test_to_dict_carries_net_pnl(self, analyzer):
        d = analyzer._analyze_single_trade(_premarket(1.13, -3.20), {}).to_dict()
        assert d["net_pnl"] == -3.20
        assert d["realized_pnl"] == 1.13  # 毛额保留, 供 净(毛) 对照显示


class TestPnlAttributionNetBasis:
    def test_gross_pos_net_neg_not_attributed_as_profit(self, analyzer):
        """盈亏归因: 毛正净负单不得归因为"盈利退出\""""
        a = analyzer._analyze_single_trade(_premarket(1.13, -3.20), {})
        assert "盈利" not in a.pnl_attribution or "亏损" in a.pnl_attribution
        assert "亏损退出" in a.pnl_attribution


class TestLessonExtractionNetBasis:
    @pytest.fixture
    def acc(self, tmp_path):
        return ExperienceAccumulator(db_path=str(tmp_path / "t.db"))

    def test_gross_pos_net_neg_no_premarket_profit_lesson(self, acc):
        lessons = acc._extract_from_debates([{
            "trade_id": 1631, "symbol": "US.PEP",
            "debate_reason": "盘前入场但净盈利 (+$45.00) — 需要辩论确认是策略有效还是运气",
            "realized_pnl": 1.13, "net_pnl": -3.20, "verdict": "不合理",
        }])
        pre = [l for l in lessons if l.lesson_subcategory == "premarket_profit_debate"]
        assert pre == []

    def test_net_positive_lesson_uses_net_amount(self, acc):
        lessons = acc._extract_from_debates([{
            "trade_id": 999, "symbol": "US.X",
            "debate_reason": "盘前入场但净盈利 (+$45.00) — 需要辩论确认是策略有效还是运气",
            "realized_pnl": 50.0, "net_pnl": 45.0, "verdict": "待定观察",
        }])
        pre = [l for l in lessons if l.lesson_subcategory == "premarket_profit_debate"]
        assert len(pre) == 1
        assert "$45.00" in pre[0].lesson_text
        assert pre[0].impact_score == 45.0

    def test_extract_from_trades_uses_net_pnl(self, acc):
        """规则1 (盘前亏损 lesson): 毛正净负单应按净亏产 lesson 金额.
        输入契约 = TradeAnalysis.to_dict() (键名 net_pnl, 非 DB 的 pnl_net)."""
        ta = dict(_premarket(1.13, -3.20))
        ta.update({"verdict": "不合理", "issues": ["盘前入场 — 流动性差，滑点风险高"],
                   "net_pnl": -3.20})
        lessons = acc._extract_from_trades([ta])
        pre = [l for l in lessons if l.lesson_subcategory == "premarket_loss"]
        assert len(pre) == 1
        assert pre[0].impact_score == 3.20  # 净额绝对值, 非毛额
