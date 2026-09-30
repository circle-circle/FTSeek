"""
入场区间 (entry band) 单一真源.
执行端 `_check_intraday_trigger` 与 DA 直写指令 (`deep-stock-analysis` skill) 共用本模块,
避免 band 百分比在两处重复硬编码而漂移 —— 否则会引发触发逻辑与归因记录不一致 (逻辑/记录冲突).

方向性 band 语义 (与 `_check_intraday_trigger` 判定完全一致):
  LONG : [entry*(1-KNIFE), entry*(1+CHASE)]  → 回调不接飞刀(下限) / 追涨设上限
  SHORT: [entry*(1-CHASE), entry*(1+KNIFE)]  → 追空设下限 / 反弹不追空过高(上限)

当前生产实现为**扁平 band** (与执行端历史常量 3%/5% 一致).

波动率化 — 只放宽变体 vol_widen (RFC-065, DEB-20260819 §二十八~§三十四):
  knife = max(3%, min(6%, 0.9*ATR%)), chase = max(5%, min(8%, 1.5*ATR%))
  只放宽不收窄 (对称缩放被回放否决: 收窄低波票拒掉10条中6条是赢家);
  chase 上限 8% = extreme_gap 过滤线 (pipeline L4466, >8%直接拦, 9%是死参数);
  中位ATR(3.36%)处与扁平带基本重合; ATR缺失回落扁平 (fail-safe).
  证据: 真基线(线上三门, 193指令) flat -12.4 → widen -2.9, lost=0 严格超集;
  局限: 高ATR池本身为负(止血非变正), 新增组n=6且MU占67%, 路径PnL非落袋口径.
  生产启用受 entry_band_vol_widen_enabled 开关控制 (默认 false, 影子期攒样后裁).
"""
from typing import Optional, Tuple

# ── 单一真源: 方向性 band 半宽 ──
CHASE_ZONE_PCT = 0.05   # 顺势加速方向最大追入幅度 (追涨/追空上限)
KNIFE_FLOOR_PCT = 0.03  # 逆势偏离上限 (接飞刀保护, 略宽于典型 2-2.5% SL)


def vol_widen_band_pct(atr_pct: Optional[float]) -> Tuple[float, float]:
    """RFC-065 vol_widen 带半宽 (比例口径): 返回 (knife, chase).

    只放宽不收窄: 下限=扁平常量, 上限按 ATR 放宽.
    ATR 缺失/非法 → 回落扁平 (fail-safe, 零行为变更).
    """
    try:
        a = float(atr_pct or 0) * 100.0
    except (TypeError, ValueError):
        a = 0.0
    if a <= 0:
        return KNIFE_FLOOR_PCT, CHASE_ZONE_PCT
    knife = max(KNIFE_FLOOR_PCT, min(0.06, 0.009 * a))
    chase = max(CHASE_ZONE_PCT, min(0.08, 0.015 * a))
    return knife, chase


def compute_entry_band(
    entry_price,
    is_short: bool,
    volatility_pct: Optional[float] = None,  # RFC-065: vol_widen 入参 (ATR比例)
) -> Tuple[Optional[float], Optional[float]]:
    """计算有效入场区间 (entry_low, entry_high); 无效价返回 (None, None).

    Args:
        entry_price: 计划入场价
        is_short: True=做空方向, False=做多方向
        volatility_pct: ATR 比例 (0.034=3.4%); 传入时按 vol_widen 放宽带,
            不传/None → 扁平带 (与历史行为完全一致, 零行为变更).
    """
    try:
        e = float(entry_price or 0)
    except (TypeError, ValueError):
        return None, None
    if e <= 0:
        return None, None

    if volatility_pct is not None:
        knife, chase = vol_widen_band_pct(volatility_pct)
    else:
        knife, chase = KNIFE_FLOOR_PCT, CHASE_ZONE_PCT

    if is_short:
        low = e * (1 - chase)
        high = e * (1 + knife)
    else:
        low = e * (1 - knife)
        high = e * (1 + chase)
    return round(low, 4), round(high, 4)


def normalize_chase_cap(direction: str, entry_price, cap) -> Tuple[object, bool]:
    """[RFC-120 2026-09-30] chase_cap 语义倒挂归一化 — DA skill 写入侧与执行层
    chase 门共用的 SSOT (双侧必须同源, 防归一口径漂移)。

    规格语义 (entry_routing.chase_cap_violated): BUY cap=上限(价>cap拦, 必须>入场);
    SHORT cap=下限(价<cap拦, 必须<入场)。病灶: LLM 常把 SHORT 的 cap 填成
    invalidation 的上破失效位 (上界语义) → 下单价天然低于它 → 出生即违例 =
    结构性死锁 (9/4 enforce 上线后 born-violated 空头 18/18 零执行, 对照合规组
    33/24; 9/30 实证 BE cap=302/entry=298, KMX 63/60.5, TSLA 358/355, TSLA
    单指令空转 37+ 轮)。倒挂值零信息增量 — 上破失效位已由 stop_loss/invalidation
    承担 → 确定性退回入场带顺势沿 (与 UE 约定 LONG=entry_high/SHORT=entry_low
    同口径的扁平带): BUY→entry×(1+CHASE), SHORT→entry×(1-CHASE)。

    Returns:
        (effective_cap, normalized)。cap/entry 缺失或畸形 → 原样返回不判
        (与无 cap 指令现状行为一致, 零行为变更); 已合规 → 原样返回。纯函数。
    """
    try:
        c = float(cap) if cap not in (None, "", "null") else None
    except (TypeError, ValueError):
        c = None
    if c is None or c <= 0:
        return cap, False
    try:
        e = float(entry_price) if entry_price not in (None, "", "null") else None
    except (TypeError, ValueError):
        e = None
    if e is None or e <= 0:
        return cap, False
    d = str(direction or "").upper()
    if d in ("BUY", "LONG") and c <= e:
        return round(e * (1 + CHASE_ZONE_PCT), 4), True
    if d in ("SHORT", "SELL_SHORT") and c >= e:
        return round(e * (1 - CHASE_ZONE_PCT), 4), True
    return cap, False
