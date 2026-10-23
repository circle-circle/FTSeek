# -*- coding: utf-8 -*-
"""RFC-112 (2026-09-26): factor_analyzer 分母污染修复 — 单元级验证。

病根: compute_trade_stats 把从未成交的非结果行 (outcome=missed/already_held/skipped,
pnl=0) 计入 win_rate 分母, 且 bayesian_binomial(wins, total) 内部 losses=total-wins
⇒ **未成交被当成亏损**。实测 900 指令中 363 (40%) 是 missed, news_positive 单因子
484 笔里 190 笔非结果行 → WR 34.9% (真实 57.5%, 压低 22.6pp), verdict=negative 错判。
连带 5 个因子被错判 negative。reconcile 检查 G 早已度量到该口径问题但未修。

修法口径与仓内既有两处一致:
  - app/metrics/unified_trade_metrics.py:116  win_rate = wins/(wins+losses)
  - app/playbook/horizon_return_model.py:64   显式排除 missed/void/skipped 伪结果
"""
import sys

import pytest

sys.path.insert(0, "/opt/ftseek/FTSeek-US")

from app.playbook.factor_analyzer import (  # noqa: E402
    _DECIDED_OUTCOMES,
    TradeStats,
    bayesian_binomial,
    classify_factor,
    compute_trade_stats,
)


def _inst(outcome, pnl=0.0, conds=None):
    return {"outcome": outcome, "pnl": pnl, "conditions_json": conds or []}


class TestNonOutcomeExcludedFromDenominator:
    def test_missed_not_counted_as_loss(self):
        """核心 bug: 未成交 (missed, pnl=0) 曾进分母, 贝叶斯把它当亏损。"""
        trades = [_inst("win", 100.0), _inst("loss", -50.0), _inst("missed", 0.0)]
        s = compute_trade_stats(trades)
        assert s.wins == 1 and s.losses == 1
        assert s.n_decided == 2
        assert s.nonoutcome == 1
        assert s.total == 3                       # 信号出现次数仍含它 (门槛语义不变)
        assert s.win_rate == pytest.approx(0.5)   # 旧口径会是 1/3=33.3%

    def test_already_held_excluded(self):
        s = compute_trade_stats([_inst("already_held", 0.0), _inst("win", 80.0)])
        assert s.win_rate == 1.0
        assert s.nonoutcome == 1

    def test_all_nonoutcome_yields_zero_not_nan(self):
        """无非结果行时 win_rate 必须 0.0 (不得 ZeroDivision)。"""
        s = compute_trade_stats([_inst("missed", 0.0), _inst("missed", 0.0)])
        assert s.n_decided == 0
        assert s.win_rate == 0.0
        assert s.avg_pnl == 0.0

    def test_bayesian_denominator_matches_decided(self):
        """贝叶斯分母必须用 n_decided, 否则 losses=total-wins 把未成交当亏损。"""
        trades = [_inst("win", 10.0)] * 5 + [_inst("missed", 0.0)] * 10
        s = compute_trade_stats(trades)
        b_clean = bayesian_binomial(s.wins, s.n_decided)
        b_polluted = bayesian_binomial(s.wins, s.total)
        assert b_clean.mean > b_polluted.mean
        assert b_clean.lower_95 > b_polluted.lower_95, "污染分母压低置信下界"

    def test_pnl_nonzero_with_other_outcome_still_counted(self):
        """口径修复不得丢真实盈亏: void 但 pnl≠0 必须进 wins/losses。

        实测 outcome=void 19 笔中 9 笔 pnl≠0 (4正5负)、10 笔 pnl=0。
        """
        s = compute_trade_stats([_inst("void", 120.0), _inst("void", -30.0), _inst("void", 0.0)])
        assert s.wins == 1 and s.losses == 1 and s.nonoutcome == 1
        assert s.total_pnl == pytest.approx(90.0)   # 真实盈亏全额保留

    def test_decided_outcomes_constant(self):
        assert _DECIDED_OUTCOMES == {"win", "loss"}, "与 horizon_return_model 口径须同源"


class TestAvgPnlDenominator:
    def test_avg_pnl_uses_decided(self):
        """均笔盈亏同样不得被非结果行稀释 (旧版 /total)。"""
        trades = [_inst("win", 100.0), _inst("missed", 0.0), _inst("missed", 0.0)]
        s = compute_trade_stats(trades)
        assert s.avg_pnl == pytest.approx(100.0)    # 旧口径会是 100/3=33.3
        assert s.total_pnl == pytest.approx(100.0)

    def test_pnls_only_decided(self):
        s = compute_trade_stats([_inst("win", 5.0), _inst("missed", 0.0)])
        assert s.pnls == [5.0]


class TestRealDataVerdictFlip:
    """真实 DB 回归: 修复前后 verdict 对账 (干跑, 不写库)。"""

    @pytest.fixture(scope="class")
    def rows(self):
        """冻结在 RFC-112 评估窗 (2026-06-28→2026-09-26), 不随生产新增交易漂移。

        旧写法 days_back=90 是滚动窗: 2026-10 新数据进来后 pe_below_industry 可合法
        转为 positive, 本测试会把"数据演化"误报成"口径修复洗白因子"。这里冻结样本窗
        以复现 RFC-112 的裁决, 不改判据/阈值, 也不要求未来数据永远保持 negative。
        """
        from app.playbook.db import PlaybookDB
        raw = PlaybookDB().get_instructions_with_outcome(days_back=365)
        start, end = "2026-06-28", "2026-09-26"
        r = [x for x in raw if start <= str(x.get("date") or "")[:10] <= end]
        if not r:
            pytest.skip("RFC-112 冻结窗无 outcome 数据")
        return r

    def _group(self, rows):
        import collections
        import json

        def pc(v):
            if not v:
                return []
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except Exception:
                    return []
            return v if isinstance(v, list) else []

        fac = collections.defaultdict(list)
        for r in rows:
            for cc in pc(r.get("conditions_json")):
                fac[cc].append(r)
        return fac

    def test_polluted_factors_no_longer_negative(self, rows):
        """reconcile 检查 G 命中的 5 个因子: 新口径下不得再是 negative。

        G 项 (data/factor_weight_reconcile_latest.json, 2026-09-26) 逐字列出:
        news_positive 34.9%→57.5% / earnings_beat_streak 36.5%→60.8% /
        above_ma20 37.2%→60.4% / insider_net_buy 35.5%→57.5% /
        pe_below_industry 35.7%→54.6%
        """
        fac = self._group(rows)
        g_factors = ["news_positive", "earnings_beat_streak", "above_ma20",
                     "insider_net_buy", "pe_below_industry"]
        flipped = []
        for k in g_factors:
            if len(fac.get(k, [])) < 20:
                continue
            s = compute_trade_stats(fac[k])
            b = bayesian_binomial(s.wins, s.n_decided)
            v = classify_factor(b, s)
            if v != "negative":
                flipped.append((k, v, s.win_rate))
            assert s.win_rate >= 0.50, f"{k} 真实 WR 应≥50% (旧口径压低), 实测 {s.win_rate:.3f}"
            assert s.nonoutcome > 0, f"{k} 应存在非结果行"
        assert len(flipped) >= 4, f"至少 4/5 应脱离 negative, 实测 {flipped}"

    def test_genuinely_negative_factors_stay_negative(self, rows):
        """反向断言: 修复不是"洗白所有 negative"。

        below_ma20 (真实 WR 18.2%) 等真负因子必须仍为 negative。
        """
        fac = self._group(rows)
        stayed = []
        for k in ("below_ma20", "pe_above_industry", "insider_net_sell"):
            rs = fac.get(k, [])
            if len(rs) < 15:
                continue
            s = compute_trade_stats(rs)
            if s.n_decided < 5:
                continue
            b = bayesian_binomial(s.wins, s.n_decided)
            if classify_factor(b, s) == "negative":
                stayed.append(k)
        assert stayed, f"真负因子应仍为 negative, 实测无一保留"

    def test_no_factor_becomes_positive(self, rows):
        """保守性: 修口径后无一因子跃升 positive (贝叶斯下界须>55%且pf>1.2)。"""
        fac = self._group(rows)
        for k, rs in fac.items():
            if len(rs) < 20:
                continue
            s = compute_trade_stats(rs)
            b = bayesian_binomial(s.wins, s.n_decided)
            assert classify_factor(b, s) != "positive", f"{k} 不应因口径修复跃升 positive"
