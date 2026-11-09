"""[RFC-195] 平仓成功后清「瞬态 pending 计时器 + 当日告警去重键」。

病灶 (2026-11-09 实测 US.GOOG):
  `_close_fail_first_seen` 键 = symbol:date:reason 且从不清除 ⇒ 同一标的当天第二笔平仓
  命中 pending 时从「当天第一次 pending」起算 age:
    06:23:12 首次 (上一笔平仓, 已成功) → 09:35:45 报 age=11553s ⇒ 误判「真卡单」
    → ERROR + HIGH 推送, 而本次 intent 09:35:40 才建、09:36:06 即 BOOKED。
  同键族的 `_close_fail_alerted` 也按 symbol:date:reason 去重 ⇒ 误报一次后, 同标的当天
  真卡单不再告警 (掩盖真缺口)。
"""
import logging
import time

import app.playbook.pipeline as plp


def _reset():
    plp._close_fail_first_seen.clear()
    plp._close_fail_alerted.clear()


def test_clear_drops_symbol_keys_only():
    _reset()
    try:
        plp._close_fail_first_seen["US.GOOG:2026-11-09:EXIT_PENDING"] = time.time()
        plp._close_fail_first_seen["US.APP:2026-11-09:EXIT_PENDING"] = time.time()
        plp._close_fail_alerted.add("US.GOOG:2026-11-09:EXIT_HELD")
        plp._close_fail_alerted.add("US.APP:2026-11-09:EXIT_HELD")

        dropped = plp._clear_close_fail_state("US.GOOG")

        assert dropped == 2
        assert not [k for k in plp._close_fail_first_seen if k.startswith("US.GOOG:")]
        assert not [k for k in plp._close_fail_alerted if k.startswith("US.GOOG:")]
        # 别的标的必须原样保留 (清理不得扩散)
        assert [k for k in plp._close_fail_first_seen if k.startswith("US.APP:")]
        assert [k for k in plp._close_fail_alerted if k.startswith("US.APP:")]
    finally:
        _reset()


def test_stale_clock_after_success_does_not_false_alarm(caplog):
    """核心场景: 上一笔平仓遗留的 3.2h 前首见时刻不得让本笔 pending 直接判真卡单。"""
    _reset()
    try:
        plp._close_fail_first_seen["US.GOOG:2026-11-09:EXIT_PENDING"] = time.time() - 11553
        plp._close_fail_alerted.add("US.GOOG:2026-11-09:EXIT_HELD")

        # 上一笔平仓成功收口
        plp._clear_close_fail_state("US.GOOG")

        with caplog.at_level(logging.INFO):
            plp._alert_close_failure(
                "US.GOOG", "sim", "EXIT_PENDING", "detail", error="exit_order_pending"
            )

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, f"清态后首轮 pending 不得升级为 ERROR: {[r.getMessage() for r in errors]}"
        assert any("playbook平仓待回报" in r.getMessage() for r in caplog.records)
    finally:
        _reset()


def test_terminal_failure_still_alerts_immediately(caplog):
    """反向 (不变语义): error 不以 _pending 结尾 = 终态失败 ⇒ 立即 ERROR, 零延迟。"""
    _reset()
    try:
        with caplog.at_level(logging.INFO):
            plp._alert_close_failure(
                "US.GOOG", "sim", "CLOSE_FAILED", "detail", error="exit_terminal_shortfall"
            )
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "终态失败必须立刻可见"
    finally:
        _reset()