"""入场区间 (entry band) 单一真源.

执行端 `_check_intraday_trigger` 与 DA 直写指令 (`deep-stock-analysis` skill) 共用本模块,
避免 band 百分比在两处重复硬编码而漂移 —— 否则会引发触发逻辑与归因记录不一致 (逻辑/记录冲突)。

方向性 band 语义 (与 `_check_intraday_trigger` 判定完全一致):
  LONG : [entry*(1-KNIFE), entry*(1+CHASE)]  → 回调不接飞刀(下限) / 追涨设上限
  SHORT: [entry*(1-CHASE), entry*(1+KNIFE)]  → 追空设下限 / 反弹不追空过高(上限)

当前实现为**扁平 band** (与执行端历史常量 3%/5% 一致, 零行为变更)。

待评估 (需 sim 验证, 见 plan「band 波动率化」):
  不同标的/分桶波动率不同, 扁平 band 一刀切不合理 (高波动股 ±3% 是噪音, 低波动股 ±3% 是大动作)。
  应复用 `_compute_volatility_based_sl_tp` 的波动率分桶口径缩放 band 宽度, 使 band 与 SL/TP 同源。
  届时 `compute_entry_band` 增加 volatility/bucket 入参, 执行端与 DA 仍共用本函数 → 保持一致。
"""
from typing import Optional, Tuple

# ── 单一真源: 方向性 band 半宽 ──
CHASE_ZONE_PCT = 0.05   # 顺势加速方向最大追入幅度 (追涨/追空上限)
KNIFE_FLOOR_PCT = 0.03  # 逆势偏离上限 (接飞刀保护, 略宽于典型 2-2.5% SL)


def compute_entry_band(
    entry_price,
    is_short: bool,
    volatility_pct: Optional[float] = None,  # 预留: 波动率化入口 (当前忽略, 保持扁平)
) -> Tuple[Optional[float], Optional[float]]:
    """计算有效入场区间 (entry_low, entry_high); 无效价返回 (None, None).

    Args:
        entry_price: 计划入场价
        is_short: True=做空方向, False=做多方向
        volatility_pct: 预留波动率入参 (当前未启用 —— 波动率化需 sim 验证后开启)
    """
    try:
        e = float(entry_price or 0)
    except (TypeError, ValueError):
        return None, None
    if e <= 0:
        return None, None

    knife, chase = KNIFE_FLOOR_PCT, CHASE_ZONE_PCT
    # 波动率化 (待启用): 若传入 volatility_pct, 未来在此按分桶缩放 knife/chase.
    # 当前保持扁平 → 与执行端常量一致, 零行为变更.

    if is_short:
        low = e * (1 - chase)
        high = e * (1 + knife)
    else:
        low = e * (1 - knife)
        high = e * (1 + chase)
    return round(low, 4), round(high, 4)
