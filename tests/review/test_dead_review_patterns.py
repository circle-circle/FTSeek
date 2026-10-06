"""复盘层死判据/假数据契约钉 (2026-10-06).

钉住三类已实证的病灶, 防回归:

  A. 化石列判据 (final_strength/consensus_level 恒 0, 3模型投票 2026-07-12 退役)
     - `(0 or 50) < 50` 与 `(0.0 or 1) < 0.5` 恒 False ⇒ severity=high 的守卫静默死亡
     - 危害不是「不触发」, 是值班人以为有 high 级守卫在守
  B. 字面量与生产值域脱钩 (`exit_reason != "STOP_LOSS"`, 全历史 41 个值中无此字面量)
     ⇒ `!=` 恒 True, 判据退化为纯金额比较, 与其名「止损延迟」语义不符
  C. or-50 fallback 造假数据
     - avg_signal_strength 恒 50.00 (不是测量值)
     - std=0 ⇒ consistency 恒 **1.00 满分** + interpretation 'high' (比假 50 更误导)

处置范式沿用本仓已裁决的 daily_review._analyze_voting_consistency (2026-09-04 M4):
上游退役 ⇒ 明确标不可用, 不用 fallback 造出看似合理的数字。
"""
import sqlite3

import pytest

from app.review.error_pattern_detector import ErrorPatternDetector
from app.review.weekly_review import WeeklyReviewGenerator

# 化石列样本: 生产现行期真实形态 (final_strength=0, consensus_level=0.0)
FOSSIL_ROWS = [
    {
        "id": i, "trade_date": "2026-09-2%d" % (i % 9 + 1), "symbol": "US.TEST%d" % i,
        "final_strength": 0, "consensus_level": 0.0, "voting_result": None,
        "signal_confidence": 0.42, "realized_pnl": -20.0 - i, "pnl_net": -24.0 - i,
        "holding_hours": 1.5, "max_drawdown": 0.5, "exit_reason": "SL_INITIAL",
        "strategy_id": "playbook_sim",
    }
    for i in range(40)
]


class TestFossilColumnPatternsRemoved:
    """A. 化石列判据必须已删除, 不得以恒 False 的尸体形式留在表里."""

    @pytest.mark.parametrize("pid", [
        "low_signal_entry",          # (final_strength or 50) < 50 恒 False
        "high_disagreement_entry",   # (consensus_level or 1) < 0.5 恒 False, sev=high
    ])
    def test_fossil_pattern_removed(self, pid):
        assert pid not in ErrorPatternDetector.ERROR_PATTERNS, (
            f"{pid} 依赖化石列 (3模型投票 2026-07-12 退役), 判据恒 False = 静默死亡的守卫; "
            "若恢复了投票管线, 须按当时真实值域重写, 不得直接复活")

    def test_no_pattern_reads_fossil_columns(self):
        """全表扫描: 任何判据都不得再读化石列 (防复活)."""
        import inspect
        fossil = ("final_strength", "consensus_level", "voting_result")
        for pid, pdef in ErrorPatternDetector.ERROR_PATTERNS.items():
            src = inspect.getsource(pdef["condition"])
            for col in fossil:
                assert col not in src, (
                    f"判据 {pid} 仍在读化石列 {col} (恒 0 ⇒ 恒 False 或 fallback 假数)")

    def test_fossil_columns_are_really_zero_in_production(self):
        """前提实证: 生产库里这两列确实恒 0 (否则删除判据就是误删)."""
        con = sqlite3.connect("/opt/ftseek/FTSeek-US/data/ftseek_us.db")
        row = con.execute(
            "SELECT COUNT(*), SUM(CASE WHEN final_strength>0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN consensus_level>0 THEN 1 ELSE 0 END) "
            "FROM trade_records WHERE trade_date>='2026-08-15'").fetchone()
        con.close()
        total, nz_fs, nz_cl = row
        assert total and total > 100, f"样本不足 ({total}), 前提未实证"
        assert (nz_fs or 0) == 0, f"final_strength 有 {nz_fs} 行非零 ⇒ 化石列前提已变, 重评"
        assert (nz_cl or 0) == 0, f"consensus_level 有 {nz_cl} 行非零 ⇒ 化石列前提已变, 重评"


class TestStopLossLiteralDecoupled:
    """B. 字面量与生产值域脱钩的判据必须已删 (而非用白名单修补)."""

    def test_late_stop_loss_removed(self):
        assert "late_stop_loss" not in ErrorPatternDetector.ERROR_PATTERNS, (
            "late_stop_loss 的判别式 exit_reason != 'STOP_LOSS' 在生产值域下恒 True "
            "(全历史 41 个 distinct exit_reason 无该字面量) ⇒ 从未检测过「止损延迟」, "
            "只是 large_loss 的低阈值重复; 白名单修补会随值域增长再次静默过期")

    def test_stop_loss_literal_absent_from_production_domain(self):
        """前提实证: 生产 exit_reason 值域确实没有 'STOP_LOSS' 字面量."""
        con = sqlite3.connect("/opt/ftseek/FTSeek-US/data/ftseek_us.db")
        vals = [r[0] for r in con.execute(
            "SELECT DISTINCT exit_reason FROM trade_records "
            "WHERE exit_reason IS NOT NULL AND exit_reason != ''")]
        con.close()
        assert len(vals) > 20, f"值域样本异常小 ({len(vals)}), 前提未实证"
        assert "STOP_LOSS" not in vals, (
            f"'STOP_LOSS' 已出现在生产值域 ⇒ 删除 late_stop_loss 的前提变了, 需重评")

    def test_large_loss_kept_as_disaster_backstop(self):
        """large_loss 当前休眠但保留: 值域脱钩≠判据写错, 仓位放大即复活."""
        assert "large_loss" in ErrorPatternDetector.ERROR_PATTERNS
        assert ErrorPatternDetector.ERROR_PATTERNS["large_loss"]["severity"] == "critical"


class TestNoFakeNumbersFromFallback:
    """C. or-50 fallback 不得再造出假数据 (假 50.00 / 假满分 1.00)."""

    def test_analyze_voting_marks_retired_not_fake_50(self):
        wr = WeeklyReviewGenerator()
        v = wr._analyze_voting(FOSSIL_ROWS)
        assert v["status"] == "retired"
        assert v["data_available"] is False
        assert v["avg_signal_strength"] is None, (
            "avg_signal_strength 不得是 50.00 — 那是 or-50 fallback 常量, 不是测量值")
        assert v["medium_strength_trades"] == 0, (
            "medium 桶不得吞掉全部样本 (原判据 335/335 全落 medium)")
        assert "note" in v and "退役" in v["note"], "必须留痕说明为何不可用"

    def test_consistency_marks_unavailable_not_fake_perfect(self):
        wr = WeeklyReviewGenerator()
        c = wr._calculate_consistency(FOSSIL_ROWS)
        assert c["score"] is None, (
            "consistency score 不得是 1.00 — 化石列全 0 ⇒ std=0 ⇒ 1-0/30=假满分, "
            "它声称「交易风格非常稳定」而实际零信息")
        assert "note" in c

    def test_weekly_formatter_skips_unavailable_consistency(self):
        """沿用本仓既有约定 (scheduler_triggers:1338): score is None 或有 note ⇒ 不渲染."""
        wr = WeeklyReviewGenerator()
        c = wr._calculate_consistency(FOSSIL_ROWS)
        renders = c.get("score") is not None and "note" not in c
        assert renders is False, "周报不得渲染不可用的一致性行"

    def test_insufficient_trades_no_longer_reports_perfect(self):
        """原判据 <2 笔时返回 score=1.0 (假满分); 应为不可用."""
        wr = WeeklyReviewGenerator()
        c = wr._calculate_consistency(FOSSIL_ROWS[:1])
        assert c["score"] is None


class TestLivePathStillWorks:
    """反向: 修化石列不得把活路径也弄死 (否则是过度删改)."""

    def test_analyze_voting_computes_when_strength_is_real(self):
        wr = WeeklyReviewGenerator()
        live = [dict(r, final_strength=80) for r in FOSSIL_ROWS[:20]] + \
               [dict(r, final_strength=30) for r in FOSSIL_ROWS[20:]]
        v = wr._analyze_voting(live)
        assert v["status"] == "ok" and v["data_available"] is True
        assert v["high_strength_trades"] == 20
        assert v["low_strength_trades"] == 20
        assert v["avg_signal_strength"] == 55.0

    def test_consistency_computes_when_strength_is_real(self):
        wr = WeeklyReviewGenerator()
        live = [dict(r, final_strength=80) for r in FOSSIL_ROWS[:20]] + \
               [dict(r, final_strength=30) for r in FOSSIL_ROWS[20:]]
        c = wr._calculate_consistency(live)
        assert c["score"] is not None and 0 < c["score"] < 1
        assert c["std_strength"] == 25.0

    def test_low_confidence_entry_still_fires_on_live_field(self):
        """signal_confidence 是活字段 ⇒ 该判据必须继续有效 (对照: 化石列判据已删)."""
        fn = ErrorPatternDetector.ERROR_PATTERNS["low_confidence_entry"]["condition"]
        assert fn({"signal_confidence": 0.42}) is True
        assert fn({"signal_confidence": 0.71}) is False

    def test_early_exit_survives_null_holding_hours(self):
        """holding_hours 可为 NULL ⇒ 原判据抛 TypeError 被静默吞; 现须安全求值."""
        fn = ErrorPatternDetector.ERROR_PATTERNS["early_exit"]["condition"]
        assert fn({"holding_hours": None, "realized_pnl": 5.0}) is True
        assert fn({"holding_hours": 3.0, "realized_pnl": 5.0}) is False
        assert fn({"holding_hours": None, "realized_pnl": -5.0}) is False

    def test_detect_patterns_on_production_data_returns_live_patterns(self):
        """端到端: 用生产真数据跑, 检出的必须是活判据."""
        con = sqlite3.connect("/opt/ftseek/FTSeek-US/data/ftseek_us.db")
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM trade_records WHERE trade_date>='2026-08-15' "
            "AND exit_time IS NOT NULL AND exit_time!='' "
            "AND source_type='PLAYBOOK_SIM'")]
        con.close()
        assert len(rows) > 200, f"生产样本不足 ({len(rows)})"
        pats = ErrorPatternDetector(db_path="/opt/ftseek/FTSeek-US/data/ftseek_us.db"
                                    ).detect_patterns(trades=rows)
        ids = {p.pattern_id for p in pats}
        assert not ids & {"low_signal_entry", "high_disagreement_entry", "late_stop_loss"}
        assert "low_confidence_entry" in ids, "活判据应在生产数据上命中 (实测 60/333)"
