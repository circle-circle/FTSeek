# -*- coding: utf-8 -*-
"""RFC-162 (2026-10-23): 延长时段台账「行级 vs 持仓级」双轨读数契约钉.

锁住的语义:
  X1 源 jsonl 是**逐分钟**采样行, 同一持仓可出几十到上百行 ⇒ 行级 n 不是决策数
  X2 持仓级 n = distinct(源, 交易日, 标的) 去重后的数; 两口径可符号相反
     (实测: B21 行级 350/均值 +0.600% vs 持仓级 29/−2.28%; 单笔 US.ACN 占 49 行)
  X3 positive_share (判据③轴) 必须在两个口径都落盘 — 旧版只有 n 与均值,
     每次裁决都得临时重算, 且均值会被少数大赢家拉正
  X4 判据 action 集合 (EXEC_B21 / EXEC_B22) 语义不变: B22 比 B21 多 pre_close_flatten
     (逐字照判据文, 不做"顺手统一")

本批为**只印不判**: 不改阈值/判决字段, 因此这里只钉读数, 不钉 verdict。
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
_spec = importlib.util.spec_from_file_location(
    "seh", REPO / "scripts" / "settle_extended_hours.py")
seh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seh)


def _row(symbol, date, action, ret, source="premarket_exit"):
    return {"_source": source, "source": source, "symbol": symbol,
            "trade_date": date, "action": action,
            "settle": {"t0_close_ret_pct": ret, "mfe_pct": ret, "capture_pct": 0.0,
                       "holdout": {"d0_close_ret_pct": ret},
                       "flip": {"initial_loss": False, "flipped": False}}}


class TestPositionCaliber:
    """X1-X3: 同一持仓的分钟行只算一笔, 且两口径都落正占比."""

    def test_one_position_many_rows_collapses(self):
        rows = [_row("US.ACN", "2026-10-02", "SL_TRAIL_LOCK", 5.0) for _ in range(49)]
        rows += [_row("US.MU", "2026-10-02", "SL_TRAIL_LOCK", 0.2)]
        row_led = seh._aggregate(rows)
        pos_led = seh._aggregate(rows, by_position=True)
        key = "premarket_exit:SL_TRAIL_LOCK"
        assert row_led[key]["n"] == 50          # 行级: 逐分钟 50 行
        assert pos_led[key]["n"] == 2           # 持仓级: 只有 2 笔决策
        # 行级均值被单笔大仓拉起 ((49×5.0 + 0.2)/50 = 4.904%)
        assert row_led[key]["avg_t0_close_ret"] == 4.904
        # 持仓级两笔各算一次 = 2.6%
        assert pos_led[key]["avg_t0_close_ret"] == 2.6

    def test_sign_can_flip_between_calibers(self):
        """单笔大仓的重复行能把符号翻正 — 这正是 RFC-162 要并列披露的理由."""
        rows = [_row("US.ACN", "2026-10-02", "SL_TRAIL_LOCK", 5.0) for _ in range(49)]
        rows += [_row("US.X%d" % i, "2026-10-0%d" % (i % 9), "SL_TRAIL_LOCK", -3.0)
                 for i in range(20)]
        row_led = seh._aggregate(rows)["premarket_exit:SL_TRAIL_LOCK"]
        pos_led = seh._aggregate(rows, by_position=True)["premarket_exit:SL_TRAIL_LOCK"]
        assert row_led["avg_t0_close_ret"] > 0 > pos_led["avg_t0_close_ret"]

    def test_positive_share_landed_on_both_calibers(self):
        """X3: 判据③ (≥60% 为正) 的轴必须能直接读, 不必重算."""
        rows = [_row("US.A", "2026-10-02", "SL_BREAKEVEN", 1.0),
                _row("US.B", "2026-10-02", "SL_BREAKEVEN", -1.0),
                _row("US.C", "2026-10-02", "SL_BREAKEVEN", 0.0),
                _row("US.D", "2026-10-02", "SL_BREAKEVEN", 2.0)]
        for by_pos in (False, True):
            entry = seh._aggregate(rows, by_position=by_pos)["premarket_exit:SL_BREAKEVEN"]
            assert entry["positive_share"] == 0.5   # 2/4 (0.0 不算正)

    def test_zero_is_not_positive(self):
        rows = [_row("US.A", "2026-10-02", "tighten", 0.0)]
        entry = seh._aggregate(rows)["premarket_exit:tighten"]
        assert entry["positive_share"] == 0.0


class TestPosN:
    """X2: 判据 n 轴的持仓级计数 (B21/B22 各一组 action 集合)."""

    def test_pos_n_dedups_same_position(self):
        rows = [_row("US.ACN", "2026-10-02", "tighten", 0.0) for _ in range(30)]
        rows += [_row("US.ACN", "2026-10-02", "SL_TRAIL_LOCK", 1.0)]   # 同日同标的另一 action
        rows += [_row("US.MU", "2026-10-03", "SL_BREAKEVEN", 1.0)]
        assert seh._pos_n(rows, seh.EXEC_B21) == 2   # ACN(1 仓) + MU(1 仓), action 不重复计人

    def test_pos_n_accepts_both_source_keys(self):
        r1 = _row("US.A", "2026-10-02", "tighten", 0.0)
        r2 = dict(_row("US.B", "2026-10-02", "tighten", 0.0))
        r2["_source"] = None                      # 结算报告路径用 source 键
        assert seh._pos_n([r1, r2], seh.EXEC_B21) == 2

    def test_non_exec_action_not_counted(self):
        rows = [_row("US.A", "2026-10-02", "hold_no_stop", 0.0)]
        assert seh._pos_n(rows, seh.EXEC_B21) == 0


class TestActionSetSemantics:
    """X4: 判据集合逐字不变 (改集合 = 改判据 = 重裁)."""

    def test_b22_superset_differs_by_pre_close_flatten_only(self):
        assert seh.EXEC_B22 - seh.EXEC_B21 == {"pre_close_flatten"}
        assert seh.EXEC_B21 - seh.EXEC_B22 == set()

    def test_exec_set_members(self):
        assert seh.EXEC_B21 == {"SL_BREAKEVEN", "SL_TRAIL_LOCK", "TAKE_PROFIT",
                                "lock_in", "tighten", "breakeven_lock"}
        assert "hold_no_stop" not in seh.EXEC_B22