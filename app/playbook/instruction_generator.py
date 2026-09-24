"""
Playbook Instruction Generator - 反幻觉架构

核心原则: LLM 只输出决策 (方向 + 条件选择 + 模板)，Python 计算所有数值。

数据流:
  Screener (triggered_factors) → LLM 决策 → Python 数值计算 → 最终指令

LLM 输出 (最小化):
  {symbol, direction, selected_conditions, thesis_template}

Python 计算 (确定性):
  entry_price = data.technical.price
  stop_loss = entry_price - 1.5 × ATR
  take_profit = entry_price + R:R × (entry_price - stop_loss)
  position_size = risk_amount / (entry_price - stop_loss)
"""
import json
import logging
import math
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

from app.playbook.data_collector import PlaybookData
from app.playbook.market_regime import MarketRegime, RegimeContext
from app.playbook.condition_taxonomy import (
    CONDITION_TAXONOMY,
    validate_conditions,
    get_taxonomy_for_prompt,
)
from app.playbook.config import get_playbook_config, PlaybookConfig

logger = logging.getLogger(__name__)


def compute_position_size(
    risk_base: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    max_position_pct: float,
    *,
    regime_mult: float = 1.0,
    water_mult: float = 1.0,
    conf_factor: float = 1.0,
) -> int:
    """确定性仓位计算 SSOT (F1, 2026-07-19, DEB-20260719-round2 Ω.3-4).

    主路径(instruction_generator)与兜底路径(pipeline auto_execute)共用, 消除 sizing 双实现。
    公式:
        risk_amount = risk_base × (risk_pct/100) × regime_mult × water_mult × conf_factor
        size = int(risk_amount / |entry_price - stop_loss|)
        受 max_position_pct 截断: size×entry > risk_base×max_position_pct 时缩至上限

    参数全开放契约 (β焦点4): 函数体只做确定性计算, 乘数由调用方决定。
      - 主路径: regime_mult=regime_multiplier, water_mult=水位乘数, conf_factor=1.0
      - 兜底: regime_mult/water_mult 补传(F1 修复点), conf_factor=含 LONG 分歧降仓
      - LONG 分歧降仓50% / market_state_mult 等路径专属逻辑留在调用侧, 不进本函数。

    Returns:
        仓位股数 (int)。sl_distance<=0 或价格非法时返回 0; 截断时至少 1 股。
    """
    if entry_price <= 0 or stop_loss <= 0:
        return 0
    per_share_risk = abs(entry_price - stop_loss)
    if per_share_risk <= 0:
        return 0
    risk_amount = risk_base * (risk_pct / 100) * regime_mult * water_mult * conf_factor
    size = int(risk_amount / per_share_risk)
    # max_position_pct 截断 — 单仓位不超过 risk_base 的配置比例
    if max_position_pct and max_position_pct > 0:
        max_position_value = risk_base * max_position_pct
        if size * entry_price > max_position_value:
            size = max(1, int(max_position_value / entry_price))
            logger.debug(
                f"[sizing] 仓位截断至 {size}股 (上限{max_position_pct*100:.0f}%=${max_position_value:,.0f})"
            )
    return size



# ========== Thesis 模板 (禁止自由文本) ==========

THESIS_TEMPLATES = {
    "trend_pullback": "趋势回调: {conditions}, 提供低风险入场机会",
    "breakout": "突破入场: {conditions}, 动量确认",
    "value_reversal": "价值反转: {conditions}, 基本面低估+技术面企稳",
    "momentum_continuation": "动量延续: {conditions}, 趋势持续",
    "insider_confidence": "内部人信心: {conditions}, 管理层用行动投票",
    "mean_reversion": "均值回归: {conditions}, 超跌反弹机会",
    # [DEB-20260924-indexcaliber R4] 空头侧模板 — 原缺陷: 六个非 skip 模板全是多头文案,
    # SELL_SHORT 被迫落入多头叙事 (实例: 9/24 AXTI 空单 thesis="均值回归: above_ma20,
    # macd_golden_cross, 超跌反弹机会", 与 DA 真实空方依据 PE2432x/below_ma20/盘前-4.36%
    # 利好出尽 相矛盾) ⇒ 指令记录语义自相矛盾, 且污染一切按 thesis 文本做的分型分析。
    "trend_short": "趋势做空: {conditions}, 下行趋势延续, 顺势做空",
    "fade_short": "反弹做空: {conditions}, 技术面偏多但估值/事件面偏空, 反弹分批做空",
    "mean_reversion_short": "均值回归做空: {conditions}, 超买回落机会",
    "skip": "跳过: {skip_reason}",
}

# ========== LLM 输出 JSON Schema (最小化) ==========

DECISION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["symbol", "direction", "selected_conditions", "thesis_template"],
        "properties": {
            "symbol": {"type": "string", "pattern": "^US\\.[A-Z]+$"},
            "direction": {"type": "string", "enum": ["BUY", "SELL_SHORT", "SKIP"]},
            "selected_conditions": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 6,
            },
            "thesis_template": {
                "type": "string",
                "enum": list(THESIS_TEMPLATES.keys()),
            },
            "skip_reason": {"type": "string"},
        },
    },
}


@dataclass
class Instruction:
    """单条交易指令 (所有数值由 Python 确定性计算)"""
    symbol: str
    direction: str  # BUY / SELL_SHORT / SKIP
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    position_size: int = 0
    risk_amount: float = 0.0
    risk_reward: float = 0.0
    confidence: int = 0
    thesis: str = ""
    conditions: List[str] = field(default_factory=list)
    data_quality: Dict[str, str] = field(default_factory=dict)
    risks: List[str] = field(default_factory=list)
    sector: str = ""
    skip_reason: str = ""
    # 数据来源追溯
    entry_price_source: str = ""  # e.g. "technical.price (Futu LV3, 2026-06-18T09:30:00)"
    atr_source: str = ""
    decision_id: str = ""  # 关联 VotingCoordinator 的 decision_id (IC 归因链路)
    # Horizon 决策 (2026-06-26)
    horizon: str = "short"
    horizon_confidence: float = 0.0
    horizon_reasoning: Dict[str, Any] = field(default_factory=dict)
    # DA 决策脑富文本 (2026-07-11 Stage 1): 失效条件 + 真实论点.
    # 仅用于归因/复盘/退出触发地基, 不参与数值计算; da_thesis 不覆盖模板 thesis.
    invalidation: str = ""
    da_thesis: str = ""
    # F1 (2026-07-31, DEB-feedback-loop): DA 入场锚偏离度 (LLM entry vs 分析时快照价, 有符号 %).
    # record-only 归因字段, 量化"入场区与市价脱节"规模; bridge 缺失时为 None.
    anchor_dev_pct: Optional[float] = None
    # [DEB-20260901] 结构化入场语义 (双轨新增, record-only, 零行为变更):
    #   entry_archetype ∈ {BREAKOUT, PULLBACK_LIMIT, FADE_SHORT} | None(=现状默认)
    #   confirmation = {"price_level", "side", "ttl_min"} | None
    #   chase_cap_price = 禁追上限 | None。消费方: Phase B 影子 (pullback_limit_shadow)。
    entry_archetype: Optional[str] = None
    confirmation: Optional[Dict[str, Any]] = None
    chase_cap_price: Optional[float] = None


# ========== 数值计算引擎 (纯 Python, 确定性) ==========

def _positive_finite_atr(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _compute_volatility_based_sl_tp(
    atr_pct: float,
    direction: str,
    _horizon_params: Dict[str, Any],
    pb_cfg: PlaybookConfig,
) -> Optional[Dict[str, Any]]:
    """按波动率分桶计算止损百分比和止盈倍数 (Phase 2 2026-06-30).

    Returns:
        {"bucket": str, "sl_pct": float, "tp_multiplier": float} or None
        未配置桶或 ATR 比例无效时返回 None，由调用方区分缺数与固定参数路径。
    """
    vol_buckets = _horizon_params.get("vol_buckets") or pb_cfg.get("vol_buckets")
    atr_pct = _positive_finite_atr(atr_pct)
    if not vol_buckets or atr_pct is None:
        return None

    # 选择 bucket: 按 atr_pct_max 从小到大匹配
    sorted_buckets = sorted(
        vol_buckets.items(),
        key=lambda x: x[1].get("atr_pct_max", 999.0),
    )
    selected_bucket = None
    for name, cfg in sorted_buckets:
        if atr_pct < cfg.get("atr_pct_max", 999.0):
            selected_bucket = (name, cfg)
            break

    if selected_bucket is None:
        return None

    bucket_name, bucket = selected_bucket

    sl_multiplier = bucket.get("sl_multiplier", 0.7)
    sl_floor = bucket.get("sl_floor_pct", 1.5)
    sl_ceil = bucket.get("sl_ceil_pct", 3.0)
    sl_pct = max(sl_floor, min(sl_ceil, atr_pct * 100 * sl_multiplier))

    # SHORT 方向加宽一点, 防 squeeze
    if direction == "SELL_SHORT":
        short_widen = bucket.get("short_widen_pct", 0.3)
        sl_pct += short_widen

    tp_multiplier = bucket.get("tp_multiplier", 1.5)

    return {
        "bucket": bucket_name,
        "sl_pct": sl_pct,
        "tp_multiplier": tp_multiplier,
    }


def _vol_bucket_sl_bounds(
    _horizon_params: Dict[str, Any],
    pb_cfg: PlaybookConfig,
) -> Optional[tuple]:
    """派生该 horizon 全部桶的全局 SL 边界: (min_floor_pct, max_ceil_pct, max_short_widen_pct).

    用途 (2026-07-17): 出价前 ATR 无关的钳制不变量。ATR 缺失时 re-price fallback 用来源原始 SL
    反算 sl_pct, 可能越过任何桶的天花板 (如 DA MCK 6% / ABT 7.3% > 桶 ceil 5.0%)。用全局
    [min_floor, max_ceil(+max_short_widen if SHORT)] 钳回, 只堵越界、不改合法值 (已按桶钳过的
    值 ceil ≤ max_ceil, 是 no-op)。无 vol_buckets 配置时返回 None, 调用方跳过钳制。
    """
    vol_buckets = _horizon_params.get("vol_buckets") or pb_cfg.get("vol_buckets")
    if not vol_buckets:
        return None
    min_floor = min(b.get("sl_floor_pct", 1.5) for b in vol_buckets.values())
    max_ceil = max(b.get("sl_ceil_pct", 3.0) for b in vol_buckets.values())
    max_short_widen = max(b.get("short_widen_pct", 0.3) for b in vol_buckets.values())
    return (min_floor, max_ceil, max_short_widen)


def _apply_market_state_overlay(
    result: Dict[str, Any],
    decision: Dict[str, Any],
    data: PlaybookData,
    _horizon_params: Dict[str, Any],
    pb_cfg: PlaybookConfig,
    regime_context: "Optional[RegimeContext]",
) -> Dict[str, Any]:
    """应用市场状态叠加 (Phase 4 2026-06-30): VIX / regime / SPY 趋势.

    调整: 止损加宽, 止盈倍率, 仓位乘数, 方向过滤.
    """
    overlay_cfg = pb_cfg.get_horizon_top("market_state_overlay")
    if not overlay_cfg or not overlay_cfg.get("enabled", False):
        return result

    try:
        # 1. 确定 VIX 档位
        vix = float(getattr(data.sentiment, "vix", 20.0) or 20.0)
        vix_thresholds = overlay_cfg.get("vix_thresholds", {})
        if vix >= float(vix_thresholds.get("high", 30.0)):
            vix_level = "extreme"
        elif vix >= float(vix_thresholds.get("medium", 25.0)):
            vix_level = "high"
        elif vix >= float(vix_thresholds.get("low", 20.0)):
            vix_level = "medium"
        else:
            vix_level = "low"

        # 2. 止损加宽
        sl_widen = float(overlay_cfg.get("vix_sl_widen", {}).get(vix_level, 0.0))
        if sl_widen > 0 and result.get("stop_loss") and result.get("entry_price"):
            entry = float(result["entry_price"])
            sl = float(result["stop_loss"])
            direction = decision.get("direction", "")
            if direction == "BUY":
                result["stop_loss"] = round(sl - entry * (sl_widen / 100.0), 2)
            elif direction == "SELL_SHORT":
                result["stop_loss"] = round(sl + entry * (sl_widen / 100.0), 2)
            result["atr_source"] = f"{result.get('atr_source', '')} vix_widen={sl_widen}%"

        # 3. 止盈倍率调整
        tp_mult = float(overlay_cfg.get("vix_tp_mult", {}).get(vix_level, 1.0))
        if tp_mult != 1.0 and result.get("take_profit") and result.get("entry_price"):
            entry = float(result["entry_price"])
            tp = float(result["take_profit"])
            sl = float(result["stop_loss"])
            # 重新基于新的 sl_distance 计算 tp
            new_sl_distance = abs(entry - sl)
            old_sl_distance = abs(entry - result["take_profit"]) / max(result.get("risk_reward", 1.0), 0.01)
            if old_sl_distance > 0:
                new_tp_distance = new_sl_distance * result.get("risk_reward", 1.0) * tp_mult
                direction = decision.get("direction", "")
                if direction == "BUY":
                    result["take_profit"] = round(entry + new_tp_distance, 2)
                elif direction == "SELL_SHORT":
                    result["take_profit"] = round(entry - new_tp_distance, 2)

        # 4. 仓位乘数 (VIX + regime)
        vix_pos_mult = float(overlay_cfg.get("vix_position_mult", {}).get(vix_level, 1.0))
        regime_pos_mult = 1.0
        if regime_context is not None:
            regime_name = getattr(regime_context.regime, "name", str(regime_context.regime))
            regime_pos_mult = float(
                overlay_cfg.get("regime_position_mult", {}).get(regime_name, 1.0)
            )
            # 如果 regime 是 NO_TRADE, 直接跳过
            if regime_pos_mult <= 0.0:
                result["skip_market_state"] = True
                return result

        total_mult = vix_pos_mult * regime_pos_mult
        if total_mult != 1.0:
            result["market_state_position_mult"] = total_mult

        # 5. SPY 趋势方向过滤
        if regime_context is not None:
            spy_price = getattr(regime_context, "spy_price", 0) or 0
            spy_ma20 = getattr(regime_context, "spy_ma20", 0) or 0
            spy_ma50 = getattr(regime_context, "spy_ma50", 0) or 0
            direction = decision.get("direction", "")
            if direction == "BUY":
                if overlay_cfg.get("skip_long_if_spy_below_ma20", False) and spy_price > 0 and spy_ma20 > 0 and spy_price < spy_ma20:
                    result["skip_market_state"] = True
            elif direction == "SELL_SHORT":
                if overlay_cfg.get("skip_short_if_spy_above_ma20", False) and spy_price > 0 and spy_ma20 > 0 and spy_price > spy_ma20:
                    result["skip_market_state"] = True

    except Exception as e:
        logger.warning("[MarketStateOverlay] 应用异常: %s", e)

    return result


def compute_instruction_values(
    decision: Dict[str, Any],
    data: PlaybookData,
    total_equity: float,
    risk_pct: float = 1.0,
    regime_multiplier: float = 1.0,
    rr_ratio: float = None,
    atr_multiplier: float = None,
    horizon: str = "short",
    regime_context: "Optional[RegimeContext]" = None,
) -> Dict[str, Any]:
    """
    基于 LLM 决策 + 原始数据，确定性计算所有数值。

    LLM 不参与任何数值计算。所有公式都在这里，可测试、可追溯。

    Args:
        decision: LLM 输出 {symbol, direction, selected_conditions, thesis_template}
        data: 该标的的 PlaybookData (6 维度原始数据)
        total_equity: 总资金
        risk_pct: 单笔风险占比 (默认 1%)
        regime_multiplier: Regime 仓位乘数
        rr_ratio: 风险回报比, None 时读配置
        atr_multiplier: ATR 止损倍数, None 时读配置 (当前未使用, 保留兼容)
        horizon: 持有周期 ("short" / "long"), 影响 rr_ratio
        regime_context: 市场环境上下文 (Phase 4 2026-06-30), 用于 VIX/regime 叠加

    Returns:
        计算后的数值字段 dict
    """
    # 读取配置 (未显式传入时使用统一配置)
    pb_cfg = get_playbook_config()
    # 2026-06-26: 按 horizon 取 R:R 和止损参数, 优先从 playbook_horizon.yaml 读取
    _horizon_params = pb_cfg.get_horizon_params(horizon)
    if rr_ratio is None:
        rr_ratio = float(_horizon_params.get("rr_ratio", 1.0 if horizon == "short" else 2.5))
    if atr_multiplier is None:
        atr_multiplier = float(_horizon_params.get("atr_multiplier", 2.0))

    result = {}
    tech = data.technical
    price = tech.price or 0

    if price <= 0 or decision["direction"] == "SKIP":
        return result

    # ── 入场价 = 可成交价 (BUY 用 ask, SELL_SHORT 用 bid) ──
    # 2026-06-25 修复: 盘前/非交易时段 last_price 可能严重滞后, 导致 entry/stop 全部失效.
    # 例如 MU last_price=$1048 但 bid/ask=$1242, 按旧价做空止损瞬间击穿.
    if decision["direction"] == "BUY":
        entry_price = tech.ask or price or 0
    elif decision["direction"] == "SELL_SHORT":
        entry_price = tech.bid or price or 0
    else:
        entry_price = price or 0

    if entry_price <= 0 or decision["direction"] == "SKIP":
        return result

    result["entry_price"] = round(entry_price, 2)
    result["entry_price_source"] = (
        f"direction={decision['direction']}, "
        f"price={price:.2f}, bid={getattr(tech, 'bid', 0) or 0:.2f}, ask={getattr(tech, 'ask', 0) or 0:.2f}"
    )

    # ── 止损 = 波动率分桶 or 固定百分比 (配置化, 方向差异化) ──
    # Phase 2 (2026-06-30): 优先按 ATR% 分桶, 未配置时回退固定百分比
    _direction = decision["direction"]
    atr_14 = _positive_finite_atr(getattr(tech, "atr_14", None))
    atr_pct = _positive_finite_atr(atr_14 / entry_price) if atr_14 is not None else None

    # ATR 数据缺失 → 无法评估波动率, 跳过 (防 vol_buckets 误匹配 low桶)
    if atr_pct is None:
        logger.info(f"[Playbook] {decision.get('symbol','')} 跳过: ATR 数据缺失 (atr_14=0), 无法评估波动率")
        # 2026-08-07 grilling Q3 (shadow): 量化 ATR 缺失丢失量 — 只记录不放行。
        #   背景: 固定百分比 fallback 仅在 vol_buckets 未配置时生效, ATR=0 是硬丢弃
        #   路径; 直接回退固定 SL 会给高波标的开噪声扫损侧门 (low_atr_gate 存在的
        #   理由), 故先 shadow 攒数据裁决 (每日丢失几条/什么标的/盘前还是盘中)。
        try:
            from pathlib import Path as _PathQ3
            from app.utils.timezone import now_et as _now_et_q3  # 2026-08-07 复核: 统一 ET 口径
            _row_q3 = {
                "ts": _now_et_q3().isoformat(timespec="seconds"),
                "symbol": decision.get("symbol", ""),
                "direction": decision.get("direction", ""),
                "price": price,
                "atr_14": atr_14,
                "source": "instruction_generator",
            }
            _pq3 = _PathQ3(__file__).resolve().parents[2] / "data" / "atr_missing_shadow.jsonl"
            with open(_pq3, "a", encoding="utf-8") as _fq3:
                _fq3.write(json.dumps(_row_q3, ensure_ascii=False) + "\n")
        except Exception:
            pass
        # [DEB-fullchain 低优 2026-08-16] 带因返回: 调用方原把本分支统一标
        #   "价格缺失" — 归因口径失真 (真实原因是 ATR 缺失), 影响供给诊断。
        return {"skip_reason": "ATR数据缺失(atr_14=0), 无法评估波动率"}

    _vol_sl_info = _compute_volatility_based_sl_tp(
        atr_pct, _direction, _horizon_params, pb_cfg
    )
    if _vol_sl_info:
        _max_loss_pct = _vol_sl_info["sl_pct"]
        rr_ratio = _vol_sl_info["tp_multiplier"]
        _sl_source = (
            f"VOL_{_vol_sl_info['bucket']}: atr_pct={atr_pct*100:.2f}% "
            f"sl={_max_loss_pct:.2f}% rr={rr_ratio:.2f}"
        )
    else:
        # 回退到固定百分比
        # 2026-06-27 辩论DEB-20260627: BUY 2%太紧(6笔0%WR, 开盘噪声扫损), 改2.5%
        #   SHORT保持2.0% (SHORT SL 2笔-$110, 损失可控)
        #   配置来源: playbook_horizon.yaml short_horizon.buy_fixed_stop_loss_pct
        if _direction == "BUY":
            _max_loss_pct = float(_horizon_params.get("buy_fixed_stop_loss_pct",
                                pb_cfg.get("fixed_stop_loss_pct", 2.5)))
        else:
            _max_loss_pct = float(_horizon_params.get("sell_fixed_stop_loss_pct",
                                pb_cfg.get("fixed_stop_loss_pct", 2.0)))
        _sl_source = f"FIXED_{_max_loss_pct}%"

    sl_distance = entry_price * (_max_loss_pct / 100.0)
    if _direction == "BUY":
        stop_loss = entry_price - sl_distance
    else:
        stop_loss = entry_price + sl_distance

    result["stop_loss"] = round(stop_loss, 2)
    result["atr_source"] = _sl_source

    # ── 止盈 = entry + R:R × sl_distance ──
    # 2026-08-01 裁决A (用户): 删除 rr_ratio>=100 死哨兵分支 — 999 配置已退役,
    #   固定 TP 为诚实声明行为 (vol_buckets tp_multiplier 1.5-2.0 真源, fallback 2.0).
    tp_distance = sl_distance * rr_ratio
    take_profit = entry_price + tp_distance if decision["direction"] == "BUY" else entry_price - tp_distance
    result["take_profit"] = round(take_profit, 2)
    result["risk_reward"] = rr_ratio

    # ── Phase 4 (2026-06-30): 市场状态叠加 ──
    result = _apply_market_state_overlay(
        result, decision, data, _horizon_params, pb_cfg, regime_context
    )
    if result.get("skip_market_state"):
        return {}

    # ── 仓位 = risk_amount / per_share_risk ──
    # 2026-07-18 (R4 SSOT): risk_base 优先取富途服务端实时净值 (capital_safety.current_equity,
    #   accinfo_query 每 30s 刷新, 跟随 AUTO_TRADE_ACCOUNT 自动切 sim/real); 与 pipeline
    #   日名义额度共用同一基数。净值随实盘变化 → 仓位同步缩放, 亏损后自动缩小 = 天然防爆仓。
    #   仅在净值取不到时兜底 config risk_base_capital (默认 $82K)。
    from app.risk.capital_safety import get_capital_safety_system
    _water_mult = 1.0
    _risk_base = float(pb_cfg.get("risk_base_capital", 82000.0))
    try:
        _cs = get_capital_safety_system()
        if _cs._state and _cs._state.current_equity and _cs._state.current_equity > 0:
            _risk_base = float(_cs._state.current_equity)
        _pos_mult = _cs.get_position_multiplier()
        _risk_mult = _cs.get_risk_multiplier()
        _water_mult = min(_pos_mult, _risk_mult)
    except Exception:
        pass

    # F1 (2026-07-19): sizing SSOT 公共函数 — 主路径/兜底共用, 消除双实现 (DEB-round2 Ω.3-4).
    #   risk_amount 保留供 result["risk_amount"] 报告; size 数学(除法+截断)全在 compute_position_size.
    risk_amount = _risk_base * (risk_pct / 100) * regime_multiplier * _water_mult
    _max_position_pct = float(pb_cfg.get("max_position_pct", 0.08))
    position_size = compute_position_size(
        _risk_base, risk_pct, entry_price, result["stop_loss"], _max_position_pct,
        regime_mult=regime_multiplier, water_mult=_water_mult, conf_factor=1.0,
    )
    # 截断可观测性: sizing 数学在 SSOT 函数内, 此处仅在上限边界触发 info 日志
    _max_position_value = _risk_base * _max_position_pct
    if position_size > 0 and position_size * entry_price <= _max_position_value < (position_size + 1) * entry_price:
        logger.info(
            f"[Playbook] {decision.get('symbol','')} 仓位触及上限: {position_size}股(${position_size*entry_price:,.0f}) "
            f"[上限{_max_position_pct*100:.0f}%=${_max_position_value:,.0f}]"
        )

    # Phase 4: 应用市场状态仓位乘数
    _market_state_mult = result.pop("market_state_position_mult", 1.0)
    if _market_state_mult != 1.0 and position_size > 0:
        _old_qty = position_size
        position_size = max(1, int(position_size * _market_state_mult))
        logger.info(
            f"[Playbook] {decision.get('symbol','')} 市场状态仓位调整: {_old_qty}股 → {position_size}股 "
            f"(mult={_market_state_mult:.2f})"
        )

    result["position_size"] = max(1, position_size)
    result["risk_amount"] = round(risk_amount, 2)
    result["horizon"] = horizon

    return result


def build_thesis(
    template_key: str,
    conditions: List[str],
    skip_reason: str = "",
) -> str:
    """模板填充 thesis，禁止自由文本"""
    template = THESIS_TEMPLATES.get(template_key, THESIS_TEMPLATES["trend_pullback"])
    conditions_str = ", ".join(conditions) if conditions else "无"
    return template.format(conditions=conditions_str, skip_reason=skip_reason or "N/A")


def compute_confidence(
    data: PlaybookData,
    selected_conditions: List[str],
    triggered_factors: Dict[str, Any],
) -> int:
    """
    基于数据质量和条件一致性计算置信度 (0-100)。
    
    规则:
    - 基础分 = 数据质量 ok 的维度数 × 12 (max 72)
    - 条件验证分 = 选中的条件中实际触发的比例 × 28 (max 28)
    - 总分 cap at 90 (留 10 分给人工确认)
    """
    # 数据质量分
    quality_fields = [
        data.fundamental.quality,
        data.technical.quality,
        data.insider.quality,
        data.news.quality,
        data.earnings.quality,
        data.sentiment.quality,
    ]
    ok_count = sum(1 for q in quality_fields if q == "ok")
    quality_score = ok_count * 12  # max 72
    
    # 条件验证分
    if selected_conditions and triggered_factors:
        verified = sum(1 for c in selected_conditions if c in triggered_factors)
        condition_score = int((verified / len(selected_conditions)) * 28)
    else:
        condition_score = 0
    
    return min(90, quality_score + condition_score)


