# -*- coding: utf-8 -*-
"""[观察点1 2026-09-29] 平仓通知跨夜标注渲染测试。

病灶: held_h 是 RTH-only 口径 (trade_records.holding_hours), 隔夜单墙钟差远大于它
(MU 9/29 案: 通知显示"持有 6.1h"实际跨 19h 墙钟), 用户易误读为当日日内单。
修复: payload 增 overnights (ET 日历日差, pipeline.py 平仓推送构造处计算),
渲染端 (飞书文本 _render_trade_text + markdown _render_trade_markdown) 在
"(持有 X.Xh" 后追加 ", 跨夜N晚)"。overnights=0/缺失 → 外观与旧版逐字一致。
"""
from app.push.message import trade_exit


_MU_EXTRA = {   # MU id=1579 真实数值 (2026-09-29 盘前锁盈案)
    "entry_price": 1061.325,
    "exit_price": 1069.28,
    "held_h": 6.1,
    "overnights": 1,
    "mfe_pct": 1.2875,
    "mae_pct": 2.7631,
    "capture_pct": 58.0,
}


class TestOvernightAnnotation:
    def test_text_renderer_shows_overnight_for_mu_case(self):
        msg = trade_exit("US.MU", "LONG", 39.77, "SL_TRAIL_LOCK",
                         strategy="playbook_sim", pnl_net=35.64, fees=4.13,
                         extra=dict(_MU_EXTRA))
        t = msg.render_text()
        assert "(持有 6.1h, 跨夜1晚)" in t
        assert "入场 $1061.33" in t and "出场 $1069.28" in t

    def test_markdown_renderer_shows_overnight_for_mu_case(self):
        msg = trade_exit("US.MU", "LONG", 39.77, "SL_TRAIL_LOCK",
                         strategy="playbook_sim", pnl_net=35.64, fees=4.13,
                         extra=dict(_MU_EXTRA))
        m = msg.render_markdown()
        assert "(持有 6.1h, 跨夜1晚)" in m

    def test_same_day_trade_appearance_unchanged(self):
        """overnights=0 → 与旧版逐字一致 '(持有 2.5h)', 无跨夜字样。"""
        extra = {"entry_price": 100.0, "exit_price": 101.0, "held_h": 2.5,
                 "overnights": 0}
        msg = trade_exit("US.X", "LONG", 1.0, "TP", extra=extra)
        t = msg.render_text()
        assert "(持有 2.5h)" in t
        assert "跨夜" not in t

    def test_legacy_payload_without_overnights_unchanged(self):
        """旧 payload (无 overnights 键) → 不加标注, 向后兼容。"""
        extra = {"entry_price": 100.0, "exit_price": 101.0, "held_h": 3.0}
        msg = trade_exit("US.X", "LONG", 1.0, "TP", extra=extra)
        assert "(持有 3.0h)" in msg.render_text()
        assert "跨夜" not in msg.render_text()

    def test_multi_night(self):
        extra = {"entry_price": 100.0, "exit_price": 101.0, "held_h": 7.0,
                 "overnights": 3}
        msg = trade_exit("US.X", "LONG", 1.0, "TP", extra=extra)
        assert "跨夜3晚" in msg.render_text()
        assert "跨夜3晚" in msg.render_markdown()

    def test_no_held_h_still_safe(self):
        """held_h 缺失 → 无持有段, overnights 也不渲染 (不出现孤立'跨夜')。"""
        extra = {"entry_price": 100.0, "exit_price": 101.0, "overnights": 1}
        msg = trade_exit("US.X", "LONG", 1.0, "TP", extra=extra)
        t = msg.render_text()
        assert "持有" not in t.split("入场")[1] if "入场" in t else True
        assert "跨夜" not in t


class TestPipelineOvernightCalc:
    """pipeline.py 平仓推送构造处的 ET 日历日差计算 (源级契约 + 纯函数复算)。"""

    def test_et_calendar_day_diff_semantics(self):
        """MU 真实时间戳: entry 9/28 09:56 ET, exit 9/29 04:55 ET → 跨夜=1。
        注意 04:55 是盘前 (04:00-09:30), 按 ET 日历日差=1 晚, 语义正确。"""
        from app.utils.timezone import parse_iso_safe, to_et
        e = to_et(parse_iso_safe("2026-09-28T09:56:06.597092-04:00"))
        x = to_et(parse_iso_safe("2026-09-29T04:55:48.253169-04:00"))
        assert (x.date() - e.date()).days == 1

    def test_same_rth_day_zero(self):
        from app.utils.timezone import parse_iso_safe, to_et
        e = to_et(parse_iso_safe("2026-09-28T09:56:06-04:00"))
        x = to_et(parse_iso_safe("2026-09-28T15:30:00-04:00"))
        assert (x.date() - e.date()).days == 0

    def test_winter_time_crossing(self):
        """冬令时 (-05:00) 同样按 ET 日历日判定, 不因 UTC 偏移错位。"""
        from app.utils.timezone import parse_iso_safe, to_et
        e = to_et(parse_iso_safe("2026-11-16T15:50:00-05:00"))   # ET 15:50
        x = to_et(parse_iso_safe("2026-11-17T05:10:00-05:00"))   # ET 次日盘前
        assert (x.date() - e.date()).days == 1

    def test_pipeline_wires_overnights_into_payload(self):
        """源级契约钉: pipeline 平仓推送构造处必须把 overnights 写进 _px_extra。"""
        import inspect
        from app.playbook import pipeline as pl
        src = inspect.getsource(pl)
        assert '"overnights": _overnights_et' in src
        assert "parse_iso_safe" in src   # 用真实 API (曾因臆测 parse_time 差点接线死)
