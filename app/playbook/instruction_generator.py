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


# ========== 数值计算引擎 (纯 Python, 确定性) ==========

def compute_instruction_values(
    decision: Dict[str, Any],
    data: PlaybookData,
    total_equity: float,
    risk_pct: float = 1.0,
    regime_multiplier: float = 1.0,
    rr_ratio: float = 2.0,
    atr_multiplier: float = 1.5,
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
        rr_ratio: 风险回报比 (默认 2:1)
        atr_multiplier: ATR 止损倍数 (默认 1.5x)
    
    Returns:
        计算后的数值字段 dict
    """
    result = {}
    tech = data.technical
    price = tech.price or 0
    
    if price <= 0 or decision["direction"] == "SKIP":
        return result
    
    # ── 入场价 = 当前价 (确定性，不猜) ──
    entry_price = price
    result["entry_price"] = round(entry_price, 2)
    result["entry_price_source"] = f"technical.price={price:.2f} (quality={tech.quality})"
    
    # ── 止损 = entry - ATR × multiplier ──
    # ⚠️ Router 的 ATR 来自 K_1M (1分钟K线), 远小于日级 ATR
    # Playbook 是日级策略, SL/TP 必须用日K ATR
    atr = tech.atr_14
    atr_source_label = "technical.atr_14"
    if atr is not None and atr > 0 and atr < price * 0.005:
        # ATR 太小 (< 0.5% 价格), 说明是分钟级数据, 拉日K重算
        try:
            from app.data.futu_lv3_provider import FutuLV3Provider
            futu = FutuLV3Provider()
            sym = getattr(data, 'symbol', '')
            if sym.startswith('US.'):
                daily_bars = futu.get_daily_kline(sym, count=30)
                if daily_bars and len(daily_bars) >= 15:
                    highs = [b['high'] for b in daily_bars[-14:]]
                    lows = [b['low'] for b in daily_bars[-14:]]
                    closes = [b['close'] for b in daily_bars[-14:]]
                    prev_closes = [b['close'] for b in daily_bars[-15:-1]]
                    trs = []
                    for h, l, pc in zip(highs, lows, prev_closes):
                        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
                    daily_atr = sum(trs) / len(trs)
                    if daily_atr > atr:
                        atr = daily_atr
                        atr_source_label = "daily_kline.atr_14"
        except Exception:
            pass  # 降级用分钟 ATR
    if atr is None or atr <= 0:
        # ATR 缺失时用 2% 价格作为 fallback
        atr = entry_price * 0.02
        result["atr_source"] = f"FALLBACK: price×2%={atr:.2f} (ATR missing)"
    else:
        result["atr_source"] = f"{atr_source_label}={atr:.2f} (quality={tech.quality})"
    
    sl_distance = atr * atr_multiplier
    stop_loss = entry_price - sl_distance if decision["direction"] == "BUY" else entry_price + sl_distance
    result["stop_loss"] = round(stop_loss, 2)
    
    # ── 止盈 = entry + R:R × sl_distance ──
    tp_distance = sl_distance * rr_ratio
    take_profit = entry_price + tp_distance if decision["direction"] == "BUY" else entry_price - tp_distance
    result["take_profit"] = round(take_profit, 2)
    result["risk_reward"] = rr_ratio
    
    # ── 仓位 = risk_amount / per_share_risk ──
    risk_amount = total_equity * (risk_pct / 100) * regime_multiplier
    per_share_risk = abs(entry_price - stop_loss)
    if per_share_risk > 0:
        position_size = int(risk_amount / per_share_risk)
    else:
        position_size = 0
    result["position_size"] = max(1, position_size)
    result["risk_amount"] = round(risk_amount, 2)
    
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


class InstructionGenerator:
    """交易指令生成器 (反幻觉架构)
    
    数据流:
    1. Screener 传入 candidates (含 triggered_factors)
    2. LLM 只输出 {symbol, direction, selected_conditions, thesis_template}
    3. Python 确定性计算所有数值
    4. 验证 selected_conditions 是否在 triggered_factors 中
    """
    
    # Regime 仓位乘数
    REGIME_MULTIPLIERS = {
        "RISK_ON": 1.0,
        "CAUTIOUS": 0.5,
        "RISK_OFF": 0.3,
        "NO_TRADE": 0.0,
    }
    
    def __init__(self, llm_client, risk_pct: float = 1.0):
        """
        Args:
            llm_client: LLM 客户端实例
            risk_pct: 单笔风险占总资金百分比 (默认 1%)
        """
        self.llm_client = llm_client
        self.risk_pct = risk_pct
    
    def generate(
        self,
        candidates: List[Dict[str, Any]],
        regime_context: RegimeContext,
        positions: List[Dict[str, Any]],
        total_equity: float,
        active_patterns: List[Dict] = None,
        retired_patterns: List[Dict] = None,
        max_retries: int = 2,
    ) -> List[Instruction]:
        """
        生成交易指令
        
        Args:
            candidates: 候选股票 (含 symbol, score, data, factors)
                        factors 是 Screener 计算的 triggered_factors
            regime_context: 市场环境
            positions: 当前持仓
            total_equity: 总资金
            active_patterns: 正收益 pattern
            retired_patterns: 负收益 pattern
            max_retries: LLM 重试次数
        
        Returns:
            完整的 Instruction 列表 (数值由 Python 计算)
        """
        if active_patterns is None:
            active_patterns = []
        if retired_patterns is None:
            retired_patterns = []
        
        # 构建 triggered_factors 索引 (用于验证 LLM 输出)
        candidate_factors = {}
        candidate_data = {}
        for c in candidates:
            sym = c["symbol"]
            candidate_factors[sym] = c.get("factors", {})
            candidate_data[sym] = c.get("data")
        
        # 构建 prompt (只给触发条件，不给原始数值)
        prompt = self._build_prompt(
            candidates, regime_context, positions, total_equity,
            active_patterns, retired_patterns
        )
        
        # 调用 LLM 获取决策 (最小化输出)
        for attempt in range(max_retries + 1):
            try:
                raw_response = self.llm_client.call(
                    feature="playbook_generator",
                    prompt=prompt,
                    max_tokens=2048,
                    temperature=0
                )
                
                # 解析 JSON (strip markdown code blocks if present)
                _raw = raw_response.strip()
                if _raw.startswith("```"):
                    # Strip ```json ... ``` wrapper
                    _raw = _raw.split("\n", 1)[-1]  # remove opening line
                    if _raw.endswith("```"):
                        _raw = _raw[:-3].strip()
                decisions = json.loads(_raw)
                
                # 验证 LLM 输出
                decisions = self._validate_decisions(decisions, candidate_factors)
                
                # Python 计算数值 + 构建 Instruction
                instructions = self._build_instructions(
                    decisions, candidate_data, candidate_factors,
                    regime_context, total_equity
                )
                
                return instructions
                
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                if attempt < max_retries:
                    logger.warning(f"LLM 输出校验失败 (attempt {attempt+1}): {e}, 重试中...")
                else:
                    logger.error(f"LLM 输出校验最终失败: {e}")
                    # 返回全部 SKIP
                    return [
                        Instruction(
                            symbol=c["symbol"],
                            direction="SKIP",
                            skip_reason=f"LLM 输出异常: {e}"
                        )
                        for c in candidates
                    ]
    
    def _validate_decisions(
        self,
        decisions: List[Dict],
        candidate_factors: Dict[str, Dict],
    ) -> List[Dict]:
        """
        验证 LLM 决策:
        1. 基本结构校验
        2. selected_conditions 必须在 candidate_factors 中 (防幻觉)
        3. 不在 candidate_factors 中的条件自动移除 (降级而非拒绝)
        """
        valid_decisions = []
        
        for dec in decisions:
            # 基本字段检查
            if not all(k in dec for k in ["symbol", "direction", "selected_conditions", "thesis_template"]):
                logger.warning(f"LLM 输出缺少必要字段: {dec}")
                continue
            
            symbol = dec["symbol"]
            direction = dec["direction"]
            
            # 验证 direction 枚举
            if direction not in ("BUY", "SELL_SHORT", "SKIP"):
                logger.warning(f"无效的 direction: {direction}, 强制 SKIP")
                direction = "SKIP"
            
            # 验证 selected_conditions (核心反幻觉逻辑)
            selected = dec.get("selected_conditions", [])
            available = set(candidate_factors.get(symbol, {}).keys())
            
            # 过滤掉未触发的条件
            verified = [c for c in selected if c in available]
            unverified = [c for c in selected if c not in available]
            
            if unverified:
                logger.warning(
                    f"{symbol}: LLM 选择了未触发的条件 {unverified}, 已移除 "
                    f"(可用: {list(available)})"
                )
            
            # 验证 thesis_template
            template = dec.get("thesis_template", "trend_pullback")
            if template not in THESIS_TEMPLATES:
                template = "trend_pullback"
            
            valid_decisions.append({
                "symbol": symbol,
                "direction": direction,
                "selected_conditions": verified,
                "thesis_template": template,
                "skip_reason": dec.get("skip_reason", ""),
            })
        
        return valid_decisions
    
    def _build_instructions(
        self,
        decisions: List[Dict],
        candidate_data: Dict[str, PlaybookData],
        candidate_factors: Dict[str, Dict],
        regime_context: RegimeContext,
        total_equity: float,
    ) -> List[Instruction]:
        """
        基于 LLM 决策 + 原始数据，构建完整 Instruction。
        所有数值在这里计算，LLM 不参与。
        """
        instructions = []
        
        regime = str(regime_context.regime) if hasattr(regime_context, 'regime') else "CAUTIOUS"
        regime_mult = self.REGIME_MULTIPLIERS.get(regime, 0.5)
        
        for dec in decisions:
            symbol = dec["symbol"]
            data = candidate_data.get(symbol)
            factors = candidate_factors.get(symbol, {})
            
            if dec["direction"] == "SKIP":
                instructions.append(Instruction(
                    symbol=symbol,
                    direction="SKIP",
                    skip_reason=dec.get("skip_reason", ""),
                    conditions=dec["selected_conditions"],
                    thesis=build_thesis("skip", [], dec.get("skip_reason", "")),
                ))
                continue
            
            if data is None:
                logger.warning(f"{symbol}: 无数据，强制 SKIP")
                instructions.append(Instruction(
                    symbol=symbol,
                    direction="SKIP",
                    skip_reason="数据缺失",
                ))
                continue
            
            # 数值计算 (确定性)
            values = compute_instruction_values(
                decision=dec,
                data=data,
                total_equity=total_equity,
                risk_pct=self.risk_pct,
                regime_multiplier=regime_mult,
            )
            
            # 防呆: 价格缺失时不能生成非 SKIP 指令
            if not values or not values.get("entry_price"):
                logger.warning(
                    f"{symbol}: 无法计算交易数值 (price={data.technical.price}), 强制 SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol,
                    direction="SKIP",
                    skip_reason="技术面价格缺失，无法计算入场价/止损/止盈",
                    conditions=dec["selected_conditions"],
                ))
                continue
            
            # 置信度计算
            confidence = compute_confidence(data, dec["selected_conditions"], factors)
            
            # 数据质量汇总
            data_quality = {
                "fundamental": data.fundamental.quality,
                "technical": data.technical.quality,
                "insider": data.insider.quality,
                "news": data.news.quality,
                "earnings": data.earnings.quality,
                "sentiment": data.sentiment.quality,
            }
            
            # Thesis 模板填充
            thesis = build_thesis(
                dec["thesis_template"],
                dec["selected_conditions"],
            )
            
            # 风险提示
            risks = []
            ok_count = sum(1 for q in data_quality.values() if q == "ok")
            if ok_count < 4:
                risks.append(f"数据不完整 ({ok_count}/6 维度 ok)")
            if data.earnings.days_until is not None and data.earnings.days_until < 14:
                risks.append(f"财报 {data.earnings.days_until} 天后")
            if values.get("atr_source", "").startswith("FALLBACK"):
                risks.append("ATR 缺失，使用 fallback 止损")
            
            inst = Instruction(
                symbol=symbol,
                direction=dec["direction"],
                entry_price=values.get("entry_price", 0),
                stop_loss=values.get("stop_loss", 0),
                take_profit=values.get("take_profit", 0),
                position_size=values.get("position_size", 0),
                risk_amount=values.get("risk_amount", 0),
                risk_reward=values.get("risk_reward", 0),
                confidence=confidence,
                thesis=thesis,
                conditions=dec["selected_conditions"],
                data_quality=data_quality,
                risks=risks,
                sector=data.fundamental.industry or "",
                entry_price_source=values.get("entry_price_source", ""),
                atr_source=values.get("atr_source", ""),
            )
            instructions.append(inst)
        
        return instructions
    
    def _build_prompt(
        self,
        candidates: List[Dict[str, Any]],
        regime_context: RegimeContext,
        positions: List[Dict[str, Any]],
        total_equity: float,
        active_patterns: List[Dict],
        retired_patterns: List[Dict],
    ) -> str:
        """构建 LLM prompt (反幻觉架构: 只给触发条件，不给原始数值让 LLM 计算)"""
        
        regime = str(regime_context.regime) if hasattr(regime_context, 'regime') else "CAUTIOUS"
        
        # 市场环境描述
        regime_desc = {
            "RISK_ON": "市场趋势向上，VIX 低，可以正常操作",
            "CAUTIOUS": "市场不确定，需要缩小仓位",
            "RISK_OFF": "市场风险偏高，防守为主",
            "NO_TRADE": "极端环境，全部 SKIP",
        }
        
        # 持仓行业暴露
        sector_exposure = {}
        for p in positions:
            sector = p.get("sector", "Unknown")
            sector_exposure[sector] = sector_exposure.get(sector, 0) + 1
        sector_text = ", ".join([f"{s}: {c}" for s, c in sector_exposure.items()]) or "无"
        
        # 候选标的: 只展示触发条件 + 数据质量 (不展示原始数值)
        candidates_text = ""
        for i, c in enumerate(candidates, 1):
            sym = c["symbol"]
            data: PlaybookData = c.get("data")
            factors = c.get("factors", {})
            
            if data is None:
                continue
            
            # 触发条件列表
            triggered = list(factors.keys()) if factors else []
            triggered_str = ", ".join(triggered) if triggered else "无触发条件"
            
            # 数据质量摘要
            qualities = [
                data.fundamental.quality,
                data.technical.quality,
                data.insider.quality,
                data.news.quality,
                data.earnings.quality,
                data.sentiment.quality,
            ]
            ok_count = sum(1 for q in qualities if q == "ok")
            
            candidates_text += f"""
### 标的 #{i}: {sym}
触发条件: {triggered_str}
数据质量: {ok_count}/6 ok
行业: {data.fundamental.industry or 'N/A'}
财报: {data.earnings.days_until or '?'} 天后
"""
        
        # Pattern 注入 (2026-06-19 Phase 0: 修复数据流断裂)
        pattern_text = ""
        if active_patterns:
            pattern_text += "\n## 已验证的正向 Pattern (历史胜率高，优先选择)\n"
            for p in active_patterns[:5]:
                conditions = p.get("conditions", [])
                win_rate = p.get("win_rate", 0)
                total = p.get("total_trades", 0)
                verdict = p.get("verdict", "active")
                cond_str = " + ".join(conditions) if isinstance(conditions, list) else str(conditions)
                pattern_text += f"- {cond_str}: 胜率 {win_rate*100:.0f}% ({total}笔, {verdict})\n"
        if retired_patterns:
            pattern_text += "\n## 已退役的负向 Pattern (历史表现差，避免)\n"
            for p in retired_patterns[:3]:
                conditions = p.get("conditions", [])
                win_rate = p.get("win_rate", 0)
                total = p.get("total_trades", 0)
                cond_str = " + ".join(conditions) if isinstance(conditions, list) else str(conditions)
                pattern_text += f"- {cond_str}: 胜率 {win_rate*100:.0f}% ({total}笔)\n"
        if not active_patterns and not retired_patterns:
            pattern_text = "\n## Pattern (冷启动期，暂无历史数据)\n"

        # 完整 prompt (最小化: 只要求决策，不要求数值)
        prompt = f"""你是 FTSeek 交易决策系统。

## 环境
Regime: {regime} ({regime_desc.get(regime, "未知")})

## 持仓行业分布
{sector_text} (已有 {len(positions)} 个持仓)
{pattern_text}
## 候选标的 (已由规则引擎预筛选，只展示触发条件)
{candidates_text}

## 你的任务
从候选中选出 1-5 个最佳机会。

## 关键规则
1. selected_conditions 必须从该标的的"触发条件"列表中选取，禁止添加未列出的条件
2. 如果 NO_TRADE 环境，全部 SKIP
3. 同行业最多 2 个持仓
4. 优先选择匹配"正向 Pattern"条件的标的
5. 避免选择匹配"负向 Pattern"条件的标的
6. thesis_template 选择:
   - trend_pullback: 趋势回调入场
   - breakout: 突破入场
   - value_reversal: 价值反转
   - momentum_continuation: 动量延续
   - insider_confidence: 内部人信心
   - mean_reversion: 均值回归
   - skip: 跳过

## 输出格式 (JSON Array):
[
  {{
    "symbol": "US.XXX",
    "direction": "BUY" | "SELL_SHORT" | "SKIP",
    "selected_conditions": ["从触发条件列表中选择"],
    "thesis_template": "trend_pullback",
    "skip_reason": "仅 SKIP 时填写"
  }}
]

注意: 你只需要做决策 (方向 + 条件选择 + 模板)，不需要计算价格、仓位、止损等数值。
"""
        return prompt
