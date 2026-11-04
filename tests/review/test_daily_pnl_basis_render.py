"""日报"毛/费/净"三列口径 + 跨夜分账上屏 (2026-11-04)

背景 (实见缺陷): 2026-11-03 日报推送原文
    总盈亏：毛 $-188.58 | 费 $42.92 | 净 $-188.58
毛-费≠净, 自相矛盾。根因: 2026-08-01 DEB-20260730 D1 把 daily_review 的
summary['total_pnl'] 从毛利改成净额, 但共享格式化器 _format_daily_review 仍按"毛"渲染;
而周报 summary['total_pnl'] 至今是 sum(realized_pnl) 毛利 ⇒ 同一字段名两种语义。

修法: 日报显式产出 total_pnl_gross (realized_pnl 求和) 供渲染; total_pnl 语义不动。
旧 JSON 无 gross → 只打"净|费", 不用净额顶替毛额 (不编造)。

同时: overnight_risk 一直只写 Obsidian 笔记, 推送看不到跨夜分账 → 补一行。
"""
import json
from pathlib import Path

import pytest

from app.core.scheduler_triggers import _format_daily_review
from app.review.daily_review import DailyReviewGenerator

REPO = Path(__file__).resolve().parents[1]
REAL_JSON = REPO / "data/daily_reviews/daily_review_2026-11-03.json"


def _closed(symbol, gross, fees, net, **kw):
    return {"symbol": symbol, "realized_pnl": gross, "fees_est": fees, "pnl_net": net,
            "exit_time": "2026-11-03T15:00:00-05:00", "trade_date": "2026-11-03",
            "pnl_pct": 0.001, "mfe_pct": 0.5, "holding_hours": 1.0, **kw}


def _summary_from(trades):
    gen = DailyReviewGenerator.__new__(DailyReviewGenerator)   # 不触发 __init__ 的 IO
    return gen._generate_summary(trades, trades, None)


class TestDailySummaryGrossBasis:
    def test_gross_and_net_both_present_and_consistent(self):
        trades = [_closed("AAA", 100.0, 5.0, 95.0), _closed("BBB", -50.0, 5.0, -55.0)]
        s = _summary_from(trades)
        assert s["total_pnl_gross"] == pytest.approx(50.0)
        assert s["total_pnl_net"] == pytest.approx(40.0)
        assert s["fees_total"] == pytest.approx(10.0)
        # 恒等式: 毛 - 费 == 净
        assert s["total_pnl_gross"] - s["fees_total"] == pytest.approx(s["total_pnl_net"])

    def test_total_pnl_semantics_unchanged(self):
        """total_pnl 仍是净口径 (DEB-20260730 D1 契约), 不得被本次改动改成毛。"""
        trades = [_closed("AAA", 100.0, 5.0, 95.0)]
        s = _summary_from(trades)
        assert s["total_pnl"] == s["total_pnl_net"] == pytest.approx(95.0)
        assert "net:" in s["pnl_basis"]

    def test_empty_day_still_renders_three_columns(self):
        s = _summary_from([])
        assert s["total_pnl_gross"] == 0 and s["total_pnl_net"] == 0 and s["fees_total"] == 0


class TestDailyRenderGrossLabel:
    def test_gross_label_uses_gross_not_net(self):
        report = {"report_date": "2026-11-03",
                  "summary": {"total_trades": 2, "closed_trades": 2, "winning_trades": 1,
                              "total_pnl": 40.0, "total_pnl_net": 40.0, "total_pnl_gross": 50.0,
                              "fees_total": 10.0, "win_rate": 0.5, "win_rate_net": 0.5,
                              "avg_pnl": 20.0}}
        text = _format_daily_review(report=report)
        assert "毛 $+50.00" in text and "费 $10.00" in text and "净 $+40.00" in text
        # 不能再出现"毛 == 净"而费不为 0 的自相矛盾行
        assert "毛 $+40.00" not in text

    def test_old_json_without_gross_does_not_fake_gross(self):
        """旧 JSON: 无 total_pnl_gross, total_pnl 是净额 ⇒ 不得打"毛"标签。"""
        report = {"report_date": "2026-10-01",
                  "summary": {"total_trades": 1, "closed_trades": 1, "winning_trades": 0,
                              "total_pnl": -20.0, "total_pnl_net": -20.0,
                              "fees_total": 5.0, "win_rate": 0.0, "win_rate_net": 0.0,
                              "avg_pnl": -20.0}}
        text = _format_daily_review(report=report)
        assert "毛 $" not in text
        assert "净 $-20.00" in text and "旧 JSON 无毛额" in text


class TestOvernightSplitOnScreen:
    def _report(self, ov):
        return {"report_date": "2026-11-03",
                "summary": {"total_trades": 10, "closed_trades": 10, "winning_trades": 8,
                            "total_pnl": -188.58, "total_pnl_net": -188.58,
                            "total_pnl_gross": -145.66, "fees_total": 42.92,
                            "win_rate": 0.8, "win_rate_net": 0.8, "avg_pnl": -18.86},
                "overnight_risk": ov}

    def test_overnight_line_rendered(self):
        text = _format_daily_review(report=self._report({
            "legacy_exit_count": 2, "legacy_exit_net_pnl": -238.24,
            "overnight_gap_stop_count": 1, "overnight_gap_stop_net_pnl": -264.35,
            "worst_legacy_symbol": "US.MRK", "worst_legacy_net_pnl": -264.35}))
        assert "跨夜 2 笔：净 $-238.24" in text
        assert "OVERNIGHT_GAP_STOP 1 笔 $-264.35" in text
        assert "最差 US.MRK $-264.35" in text

    def test_no_overnight_line_when_zero(self):
        text = _format_daily_review(report=self._report({
            "legacy_exit_count": 0, "legacy_exit_net_pnl": 0}))
        assert "跨夜" not in text


@pytest.mark.skipif(not REAL_JSON.exists(), reason="2026-11-03 真实日报 JSON 不在仓库 (data/ 被 gitignore)")
class TestReal20261103Regression:
    """用昨日真实 JSON 反渲染: 三个数必须自洽 (毛-费=净) 且跨夜行出现。"""

    def test_real_file_self_consistent(self):
        data = json.loads(REAL_JSON.read_text(encoding="utf-8"))
        s = data.get("summary") or {}
        if s.get("total_pnl_gross") is None:
            pytest.skip("真实 JSON 生成于修复前, 无 total_pnl_gross (历史文件)")
        assert s["total_pnl_gross"] - s["fees_total"] == pytest.approx(s["total_pnl_net"], abs=0.02)
        text = _format_daily_review(report=data)
        assert f"毛 ${s['total_pnl_gross']:+.2f}" in text
        assert "跨夜" in text