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


# ========== Thesis 模板 (禁止自由文本) ==========

THESIS_TEMPLATES = {
    "trend_pullback": "趋势回调: {conditions}, 提供低风险入场机会",
    "breakout": "突破入场: {conditions}, 动量确认",
    "value_reversal": "价值反转: {conditions}, 基本面低估+技术面企稳",
    "momentum_continuation": "动量延续: {conditions}, 趋势持续",
    "insider_confidence": "内部人信心: {conditions}, 管理层用行动投票",
    "mean_reversion": "均值回归: {conditions}, 超跌反弹机会",
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


# ========== 数值计算引擎 (纯 Python, 确定性) ==========

def _compute_volatility_based_sl_tp(
    atr_pct: float,
    direction: str,
    _horizon_params: Dict[str, Any],
    pb_cfg: PlaybookConfig,
) -> Optional[Dict[str, Any]]:
    """按波动率分桶计算止损百分比和止盈倍数 (Phase 2 2026-06-30).

    Returns:
        {"bucket": str, "sl_pct": float, "tp_multiplier": float} or None
        未配置 vol_buckets 时返回 None, 走固定百分比兜底.
    """
    vol_buckets = _horizon_params.get("vol_buckets") or pb_cfg.get("vol_buckets")
    if not vol_buckets:
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
    atr_14 = getattr(tech, "atr_14", 0) or 0
    atr_pct = (atr_14 / entry_price) if entry_price > 0 else 0.0

    # ATR 数据缺失 → 无法评估波动率, 跳过 (防 vol_buckets 误匹配 low桶)
    if atr_pct <= 0:
        logger.info(f"[Playbook] {decision.get('symbol','')} 跳过: ATR 数据缺失 (atr_14=0), 无法评估波动率")
        return {}

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
    # 2026-06-30: rr_ratio=999 是"禁用固定止盈"哨兵 → TP=0, 让 trailing stop 管理退出
    if rr_ratio >= 100:
        take_profit = 0.0
        result["risk_reward"] = 0.0  # 999哨兵不落DB, 避免下游误解
    else:
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
    # P0: 用配置 risk_base_capital (默认 $82K) 作为 risk_base, 不读 sim BP ($313K)
    # 原因: sim BP 是 $313K → 单仓 $25K → 切实盘后仓位缩3.8x, PnL不可比
    # 用 $82K: sim 和 live 仓位 1:1, OKR 数据直接代表实盘表现
    from app.risk.capital_safety import get_capital_safety_system
    _water_mult = 1.0
    _risk_base = float(pb_cfg.get("risk_base_capital", 82000.0))
    try:
        _cs = get_capital_safety_system()
        _pos_mult = _cs.get_position_multiplier()
        _risk_mult = _cs.get_risk_multiplier()
        _water_mult = min(_pos_mult, _risk_mult)
    except Exception:
        pass

    risk_amount = _risk_base * (risk_pct / 100) * regime_multiplier * _water_mult
    per_share_risk = abs(entry_price - result["stop_loss"])
    if per_share_risk > 0:
        position_size = int(risk_amount / per_share_risk)
    else:
        position_size = 0

    # P0: max_position_pct 截断 — 单仓位不超过 risk_base 的配置比例
    _max_position_pct = float(pb_cfg.get("max_position_pct", 0.08))
    _max_position_value = _risk_base * _max_position_pct
    _position_value = position_size * entry_price
    if _position_value > _max_position_value:
        _old_qty = position_size
        position_size = max(1, int(_max_position_value / entry_price))
        logger.info(
            f"[Playbook] {decision.get('symbol','')} 仓位截断: {_old_qty}股(${_position_value:,.0f}) "
            f"→ {position_size}股(${position_size*entry_price:,.0f}) [上限{_max_position_pct*100:.0f}%=${_max_position_value:,.0f}]"
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


