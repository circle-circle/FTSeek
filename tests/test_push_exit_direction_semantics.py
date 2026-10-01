# -*- coding: utf-8 -*-
"""[hotfix 2026-10-02] 平仓推送方向盲 + capture 钳位契约测试 (INTC 10/1 案).

病灶 (用户 10/1 核查确认):
  ① 触发线百分比 `(线-入场)/入场` 无方向分流 — SHORT 锁利线必然在入场价下方 →
     恒显负号: INTC 锁住 +0.32% 毛利的线被推成 "(-0.3%)", 用户读成亏损线。
  ② capture 构造侧 max(0.0,·) 钳位 + 渲染侧 :.0f — 负捕获 (吐回超过峰值,
     INTC 真值 -8.7%) 恒显 "capture 0%"。
修复: message.py `_eff_line_gap` 盈亏语义 (SHORT=入场-线, LONG 不变);
      pipeline._px_extra 钳位放开 + 渲染 :.1f。
零交易行为变更 (纯显示/payload 数值口径)。
"""
from app.push.message import trade_exit, _eff_line_gap


def _intc_like(**over):
    """INTC id=1599 真实数值 (2026-10-01 SHORT 锁利线净亏案)."""
    kw = dict(symbol="US.INTC", direction="SHORT", pnl=-7.01,
              exit_reason="SL_TRAIL_LOCK", strategy="playbook_sim",
              trace_id="1599", source_type="PLAYBOOK_SIM",
              fees=4.4, pnl_net=-11.41)
    kw.update(over)
    return kw


class TestEffLineDirectionSemantics:
    def test_short_lock_line_shows_positive_lock(self):
        """SHORT: 线 118.6245 < 入场 119.0 = 锁住毛利 → 必须显示正号 '锁利'。"""
        msg = trade_exit(extra={"entry_price": 119.0, "exit_price": 119.143,
                                "effective_sl": 118.6245}, **_intc_like())
        for rendered in (msg.render_text(), msg.render_markdown()):
            assert "触发线: $118.62" in rendered or "触发线**: $118.62" in rendered
            assert "锁利 +0.3%" in rendered
            assert "(-0.3%)" not in rendered   # 旧方向盲口径不得回归

    def test_short_stop_line_shows_negative_stop(self):
        """SHORT: 线在入场价上方 = 止损语义 → 负号 '止损'。"""
        msg = trade_exit(extra={"entry_price": 119.0, "effective_sl": 124.95},
                         **_intc_like())
        for rendered in (msg.render_text(), msg.render_markdown()):
            assert "止损 -5.0%" in rendered

    def test_long_lock_line_unchanged_word_for_word(self):
        """LONG 回归红线: 旧口径对 LONG 本就正确 (id=1531 INTC LONG 真实数值)."""
        msg = trade_exit(extra={"entry_price": 105.895, "exit_price": 108.23,
                                "effective_sl": 108.505}, **_intc_like(direction="LONG"))
        for rendered in (msg.render_text(), msg.render_markdown()):
            assert "锁利 +2.5%" in rendered    # (108.505-105.895)/105.895=+2.46%→+2.5%

    def test_long_stop_line_negative(self):
        msg = trade_exit(extra={"entry_price": 100.0, "effective_sl": 95.0},
                         **_intc_like(direction="LONG"))
        assert "止损 -5.0%" in msg.render_text()

    def test_missing_direction_falls_back_to_long_caliber(self):
        """direction 缺失 → 不翻转 (向后兼容旧 payload)."""
        assert _eff_line_gap(95.0, 100.0, "") == -5.0
        assert _eff_line_gap(95.0, 100.0, None) == -5.0

    def test_helper_edge_cases(self):
        assert _eff_line_gap(0, 100.0, "SHORT") is None     # 线缺失
        assert _eff_line_gap(95.0, 0, "SHORT") is None      # 入场缺失
        assert _eff_line_gap(95.0, 100.0, "SELL_SHORT") == 5.0
        assert _eff_line_gap(95.0, 100.0, "SELL") == 5.0
        assert _eff_line_gap("bad", 100.0, "SHORT") is None  # 脏值不炸


class TestCaptureNegativeVisible:
    def test_negative_capture_rendered(self):
        """INTC 真值: pnl_pct=-0.0012, mfe=1.3782 → capture=-8.7% 必须可见 (不是 0%)."""
        msg = trade_exit(extra={"entry_price": 119.0, "exit_price": 119.143,
                                "mfe_pct": 1.3782, "capture_pct": -8.7},
                         **_intc_like())
        for rendered in (msg.render_text(), msg.render_markdown()):
            assert "capture -8.7%" in rendered

    def test_positive_capture_one_decimal(self):
        msg = trade_exit(extra={"mfe_pct": 1.2875, "capture_pct": 58.0},
                         **_intc_like(direction="LONG"))
        assert "capture 58.0%" in msg.render_text()


class TestPipelineCaptureClampRemoved:
    """源级契约钉: pipeline 构造侧钳位放开 (max(0.0,·) 不得回归) + L1 book_src 接线."""

    def _src(self):
        from pathlib import Path
        return (Path(__file__).resolve().parents[1] / "app" / "playbook"
                / "pipeline.py").read_text(encoding="utf-8")

    def test_capture_clamp_removed(self):
        s = self._src()
        assert '_px_extra["capture_pct"] = round(' in s
        assert "max(0.0, min(1.0, float(pnl_pct or 0)" not in s, (
            "capture 负值钳位不得回归 (INTC 10/1 案: -8.7% 被钳成 0)")

    def test_be_telemetry_reads_real_l1_book(self):
        """L1 尺子修: bid/ask 优先读 ORDER_BOOK push 真盘口 + book_src 如实标注."""
        s = self._src()
        assert "get_push_orderbook(symbol)" in s, "必须消费已有的真 L1 push 缓存"
        assert '"book_src": _be_book_src' in s, "遥测必须标注盘口来源"
        assert '"l1_push"' in s and '"proxy_quote"' in s

    def test_side_judgment_not_changed(self):
        """回归红线: 侧判定/限价计算仍基于 current_price, 本次只修测量不修行为."""
        s = self._src()
        idx = s.find('if exit_reason in ("SL_BREAKEVEN", "SL_TRAIL_LOCK", "PATIENT_EXIT_FILL"):')
        assert idx > 0
        seg = s[idx:idx + 4600]
        assert '_be_side = "passive" if _close_limit_price > current_price else "active"' in seg
        assert '_be_side = "passive" if _close_limit_price < current_price else "active"' in seg
