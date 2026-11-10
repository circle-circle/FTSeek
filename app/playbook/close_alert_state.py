"""平仓失败告警的进程内去重/宽限状态 (RFC-195 自有载体)。

为什么单独成模块: `app/playbook/pipeline.py` 处在受控拆分路线 + 行数棘轮
(tests/test_pipeline_size_ratchet.py, 只降不升), 新增逻辑必须进新模块。
本模块只承载状态与清理函数, 判定语义仍全在 pipeline._alert_close_failure 里。

病灶 (2026-11-09 实测 US.GOOG):
  `FIRST_SEEN` 的键是 symbol:date:reason 且**从不清除** ⇒ 同一标的当天第二笔平仓命中
  瞬态 pending 时, age 从「当天第一次 pending」起算:
    06:23:12 首次 (上一笔平仓, 09:23:42 已成功收口) → 09:35:45 报 age=11553s
    ⇒ 判定「真卡单」ERROR + HIGH 推送; 而本次 intent 09:35:40 才建立、09:36:06 即 BOOKED。
  `ALERTED` 同样按 symbol:date:reason 去重 ⇒ 误报一次后, 同标的当天真卡单不再告警
  (把真缺口掩埋成「那条常报的」)。
"""
import logging

logger = logging.getLogger(__name__)

# 瞬态 pending 首次见到时刻: {symbol:date:reason -> epoch}
FIRST_SEEN = {}
# 当日已告警键 (防刷屏): {"symbol:date:reason"}
ALERTED = set()


def clear_symbol(symbol: str) -> int:
    """平仓成功后清该标的的两族键 (reason 未知 ⇒ 按 symbol 前缀匹配)。

    返回清掉的键数 (供测试/日志断言); 异常不外抛。
    """
    try:
        prefix = f"{symbol}:"
        dropped = 0
        for key in [k for k in list(FIRST_SEEN) if k.startswith(prefix)]:
            FIRST_SEEN.pop(key, None)
            dropped += 1
        for key in [k for k in list(ALERTED) if k.startswith(prefix)]:
            ALERTED.discard(key)
            dropped += 1
        return dropped
    except Exception as e:
        logger.warning("[playbook] 平仓成功清告警态失败 (%s): %s", symbol, e, exc_info=True)
        return 0